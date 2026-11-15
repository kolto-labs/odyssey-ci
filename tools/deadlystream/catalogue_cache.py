#!/usr/bin/env python3
"""Resumable private Deadly Stream catalogue operations. No repository mutations."""
import argparse
import base64
import contextlib
import fcntl
from email.message import Message
from email.utils import parsedate_to_datetime
import hashlib
import gzip
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import urllib.robotparser
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from urllib.parse import urljoin, urlsplit, parse_qs, urlencode, urlunsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from bs4 import BeautifulSoup
import requests

CHUNK = 16 * 1024 * 1024
UA = 'KoltoLabsCatalogueCache/1.0'
SECRET_QUERY = {'csrfkey', 'csrf', 'token', 'signature', 'x-amz-signature', 'x-amz-security-token'}


def stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def download_filename(header):
    message=Message();message['Content-Disposition']=header
    name=message.get_filename() or 'download'
    return Path(name.replace('\\','/')).name[:240] or 'download'


def emit(event, **fields):
    print(json.dumps({'at': stamp(), 'event': event, **fields}, sort_keys=True), flush=True)


def canonical(url):
    p = urlsplit(url)
    if p.hostname not in ('deadlystream.com', 'www.deadlystream.com'):
        raise ValueError('foreign catalogue address')
    path = p.path.removeprefix('/forum') if p.path.startswith('/forum/files/') else p.path
    return urlunsplit(('https', 'deadlystream.com', path, '', ''))


def public_identity(url):
    p = urlsplit(url)
    query = [(k, v) for k, values in parse_qs(p.query).items()
             if k.lower() not in SECRET_QUERY for v in values]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(sorted(query)), ''))


def same_file(left, right):
    a,b=urlsplit(left),urlsplit(right)
    x,y=re.search(r'/files/file/(\d+)',a.path),re.search(r'/files/file/(\d+)',b.path)
    return a.hostname in ('deadlystream.com','www.deadlystream.com') and x and y and x[1]==y[1]


class SourceError(Exception):
    def __init__(self,code,status=0,details=None):
        self.code,self.status,self.details=code,status,details or {}
        super().__init__(code)


