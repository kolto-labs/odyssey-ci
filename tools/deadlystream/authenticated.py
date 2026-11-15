#!/usr/bin/env python3
"""Retry public-linked downloads using an explicitly provisioned site session."""
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import mimetypes
from urllib.parse import unquote, urlsplit
from bs4 import BeautifulSoup


def run(runtime, cfg, cache, reader, minutes, public_checkpoint=None):
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
    own_cfg = {**cfg, 'state': str(root / 'authenticated'), 'source_interval_seconds': 1 / 3,
               'provenance_flags': {'authenticated_retrieval': True}}
    catalogue_module = runtime.module('catalogue_cache')
    source = catalogue_module.Catalogue(own_cfg)
    for cookie in cookies:
        source.session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'],
                                   path=cookie['path'], secure=True)
    # A cookie's presence is not authorization. Require the site to acknowledge it.
    def validate_session():
        try:
            response = source.get('https://deadlystream.com/files/file/3193-pazaak-deck-collection/')
            body = bytearray()
            try:
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 16 * 1024 * 1024:
                        raise ValueError('oversized site-session validation page')
            finally:
                response.close()
        except Exception as error:
            runtime.emit('site-session-validation-denied', status=getattr(error, 'status', 0), category=type(error).__name__)
            raise
        if re.search(br'<h[1-6][^>]*>\s*(?:Banned|Awaiting Validation)', body, re.I):
            raise RuntimeError('site account is restricted; authenticated capture stopped')
        nav = BeautifulSoup(bytes(body), 'html.parser').select_one('#elUserNav')
        if not nav or not any(re.search(r'/profile/' + member + r'[-/]', a.get('href', '')) for a in nav.select('a[href]')):
            soup = BeautifulSoup(bytes(body), 'html.parser')
            runtime.emit('site-session-not-accepted', user_navigation_present=bool(nav),
                         login_link_present=bool(soup.select('a[href*="/login/"]')),
                         member_profile_visible=bool(soup.find('a', href=re.compile(r'/profile/' + member + r'[-/]'))),
                         html_bytes=len(body))
            raise RuntimeError('site did not acknowledge the provided signed-in session')
    validate_session()
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
    report['session_revalidation_failed'] = False
    for table, rows in report['linked'].items():
        columns = {r[1] for r in source.db.execute('PRAGMA table_info(' + table + ')')}
        for row in rows:
            fields = [key for key in row if key in columns]
            source.db.execute('INSERT OR IGNORE INTO ' + table + '(' + ','.join(fields) + ') VALUES(' + ','.join('?' for _ in fields) + ')', tuple(row[key] for key in fields))
    source.db.commit()
    def terminal(row):
        if row.get('kind') == 'resource' and row.get('status') in (401, 403, 404, 410) and row.get('published_alternatives_checked') is not True:
            return False
        return row.get('captured') or row.get('status') in (401, 403, 404, 410) or row.get('source_code') in (
            'authenticated-source-returned-page', 'historical-download-unavailable', 'download-unavailable',
            'source-empty-download', 'source-version-has-no-files', 'empty-download-selection',
            'previously-listed-download-unavailable', 'some-download-variants-unavailable',
            'current-release-changed-before-download')
    attempted = {row['identity'] for row in report['attempts'] if row.get('kind') == 'resource' and terminal(row)}
    end = time.monotonic() + minutes * 60
    resource_end = time.monotonic() + min(600, minutes * 20)
    next_validation = time.monotonic() + 300
    def revalidate():
        nonlocal next_validation
        if time.monotonic() < next_validation:
            return
        try:
            validate_session()
        except Exception:
            report['session_revalidation_failed'] = True
            raise
        next_validation = time.monotonic() + 300

    def acquire_resource(row):
        urls = [row['url']]
        if row['kind'] == 'attachment':
            urls.extend(value[0] for value in discovery.execute(
                'SELECT DISTINCT url FROM attachment_links WHERE identity=? ORDER BY url', (row['identity'],))
                if value[0] != row['url'])
        last_error = None
        for url in urls:
            try:
                response = source.get(url)
                content_type = response.headers.get('Content-Type', '').lower()
                if ('html' in content_type or 'json' in content_type) and 'attachment' not in response.headers.get('Content-Disposition', '').lower():
                    response.close()
                    raise catalogue_module.SourceError('authenticated-source-returned-page', response.status_code)
                file_id = -row['id']
                revision = 'site-resource-' + hashlib.sha256(row['identity'].encode()).hexdigest()[:24]
                source.db.execute('INSERT OR IGNORE INTO files(id,url) VALUES(?,?)', (file_id, url))
                source.db.execute('INSERT OR IGNORE INTO versions(file_id,revision,label,history_id,source_url) VALUES(?,?,?,?,?)',
                                  (file_id, revision, row['kind'], '0', url))
                source.db.commit()
                version = dict(source.db.execute('SELECT * FROM versions WHERE file_id=? AND revision=?', (file_id, revision)).fetchone())
                version['source_url'] = url
                hint = unquote(Path(urlsplit(response.url).path).name)
                if hint in ('', 'attachment.php', 'index.php', 'download.php'):
                    hint = 'attachment-' + str(row['id']) + (mimetypes.guess_extension(content_type.split(';')[0]) or '.bin')
                source.store_file(version, response, hashlib.sha256(row['identity'].encode()).hexdigest(),
                                  {'kind': row['kind'], 'public_link': True}, hint)
                item = source.db.execute('SELECT * FROM items WHERE file_id=? AND revision=? ORDER BY cached_at DESC LIMIT 1', (file_id, revision)).fetchone()
                captured = dict(row)
                captured.update({key: item[key] for key in ('filename', 'sha256', 'size', 'manifest')})
                captured.update(url=url, state='captured', authenticated_retrieval=True, authenticated_status=200)
                return captured
            except catalogue_module.SourceError as error:
                last_error = error
                if error.status not in (401, 403, 404, 410) and error.code not in ('authenticated-source-returned-page', 'source-empty-download'):
                    raise
        raise last_error
    def persist():
        nonlocal previous
        report['linked'] = {}
        for table in ('files', 'versions', 'items', 'gaps'):
            key = 'id' if table == 'files' else 'file_id'
            report['linked'][table] = [dict(row) for row in source.db.execute('SELECT * FROM ' + table + ' WHERE ' + key + '>0')]
        report['at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        report['rate_policy'] = runtime.rate_policy(cache, reader, 'member')
        report['source_public_checkpoint'] = public_checkpoint
        report['resource_attempts_terminal'] = sum(a.get('kind') == 'resource' and bool(terminal(a)) for a in report['attempts'])
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
            if time.monotonic() >= resource_end:
                break
            if row['identity'] in attempted:
                continue
            revalidate()
            result = {'kind': 'resource', 'identity': row['identity'], 'source_status': row['status'],
                      'published_alternatives_checked': True}
            try:
                report['assets'].append(acquire_resource(row))
                result['captured'] = True
            except Exception as error:
                result.update(captured=False, status=getattr(error, 'status', 0), category=type(error).__name__, source_code=getattr(error, 'code', None))
            report['attempts'].append(result)
            attempted.add(row['identity'])
            if len(report['attempts']) % 10 == 0:
                persist()
        # Native recovery preserves exact historic version controls and all variants.
        _, native = reader.snapshot()
        files = {f['id']: f for f in native['files']}
        all_versions = {(v['file_id'], v['revision']): v for v in native['versions']}
        linked = {}
        if public_checkpoint and public_checkpoint.get('index'):
            linked = json.loads(gzip.decompress(runtime.read_private(reader, public_checkpoint['index'], 64 * 1024 * 1024))).get('linked') or {}
        files.update({f['id']: f for f in linked.get('files', [])})
        all_versions.update({(v['file_id'], v['revision']): v for v in linked.get('versions', [])})
        linked_version_ids = {v['file_id'] for v in linked.get('versions', [])}
        scan_targets = [f for f in linked.get('files', []) if f['status'] == 'failed' or f['id'] not in linked_version_ids]
        for file in scan_targets:
            if time.monotonic() >= end:
                break
            if any(a.get('kind') == 'native-scan' and a.get('file_id') == file['id'] and terminal(a) for a in report['attempts']):
                continue
            revalidate()
            # Reuse cached selectors before rescanning an existing native record.
            for table in ('files', 'versions', 'items', 'gaps'):
                columns = {r[1] for r in source.db.execute('PRAGMA table_info(' + table + ')')}
                for value in native[table]:
                    if value['id' if table == 'files' else 'file_id'] != file['id']:
                        continue
                    fields = [key for key in value if key in columns]
                    source.db.execute('INSERT OR IGNORE INTO ' + table + '(' + ','.join(fields) + ') VALUES(' + ','.join('?' for _ in fields) + ')', tuple(value[key] for key in fields))
            source.db.execute('INSERT OR IGNORE INTO files(id,url) VALUES(?,?)', (file['id'], file['url']))
            source.db.commit()
            row = source.db.execute('SELECT * FROM files WHERE id=?', (file['id'],)).fetchone()
            result = {'kind': 'native-scan', 'file_id': file['id']}
            try:
                source.scan(row)
                files[file['id']] = dict(source.db.execute('SELECT * FROM files WHERE id=?', (file['id'],)).fetchone())
                result['captured'] = True
            except Exception as error:
                source.failure('files', row, error)
                result.update(captured=False, status=getattr(error, 'status', 0), category=type(error).__name__, source_code=getattr(error, 'code', None))
            report['attempts'].append(result)
            persist()
        all_versions.update({(v['file_id'], v['revision']): dict(v) for v in source.db.execute('SELECT * FROM versions WHERE file_id>0')})
        versions = [v for v in all_versions.values() if v['status'] not in ('cached', 'superseded')]
        for version in versions:
            if time.monotonic() >= end:
                break
            if any(a.get('kind') == 'native' and a.get('file_id') == version['file_id'] and a.get('revision') == version['revision']
                   and terminal(a) for a in report['attempts']):
                continue
            revalidate()
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
                result.update(captured=False, status=getattr(error, 'status', 0), category=type(error).__name__, source_code=getattr(error, 'code', None))
            report['attempts'].append(result)
            persist()
        done_resources = {a['identity'] for a in report['attempts'] if a.get('kind') == 'resource' and terminal(a)}
        candidates = discovery.execute("""SELECT identity FROM assets WHERE state='unavailable'
            AND kind IN ('attachment','legacy-attachment','linked-download','linked-file')
            AND (status IN (401,403) OR error='source-returned-page-instead-of-download')""")
        report['remaining_resources'] = sum(row['identity'] not in done_resources for row in candidates)
        done_native = {(a['file_id'], a['revision']) for a in report['attempts'] if a.get('kind') == 'native' and terminal(a)}
        report['remaining_native_versions'] = sum((v['file_id'], v['revision']) not in done_native for v in versions)
        done_scans = {a['file_id'] for a in report['attempts'] if a.get('kind') == 'native-scan' and terminal(a)}
        report['remaining_native_files'] = sum(f['id'] not in done_scans for f in scan_targets)
        report['access_review_required'] = sum(a.get('source_code') == 'source-login-challenge-or-unavailable' for a in report['attempts'])
    finally:
        persist()
