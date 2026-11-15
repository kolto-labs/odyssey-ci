#!/usr/bin/env python3
"""Bounded cloud-only preservation, encrypted checkpoints and scoped Drive jobs."""
import argparse
import concurrent.futures
import gzip
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = Path(__file__).resolve().parent
BASE = Path(os.environ.get('DEADLYSTREAM_WORK_ROOT', '/tmp/deadlystream-preservation'))
PREFIX = 'whole-site/actions/'
DELIVERY = None


def module(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def emit(event, **values):
    print(json.dumps({'event': event, **values}), flush=True)


def setup():
    os.umask(0o077)
    BASE.mkdir(parents=True, exist_ok=True)
    private = BASE / 'private'
    private.mkdir(exist_ok=True)
    required = ['WRITE_ACCESS_KEY_ID', 'WRITE_SECRET_ACCESS_KEY', 'READ_ACCESS_KEY_ID',
                'READ_SECRET_ACCESS_KEY', 'DECRYPTION_IDENTITY']
    missing = [name for name in required if not os.environ.get('DEADLYSTREAM_CACHE_' + name)]
    if missing:
        raise RuntimeError('Missing catalogue settings: ' + ', '.join('DEADLYSTREAM_CACHE_' + x for x in missing))
    for kind in ('write', 'read'):
        (private / (kind + '.json')).write_text(json.dumps({
            'id': os.environ.pop('DEADLYSTREAM_CACHE_' + kind.upper() + '_ACCESS_KEY_ID'),
            'secret_access_key': os.environ.pop('DEADLYSTREAM_CACHE_' + kind.upper() + '_SECRET_ACCESS_KEY')}))
    identity = private / 'catalogue.agekey'
    identity.write_text(os.environ.pop('DEADLYSTREAM_CACHE_DECRYPTION_IDENTITY'))
    site_session = os.environ.pop('DEADLYSTREAM_SESSION_COOKIES', '')
    if site_session:
        json.loads(site_session)
        (private / 'site-cookies.json').write_text(site_session)
    cfg = {
        'bucket': os.environ.get('DEADLYSTREAM_CACHE_BUCKET') or 'kolto-deadlystream-cache',
        'endpoint': os.environ.get('DEADLYSTREAM_CACHE_ENDPOINT') or 'https://e4eb3dbeb12beaacc177041231790454.r2.cloudflarestorage.com',
        'recipient': os.environ.get('DEADLYSTREAM_CACHE_ENCRYPTION_RECIPIENT') or 'age1gsph5wg3w4lsfwanyl4gqgp78200ewt9w4p08djzxjxrxwzr9pnqdt7t4x',
        'writer_file': str(private / 'write.json'), 'reader_file': str(private / 'read.json'),
        'identity': str(identity), 'age': shutil.which('age'), 'state': str(BASE / 'state'),
        'source_proxy': 'socks5h://127.0.0.1:40000', 'source_require_proxy': True,
        'source_interval_seconds': 1 / 3, 'source_rate_state': str(BASE / 'state'),
        'page_import_version': 'v6',
        'source_policy_state': str(BASE / 'state'), 'catalogue_urls': str(BASE / 'file-urls.json'),
    }
    if not cfg['age']:
        raise RuntimeError('age is unavailable')
    (BASE / 'config.json').write_text(json.dumps(cfg))
    for name in ('catalogue_cache.py', 'catalogue_read.py'):
        shutil.copyfile(HERE / name, BASE / name)
    cache = module('catalogue_cache').Catalogue(cfg)
    reader = module('catalogue_read').Reader(cfg, str(identity))
    emit('catalogue-credentials-admitted', scopes='catalogue-only; repo or organization secrets')
    return cfg, cache, reader


def private_object(cache, key, data):
    encrypted = cache.encrypt(data)
    digest = hashlib.sha256(encrypted).hexdigest()
    address = PREFIX + key + '/' + digest + '.age'
    cache.put(address, encrypted, immutable=True)
    return {'object': address, 'cipher_sha256': digest, 'plaintext_sha256': hashlib.sha256(data).hexdigest()}


def read_private(reader, pointer, limit):
    data = reader.encrypted(pointer['object'], pointer['cipher_sha256'], limit)
    if hashlib.sha256(data).hexdigest() != pointer['plaintext_sha256']:
        raise ValueError('checkpoint plaintext checksum mismatch')
    return data


def pointer(reader, key):
    from botocore.exceptions import ClientError
    try:
        return json.loads(reader.get(key, 65536))
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode'] != 404:
            raise
        return None


def rate_policy(cache, reader, channel, apply=False):
    key = PREFIX + channel + '-rate-policy.json'
    saved = pointer(reader, key) or {'not_before': 0, 'minimum_interval': 0}
    state = BASE / 'state'
    state.mkdir(exist_ok=True)
    values = {}
    for field, filename in (('not_before', 'source-not-before'), ('minimum_interval', 'source-interval')):
        try:
            local = float((state / filename).read_text())
        except (OSError, ValueError):
            local = 0
        remote = float(saved.get(field, 0))
        if not math.isfinite(local) or not math.isfinite(remote) or min(local, remote) < 0:
            raise ValueError('invalid source backoff metadata')
        values[field] = max(local, remote)
        if apply:
            (state / filename).write_text(str(values[field]))
    if values != {name: saved.get(name, 0) for name in values}:
        cache.put(key, json.dumps(values).encode())
    return values


def extract_checkpoint(archive):
    exact = {'whole-site/whole-site.sqlite', 'output/whole-site-inventory.json',
             'whole-site/legacy-page-sources.json.gz',
             'state/robots.txt', 'state/source-not-before', 'state/source-interval', 'state/source-last-request',
             'whole-site/native-baseline.json.gz', 'whole-site/known-native-ids.json',
             'whole-site/linked-catalogue/catalogue.sqlite',
             'whole-site/asset-state/catalogue.sqlite', 'whole-site/asset-state/catalogue.sqlite-wal',
             'whole-site/asset-state/catalogue.sqlite-shm'}
    directories = {'whole-site', 'output', 'state', 'state/source-pages', 'whole-site/pages', 'whole-site/sitemaps',
                   'whole-site/asset-state', 'whole-site/linked-catalogue',
                   'whole-site/linked-catalogue/page-observations',
                   'whole-site/linked-catalogue/page-observations/bytes',
                   'whole-site/linked-catalogue/source-pages'}
    total = 0
    for entry in archive.getmembers():
        name = entry.name.rstrip('/')
        if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
            raise ValueError('unsafe checkpoint path')
        if entry.isdir() and name in directories:
            continue
        allowed = (name in exact or re.fullmatch(r'(?:whole-site/pages|state/source-pages)/[0-9a-f]{64}\.html\.gz', name)
                   or re.fullmatch(r'whole-site/sitemaps/[0-9a-f]{64}\.xml', name)
                   or re.fullmatch(r'whole-site/linked-catalogue/page-observations/(?:bytes/[0-9a-f]{64}\.html\.gz|[0-9]+-[0-9]+-[0-9a-f]{12}\.json)', name)
                   or re.fullmatch(r'whole-site/linked-catalogue/source-pages/[A-Za-z0-9_.-]+\.gz', name))
        if not entry.isfile() or not allowed:
            raise ValueError('unexpected checkpoint member; code, credentials and links are forbidden')
        total += entry.size
        if total > 8 * 1024**3:
            raise ValueError('checkpoint expansion exceeds workspace budget')
    archive.extractall(BASE, filter='data')


def restore(cfg, cache, reader):
    root = BASE / 'whole-site'
    root.mkdir(exist_ok=True)
    previous = pointer(reader, PREFIX + 'latest.json')
    if previous:
        data = read_private(reader, previous, 1024 * 1024 * 1024)
        path = BASE / 'restore.tar.gz'
        path.write_bytes(data)
        with tarfile.open(path, 'r:gz') as archive:
            extract_checkpoint(archive)
        path.unlink()
        emit('actions-checkpoint-restored', cipher_sha256=previous['cipher_sha256'])
    else:
        initial = pointer(reader, 'whole-site/discovery/latest.json')
        if not initial:
            raise RuntimeError('Missing original discovery checkpoint; refusing an empty crawl')
        plain = read_private(reader, initial, 1024 * 1024 * 1024)
        (root / 'whole-site.sqlite').write_bytes(gzip.decompress(plain))
        seed = pointer(reader, PREFIX + 'bootstrap.json')
        if not seed:
            raise RuntimeError('Missing encrypted sitemap inventory')
        inventory = read_private(reader, seed, 16 * 1024 * 1024)
        (BASE / 'output').mkdir(exist_ok=True)
        (BASE / 'output/whole-site-inventory.json').write_bytes(inventory)
        emit('original-discovery-restored', cipher_sha256=initial['cipher_sha256'])
    connection = sqlite3.connect(root / 'whole-site.sqlite')
    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('recovered discovery database failed integrity check')
    connection.close()
    return previous


def checkpoint(cache, reader, previous):
    root = BASE / 'whole-site'
    if not (root / 'whole-site.sqlite').exists():
        return previous
    policy = rate_policy(cache, reader, 'public')
    # No source payloads or account credentials are included in the metadata index.
    original = sqlite3.connect(root / 'whole-site.sqlite')
    target = sqlite3.connect(root / 'checkpoint.sqlite')
    original.backup(target)
    target.execute('PRAGMA journal_mode=DELETE')
    target.close()
    original.close()
    frozen = sqlite3.connect(root / 'checkpoint.sqlite')
    frozen.row_factory = sqlite3.Row
    counts = {name: dict(frozen.execute('SELECT state,count(*) FROM ' + name + ' GROUP BY state'))
              for name in ('pages', 'assets')}
    rows = [dict(row) for row in frozen.execute("SELECT * FROM assets WHERE state='captured'")]
    page_paths = [row[0] for row in frozen.execute("SELECT DISTINCT path FROM pages WHERE path IS NOT NULL")]
    frozen.close()
    linked = {}
    linked_path = root / 'linked-catalogue/catalogue.sqlite'
    if linked_path.exists():
        with sqlite3.connect(linked_path) as src, sqlite3.connect(root / 'linked-checkpoint.sqlite') as dst:
            src.backup(dst)
        with sqlite3.connect(root / 'linked-checkpoint.sqlite') as src:
            src.row_factory = sqlite3.Row
            linked = {table: [dict(row) for row in src.execute('SELECT * FROM ' + table)]
                      for table in ('files', 'versions', 'items', 'gaps')}
            counts['native_files'] = dict(src.execute('SELECT status,count(*) FROM files GROUP BY status'))
            counts['native_versions'] = dict(src.execute('SELECT status,count(*) FROM versions GROUP BY status'))
    index = private_object(cache, 'indexes', gzip.compress(json.dumps({'counts': counts, 'assets': rows, 'linked': linked}).encode(), mtime=0))
    package = BASE / 'checkpoint.tar.gz'
    legacy_index = root / 'legacy-page-sources.json.gz'
    legacy_names = set()
    if legacy_index.exists():
        legacy_names = {row['path'].split('/')[-1] for row in json.loads(gzip.decompress(legacy_index.read_bytes()))['pages']
                        if row['path'].startswith('whole-site/pages/')}
    with tarfile.open(package, 'w:gz') as archive:
        archive.add(root / 'checkpoint.sqlite', arcname='whole-site/whole-site.sqlite')
        archive.add(BASE / 'output/whole-site-inventory.json', arcname='output/whole-site-inventory.json')
        if legacy_index.exists():
            archive.add(legacy_index, arcname='whole-site/legacy-page-sources.json.gz')
        if linked:
            archive.add(root / 'linked-checkpoint.sqlite', arcname='whole-site/linked-catalogue/catalogue.sqlite')
        for context in (root / 'linked-catalogue/page-observations', root / 'linked-catalogue/source-pages'):
            if context.exists():
                archive.add(context, arcname='whole-site/linked-catalogue/' + context.name)
        for path in (root / 'native-baseline.json.gz', root / 'known-native-ids.json', root / 'sitemaps'):
            if path.exists():
                archive.add(path, arcname='whole-site/' + path.name)
        for name in ('robots.txt', 'source-not-before', 'source-interval', 'source-last-request'):
            path = BASE / 'state' / name
            if path.exists():
                archive.add(path, arcname='state/' + name)
        # Content-addressed pages are included only after their completed capture
        # appears in the consistent SQLite backup. In-flight files are excluded.
        for old_path in page_paths:
            page = root / 'pages' / Path(old_path).name
            if page.is_file() and page.name not in legacy_names:
                archive.add(page, arcname='whole-site/pages/' + page.name)
    value = private_object(cache, 'checkpoints', package.read_bytes())
    recovered = read_private(reader, value, package.stat().st_size + 1024 * 1024)
    if hashlib.sha256(recovered).hexdigest() != hashlib.sha256(package.read_bytes()).hexdigest():
        raise ValueError('checkpoint independent readback mismatch')
    # A single workflow concurrency group owns this pointer. Refuse a stale writer.
    current = pointer(reader, PREFIX + 'latest.json')
    if current != previous:
        raise RuntimeError('Checkpoint owner changed; immutable snapshot retained without overwriting pointer')
    value.update(index=index, counts=counts, run_id=os.environ.get('GITHUB_RUN_ID'),
                 rate_policy=policy,
                 source_commit=os.environ.get('GITHUB_SHA'), at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    cache.put(PREFIX + 'latest.json', json.dumps(value).encode())
    emit('encrypted-checkpoint-verified', counts=counts, run_id=value['run_id'])
    package.unlink()
    return value


class Delivery:
    def __init__(self, cfg, cache, reader, workers, reverse):
        self.cfg, self.cache, self.reader = cfg, cache, reader
        self.workers, self.reverse = workers, reverse
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        self.mirror = module('mirror')
        self.active = {}
        self.completed = set()
        self.batch_sha = None
        self.jobs = []

    def run(self, job):
        job = dict(job)
        sha = job['sha256']
        old = pointer(self.reader, PREFIX + 'drive-receipts/' + sha + '.json')
        if old and old.get('ok'):
            return old
        job.update(reader_module=str(BASE / 'catalogue_read.py'), cache_config=str(BASE / 'config.json'),
                   catalogue_identity_file=self.cfg['identity'])
        job_file = BASE / 'private' / (sha + '.json')
        result_file = BASE / 'private' / (sha + '-result.json')
        job_file.write_text(json.dumps(job))
        try:
            result = self.mirror.execute(job_file, result_file)
            result['github_run_id'] = os.environ.get('GITHUB_RUN_ID')
            # Preserve successful receipts if another lane completed this session.
            latest = pointer(self.reader, PREFIX + 'drive-receipts/' + sha + '.json')
            if latest and latest.get('ok'):
                return latest
            self.cache.put(PREFIX + 'drive-receipts/' + sha + '.json', json.dumps(result).encode())
            return result
        except Exception as error:
            return {'ok': False, 'sha256': sha, 'category': type(error).__name__}
        finally:
            job_file.unlink(missing_ok=True)
            result_file.unlink(missing_ok=True)

    def collect(self):
        receipts = []
        for sha, future in list(self.active.items()):
            if not future.done():
                continue
            try:
                receipt = future.result()
            except Exception as error:
                receipt = {'ok': False, 'sha256': sha, 'category': type(error).__name__}
            receipts.append(receipt)
            if receipt.get('ok'):
                self.completed.add(sha)
            del self.active[sha]
        if receipts:
            emit('drive-cloud-delivery', uploaded=sum(bool(row.get('ok')) for row in receipts),
                 failed=sum(not row.get('ok') for row in receipts), active=len(self.active),
                 bytes=sum(row.get('size', 0) for row in receipts if row.get('ok')))

    def step(self):
        self.collect()
        batch = pointer(self.reader, PREFIX + 'drive-jobs/latest.json')
        if not batch:
            return
        if batch['cipher_sha256'] != self.batch_sha:
            todo = json.loads(read_private(self.reader, batch, 16 * 1024 * 1024))
            self.jobs = list(reversed(todo['jobs'])) if self.reverse else todo['jobs']
            self.batch_sha = batch['cipher_sha256']
        # Keep free workers moving while individual large or interrupted files run.
        for job in self.jobs:
            sha = job['sha256']
            if sha in self.completed or sha in self.active:
                continue
            if len(self.active) >= self.workers:
                break
            self.active[sha] = self.pool.submit(self.run, job)

    def close(self):
        self.pool.shutdown(wait=True)
        self.collect()


def deliver(cfg, cache, reader, reverse=False, workers=8):
    global DELIVERY
    if DELIVERY is None:
        DELIVERY = Delivery(cfg, cache, reader, workers, reverse)
    DELIVERY.step()


def source_frontier_drained():
    root = BASE / 'whole-site'
    if not (root / 'status.json').exists() or not (root / 'linked-last-pass.json').exists():
        return False
    with sqlite3.connect(root / 'whole-site.sqlite') as db:
        page_count = db.execute('SELECT count(*) FROM pages').fetchone()[0]
        for table in ('pages', 'assets'):
            if db.execute("SELECT count(*) FROM " + table + " WHERE state IN ('pending','fetching')").fetchone()[0]:
                return False
    last_pass = json.loads((root / 'linked-last-pass.json').read_text())
    if last_pass['discovery_page_count'] < page_count:
        return False
    linked = root / 'linked-catalogue/catalogue.sqlite'
    if not linked.exists():
        return False
    with sqlite3.connect(linked) as db:
        for table in ('files', 'versions'):
            if db.execute("SELECT count(*) FROM " + table + " WHERE status='pending'").fetchone()[0]:
                return False
    return True


def refresh_source_policy(cache):
    policy = BASE / 'state/robots.txt'
    cached = policy.read_bytes() if policy.exists() else b''
    html = br'<(?:!doctype\s+html|html|head|title)(?:\s|>)'
    cached_valid = bool(re.search(br'^\s*user-agent\s*:', cached, re.I | re.M)) and not re.search(html, cached, re.I)
    try:
        response = cache.get('https://deadlystream.com/robots.txt')
    except Exception as error:
        if not cached_valid:
            raise
        emit('source-policy-verified-cache-retained', sha256=hashlib.sha256(cached).hexdigest(),
             refresh_category=type(error).__name__)
        return
    robots = bytearray()
    try:
        for block in response.iter_content(65536):
            robots.extend(block)
            if len(robots) > 1024 * 1024:
                raise ValueError('oversized source robots response')
    finally:
        response.close()
    robots = bytes(robots)
    if re.search(html, robots, re.I):
        if cached_valid:
            emit('source-policy-verified-cache-retained', sha256=hashlib.sha256(cached).hexdigest(),
                 refresh_category='browser-check-page')
            return
        raise module('catalogue_cache').SourceError('source-browser-check-required', 403)
    policy.write_bytes(robots)
    emit('proxied-source-admitted', http_status=200, robots_sha256=hashlib.sha256(robots).hexdigest())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--minutes', type=int, default=25)
    parser.add_argument('--mode', choices=('capture', 'deliver', 'both', 'probe', 'authenticated', 'audit', 'legacy-probe', 'response-probe'), default='both')
    args = parser.parse_args()
    if not 1 <= args.minutes <= 240:
        raise ValueError('minutes must be between 1 and 240')
    if args.mode == 'authenticated' and not os.environ.get('DEADLYSTREAM_SESSION_COOKIES'):
        emit('site-session-setting-required', setting='DEADLYSTREAM_SESSION_COOKIES')
        raise SystemExit(2)
    cfg, cache, reader = setup()
    if args.mode in ('audit', 'response-probe'):
        module('audit').run(sys.modules[__name__], cfg, cache, reader, response_probe=args.mode == 'response-probe')
        return
    if args.mode == 'legacy-probe':
        if not pointer(reader, PREFIX + 'legacy-pages/latest.json'):
            emit('legacy-page-export-not-ready')
            raise SystemExit(2)
        restore(cfg, cache, reader)
        proof = module('legacy_pages').restore(sys.modules[__name__], cfg, reader, force=True)
        proof.update(at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), run_id=os.environ.get('GITHUB_RUN_ID'))
        cache.put(PREFIX + 'legacy-pages/verification.json', json.dumps(proof).encode())
        return
    if args.mode == 'deliver':
        end = time.monotonic() + args.minutes * 60
        while time.monotonic() < end:
            deliver(cfg, cache, reader, reverse=True, workers=32)
            queue = pointer(reader, PREFIX + 'drive-queue-status.json')
            if queue and time.time() - queue['at_unix'] < 120 and queue['remaining'] == 0:
                public = pointer(reader, PREFIX + 'latest.json') or {}
                member = pointer(reader, PREFIX + 'authenticated/latest.json') or {}
                admitted = queue.get('admission') or {}
                if admitted.get('public_object') == public.get('object') and admitted.get('member_object') == member.get('object'):
                    emit('drive-queue-drained-final-audit-required')
                    break
            time.sleep(5)
        return
    previous = restore(cfg, cache, reader)
    channel = 'member' if args.mode == 'authenticated' else 'public'
    rate_policy(cache, reader, channel, apply=True)
    if args.mode == 'authenticated':
        try:
            module('authenticated').run(sys.modules[__name__], cfg, cache, reader, args.minutes, previous)
        finally:
            rate_policy(cache, reader, channel)
        return
    # Explicit SOCKS for source only. TLS remains verified; never fall back direct.
    try:
        refresh_source_policy(cache)
    except Exception:
        policy = rate_policy(cache, reader, channel)
        if policy['not_before'] > time.time():
            emit('provider-backoff-persisted', not_before=policy['not_before'])
            return
        raise
    if args.mode == 'probe':
        deliver(cfg, cache, reader)
        return
    module('legacy_pages').restore(sys.modules[__name__], cfg, reader)
    try:
        module('sitemaps').refresh(sys.modules[__name__], cache, reader)
    except Exception as error:
        if type(error).__name__ not in ('SourceError', 'ParseError', 'ValueError'):
            raise
        emit('official-sitemap-refresh-incomplete', category=type(error).__name__,
             source_code=getattr(error, 'code', None), http_status=getattr(error, 'status', None),
             existing_frontier_retained=True)
    child = None
    linked_child = None
    delivery_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    delivery_task = None
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    failure = None
    try:
        child = subprocess.Popen([sys.executable, str(HERE / 'capture.py')], env=os.environ.copy())
        linked_child = subprocess.Popen([sys.executable, str(HERE / 'linked.py')], env=os.environ.copy())
        end = time.monotonic() + args.minutes * 60
        next_delivery = 0
        next_checkpoint = time.monotonic() + 300
        drained_since = None
        while time.monotonic() < end and not stopping:
            if child.poll() is not None:
                raise RuntimeError('source collector stopped unexpectedly')
            if linked_child.poll() is not None:
                raise RuntimeError('linked native collector stopped unexpectedly')
            if delivery_task and delivery_task.done():
                try:
                    delivery_task.result()
                except Exception as error:
                    emit('drive-batch-retry', category=type(error).__name__)
                delivery_task = None
            if args.mode == 'both' and delivery_task is None and time.monotonic() >= next_delivery:
                delivery_task = delivery_pool.submit(deliver, cfg, cache, reader)
                next_delivery = time.monotonic() + 5
            if time.monotonic() >= next_checkpoint:
                previous = checkpoint(cache, reader, previous)
                next_checkpoint = time.monotonic() + 300
            if source_frontier_drained():
                drained_since = drained_since or time.monotonic()
                if time.monotonic() - drained_since >= 60:
                    emit('source-frontier-drained-final-audit-required')
                    break
            else:
                drained_since = None
            time.sleep(2)
    except Exception as error:
        failure = error
    finally:
        for process in (child, linked_child):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        checkpoint(cache, reader, previous)
        # Persist source state before waiting for a slow Drive transfer to finish.
        delivery_pool.shutdown(wait=True)
    if failure:
        raise failure


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Requests/boto exception strings can embed credentials; report types only.
        emit('cloud-run-failed', category=type(error).__name__,
             source_code=getattr(error, 'code', None) if type(error).__name__ == 'SourceError' else None,
             http_status=getattr(error, 'status', None) if type(error).__name__ == 'SourceError' else None)
        raise SystemExit(1)
    finally:
        if DELIVERY is not None:
            DELIVERY.close()
