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
known = {int(match[1]) for url in inventory['urls'] if (match := re.search(r'/files/file/(\d+)', url))}
while True:
    with sqlite3.connect(ROOT / 'whole-site.sqlite') as discovery:
        for url, in discovery.execute('SELECT url FROM pages'):
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
    time.sleep(90)