class Catalogue:
    def __init__(self, config):
        self.cfg = config
        self.state = Path(config['state']); self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.state / 'catalogue.sqlite', timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,url TEXT NOT NULL,metadata TEXT,status TEXT NOT NULL DEFAULT 'pending',error TEXT,scanned_at TEXT);
          CREATE TABLE IF NOT EXISTS versions(file_id INTEGER,revision TEXT,label TEXT,history_id TEXT,version_token TEXT,source_url TEXT,status TEXT NOT NULL DEFAULT 'pending',error TEXT,attempts INTEGER DEFAULT 0,updated_at TEXT,PRIMARY KEY(file_id,revision));
          CREATE TABLE IF NOT EXISTS items(file_id INTEGER,revision TEXT,row_key TEXT,filename TEXT,sha256 TEXT,size INTEGER,manifest TEXT,cached_at TEXT,PRIMARY KEY(file_id,revision,row_key,sha256));
          CREATE TABLE IF NOT EXISTS events(at TEXT,kind TEXT,file_id INTEGER,revision TEXT,category TEXT);
          CREATE TABLE IF NOT EXISTS gaps(file_id INTEGER,revision TEXT,row_key TEXT,selector TEXT,category TEXT,http_status INTEGER,observed_at TEXT,PRIMARY KEY(file_id,revision,row_key));
        ''')
        if 'details' not in {r[1] for r in self.db.execute('PRAGMA table_info(gaps)')}:
            self.db.execute('ALTER TABLE gaps ADD COLUMN details TEXT')
        self.db.commit()
        (self.state / 'catalogue.sqlite').chmod(0o600)
        self.session = requests.Session(); self.session.trust_env = False
        self.session.headers.update({'User-Agent': UA, 'Accept-Encoding': 'identity'})
        self._s3 = None
        self.ns = hashlib.sha256(config['recipient'].encode()).hexdigest()[:20]

    def s3(self):
        if self._s3 is None:
            c = json.loads(Path(self.cfg['writer_file']).read_text())
            self._s3 = boto3.client('s3', endpoint_url=self.cfg['endpoint'], region_name='auto',
                aws_access_key_id=c['id'], aws_secret_access_key=c['secret_access_key'],
                config=Config(signature_version='s3v4', retries={'max_attempts': 5},
                    request_checksum_calculation='when_required', response_checksum_validation='when_required'))
        return self._s3

    def pace(self):
        rate_state=Path(self.cfg.get('source_rate_state',self.state))
        with (rate_state / 'source-rate.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            clock = rate_state / 'source-last-request'
            try: last = float(clock.read_text())
            except (ValueError, OSError): last = 0
            try:not_before=float((rate_state/'source-not-before').read_text())
            except (ValueError,OSError):not_before=0
            try:robots_interval=float((rate_state/'source-interval').read_text())
            except (ValueError,OSError):robots_interval=0
            delay = max(not_before, last + max(robots_interval,self.cfg.get('source_interval_seconds',1.0))) - time.time()
            if delay > 0: time.sleep(delay)
            clock.write_text(str(time.time()))

    def get(self, url, ajax=False):
        rate_state=Path(self.cfg.get('source_rate_state',self.state))
        for redirects in range(10):
            p = urlsplit(url)
            native_source=p.hostname in ('deadlystream.com','www.deadlystream.com')
            source_proxy=self.cfg.get('source_proxy') if native_source else None
            if native_source and self.cfg.get('source_require_proxy') and not source_proxy:raise SourceError('source-proxy-required')
            source_proxies={'http':source_proxy,'https':source_proxy} if source_proxy else {}
            if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
                raise SourceError('unsafe-source-address')
            policy=Path(self.cfg.get('source_policy_state',self.state))/'robots.txt'
            if p.hostname in ('deadlystream.com','www.deadlystream.com') and p.path!='/robots.txt' and policy.exists():
                rules=urllib.robotparser.RobotFileParser();rules.parse(policy.read_text().splitlines())
                if not rules.can_fetch(UA,url):raise SourceError('source-policy-disallows-address',403)
            try:
                addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
            except OSError: raise SourceError('source-dns-unavailable') from None
            if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
                raise SourceError('nonpublic-source-address')
            # All origin requests, including separate workers, share one pacing lock.
            for attempt in range(4):
                if p.hostname in ('deadlystream.com','www.deadlystream.com'):self.pace()
                try: response = self.session.get(url,stream=True,timeout=(20,90),allow_redirects=False,proxies=source_proxies,headers=({'X-Requested-With':'XMLHttpRequest'} if ajax and p.hostname in ('deadlystream.com','www.deadlystream.com') else {}))
                except requests.RequestException:
                    if attempt<3:time.sleep(5*2**attempt);continue
                    raise SourceError('source-proxy-unavailable' if source_proxy else 'source-connection-failed') from None
                if response.status_code not in (429, 502, 503, 504): break
                retry = response.headers.get('Retry-After', '')
                try:delay=int(retry) if retry.isdigit() else max(0,parsedate_to_datetime(retry).timestamp()-time.time())
                except (TypeError,ValueError,OverflowError):delay=15*2**attempt
                response.close()
                # Do not shorten a provider's requested backoff or bypass its controls.
                if p.hostname in ('deadlystream.com','www.deadlystream.com'):
                    with (rate_state/'source-rate.lock').open('a+') as lock:
                        fcntl.flock(lock,fcntl.LOCK_EX)
                        try:previous=float((rate_state/'source-not-before').read_text())
                        except (OSError,ValueError):previous=0
                        (rate_state/'source-not-before').write_text(str(max(previous,time.time()+max(1,delay))))
                        if self.cfg.get('source_interval_seconds',1.0)<0.25:
                            try:current_interval=float((rate_state/'source-interval').read_text())
                            except (OSError,ValueError):current_interval=0
                            if current_interval<0.25:(rate_state/'source-interval').write_text('0.25')
                if delay > 900: raise SourceError('source-backoff-required', 429)
                time.sleep(max(1, delay))
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get('Location'); response.close()
                if not location: raise SourceError('invalid-source-redirect')
                url = urljoin(url, location)
                if url.startswith('http://deadlystream.com/'): url = 'https://' + url[7:]
                continue
            if response.status_code >= 400:
                status = response.status_code; response.close()
                raise SourceError('source-not-found' if status in (404,410) else 'source-access-denied' if status in (401,403) else 'source-http-error', status)
            return response
        raise SourceError('source-redirect-limit')

    def html(self, response):
        data = bytearray()
        try:
            for chunk in response.iter_content(128 * 1024):
                data.extend(chunk)
                if len(data) > 16 * 1024 * 1024: raise SourceError('source-page-too-large')
        finally: response.close()
        soup=BeautifulSoup(bytes(data),'html.parser')
        parsed=urlsplit(response.url);query=parse_qs(parsed.query);match=re.search(r'/files/file/(\d+)',parsed.path)
        if match and parsed.hostname in ('deadlystream.com','www.deadlystream.com') and set(query)<= {'changelog'}:
            history=query.get('changelog',['0'])[0]
            if history.isdigit():
                label=str(self.describe(soup,canonical(response.url)).get('softwareVersion') or '') if history=='0' else ''
                directory=self.state/'page-observations';directory.mkdir(mode=0o700,exist_ok=True)
                name=match[1]+'-'+history+'-'+hashlib.sha256(label.encode()).hexdigest()[:12]+'.json';index=directory/name
                if not index.exists() and not (directory/'delivered'/name).exists():
                    payload=gzip.compress(bytes(data),mtime=0);sha=hashlib.sha256(payload).hexdigest();objects=directory/'bytes';objects.mkdir(mode=0o700,exist_ok=True);target=objects/(sha+'.html.gz')
                    if not target.exists():
                        tmp=target.with_suffix('.'+uuid.uuid4().hex+'.new');tmp.write_bytes(payload);tmp.chmod(0o600);os.replace(tmp,target)
                    record={'file_id':int(match[1]),'history_id':history,'source_url':public_identity(response.url),'sha256':sha,'local_path':str(target),'captured_at':stamp(),'version_label':label}
                    tmp=index.with_suffix('.'+uuid.uuid4().hex+'.new');tmp.write_text(json.dumps(record));tmp.chmod(0o600);os.replace(tmp,index)
        return soup

    def seed(self):
        urls = json.loads(Path(self.cfg['catalogue_urls']).read_text())
        for url in urls:
            url = canonical(url); m = re.search(r'/files/file/(\d+)', url)
            if m: self.db.execute('INSERT OR IGNORE INTO files(id,url) VALUES(?,?)',(int(m[1]),url))
        self.db.commit()

    def refresh(self):
        def read(url):
            response=self.get(url);data=bytearray()
            try:
                for part in response.iter_content(65536):
                    data.extend(part)
                    if len(data)>16*1024*1024:raise SourceError('sitemap-too-large')
            finally:response.close()
            return bytes(data)
        robots=read('https://deadlystream.com/robots.txt').decode(errors='replace').replace('\\r\\n','\n')
        rules=urllib.robotparser.RobotFileParser();rules.parse(robots.splitlines())
        if not rules.can_fetch(UA,'https://deadlystream.com/files/'):
            raise SourceError('catalogue-crawl-disallowed')
        (self.state/'robots.txt').write_text(robots)
        rate=rules.request_rate(UA)
        (self.state/'source-interval').write_text(str(max(rules.crawl_delay(UA) or 0,rate.seconds/rate.requests if rate else 0)))
        root=ET.fromstring(read('https://deadlystream.com/sitemap.php'))
        maps=[]
        for node in root.findall('.//{*}loc'):
            p=urlsplit(node.text or '')
            if p.hostname=='deadlystream.com' and 'sitemap_content_downloads_File_' in p.query:
                maps.append(urlunsplit(('https','deadlystream.com','/sitemap.php',p.query,'')))
        if not maps:raise SourceError('catalogue-sitemap-not-found')
        urls=set()
        for url in maps:
            tree=ET.fromstring(read(url))
            for node in tree.findall('.//{*}loc'):
                if '/files/file/' in (node.text or ''):urls.add(canonical(node.text))
        old=json.loads(Path(self.cfg['catalogue_urls']).read_text())
        if len(urls)<len(old)*0.9:raise SourceError('catalogue-sitemap-incomplete')
        p=Path(self.cfg['catalogue_urls']);temp=p.with_suffix('.new')
        temp.write_text(json.dumps(sorted(urls),indent=2)+'\n');os.replace(temp,p)
        self.seed()
        self.db.execute("UPDATE files SET status='pending',error=NULL WHERE status!='pending'")
        self.db.execute("UPDATE versions SET status='pending',error=NULL WHERE status IN ('failed','blocked','missing')")
        self.db.commit()
        receipt={'at':stamp(),'sitemaps':len(maps),'records':len(urls),'robots_sha256':hashlib.sha256(robots.encode()).hexdigest()}
        (self.state/'discovery-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
        emit('sitemap-refreshed',records=len(urls),sitemaps=len(maps))

    def describe(self,soup,url):
        apps = []
        for script in soup.select('script[type="application/ld+json"]'):
            try: value = json.loads(script.string or script.get_text())
            except (ValueError, TypeError): continue
            values = value if isinstance(value, list) else value.get('@graph', [value]) if isinstance(value, dict) else []
            apps.extend(x for x in values if isinstance(x, dict) and x.get('@type') in ('SoftwareApplication','WebApplication','CreativeWork'))
        app = apps[0] if apps else {}
        title = app.get('name') or (soup.find('h1').get_text(' ',strip=True) if soup.find('h1') else '')
        metadata = {k: app.get(k) for k in ('softwareVersion','dateCreated','dateModified','fileSize','applicationCategory')}
        if not metadata['softwareVersion']:
            for anchor in soup.select('a[href]'):
                if same_file(urljoin(url,anchor['href']),url) and parse_qs(urlsplit(anchor['href']).query).get('changelog')==['0']:
                    metadata['softwareVersion']=anchor.get_text(' ',strip=True);break
        author = app.get('author', {}); metadata['author'] = author.get('name') if isinstance(author, dict) else None
        metadata['title'] = title
        return metadata

    def scan(self,row):
        soup=self.html(self.get(row['url']))
        metadata=self.describe(soup,row['url'])
        current = hashlib.sha256(json.dumps(metadata,sort_keys=True).encode()).hexdigest()[:24]
        versions = [('current-'+current, str(metadata['softwareVersion'] or 'current'), '0', '')]
        for anchor in soup.select('a[href]'):
            href = urljoin(row['url'], anchor['href']); parsed = urlsplit(href)
            if not same_file(href,row['url']): continue
            history = parse_qs(parsed.query).get('changelog', [''])[0]
            if not history.isdigit() or history == '0': continue
            li = anchor.find_parent('li')
            token = li.get('data-ipsmenuvalue', '') if li else ''
            label = (li.get('data-changelogtitle') if li else None) or anchor.get('title') or anchor.get_text(' ',strip=True)
            if token and not re.fullmatch('[A-Za-z0-9_.-]{1,256}',token): token = ''
            versions.append(('history-'+history,str(label),history,token))
        for rev,label,history,token in set(versions):
            self.db.execute('INSERT OR IGNORE INTO versions(file_id,revision,label,history_id,version_token,source_url,updated_at) VALUES(?,?,?,?,?,?,?)',
                (row['id'],rev,label,history,token,row['url'],stamp()))
        self.db.execute("UPDATE files SET metadata=?,status='scanned',error=NULL,scanned_at=? WHERE id=?",(json.dumps(metadata,sort_keys=True),stamp(),row['id']))
        self.db.commit()

    def encrypt(self, plain):
        env = {'PATH':os.environ.get('PATH','/usr/bin:/bin'), 'LANG':'C'}
        result = subprocess.run([self.cfg['age'],'--encrypt','--recipient',self.cfg['recipient']],
            input=plain,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        if result.returncode or not result.stdout.startswith(b'age-encryption.org/v1\n'):
            raise RuntimeError('encryption-failed')
        return result.stdout

    def put(self, key, data, metadata=None, immutable=False):
        digest = hashlib.sha256(data).hexdigest(); md5 = hashlib.md5(data).digest()
        metadata = {**(metadata or {}),'cipher-sha256':digest}
        reply = self.s3().put_object(Bucket=self.cfg['bucket'],Key=key,Body=data,
            ContentType='application/octet-stream',ContentMD5=base64.b64encode(md5).decode(),Metadata=metadata,
            **({'IfNoneMatch':'*'} if immutable else {}))
        if reply.get('ETag','').strip('"') != md5.hex(): raise RuntimeError('remote-upload-integrity-failed')
        head = self.s3().head_object(Bucket=self.cfg['bucket'],Key=key)
        if head['ContentLength'] != len(data) or head.get('Metadata',{}).get('cipher-sha256') != digest:
            raise RuntimeError('remote-receipt-integrity-failed')
        return digest

    def chunk(self, data):
        sha = hashlib.sha256(data).hexdigest(); key = 'objects/'+self.ns+'/'+sha+'.age'
        try:
            head = self.s3().head_object(Bucket=self.cfg['bucket'],Key=key)
            meta = head.get('Metadata',{})
            if meta.get('plain-sha256') != sha or meta.get('plain-size') != str(len(data)) or not re.fullmatch('[0-9a-f]{64}',meta.get('cipher-sha256','')):
                raise RuntimeError('existing-object-metadata-invalid')
            cipher = meta['cipher-sha256']
        except ClientError as exc:
            if exc.response['ResponseMetadata']['HTTPStatusCode'] != 404: raise
            try:cipher = self.put(key,self.encrypt(data),{'plain-sha256':sha,'plain-size':str(len(data))},immutable=True)
            except ClientError as race:
                if race.response['ResponseMetadata']['HTTPStatusCode'] != 412:raise
                head=self.s3().head_object(Bucket=self.cfg['bucket'],Key=key);meta=head.get('Metadata',{})
                if meta.get('plain-sha256')!=sha or meta.get('plain-size')!=str(len(data)) or not re.fullmatch('[0-9a-f]{64}',meta.get('cipher-sha256','')):
                    raise RuntimeError('concurrent-object-metadata-invalid')
                cipher=meta['cipher-sha256']
        return {'key':key,'sha256':sha,'size':len(data),'cipher_sha256':cipher}

    def store_file(self, version, response, row_key, selector, filename_hint=None):
        header = response.headers.get('Content-Disposition','')
        filename=download_filename(header) if header else (Path(filename_hint.replace('\\','/')).name[:240] if filename_hint else 'download')
        expected = response.headers.get('Content-Length')
        expected = int(expected) if expected and expected.isdigit() and not response.headers.get('Content-Encoding') else None
        chunks=[]; whole=hashlib.sha256(); total=0; pending=bytearray()
        self.s3()
        uploads=ThreadPoolExecutor(max_workers=2);inflight=deque()
        try:
            for part in response.iter_content(1024*1024):
                if not part: continue
                whole.update(part); total+=len(part); pending.extend(part)
                while len(pending)>=CHUNK:
                    chunk=bytes(pending[:CHUNK]);del pending[:CHUNK]
                    inflight.append(uploads.submit(self.chunk,chunk))
                    if len(inflight)>=2:chunks.append(inflight.popleft().result())
            if pending:inflight.append(uploads.submit(self.chunk,bytes(pending)))
            while inflight:chunks.append(inflight.popleft().result())
        finally:
            response.close();uploads.shutdown(wait=True,cancel_futures=True)
        if total==0:raise SourceError('source-empty-download',response.status_code,{'filename':filename,'advertised_response_length':expected,'delivered_bytes':0,'content_type':response.headers.get('Content-Type')})
        if expected is not None and total!=expected:raise SourceError('incomplete-source-file',response.status_code)
        manifest={'schema':'kolto-deadlystream-file/v1','source_url':version['source_url'],
            'file_id':version['file_id'],'revision':version['revision'],'version_label':version['label'],
            'history_id':version['history_id'],'row_key':row_key,'filename':filename,
            'sha256':whole.hexdigest(),'size':total,'chunks':chunks,'observed_at':stamp(),
            'recipient':self.cfg['recipient'],'download_selector':selector,
            'source_headers':{name:response.headers[name] for name in ('ETag','Last-Modified','Content-Type','Content-Length','Content-Disposition') if name in response.headers}}
        safe_revision=hashlib.sha256(version['revision'].encode()).hexdigest()[:24]
        payload=self.encrypt(json.dumps(manifest,sort_keys=True).encode())
        key='manifests/'+str(version['file_id'])+'/'+safe_revision+'/'+whole.hexdigest()+'-'+hashlib.sha256(payload).hexdigest()+'.json.age'
        self.put(key,payload,immutable=True)
        self.db.execute('INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?,?,?)',
            (version['file_id'],version['revision'],row_key,filename,whole.hexdigest(),total,key,stamp()))
        self.db.commit()
        emit('file-cached',file_id=version['file_id'],revision=version['revision'],bytes=total,chunks=len(chunks))

    def acquire(self, version, controls=None, control_token='', hints=None, complete=True):
        if controls is None:
            page=version['source_url']+('' if version['history_id']=='0' else '?changelog='+version['history_id'])
            soup=self.html(self.get(page));links=[]
            if version['history_id']=='0':
                actual=self.describe(soup,version['source_url'])
                if str(actual.get('softwareVersion') or 'current')!=version['label']:
                    self.db.execute("UPDATE files SET status='pending' WHERE id=?",(version['file_id'],));self.db.commit()
                    raise SourceError('current-release-changed-before-download')
            history_tokens={parse_qs(urlsplit(urljoin(page,a['href'])).query).get('version',[''])[0]
                for a in soup.select('a[href]') if same_file(urljoin(page,a['href']),version['source_url'])
                and parse_qs(urlsplit(urljoin(page,a['href'])).query).get('do')==['download']}
            history_tokens.discard('')
            expected_token=next(iter(history_tokens)) if len(history_tokens)==1 else version['version_token'] if version['version_token'] in history_tokens else ''
            for anchor in soup.select('a[href]'):
                u=urljoin(page,anchor['href']);p=urlsplit(u);q=parse_qs(p.query)
                if not same_file(u,version['source_url']) or q.get('do')!=['download']:continue
                if version['history_id']!='0':
                    if not expected_token or q.get('version')!=[expected_token]:continue
                elif q.get('version'):continue
                links.append((u,anchor.has_attr('data-ipsdialog')))
            if not links: raise SourceError('historical-download-unavailable' if version['history_id']!='0' else 'download-unavailable')
            resolved_controls={}
            for u,dialog in links:
                key=public_identity(u)
                if key not in resolved_controls or dialog:resolved_controls[key]=(u,dialog)
            queue=list(resolved_controls.values())
        else:
            queue=list(controls);expected_token=control_token
        visited=set();seen_files=0;failures=[];hints=dict(hints or {})
        while queue:
            u,dialog=queue.pop(0);identity=public_identity(u);visit=(identity,dialog)
            if visit in visited:continue
            visited.add(visit)
            if len(visited)>1024:raise SourceError('download-selection-too-large')
            selector={k:v for k,v in parse_qs(urlsplit(u).query).items() if k in ('do','version','r','id','confirm')}
            row_key=hashlib.sha256(identity.encode()).hexdigest()
            if not dialog and not self.cfg.get('force_verify_payload',False) and self.db.execute('SELECT 1 FROM items WHERE file_id=? AND revision=? AND row_key=?',
                (version['file_id'],version['revision'],row_key)).fetchone():
                seen_files+=1
                self.db.execute('DELETE FROM gaps WHERE file_id=? AND revision=? AND row_key=?',(version['file_id'],version['revision'],row_key));self.db.commit()
                continue
            try:
                response=self.get(u,ajax=dialog);kind=response.headers.get('Content-Type','').lower()
                disposition=response.headers.get('Content-Disposition','').lower()
                if 'json' in kind and 'attachment' not in disposition:
                    response.close();raise SourceError('unsupported-json-download-control')
                if 'text/html' not in kind or ('attachment' in disposition and '.html' in disposition):
                    self.store_file(version,response,row_key,selector);seen_files+=1
                    self.db.execute('DELETE FROM gaps WHERE file_id=? AND revision=? AND row_key=?',(version['file_id'],version['revision'],row_key));self.db.commit()
                    continue
                landing=self.html(response);next_links=[]
                for anchor in landing.select('a[href]'):
                    target=urljoin(u,anchor['href']);p=urlsplit(target);q=parse_qs(p.query)
                    if not same_file(target,version['source_url']) or q.get('do')!=['download']:continue
                    if version['history_id']!='0':
                        if q.get('version') and q['version']!=[expected_token]:continue
                        if not q.get('version') and not q.get('r'):continue
                    next_dialog=anchor.has_attr('data-ipsdialog')
                    if (public_identity(target),next_dialog) not in visited:
                        next_links.append((target,next_dialog))
                        container=anchor.find_parent(class_='ipsDataItem') or anchor.find_parent('li')
                        if container:hints[public_identity(target)]=container.get_text(' ',strip=True)[:2000]
                row_links=[control for control in next_links if parse_qs(urlsplit(control[0]).query).get('r')]
                if row_links:next_links=row_links
                if not next_links:
                    if dialog and (identity,False) not in visited:
                        queue.append((u,False));continue
                    text=landing.get_text(' ',strip=True).lower()
                    raise SourceError('source-version-has-no-files' if re.search(r'\b0 files\b',text) else 'source-login-challenge-or-unavailable',200)
                # A resolved chooser is an interface control, not a missing file.
                self.db.execute('DELETE FROM gaps WHERE file_id=? AND revision=? AND row_key=?',(version['file_id'],version['revision'],row_key));self.db.commit()
                parallel=min(4,max(1,int(self.cfg.get('parallel_variants',4))))
                if len(row_links)>1 and parallel>1:
                    def leaf(control):
                        child=Catalogue({**self.cfg,'parallel_variants':1})
                        child.session.cookies.update(self.session.cookies)
                        try:return child.acquire(version,[control],expected_token,hints,False)
                        finally:child.db.close();child.session.close()
                    with ThreadPoolExecutor(max_workers=parallel) as pool:
                        futures=[pool.submit(leaf,control) for control in next_links]
                        for future in futures:
                            try:seen_files+=future.result()
                            except SourceError as error:failures.append(error.code)
                else:queue.extend(next_links)
            except (SourceError,requests.RequestException) as exc:
                category=exc.code if isinstance(exc,SourceError) else 'source-transfer-incomplete'
                status=exc.status if isinstance(exc,SourceError) else 0
                details=dict(exc.details) if isinstance(exc,SourceError) else {}
                if identity in hints:details['published_file_listing']=hints[identity]
                self.db.execute('INSERT OR REPLACE INTO gaps(file_id,revision,row_key,selector,category,http_status,observed_at,details) VALUES(?,?,?,?,?,?,?,?)',
                    (version['file_id'],version['revision'],row_key,json.dumps(selector,sort_keys=True),category,status,stamp(),json.dumps(details,sort_keys=True)))
                self.db.commit();failures.append(category)
                emit('variant-gap',file_id=version['file_id'],revision=version['revision'],category=category)
                # Other independently available file variants must still be attempted.
        if failures:raise SourceError('some-download-variants-unavailable')
        if not complete:
            if not seen_files:raise SourceError('empty-download-selection')
            return seen_files
        if self.db.execute('SELECT 1 FROM gaps WHERE file_id=? AND revision=?',(version['file_id'],version['revision'])).fetchone():
            raise SourceError('previously-listed-download-unavailable')
        if not seen_files:raise SourceError('empty-download-selection')
        self.db.execute("UPDATE versions SET status='cached',error=NULL,updated_at=? WHERE file_id=? AND revision=?",(stamp(),version['file_id'],version['revision']))
        self.db.commit()

    def failure(self, table, row, error):
        category=error.code if isinstance(error,SourceError) else type(error).__name__
        state='missing' if isinstance(error,SourceError) and error.status in (404,410) else 'blocked' if isinstance(error,SourceError) and ('denied' in category or 'unavailable' in category or 'challenge' in category) else 'failed'
        if table=='files':
            self.db.execute('UPDATE files SET status=?,error=?,scanned_at=? WHERE id=?',(state,category,stamp(),row['id']))
        else:
            self.db.execute('UPDATE versions SET status=?,error=?,attempts=attempts+1,updated_at=? WHERE file_id=? AND revision=?',(state,category,stamp(),row['file_id'],row['revision']))
        self.db.commit();emit('source-gap',kind=table,file_id=row['id'] if table=='files' else row['file_id'],category=category)

    def status(self):
        value={'at':stamp(),'files':dict(self.db.execute('SELECT status,count(*) FROM files GROUP BY status').fetchall()),
            'versions':dict(self.db.execute('SELECT status,count(*) FROM versions GROUP BY status').fetchall()),
            'cached_items':self.db.execute('SELECT count(*) FROM items').fetchone()[0],
            'cached_plaintext_bytes':self.db.execute('SELECT coalesce(sum(size),0) FROM items').fetchone()[0]}
        value['unique_archives']=self.db.execute('SELECT count(DISTINCT sha256) FROM items').fetchone()[0]
        value['unique_archive_bytes']=self.db.execute('SELECT coalesce(sum(size),0) FROM (SELECT max(size) AS size FROM items GROUP BY sha256)').fetchone()[0]
        value['variant_gaps']=self.db.execute('SELECT count(*) FROM gaps').fetchone()[0]
        value['collection_pass_finished']=not value['files'].get('pending',0) and not value['versions'].get('pending',0) and not value['versions'].get('capturing-history',0)
        invalid_aliases=[];verified_aliases=0
        for alias in self.db.execute("SELECT * FROM versions WHERE status='superseded'").fetchall():
            match=re.fullmatch(r'Current URL advanced; original release preserved as (history-\d+)',alias['error'] or '')
            target=self.db.execute('SELECT * FROM versions WHERE file_id=? AND revision=?',(alias['file_id'],match[1])).fetchone() if match else None
            good=target and target['history_id']!='0' and target['label']==alias['label'] and target['status']=='cached'
            if good:
                original={r[0] for r in self.db.execute('SELECT sha256 FROM items WHERE file_id=? AND revision=?',(alias['file_id'],alias['revision']))}
                recovered={r[0] for r in self.db.execute('SELECT sha256 FROM items WHERE file_id=? AND revision=?',(alias['file_id'],target['revision']))}
                good=bool(recovered) and original.issubset(recovered)
            if good:verified_aliases+=1
            else:invalid_aliases.append({'file_id':alias['file_id'],'revision':alias['revision']})
        value['verified_superseded_aliases']=verified_aliases;value['unverified_superseded_aliases']=invalid_aliases
        value['complete']=value['variant_gaps']==0 and not invalid_aliases and bool(value['files']) and set(value['files'])=={'scanned'} and bool(value['versions']) and set(value['versions'])<={'cached','superseded'}
        p=self.state/'status.json';temp=self.state/('status.json.'+str(os.getpid())+'.new');temp.write_text(json.dumps(value,indent=2)+'\n');os.replace(temp,p)
        return value

    def run(self, operation, limit=0, shard_index=0, shard_count=1):
        lock_name=operation+('-'+str(shard_index) if operation=='download' and shard_count>1 else '')
        with (self.state/(lock_name+'.lock')).open('a+') as owner:
            try:fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:emit('already-running',operation=operation);return
            self.seed();count=0
            while not limit or count<limit:
                if operation=='discover':
                    row=self.db.execute("SELECT * FROM files WHERE status='pending' ORDER BY id").fetchone()
                else:
                    row=self.db.execute("""SELECT v.* FROM versions v JOIN files f ON f.id=v.file_id
                        WHERE v.status='pending' AND v.file_id % ? = ?
                        ORDER BY v.history_id='0' DESC,
                        (SELECT count(*) FROM versions done WHERE done.file_id=v.file_id AND done.status='cached'),
                        CAST(replace(json_extract(f.metadata,'$.fileSize'),',','') AS REAL) *
                          CASE upper(substr(trim(json_extract(f.metadata,'$.fileSize')),-2))
                            WHEN 'TB' THEN 1000000000000 WHEN 'GB' THEN 1000000000
                            WHEN 'MB' THEN 1000000 WHEN 'KB' THEN 1000 ELSE 1 END,
                        CAST(v.history_id AS INTEGER) DESC,v.file_id""",(shard_count,shard_index)).fetchone()
                if row is None:
                    if operation=='download' and self.db.execute("SELECT count(*) FROM files WHERE status='pending'").fetchone()[0]:time.sleep(5);continue
                    break
                try:self.scan(row) if operation=='discover' else self.acquire(row)
                except ClientError:
                    emit('storage-unavailable',operation=operation)
                    raise SystemExit(2)
                except Exception as error:self.failure('files' if operation=='discover' else 'versions',row,error)
                count+=1
                if count%10==0:emit('progress',**self.status())
            emit('worker-finished',operation=operation,**self.status())

    def snapshot(self):
        self.db.execute('BEGIN')
        try:
            data={'schema':'kolto-deadlystream-catalogue/v1','status':self.status(),
                'files':[dict(x) for x in self.db.execute('SELECT * FROM files')],
                'versions':[dict(x) for x in self.db.execute('SELECT * FROM versions')],
                'items':[dict(x) for x in self.db.execute('SELECT * FROM items')],
                'gaps':[dict(x) for x in self.db.execute('SELECT * FROM gaps')]}
        finally:self.db.rollback()
        files={row['id']:row for row in data['files']}
        versions={(row['file_id'],row['revision']):row for row in data['versions']}
        gaps=[]
        for row in data['gaps']:
            version=versions[(row['file_id'],row['revision'])]
            source=files[row['file_id']]
            gaps.append({**row,'title':json.loads(source['metadata'] or '{}').get('title'),
                'version_label':version['label'],'source_url':source['url']+('' if version['history_id']=='0' else '?changelog='+version['history_id']),
                'selector':json.loads(row['selector'] or '{}'),'details':json.loads(row['details'] or '{}')})
        report={'at':data['status']['at'],'status':data['status'],'variant_gaps':gaps,
            'file_errors':[{'id':row['id'],'source_url':row['url'],'status':row['status'],'error':row['error']} for row in data['files'] if row['error']],
            'version_errors':[{'file_id':row['file_id'],'revision':row['revision'],'label':row['label'],'source_url':row['source_url'],
                'history_id':row['history_id'],'status':row['status'],'error':row['error']} for row in data['versions'] if row['error']]}
        report_path=self.state/'source-gaps.json';temporary=report_path.with_suffix('.new')
        temporary.write_text(json.dumps(report,indent=2)+'\n');os.replace(temporary,report_path)
        payload=self.encrypt(json.dumps(data,sort_keys=True).encode())
        key='catalogue/snapshots/'+stamp().replace(':','')+'-'+uuid.uuid4().hex+'.json.age'
        digest=self.put(key,payload,immutable=True)
        self.s3().put_object(Bucket=self.cfg['bucket'],Key='catalogue/latest.json',Body=json.dumps({'snapshot':key,'cipher_sha256':digest,'status':data['status']}).encode(),ContentType='application/json')
        emit('catalogue-checkpoint-uploaded',snapshot=key)

    def maintain(self, config_path):
        with (self.state/'maintain.lock').open('a+') as owner:
            try:fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:emit('already-running',operation='maintain');return
            receipt=self.state/'discovery-receipt.json'
            if not receipt.exists() or time.time()-receipt.stat().st_mtime>20*3600:
                self.refresh()
            self.seed()
            command=[sys.executable,str(Path(__file__).resolve()),'--config',str(Path(config_path).resolve())]
            workers=max(1,min(16,int(self.cfg.get('download_workers',1))))
            jobs=[('discover','discover',[])]+[('download-'+str(i),'download',['--shard-index',str(i),'--shard-count',str(workers)]) for i in range(workers)]
            children={};last_start={};last_snapshot=0
            try:
                while True:
                    pending={
                        'discover':self.db.execute("SELECT count(*) FROM files WHERE status='pending'").fetchone()[0],
                        'download':self.db.execute("SELECT count(*) FROM versions WHERE status='pending'").fetchone()[0],
                    }
                    for label,mode,extra in jobs:
                        process=children.get(label)
                        wanted=pending[mode]>0 or mode=='download' and pending['discover']>0
                        if mode=='download' and not pending['discover']:
                            wanted=bool(self.db.execute("SELECT 1 FROM versions WHERE status='pending' AND file_id % ? = ? LIMIT 1",(workers,int(extra[1]))).fetchone())
                        if process is not None and process.poll() is not None:
                            code=process.returncode;del children[label]
                            if code:emit('worker-restarting',operation=label,exit_code=code)
                            process=None
                        if wanted and process is None and time.time()-last_start.get(label,0)>=60:
                            children[label]=subprocess.Popen(command+[mode]+extra);last_start[label]=time.time()
                    if time.time()-last_snapshot>=300:
                        try:self.snapshot();last_snapshot=time.time()
                        except Exception as exc:emit('checkpoint-delayed',category=type(exc).__name__);last_snapshot=time.time()-240
                    if not children and not any(pending.values()):break
                    time.sleep(5)
                self.snapshot()
                emit('collection-pass-finished',**self.status())
            finally:
                for process in children.values():
                    if process.poll() is None:process.terminate()
                for process in children.values():
                    try:process.wait(timeout=110)
                    except subprocess.TimeoutExpired:process.kill();process.wait()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True)
    p.add_argument('operation',choices=('refresh','discover','download','status','snapshot','maintain'));p.add_argument('--limit',type=int,default=0)
    p.add_argument('--shard-index',type=int,default=0);p.add_argument('--shard-count',type=int,default=1)
    args=p.parse_args();os.umask(0o077)
    if not 1<=args.shard_count<=16 or not 0<=args.shard_index<args.shard_count:p.error('invalid worker shard')
    c=Catalogue(json.loads(Path(args.config).read_text()))
    if args.operation=='status':print(json.dumps(c.status(),indent=2))
    elif args.operation=='refresh':c.refresh()
    elif args.operation=='snapshot':c.snapshot()
    elif args.operation=='maintain':c.maintain(args.config)
    else:c.run(args.operation,args.limit,args.shard_index,args.shard_count)


if __name__=='__main__':main()
