"""Recover retained public HTML for complete, provenance-bearing link replay."""
import gzip
import hashlib
import io
import json
import re
import sqlite3
import tarfile


def restore(runtime, cfg, reader, force=False):
    pointer = runtime.pointer(reader, runtime.PREFIX + 'legacy-pages/latest.json')
    if not pointer:
        runtime.emit('legacy-page-recovery-not-provisioned')
        return
    index_path = runtime.BASE / 'whole-site/legacy-page-sources.json.gz'
    if index_path.exists() and not force:
        index = json.loads(gzip.decompress(index_path.read_bytes()))
        with sqlite3.connect(runtime.BASE / 'whole-site/whole-site.sqlite') as db:
            revision = cfg['page_import_version']
            already_parsed = {row[0] for row in db.execute('SELECT key FROM meta WHERE key LIKE ?', ('parsed-' + revision + ':%',))}
        if all('parsed-' + revision + ':' + page['path'].split('/')[-1] in already_parsed for page in index['pages']):
            runtime.emit('legacy-pages-already-replayed', pages=len(index['pages']))
            return
    recovered = 0
    for batch in pointer['batches']:
        plain = runtime.read_private(reader, batch, 160 * 1024**2)
        with tarfile.open(fileobj=io.BytesIO(plain), mode='r:') as archive:
            runtime.extract_checkpoint(archive)
        recovered += batch['pages']
        runtime.emit('legacy-page-batch-recovered', pages=recovered)
    index = json.loads(gzip.decompress(index_path.read_bytes()))
    if len(index['pages']) != pointer['pages'] or recovered != pointer['pages']:
        raise ValueError('legacy metadata recovery coverage mismatch')
    for page in index['pages']:
        if not re.fullmatch(r'(?:state/source-pages|whole-site/pages)/[0-9a-f]{64}\.html\.gz', page['path']):
            raise ValueError('unexpected legacy source-page path')
        path = runtime.BASE / page['path']
        if not path.is_file() or path.stat().st_size != page['size']:
            raise ValueError('legacy metadata page missing or truncated')
        if hashlib.sha256(path.read_bytes()).hexdigest() != page['gzip_sha256']:
            raise ValueError('legacy metadata page checksum mismatch')
    runtime.emit('legacy-pages-recovered-and-verified', pages=len(index['pages']))
    return {'pages': len(index['pages']), 'batches': len(pointer['batches']),
            'source_at': pointer['at'], 'individual_gzip_checksums_verified': True}
