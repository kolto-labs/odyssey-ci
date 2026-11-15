"""Audit immutable manifests against all preserved catalogue associations."""
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
import re
import threading
import time


def run(runtime, cfg, cache, reader):
    started = time.monotonic()
    public = runtime.pointer(reader, runtime.PREFIX + 'latest.json')
    member = runtime.pointer(reader, runtime.PREFIX + 'authenticated/latest.json')
    public_index = json.loads(gzip.decompress(runtime.read_private(reader, public['index'], 128 * 1024**2)))
    member_index = json.loads(gzip.decompress(runtime.read_private(reader, member, 128 * 1024**2))) if member else {}
    native_pointer, native = reader.snapshot()
    work = []
    for source in (native, public_index.get('linked') or {}, member_index.get('linked') or {}):
        versions = {(row['file_id'], row['revision']): row for row in source.get('versions', [])}
        for item in source.get('items', []):
            version = versions.get((item['file_id'], item['revision']))
            if not version:
                raise ValueError('manifest association has no version')
            work.append((item, version, False))
    for source in (public_index, member_index):
        for row in source.get('assets', []):
            if row['state'] != 'captured':
                continue
            revision = 'site-resource-' + hashlib.sha256(row['identity'].encode()).hexdigest()[:24]
            item = {**row, 'file_id': -row['id'], 'revision': revision,
                    'row_key': hashlib.sha256(row['identity'].encode()).hexdigest()}
            work.append((item, {'source_url': row['url'], 'history_id': '0', 'label': row['kind']}, bool(row.get('authenticated_retrieval'))))
    grouped = {}
    for item, version, authenticated in work:
        grouped.setdefault(item['manifest'], []).append((item, version, authenticated))
    local = threading.local()
    namespace = hashlib.sha256(cfg['recipient'].encode()).hexdigest()[:20]
    def verify(entry):
        key, associations = entry
        try:
            if not hasattr(local, 'reader'):
                local.reader = runtime.module('catalogue_read').Reader(cfg, cfg['identity'])
            remote = local.reader
            match = re.fullmatch(r'manifests/-?[0-9]+/[0-9a-f]{24}/([0-9a-f]{64})(?:-([0-9a-f]{64}))?\.json\.age', key)
            if not match:
                raise ValueError('manifest-address')
            cipher = match[2] or remote.s3.head_object(Bucket=cfg['bucket'], Key=key)['Metadata']['cipher-sha256']
            manifest = json.loads(remote.encrypted(key, cipher, 16 * 1024**2))
            if manifest.get('schema') != 'kolto-deadlystream-file/v1' or manifest.get('recipient') != cfg['recipient']:
                raise ValueError('schema-or-recipient')
            for item, version, authenticated in associations:
                for name in ('sha256', 'size', 'filename', 'file_id', 'revision', 'row_key'):
                    if manifest.get(name) != item[name]:
                        raise ValueError('catalogue-' + name)
                for name, other in (('source_url', 'source_url'), ('history_id', 'history_id'), ('version_label', 'label')):
                    if manifest.get(name) != version[other]:
                        raise ValueError('source-' + name)
                if authenticated and not manifest.get('download_selector', {}).get('authenticated_retrieval'):
                    raise ValueError('authenticated-provenance')
            if manifest['sha256'] != match[1] or sum(c['size'] for c in manifest['chunks']) != manifest['size']:
                raise ValueError('payload-length-or-address')
            for chunk in manifest['chunks']:
                if chunk['key'] != 'objects/' + namespace + '/' + chunk['sha256'] + '.age' or not re.fullmatch('[0-9a-f]{64}', chunk['cipher_sha256']):
                    raise ValueError('chunk-address')
            return None
        except Exception as error:
            return {'manifest': key, 'category': str(error) if isinstance(error, ValueError) else type(error).__name__}
    errors = []
    checked = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for error in pool.map(verify, grouped.items()):
            checked += 1
            if error:
                errors.append(error)
            if checked % 1000 == 0:
                runtime.emit('provenance-audit-progress', checked=checked, errors=len(errors))
    report = {'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'public_checkpoint': public, 'member_checkpoint': member, 'native_checkpoint': native_pointer,
              'associations': len(work), 'manifests': len(grouped), 'verified': checked - len(errors),
              'errors': errors, 'seconds': round(time.monotonic() - started, 2),
              'scope': 'Immutable manifest ciphertext, catalogue association, source provenance, authenticated provenance, chunk addresses and lengths. Payload bytes verified during Drive delivery.'}
    value = runtime.private_object(cache, 'audits', gzip.compress(json.dumps(report).encode(), mtime=0))
    runtime.read_private(reader, value, 128 * 1024**2)
    value.update(manifests=len(grouped), errors=len(errors), at=report['at'])
    cache.put(runtime.PREFIX + 'audits/latest.json', json.dumps(value).encode())
    runtime.emit('provenance-audit-finished', manifests=len(grouped), errors=len(errors))
    if errors:
        raise RuntimeError('provenance audit needs inspection')
