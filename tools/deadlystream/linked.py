#!/usr/bin/env python3
"""Preserve all versions of native Files records found outside sitemap inventory."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import time

BASE = Path(os.environ['DEADLYSTREAM_WORK_ROOT'])
ROOT = BASE / 'whole-site'
spec = importlib.util.spec_from_file_location('catalogue', BASE / 'catalogue_cache.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
cfg = json.loads((BASE / 'config.json').read_text())
cfg['state'] = str(ROOT / 'linked-catalogue')
cache = module.Catalogue(cfg)
inventory = json.loads((BASE / 'output/whole-site-inventory.json').read_text())
known_file = ROOT / 'known-native-ids.json'
known = set(json.loads(known_file.read_text())) if known_file.exists() else {int(match[1]) for url in inventory['urls'] if (match := re.search(r'/files/file/(\d+)', url))}
while True:
    with sqlite3.connect(ROOT / 'whole-site.sqlite') as discovery:
        updates = discovery.execute("SELECT file_id,url FROM native_updates WHERE state='pending'").fetchall() if discovery.execute("SELECT 1 FROM sqlite_master WHERE name='native_updates'").fetchone() else []
        if updates:
            import gzip
            baseline = json.loads(gzip.decompress((ROOT / 'native-baseline.json.gz').read_bytes()))
            affected = {file_id for file_id, url in updates}
            for table in ('files', 'versions', 'items', 'gaps'):
                columns = {r[1] for r in cache.db.execute('PRAGMA table_info(' + table + ')')}
                for row in baseline[table]:
                    if row['id' if table == 'files' else 'file_id'] not in affected:
                        continue
                    fields = [key for key in row if key in columns]
                    cache.db.execute('INSERT OR IGNORE INTO ' + table + '(' + ','.join(fields) + ') VALUES(' + ','.join('?' for _ in fields) + ')', tuple(row[key] for key in fields))
            for file_id, url in updates:
                cache.db.execute("INSERT INTO files(id,url,status) VALUES(?,?,'pending') ON CONFLICT(id) DO UPDATE SET url=excluded.url,status='pending',error=NULL", (file_id, module.canonical(url)))
            cache.db.commit()
            discovery.executemany("UPDATE native_updates SET state='admitted' WHERE file_id=?", [(file_id,) for file_id, url in updates])
        observed_pages = discovery.execute('SELECT url FROM pages').fetchall()
        for url, in observed_pages:
            match = re.search(r'/files/file/(\d+)', url)
            if match and int(match[1]) not in known:
                cache.db.execute('INSERT OR IGNORE INTO files(id,url) VALUES(?,?)', (int(match[1]), module.canonical(url)))
    cache.db.commit()
    for row in cache.db.execute("SELECT * FROM files WHERE status='pending'").fetchall():
        try:
            cache.scan(row)
        except Exception as error:
            if isinstance(error, module.SourceError) and error.code in ('source-proxy-unavailable', 'source-proxy-required'):
                break
            cache.failure('files', row, error)
    for version in cache.db.execute("SELECT * FROM versions WHERE status='pending' ORDER BY history_id='0' DESC").fetchall():
        try:
            cache.acquire(version)
        except Exception as error:
            if isinstance(error, module.SourceError) and error.code in ('source-proxy-unavailable', 'source-proxy-required'):
                break
            cache.failure('versions', version, error)
    print(json.dumps({'event': 'linked-native-pass', 'files': cache.db.execute('SELECT count(*) FROM files').fetchone()[0],
                      'items': cache.db.execute('SELECT count(*) FROM items').fetchone()[0]}), flush=True)
    pass_file = ROOT / 'linked-last-pass.new'
    pass_file.write_text(json.dumps({'discovery_page_count': len(observed_pages), 'at_unix': time.time()}))
    pass_file.replace(ROOT / 'linked-last-pass.json')
    time.sleep(90)
