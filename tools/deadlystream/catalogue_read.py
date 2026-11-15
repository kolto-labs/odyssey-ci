#!/usr/bin/env python3
"""Read-only recovery and integrity checks for the private catalogue cache."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import socket
import urllib3.util.connection
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config


class Reader:
    def __init__(self, config, identity):
        self.config = config
        if config.get("force_ipv4",False):
            urllib3.util.connection.allowed_gai_family=lambda:socket.AF_INET
        self.identity = identity
        credential = json.loads(Path(config['reader_file']).read_text())
        self.s3 = boto3.client('s3', endpoint_url=config['endpoint'], region_name='auto',
            aws_access_key_id=credential['id'], aws_secret_access_key=credential['secret_access_key'],
            config=Config(signature_version='s3v4', retries={'max_attempts': 5},
                max_pool_connections=40,
                request_checksum_calculation='when_required', response_checksum_validation='when_required'))

    def get(self, key, limit):
        use_ranges=self.config.get('parallel_read_ranges',True) and limit>=512*1024
        response = self.s3.get_object(Bucket=self.config['bucket'], Key=key,
            **({'Range':'bytes=0-262143'} if use_ranges else {}))
        try:
            if response['ContentLength'] > limit:
                raise ValueError('remote object exceeds expected size')
            result = response['Body'].read(response['ContentLength'])
        finally:
            response['Body'].close()
        if len(result) != response['ContentLength'] or len(result) > limit:
            raise ValueError('remote object length mismatch')
        partial=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.get('ContentRange',''))
        if use_ranges and partial:
            start,end,total=map(int,partial.groups())
            if start!=0 or end+1!=len(result) or total>limit:raise ValueError('invalid initial object range')
            if total>len(result):
                etag=response.get('ETag')
                if not etag:raise ValueError('missing range consistency validator')
                spans=[(p,min(p+1024*1024,total)-1) for p in range(len(result),total,1024*1024)]
                def fetch(span):
                    first,last=span
                    part=self.s3.get_object(Bucket=self.config['bucket'],Key=key,Range=f'bytes={first}-{last}',IfMatch=etag)
                    try:body=part['Body'].read(last-first+1)
                    finally:part['Body'].close()
                    if len(body)!=last-first+1 or part.get('ContentRange')!=f'bytes {first}-{last}/{total}':
                        raise ValueError('remote range length mismatch')
                    return body
                with ThreadPoolExecutor(max_workers=4) as pool:result+=b''.join(pool.map(fetch,spans))
            if len(result)!=total:raise ValueError('range reconstruction length mismatch')
        return result

    def decrypt(self, data):
        result = subprocess.run([self.config['age'], '--decrypt', '--identity', self.identity],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C'})
        if result.returncode:
            raise ValueError('decryption failed')
        return result.stdout

    def encrypted(self, key, digest, limit):
        if not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('invalid ciphertext digest')
        data = self.get(key, limit)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('ciphertext digest mismatch')
        return self.decrypt(data)

    def snapshot(self):
        pointer = json.loads(self.get('catalogue/latest.json', 65536))
        if not re.fullmatch(r'catalogue/snapshots/[A-Za-z0-9TZ-]+\.json\.age', pointer['snapshot']):
            raise ValueError('invalid snapshot location')
        value = json.loads(self.encrypted(pointer['snapshot'], pointer['cipher_sha256'], 512 * 1024 * 1024))
        if value.get('schema') != 'kolto-deadlystream-catalogue/v1':
            raise ValueError('unknown catalogue schema')
        return pointer, value

    def verify(self, snapshot):
        verified_archives = set()
        verified_manifests = 0
        verified_bytes = 0
        versions = {(v['file_id'], v['revision']): v for v in snapshot['versions']}
        for item in snapshot['items']:
            key = item['manifest']
            match = re.fullmatch(r'manifests/\d+/[0-9a-f]{24}/([0-9a-f]{64})(?:-([0-9a-f]{64}))?\.json\.age', key)
            if not match:
                raise ValueError('invalid file manifest location')
            if match[1] != item['sha256']:
                raise ValueError('manifest address disagrees with archive digest')
            cipher_digest = match[2]
            if cipher_digest is None:
                # Two bootstrap objects predate ciphertext-addressed manifests.
                head = self.s3.head_object(Bucket=self.config['bucket'], Key=key)
                cipher_digest = head.get('Metadata', {}).get('cipher-sha256', '')
            manifest = json.loads(self.encrypted(key, cipher_digest, 16 * 1024 * 1024))
            if manifest.get('schema') != 'kolto-deadlystream-file/v1':
                raise ValueError('unknown file schema')
            for field in ('file_id', 'revision', 'row_key', 'filename', 'sha256', 'size'):
                if manifest[field] != item[field]:
                    raise ValueError('file manifest disagrees with catalogue')
            version = versions[(item['file_id'], item['revision'])]
            if any(manifest[field] != version[other] for field, other in
                   (('source_url', 'source_url'), ('history_id', 'history_id'), ('version_label', 'label'))):
                raise ValueError('file provenance disagrees with catalogue')
            verified_manifests += 1
            if item['sha256'] in verified_archives:
                continue
            digest = hashlib.sha256()
            size = 0
            for chunk in manifest['chunks']:
                if not re.fullmatch(r'objects/[0-9a-f]{20}/[0-9a-f]{64}\.age', chunk['key']):
                    raise ValueError('invalid chunk location')
                plain = self.encrypted(chunk['key'], chunk['cipher_sha256'], 17 * 1024 * 1024)
                if len(plain) != chunk['size'] or hashlib.sha256(plain).hexdigest() != chunk['sha256']:
                    raise ValueError('plaintext chunk mismatch')
                digest.update(plain)
                size += len(plain)
            if size != item['size'] or digest.hexdigest() != item['sha256']:
                raise ValueError('archive reconstruction mismatch')
            verified_archives.add(item['sha256'])
            verified_bytes += size
            if len(verified_archives) % 25 == 0:
                print(json.dumps({'event': 'archives-verified', 'count': len(verified_archives), 'bytes': verified_bytes}), flush=True)
        return {'manifests_verified': verified_manifests,
            'unique_archives_verified': len(verified_archives), 'archive_bytes_verified': verified_bytes}


def restore(snapshot, destination):
    """Restore metadata without depending on a live local collector or its DB."""
    destination = Path(destination)
    if destination.exists():
        raise ValueError('restore destination already exists')
    fd, temporary = tempfile.mkstemp(prefix='.catalogue-restore-', dir=destination.parent)
    os.close(fd)
    try:
        db = sqlite3.connect(temporary)
        try:
            columns = {
                'files': ('id', 'url', 'metadata', 'status', 'error', 'scanned_at'),
                'versions': ('file_id', 'revision', 'label', 'history_id', 'version_token', 'source_url', 'status', 'error', 'attempts', 'updated_at'),
                'items': ('file_id', 'revision', 'row_key', 'filename', 'sha256', 'size', 'manifest', 'cached_at'),
                'gaps': ('file_id', 'revision', 'row_key', 'selector', 'category', 'http_status', 'observed_at', 'details'),
            }
            primary = {'files': 'id', 'versions': 'file_id,revision', 'items': 'file_id,revision,row_key,sha256', 'gaps': 'file_id,revision,row_key'}
            integer = {'id', 'file_id', 'attempts', 'size', 'http_status'}
            for table, names in columns.items():
                declarations = []
                for name in names:
                    declaration = name + (' INTEGER' if name in integer else ' TEXT')
                    if name == 'status':
                        declaration += " NOT NULL DEFAULT 'pending'"
                    elif name == 'attempts':
                        declaration += ' DEFAULT 0'
                    elif name == 'url':
                        declaration += ' NOT NULL'
                    declarations.append(declaration)
                schema = ','.join(declarations)
                db.execute(f'CREATE TABLE {table} ({schema},PRIMARY KEY({primary[table]}))')
                db.executemany(f'INSERT INTO {table} VALUES ({",".join("?" for _ in names)})',
                    ([('pending' if name=='status' and row.get(name)=='capturing-history' else row.get(name)) for name in names] for row in snapshot[table]))
            db.commit()
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('restored catalogue integrity failed')
            if db.execute('SELECT count(*) FROM items i LEFT JOIN versions v USING(file_id,revision) WHERE v.file_id IS NULL').fetchone()[0]:
                raise ValueError('orphaned catalogue item')
        finally:
            db.close()
        # Exclusive link prevents replacing a destination created during restore.
        os.link(temporary, destination)
    finally:
        os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--identity', required=True)
    parser.add_argument('--verify-archives', action='store_true')
    parser.add_argument('--restore-state')
    parser.add_argument('--receipt')
    args = parser.parse_args()
    os.umask(0o077)
    reader = Reader(json.loads(Path(args.config).read_text()), args.identity)
    pointer, snapshot = reader.snapshot()
    receipt = {'snapshot': pointer['snapshot'], 'snapshot_cipher_sha256': pointer['cipher_sha256'],
        'snapshot_status': snapshot['status'], 'snapshot_decrypted': True}
    if args.verify_archives:
        receipt.update(reader.verify(snapshot))
    if args.restore_state:
        restore(snapshot, args.restore_state)
        receipt['metadata_restored'] = True
    if args.receipt:
        Path(args.receipt).write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Credentials, identity material and provider error bodies stay out of logs.
        print(json.dumps({'event': 'cache-read-failed', 'category': type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
