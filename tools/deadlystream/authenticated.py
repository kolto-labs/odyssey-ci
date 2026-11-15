#!/usr/bin/env python3
"""Retry public-linked downloads using an explicitly provisioned site session."""
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time


def run(runtime, cfg, cache, reader, minutes):
    root = runtime.BASE / 'whole-site'
    cookie_file = runtime.BASE / 'private/site-cookies.json'
    if not cookie_file.exists():
        raise RuntimeError('A dedicated Deadly Stream session must be provisioned for this mode')
    cookies = json.loads(cookie_file.read_text())
    allowed_names = {'ips4_member_id', 'ips4_login_key', 'ips4_IPSSessionFront', 'ips4_device_key', 'ips4_loggedIn'}
    if not cookies or any(c['name'] not in allowed_names or c['domain'] not in
                         ('deadlystream.com', '.deadlystream.com', 'www.deadlystream.com') for c in cookies):
        raise ValueError('unexpected site-session scope')
    member = next((c['value'] for c in cookies if c['name'] == 'ips4_member_id'), '')
    if not re.fullmatch(r'[1-9][0-9]*', member):
        raise ValueError('missing member identity')
    own_cfg = {**cfg, 'state': str(root / 'authenticated'), 'source_interval_seconds': 1.0}
    source = runtime.module('catalogue_cache').Catalogue(own_cfg)
    for cookie in cookies:
        source.session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'],
                                   path=cookie['path'], secure=True)
    # A cookie's presence is not authorization. Require the site to acknowledge it.
    response = source.get('https://deadlystream.com/files/file/3193-pazaak-deck-collection/')
    body = response.content
    response.close()
    if re.search(br'<h[1-6][^>]*>\s*(?:Banned|Awaiting Validation)', body, re.I):
        raise RuntimeError('site account is restricted; authenticated capture stopped')
    if not re.search(rb'/profile/' + member.encode() + rb'[-/]', body):
        raise RuntimeError('site did not acknowledge the provided signed-in session')
    runtime.emit('site-session-admitted', scope='public-linked downloads only; no private forum crawl')
    original_store = source.store_file
    def store(version, response, row_key, selector, filename_hint=None):
        return original_store(version, response, row_key,
                              {**selector, 'authenticated_retrieval': True}, filename_hint)
    source.store_file = store
    discovery = sqlite3.connect(root / 'whole-site.sqlite')
    discovery.row_factory = sqlite3.Row
    report_path = root / 'authenticated-report.json'
    previous = runtime.pointer(reader, 'whole-site/actions/authenticated/latest.json')
    report = json.loads(gzip.decompress(runtime.read_private(reader, previous, 64 * 1024 * 1024))) if previous else {'assets': [], 'linked': {'files': [], 'versions': [], 'items': [], 'gaps': []}, 'attempts': []}
    for table, rows in report['linked'].items():
        columns = {r[1] for r in source.db.execute('PRAGMA table_info(' + table + ')')}
        for row in rows:
            fields = [key for key in row if key in columns]
            source.db.execute('INSERT OR IGNORE INTO ' + table + '(' + ','.join(fields) + ') VALUES(' + ','.join('?' for _ in fields) + ')', tuple(row[key] for key in fields))
    source.db.commit()
    attempted = {row['identity'] for row in report['attempts'] if row.get('kind') == 'resource'}
    end = time.monotonic() + minutes * 60
    def persist():
        nonlocal previous
        report['at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        report['source_public_checkpoint'] = runtime.pointer(reader, runtime.PREFIX + 'latest.json')
        plain = gzip.compress(json.dumps(report).encode(), mtime=0)
        value = runtime.private_object(cache, 'authenticated/reports', plain)
        runtime.read_private(reader, value, len(plain) + 1024 * 1024)
        current = runtime.pointer(reader, 'whole-site/actions/authenticated/latest.json')
        if current != previous:
            raise RuntimeError('authenticated report owner changed')
        value.update(at=report['at'], run_id=__import__('os').environ.get('GITHUB_RUN_ID'))
        cache.put('whole-site/actions/authenticated/latest.json', json.dumps(value).encode())
        previous = value
        report_path.write_text(json.dumps(report))
        runtime.emit('authenticated-progress', attempts=len(report['attempts']),
                     captured_resources=len(report['assets']), captured_native_items=len(report['linked']['items']))
    try:
        for row in discovery.execute("""SELECT * FROM assets WHERE state='unavailable'
            AND kind IN ('attachment','legacy-attachment','linked-download','linked-file')
            AND (status IN (401,403) OR error='source-returned-page-instead-of-download')
            ORDER BY CASE WHEN kind='attachment' THEN 0 ELSE 1 END,id"""):
            if time.monotonic() >= end:
                break
            if row['identity'] in attempted:
                continue
            result = {'kind': 'resource', 'identity': row['identity'], 'source_status': row['status']}
            try:
                response = source.get(row['url'])
                content_type = response.headers.get('Content-Type', '').lower()
                if ('html' in content_type or 'json' in content_type) and 'attachment' not in response.headers.get('Content-Disposition', '').lower():
                    response.close()
                    raise ValueError('authenticated endpoint returned a page')
                file_id = -row['id']
                revision = 'site-resource-' + hashlib.sha256(row['identity'].encode()).hexdigest()[:24]
                source.db.execute('INSERT OR IGNORE INTO files(id,url) VALUES(?,?)', (file_id, row['url']))
                source.db.execute('INSERT OR IGNORE INTO versions(file_id,revision,label,history_id,source_url) VALUES(?,?,?,?,?)',
                                  (file_id, revision, row['kind'], '0', row['url']))
                source.db.commit()
                version = source.db.execute('SELECT * FROM versions WHERE file_id=? AND revision=?', (file_id, revision)).fetchone()
                source.store_file(version, response, hashlib.sha256(row['identity'].encode()).hexdigest(),
                                  {'kind': row['kind'], 'public_link': True}, 'attachment-' + str(row['id']))
                item = source.db.execute('SELECT * FROM items WHERE file_id=? AND revision=? ORDER BY cached_at DESC LIMIT 1', (file_id, revision)).fetchone()
                captured = dict(row)
                captured.update({key: item[key] for key in ('filename', 'sha256', 'size', 'manifest')})
                captured.update(state='captured', authenticated_retrieval=True, authenticated_status=200)
                report['assets'].append(captured)
                result['captured'] = True
            except Exception as error:
                result.update(captured=False, status=getattr(error, 'status', 0), category=type(error).__name__)
            report['attempts'].append(result)
            attempted.add(row['identity'])
            if len(report['attempts']) % 10 == 0:
                persist()
        # Native recovery preserves exact historic version controls and all variants.
        _, native = reader.snapshot()
        versions = [v for v in native['versions'] if v['status'] not in ('cached', 'superseded')]
        files = {f['id']: f for f in native['files']}
        for version in versions:
            if time.monotonic() >= end:
                break
            if any(a.get('kind') == 'native' and a.get('file_id') == version['file_id'] and a.get('revision') == version['revision'] for a in report['attempts']):
                continue
            for table, value in (('files', files[version['file_id']]), ('versions', version)):
                columns = {r[1] for r in source.db.execute('PRAGMA table_info(' + table + ')')}
                fields = [k for k in value if k in columns]
                source.db.execute('INSERT OR IGNORE INTO ' + table + '(' + ','.join(fields) + ') VALUES(' + ','.join('?' for _ in fields) + ')', tuple(value[k] for k in fields))
            source.db.commit()
            current = source.db.execute('SELECT * FROM versions WHERE file_id=? AND revision=?', (version['file_id'], version['revision'])).fetchone()
            result = {'kind': 'native', 'file_id': version['file_id'], 'revision': version['revision']}
            try:
                source.acquire(current)
                result['captured'] = True
            except Exception as error:
                result.update(captured=False, status=getattr(error, 'status', 0), category=type(error).__name__)
            report['attempts'].append(result)
            report['linked'] = {table: [dict(r) for r in source.db.execute('SELECT * FROM ' + table)] for table in ('files', 'versions', 'items', 'gaps')}
            # Resource items are indexed above and must not be mistaken for native files.
            for table in ('files', 'versions', 'items', 'gaps'):
                key = 'id' if table == 'files' else 'file_id'
                report['linked'][table] = [r for r in report['linked'][table] if r[key] > 0]
            persist()
    finally:
        persist()
