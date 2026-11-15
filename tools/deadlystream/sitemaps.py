"""Refresh the complete official sitemap frontier and detect native release updates."""
import datetime
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


def timestamp(value):
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def requeue_changed_pages(connection, url, modified, previous):
    current = timestamp(modified)
    if current is None:
        return 0
    base = url.split('?', 1)[0].rstrip('/')
    escaped = base.replace('!', '!!').replace('%', '!%').replace('_', '!_')
    rows = connection.execute("""SELECT url,at FROM pages
        WHERE url=? OR url LIKE ? ESCAPE '!' OR url LIKE ? ESCAPE '!'""",
        (base, escaped + '/%', escaped + '?%')).fetchall()
    if not rows:
        return 0
    # The first refresh compares against every retained page capture. Later
    # refreshes compare the last committed official modification timestamp.
    before = timestamp(previous)
    if before is None:
        before = min(timestamp(row[1]) or 0 for row in rows)
    if current <= before:
        return 0
    connection.executemany("UPDATE pages SET state='pending',next_attempt=0 WHERE url=? AND state<>'fetching'",
                           [(row[0],) for row in rows])
    return len(rows)


def refresh(runtime, cache, reader):
    root = runtime.BASE / 'whole-site'
    baseline_file = root / 'native-baseline.json.gz'
    if not baseline_file.exists():
        _, baseline = reader.snapshot()
        baseline_file.write_bytes(gzip.compress(json.dumps(baseline).encode(), mtime=0))
    else:
        baseline = json.loads(gzip.decompress(baseline_file.read_bytes()))
    known = {row['id']: row for row in baseline['files']}
    (root / 'known-native-ids.json').write_text(json.dumps(sorted(known)))
    inventory_path = runtime.BASE / 'output/whole-site-inventory.json'
    old = json.loads(inventory_path.read_text())
    directory = root / 'sitemaps'
    directory.mkdir(exist_ok=True)
    def read(url):
        address = urlsplit(url)
        if address.scheme != 'https' or address.hostname not in ('deadlystream.com', 'www.deadlystream.com'):
            raise ValueError('unexpected sitemap origin')
        response = cache.get(url)
        data = bytearray()
        try:
            for block in response.iter_content(65536):
                data.extend(block)
                if len(data) > 16 * 1024 * 1024:
                    raise ValueError('oversized sitemap')
        finally:
            response.close()
        data = bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        (directory / (digest + '.xml')).write_bytes(data)
        return ET.fromstring(data), digest
    index, index_sha = read('https://deadlystream.com/sitemap.php')
    maps = [node.text for node in index.findall('.//{*}loc')]
    if not 1 <= len(maps) <= 256:
        raise ValueError('unexpected sitemap count')
    fresh = {}
    proofs = []
    for url in maps:
        tree, digest = read(url)
        proofs.append({'url': url, 'sha256': digest})
        for node in tree.findall('.//{*}url'):
            location = node.find('{*}loc')
            modified = node.find('{*}lastmod')
            if location is not None and location.text:
                address = urlsplit(location.text)
                if address.scheme == 'https' and address.hostname in ('deadlystream.com', 'www.deadlystream.com'):
                    fresh[location.text] = modified.text if modified is not None else ''
    if len(fresh) < len(old['urls']) * 0.9:
        raise ValueError('sitemap refresh unexpectedly shrank; previous inventory retained')
    connection = sqlite3.connect(root / 'whole-site.sqlite')
    connection.execute('''CREATE TABLE IF NOT EXISTS native_updates(
        file_id INTEGER PRIMARY KEY,url TEXT,observed_modified TEXT,state TEXT DEFAULT 'pending')''')
    changed = 0
    refreshed_pages = 0
    for url, modified in fresh.items():
        match = re.search(r'/files/file/(\d+)', url)
        key = 'official-lastmod:' + url
        previous = connection.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        refreshed_pages += requeue_changed_pages(connection, url, modified, previous[0] if previous else None)
        if match:
            file_id = int(match[1])
            metadata = json.loads(known.get(file_id, {}).get('metadata') or '{}')
            before = previous[0] if previous else metadata.get('dateModified')
            current_time, previous_time = timestamp(modified), timestamp(before)
            is_changed = file_id not in known or (current_time is not None and previous_time is not None and current_time > previous_time)
            if is_changed:
                changed_row = connection.execute('''INSERT INTO native_updates(file_id,url,observed_modified,state) VALUES(?,?,?,'pending')
                    ON CONFLICT(file_id) DO UPDATE SET url=excluded.url,observed_modified=excluded.observed_modified,state='pending'
                    WHERE native_updates.observed_modified<>excluded.observed_modified''', (file_id, url, modified))
                if changed_row.rowcount:
                    connection.execute("UPDATE pages SET state='pending',next_attempt=0 WHERE url=? AND state<>'fetching'", (url,))
                    changed += 1
        connection.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (key, modified or ''))
    connection.commit()
    connection.close()
    value = {**old, 'urls': sorted(set(old['urls']) | set(fresh)), 'last_refresh': {
        'index_sha256': index_sha, 'sitemaps': proofs, 'live_urls': len(fresh),
        'retained_old_urls': len(set(old['urls']) - set(fresh)), 'changed_native_records': changed,
        'refreshed_existing_pages': refreshed_pages}}
    inventory_path.write_text(json.dumps(value))
    runtime.emit('official-sitemaps-refreshed', maps=len(maps), live_urls=len(fresh), changed_native_records=changed,
                 refreshed_existing_pages=refreshed_pages)
