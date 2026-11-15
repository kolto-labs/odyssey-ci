#!/usr/bin/env python3
"""Stream one immutable cached original to a one-file Drive upload capability.

No Google account token is accepted. Job/config files stay private; stdout and
receipts contain no credentials or upload-session URLs.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

import requests


def google_failure(response,category,**extra):
    reasons=[]
    try:
        for item in response.json().get('error',{}).get('errors',[]):
            reason=item.get('reason','')
            if re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',reason):reasons.append(reason)
    except (ValueError,AttributeError,TypeError):pass
    return {'ok':False,'category':category,'http_status':response.status_code,'google_error_reasons':reasons,**extra}


def execute(job_path, output):
    started=time.monotonic();job=json.loads(Path(job_path).read_text())
    uri=job['upload_session_uri'];address=urlsplit(uri)
    if address.scheme!='https' or not (address.hostname or '').endswith('.googleapis.com') or address.username or address.password:
        raise ValueError('unexpected upload origin')
    spec=importlib.util.spec_from_file_location('catalogue_reader',job['reader_module'])
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    config=json.loads(Path(job['cache_config']).read_text())
    reader=module.Reader(config,job['catalogue_identity_file'])
    key=job['manifest'];match=re.search(r'-([0-9a-f]{64})\.json\.age$',key)
    cipher_sha=match[1] if match else reader.s3.head_object(Bucket=config['bucket'],Key=key)['Metadata']['cipher-sha256']
    manifest=json.loads(reader.encrypted(key,cipher_sha,16*1024*1024))
    if manifest.get('schema')!='kolto-deadlystream-file/v1' or any(manifest.get(k)!=job[k] for k in ('sha256','size','file_id')) or (manifest.get('kind')!='page' and manifest.get('filename')!=job['filename']) or manifest.get('recipient')!=config['recipient']:
        raise ValueError('immutable manifest mismatch')
    manifest_seconds=round(time.monotonic()-started,3);session=requests.Session();session.trust_env=False
    response=session.put(uri,data=b'',headers={'Content-Length':'0','Content-Range':'bytes */'+str(job['size'])},timeout=(20,60))
    if response.status_code in (200,201):
        proof=response.json();offset=job['size']
    elif response.status_code==308:
        match=re.fullmatch(r'bytes=0-(\d+)',response.headers.get('Range',''))
        offset=int(match[1])+1 if match else 0
    else:
        return google_failure(response,'upload-session-unavailable')

    if offset<job['size']:
        class PlainStream:
            def __len__(self):return job['size']-offset
            def __iter__(self):
                position=0;digest=hashlib.sha256()
                for chunk in manifest['chunks']:
                    end=position+chunk['size']
                    if end<=offset:position=end;continue
                    plain=reader.encrypted(chunk['key'],chunk['cipher_sha256'],17*1024*1024)
                    if len(plain)!=chunk['size'] or hashlib.sha256(plain).hexdigest()!=chunk['sha256']:
                        raise ValueError('source chunk mismatch')
                    if offset==0:digest.update(plain)
                    start=max(0,offset-position);position=end
                    yield plain[start:]
                if position!=job['size']:raise ValueError('source length mismatch')
                if offset==0 and digest.hexdigest()!=job['sha256']:raise ValueError('source archive mismatch')
        response=session.put(uri,data=PlainStream(),headers={
            'Content-Length':str(job['size']-offset),'Content-Type':job.get('mime_type','application/octet-stream'),
            'Content-Range':'bytes '+str(offset)+'-'+str(job['size']-1)+'/'+str(job['size'])},timeout=(20,180))
        if response.status_code not in (200,201):
            return google_failure(response,'upload-interrupted',resumable=True)
        proof=response.json()
    if int(proof.get('size',-1))!=job['size'] or proof.get('sha256Checksum')!=job['sha256']:
        raise ValueError('Drive returned an unexpected archive checksum')
    result={'ok':True,'sha256':job['sha256'],'size':job['size'],'drive_id':proof['id'],
            'url':proof.get('webViewLink'),'resumed_from':offset,
            'remote_sha256_verified':True,'immutable_manifest_provenance_verified':True,'manifest_seconds':manifest_seconds,'total_seconds':round(time.monotonic()-started,3),'requires_independent_controller_readback':True}
    Path(output).write_text(json.dumps(result,indent=2)+'\n')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:result=execute(args.job,args.output)
    except Exception as error:result={'ok':False,'category':type(error).__name__}
    print(json.dumps(result),flush=True)
    if not result.get('ok'):raise SystemExit(2)


if __name__=='__main__':main()
