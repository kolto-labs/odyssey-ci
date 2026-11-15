#!/usr/bin/env python3
"""Public linked-resource preservation; independent from native release catalogue."""
import concurrent.futures,fcntl,gzip,hashlib,importlib.util,json,os,re,sqlite3,threading,time,mimetypes
from pathlib import Path
from urllib.parse import urljoin,urlsplit,urlunsplit,parse_qsl,urlencode,unquote
from html.parser import HTMLParser
BASE=Path(os.environ['DEADLYSTREAM_WORK_ROOT']); ROOT=BASE/'whole-site';ROOT.mkdir(mode=0o700,exist_ok=True)
spec=importlib.util.spec_from_file_location('catalogue',BASE/'catalogue_cache.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
cfg=json.loads((BASE/'config.json').read_text());cfg.update(state=str(ROOT/'asset-state'),source_rate_state=str(BASE/'state'),source_policy_state=str(BASE/'state'),source_interval_seconds=1 / 3)
LOCAL=threading.local()
def db():
 c=sqlite3.connect(ROOT/'whole-site.sqlite',timeout=90);c.row_factory=sqlite3.Row;c.execute('PRAGMA journal_mode=WAL');return c
c=db();c.executescript('''
CREATE TABLE IF NOT EXISTS pages(url TEXT PRIMARY KEY,kind TEXT,state TEXT DEFAULT 'pending',sha TEXT,path TEXT,status INTEGER,error TEXT,at TEXT);
CREATE TABLE IF NOT EXISTS assets(id INTEGER PRIMARY KEY,identity TEXT UNIQUE,url TEXT,kind TEXT,state TEXT DEFAULT 'pending',filename TEXT,sha256 TEXT,size INTEGER,manifest TEXT,error TEXT,status INTEGER,at TEXT);
CREATE TABLE IF NOT EXISTS refs(identity TEXT,page_url TEXT,label TEXT,PRIMARY KEY(identity,page_url));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS attachment_links(identity TEXT,url TEXT,page_url TEXT,label TEXT,tried_at TEXT,status INTEGER,error TEXT,PRIMARY KEY(identity,url,page_url));
''')
if 'source_url' not in {r[1] for r in c.execute('PRAGMA table_info(refs)')}:c.execute('ALTER TABLE refs ADD COLUMN source_url TEXT')
for table in ('pages','assets'):
 columns={r[1] for r in c.execute('PRAGMA table_info('+table+')')}
 if 'attempts' not in columns:c.execute('ALTER TABLE '+table+' ADD COLUMN attempts INTEGER DEFAULT 0')
 if 'next_attempt' not in columns:c.execute('ALTER TABLE '+table+' ADD COLUMN next_attempt REAL DEFAULT 0')
c.commit()
def source():
 if not hasattr(LOCAL,'source'):LOCAL.source=m.Catalogue(cfg)
 return LOCAL.source

def normalize(value,page):
 value=value.strip().replace('&amp;','&').replace('\\/','/')
 u=urlsplit(urljoin(page,value))
 if u.hostname not in ('deadlystream.com','www.deadlystream.com'):return
 if u.path.endswith('/index.php') and u.query.startswith('/'):
  route,sep,query=u.query.partition('&');u=urlsplit(urlunsplit(('https','deadlystream.com',route,query,'')))
 path=u.path
 if re.match(r'^/forum/(?:topic|gallery|blogs|files|page)/',path):path=path[6:]
 return urlunsplit(('https','deadlystream.com',path,u.query,''))

OFFICIAL_PATHS={urlsplit(normalize(u,'https://deadlystream.com/')).path for u in json.loads((BASE/'output/whole-site-inventory.json').read_text())['urls'] if normalize(u,'https://deadlystream.com/')}

def classify(u):
 p=urlsplit(u);q=dict(parse_qsl(p.query));path=p.path
 if q.get('do') and q['do']!='download':return
 if path.endswith('/index.php') and q.get('app')=='core' and q.get('module')=='attach' and q.get('section')=='attach' and q.get('attach_id','').isdigit():return 'asset','legacy-attachment'
 if path.endswith('/index.php') and q.get('app')=='downloads' and q.get('module')=='display' and q.get('section')=='screenshot':return 'page',u
 if path.startswith('/uploads/'):return 'asset','uploaded-resource'
 if re.search(r'\.(?:zip|rar|7z|jar|dmg|apk|tgz|tar|exe|msi|pdf|txt|docx?|xlsx?|lua|nss|ncs|2da|tga|dds|mp[34]|wav|ogg|flac|bik|png|jpe?g|gif|webp|psd|xcf|blend|mdl|mdx|tpc|tlk|dlg|uti|utc|utp|utw|utm|uts|utt|ute|utd|are|git|gff|ifo|mod|rim|erf|lyt|vis|wok|pwk|dwk|bif|key|fbx|obj|stl|ttf|otf|gz|bz2|xz|(?:zip|7z)\.\d{3})$',path,re.I):return 'asset','linked-file'
 if path.endswith('/attachment.php') and q.get('id','').isdigit():return 'asset','attachment'
 if q.get('do')=='download' and path.startswith('/gallery/'):return 'asset','gallery-original'
 if q.get('do')=='download' and '/files/file/' not in path:return 'asset','linked-download'
 if q.get('do') or any(k.lower() in ('csrfkey','token','auth') for k in q):return
 if path in OFFICIAL_PATHS or re.match(r'^/(?:topic/\d+|gallery/(?:image|album|category)/\d+|blogs/(?:entry|blog)/\d+|files/(?:file|category)/\d+|forum/\d+|page/)',path) or path in ('/files/','/gallery/','/blogs/','/forums/'):
  if any(k not in ('page','sortby','sortdirection','tab','changelog','_fromLogin') for k in q):return
  # Sorting duplicates do not add content, pagination does.
  q={k:v for k,v in q.items() if k in ('page','changelog') or (k=='tab' and v in ('comments','reviews'))}
  return 'page',urlunsplit(('https','deadlystream.com',path,urlencode(q),''))

def queue(u,page,label=''):
 u=normalize(u,page)
 if not u:return
 cl=classify(u)
 if not cl:
  if '/files/file/' in urlsplit(u).path and dict(parse_qsl(urlsplit(u).query)).get('do')=='download':queue(urlunsplit(('https','deadlystream.com',urlsplit(u).path,'','')),page,'native-download-parent')
  return
 typ,kind=cl;cx=LOCAL.db if hasattr(LOCAL,'db') else c
 if typ=='page':cx.execute('INSERT OR IGNORE INTO pages(url,kind) VALUES(?,?)',(kind,'content'));return
 p=urlsplit(u);q=dict(parse_qsl(p.query));identity='attachment:'+q['id'] if kind=='attachment' else u
 cx.execute('INSERT OR IGNORE INTO assets(identity,url,kind) VALUES(?,?,?)',(identity,u,kind))
 cx.execute('INSERT OR IGNORE INTO refs(identity,page_url,label,source_url) VALUES(?,?,?,?)',(identity,page,label[:300],u))
 cx.execute('UPDATE refs SET source_url=? WHERE identity=? AND page_url=? AND source_url IS NULL',(u,identity,page))
 if kind=='attachment':
  cx.execute('INSERT OR IGNORE INTO attachment_links(identity,url,page_url,label) VALUES(?,?,?,?)',(identity,u,page,label[:300]))
  if q.get('key') and not cx.execute('SELECT 1 FROM attachment_links WHERE identity=? AND url=? AND tried_at IS NOT NULL',(identity,u)).fetchone():
   cx.execute("UPDATE assets SET url=?,state=CASE WHEN state='unavailable' AND status IN (403,404) THEN 'pending' ELSE state END WHERE identity=? AND url<>? AND state<>'captured'",(u,identity,u))

class Links(HTMLParser):
 def __init__(self,url):super().__init__(convert_charrefs=True);self.url=url
 def handle_starttag(self,tag,attrs):
  d=dict(attrs)
  for attr in ('href','src','data-src','data-fullurl','data-ipslightbox-src','data-ipslightbox-href','poster','content'):
   v=d.get(attr)
   if isinstance(v,str) and (attr!='content' or v.startswith(('http','/uploads/'))):queue(v,self.url,attr)
  for attr in ('srcset','data-srcset'):
   if d.get(attr):
    for v in d[attr].split(','):queue(v.strip().split(' ')[0],self.url,attr)
 handle_startendtag=handle_starttag

def parse(body,url):
 if re.search(br'<title[^>]*>\s*(?:one moment, please|just a moment)\.*\s*</title>',body,re.I):raise m.SourceError('source-browser-check-required',403)
 text=body.decode('utf-8','replace').replace('\\/','/')
 parser=Links(url);parser.feed(text)
 for v in re.findall(r"(?:https?://(?:www\.)?deadlystream\.com)?/uploads/[^\s<>\"'\\)]+",text):queue(v,url,'embedded-upload')

for u in json.loads((BASE/'output/whole-site-inventory.json').read_text())['urls']:
 normalized=normalize(u,'https://deadlystream.com/')
 if normalized and not classify(normalized) and not urlsplit(normalized).query:
  c.execute('INSERT OR IGNORE INTO pages(url,kind) VALUES(?,?)',(normalized,'official-sitemap-page'))
 else:queue(u,'https://deadlystream.com/')
# These are public IDs explicitly present in preserved legacy attachment links. The current handler enforces its own public access rules; no keys or IDs are guessed.
legacy=ROOT/'legacy-links.json'
if legacy.exists():
 for old in json.loads(legacy.read_text()).get('legacy_attachments',[]):
  attach_id=dict(parse_qsl(urlsplit(old).query)).get('attach_id','')
  if attach_id.isdigit():queue('https://deadlystream.com/applications/core/interface/file/attachment.php?id='+attach_id,old,'legacy-handler-compatibility-candidate; historical association not yet proven')
legacy=ROOT/'legacy-links.json'
if legacy.exists():
 for urls in json.loads(legacy.read_text()).values():
  for u in urls:queue(u,'urn:kolto:preserved-source:legacy-links','observed-preserved-legacy-reference')
c.commit()

lock=(ROOT/'capture.lock').open('a+');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
c.execute("UPDATE pages SET state='pending' WHERE state='fetching'");c.execute("UPDATE assets SET state='pending' WHERE state='fetching'");c.commit()

def claim(cx,table):
 cx.execute('BEGIN IMMEDIATE')
 order="CASE WHEN kind='gallery-original' THEN 0 WHEN kind IN ('attachment','legacy-attachment','linked-download') OR lower(url) GLOB '*.zip*' OR lower(url) GLOB '*.rar*' OR lower(url) GLOB '*.7z*' OR lower(url) GLOB '*.exe*' THEN 1 WHEN kind='uploaded-resource' THEN 2 ELSE 3 END,id" if table=='assets' else "CASE WHEN url LIKE '%/gallery/image/%' THEN 0 WHEN url LIKE '%/files/file/%' AND (url LIKE '%tab=comments%' OR url LIKE '%tab=reviews%') THEN 1 WHEN url LIKE '%/topic/%' THEN 2 ELSE 3 END,url"
 row=cx.execute(f"SELECT * FROM {table} WHERE state='pending' AND next_attempt<={time.time()} ORDER BY {order} LIMIT 1").fetchone()
 if row:cx.execute(f"UPDATE {table} SET state='fetching',attempts=attempts+1 WHERE "+('id=?' if table=='assets' else 'url=?'),(row['id'] if table=='assets' else row['url'],))
 cx.commit();return row

def worker(table):
 LOCAL.db=db();cx=LOCAL.db;s=source()
 while True:
  try:r=claim(cx,table)
  except sqlite3.OperationalError:
   cx.rollback();time.sleep(2);continue
  if r is None:time.sleep(3);continue
  key=r['id'] if table=='assets' else r['url'];where='id=?' if table=='assets' else 'url=?'
  try:
   response=s.get(r['url']);content_type=response.headers.get('Content-Type','').lower()
   if table=='pages':
    if not any(t in content_type for t in ('html','json','xml','text/')):
     response.close();cx.execute('INSERT OR IGNORE INTO assets(identity,url,kind) VALUES(?,?,?)',(r['url'],r['url'],'legacy-screenshot'));cx.execute('INSERT OR IGNORE INTO refs(identity,page_url,label,source_url) VALUES(?,?,?,?)',(r['url'],r['url'],'legacy-image-endpoint',r['url']));cx.execute("UPDATE pages SET state='download-resource',status=?,at=? WHERE url=?",(response.status_code,m.stamp(),key));cx.commit();continue
    body=response.content;response.close()
    if len(body)>32*1024*1024:raise m.SourceError('oversized-source-page')
    digest=hashlib.sha256(body).hexdigest();folder=ROOT/'pages';folder.mkdir(exist_ok=True);path=folder/(digest+'.html.gz')
    if not path.exists():path.write_bytes(gzip.compress(body,mtime=0))
    parse(body,response.url)
    cx.execute("UPDATE pages SET state='captured',error=NULL,sha=?,path=?,status=?,at=? WHERE url=?",(digest,str(path),response.status_code,m.stamp(),key));cx.commit()
   else:
    if ('html' in content_type or 'json' in content_type) and 'attachment' not in response.headers.get('Content-Disposition','').lower() and not urlsplit(r['url']).path.startswith('/uploads/'):
     body=bytearray()
     try:
      for chunk in response.iter_content(128*1024):
       body.extend(chunk)
       if len(body)>16*1024*1024:raise m.SourceError('oversized-download-landing-page')
     finally:response.close()
     body=bytes(body);digest=hashlib.sha256(body).hexdigest();folder=ROOT/'pages';folder.mkdir(exist_ok=True);path=folder/(digest+'.html.gz')
     if not path.exists():path.write_bytes(gzip.compress(body,mtime=0))
     parse(body,response.url);cx.execute("INSERT OR REPLACE INTO pages(url,kind,state,sha,path,status,at) VALUES(?,?,?,?,?,?,?)",(r['url'],'resource-landing','captured',digest,str(path),response.status_code,m.stamp()));cx.commit()
     raise m.SourceError('source-returned-page-instead-of-download',response.status_code)
    aid=-r['id'];revision='site-resource-'+hashlib.sha256(r['identity'].encode()).hexdigest()[:24]
    s.db.execute('INSERT OR IGNORE INTO files(id,url,status) VALUES(?,?,?)',(aid,r['url'],'pending'))
    s.db.execute('INSERT OR IGNORE INTO versions(file_id,revision,label,history_id,source_url) VALUES(?,?,?,?,?)',(aid,revision,r['kind'],'0',r['url']));s.db.commit()
    version=dict(s.db.execute('SELECT * FROM versions WHERE file_id=?',(aid,)).fetchone());version['source_url']=r['url'];hint=unquote(Path(urlsplit(response.url).path).name)
    if hint in ('index.php','download.php',''):hint=r['kind']+'-'+str(r['id'])+(mimetypes.guess_extension(content_type.split(';')[0]) or '.bin')
    if hint=='attachment.php':hint='attachment-'+str(r['id'])
    s.store_file(version,response,hashlib.sha256(r['identity'].encode()).hexdigest(),{'kind':r['kind'],'public_link':True},filename_hint=hint)
    item=s.db.execute('SELECT * FROM items WHERE file_id=? ORDER BY cached_at DESC LIMIT 1',(aid,)).fetchone()
    cx.execute('UPDATE attachment_links SET tried_at=?,status=200,error=NULL WHERE identity=? AND url=?',(m.stamp(),r['identity'],r['url']))
    cx.execute("UPDATE assets SET state='captured',status=200,error=NULL,filename=?,sha256=?,size=?,manifest=?,at=? WHERE id=?",(item['filename'],item['sha256'],item['size'],item['manifest'],m.stamp(),key));cx.commit()
  except Exception as e:
   code=e.code if isinstance(e,m.SourceError) else type(e).__name__;status=getattr(e,'status',0)
   cx.rollback()
   if table=='assets' and r['kind']=='attachment':
    cx.execute('UPDATE attachment_links SET tried_at=?,status=?,error=? WHERE identity=? AND url=?',(m.stamp(),status,code,r['identity'],r['url']))
    if status in (401,403,404,410) or code in ('source-returned-page-instead-of-download','source-empty-download','incomplete-source-file'):
     alternative=cx.execute('SELECT url FROM attachment_links WHERE identity=? AND tried_at IS NULL AND url<>? ORDER BY url LIMIT 1',(r['identity'],r['url'])).fetchone()
     if alternative:
      cx.execute("UPDATE assets SET state='pending',url=?,error=?,status=?,next_attempt=0,at=? WHERE id=?",(alternative['url'],code,status,m.stamp(),key));cx.commit();print(json.dumps({'event':'published-attachment-alternative','id':key}),flush=True);continue
   transient=status in (429,500,502,503,504) or code in ('source-browser-check-required','source-proxy-unavailable','source-proxy-required','source-connection-failed','source-dns-unavailable','source-backoff-required','ConnectionError','ReadTimeout','ClientError','OperationalError')
   state='pending' if (transient and r['attempts']<5) or code in ('source-browser-check-required','source-proxy-unavailable','source-proxy-required') else 'unavailable'
   cx.execute(f"UPDATE {table} SET state=?,error=?,status=?,at=?,next_attempt=? WHERE {where}",(state,code,status,m.stamp(),time.time()+60*2**min(r['attempts'],5) if transient else 0,key));cx.commit()
   print(json.dumps({'event':'resource-gap','table':table,'id':key if table=='assets' else hashlib.sha256(key.encode()).hexdigest()[:16],'reason':code,'status':status}),flush=True)

pool=concurrent.futures.ThreadPoolExecutor(max_workers=14)
tasks=[pool.submit(worker,'pages') for _ in range(6)]+[pool.submit(worker,'assets') for _ in range(8)]
# Reuse preserved catalogue HTML; no extra origin requests. Local-only originals are imported separately.
saved_urls={r['path']:r['url'] for r in c.execute("SELECT path,url FROM pages WHERE path IS NOT NULL")}
legacy_sources=ROOT/'legacy-page-sources.json.gz'
legacy_urls={}
if legacy_sources.exists():legacy_urls={row['path']:row['urls'] for row in json.loads(gzip.decompress(legacy_sources.read_bytes()))['pages']}
parsed_prefix='parsed-'+cfg.get('page_import_version','v5')+':'
for file in list((BASE/'state/source-pages').glob('*.gz'))+list((ROOT/'pages').glob('*.gz')):
 if c.execute('SELECT 1 FROM meta WHERE key=?',(parsed_prefix+file.name,)).fetchone():continue
 try:
  body=gzip.decompress(file.read_bytes());canonical=re.search(br'<link[^>]+rel=[\"\']canonical[\"\'][^>]+href=[\"\']([^\"\']+)',body);url=saved_urls.get(str(file)) or (canonical[1].decode('utf-8') if canonical else 'https://deadlystream.com/files/')
  source_urls=legacy_urls.get(str(file.relative_to(BASE))) or [url]
  for source_url in source_urls:parse(body,source_url)
  c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(parsed_prefix+file.name,m.stamp()));c.commit()
 except Exception as e:print(json.dumps({'event':'cached-page-error','class':type(e).__name__}),flush=True)

while True:
 for task in tasks:
  if task.done():task.result();raise RuntimeError('preservation worker stopped')
 status={'at':m.stamp()}
 for table in ('pages','assets'):status[table]={r[0]:r[1] for r in c.execute(f'SELECT state,count(*) FROM {table} GROUP BY state')}
 status['bytes']=c.execute("SELECT coalesce(sum(size),0) FROM assets WHERE state='captured'").fetchone()[0]
 (ROOT/'status.json').write_text(json.dumps(status,indent=2));print(json.dumps(status),flush=True);time.sleep(30)
