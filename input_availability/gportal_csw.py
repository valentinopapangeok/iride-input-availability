"""Public G-Portal CSW search (October 2026 service). No authentication needed.
Sample downloads use key-authenticated SFTP with a pinned server fingerprint.
"""
import xml.etree.ElementTree as ET
from urllib.parse import urljoin,urlsplit
import requests
ENDPOINT='https://csw.gportal.jaxa.jp/csw'
DATASETS={'L3-LST':'10003121,10003120','L3-SST':'10003027,10003026','L2-ARNP':'10002079'}
ATOM='{http://www.w3.org/2005/Atom}'
def parse_page(content):
 root=ET.fromstring(content)
 if root.tag!=ATOM+'feed':raise ValueError('G-Portal returned a non-Atom response')
 products=[]
 for entry in root.findall(ATOM+'entry'):
  fields={el.tag.split('}')[-1]:(el.text or '').strip() for el in entry.iter() if len(el)==0}
  fields['catalogue_updated']=entry.findtext(ATOM+'updated','')
  fields['download_links']=[el.get('href','') for el in entry.findall(ATOM+'link') if el.get('rel')=='enclosure']
  products.append(fields)
 next_url=next((el.get('href') for el in root.findall(ATOM+'link') if el.get('rel')=='next'),None)
 return products,next_url

def search_day(dataset_path,day,params=None,bbox=None,product_filter=None):
 query={'service':'CSW','version':'3.0.0','request':'GetRecords','outputFormat':'application/atom+xml','datasetId':DATASETS[dataset_path[-1]],'startTime':f'{day}T00:00:00Z','endTime':f'{day}T23:59:59Z','count':100}
 query.update(params or {})
 if bbox:query['bbox']=','.join(map(str,bbox))
 url=ENDPOINT;seen=set();products=[]
 for _ in range(100):
  r=requests.get(url,params=query,timeout=(15,90));r.raise_for_status()
  if r.url in seen:raise ValueError('Repeated CSW pagination')
  seen.add(r.url);page,next_url=parse_page(r.content);products.extend(page)
  if not next_url:break
  url=urljoin(ENDPOINT,next_url)
  if urlsplit(url).scheme!='https' or urlsplit(url).netloc!=urlsplit(ENDPOINT).netloc:raise ValueError('Untrusted CSW pagination URL')
  query=None
 else:raise ValueError('CSW pagination limit exceeded')
 return [p for p in products if not product_filter or product_filter(p)]

# Trust-on-first-use fingerprint approved by the user on 2026-10-28.
SFTP_HOST='sftp.gportal.jaxa.jp'
SFTP_FINGERPRINT='SHA256:pYLmuMNFi9tQYRAZLXERRuyOszorS0qJQEE/u4Co+xM'

def download_sample(product, directory):
    import os, io, base64, hashlib, socket
    from pathlib import Path, PurePosixPath
    import paramiko
    key_text=os.getenv('GPORTAL_PRIVATE_KEY','')
    key_path=os.getenv('GPORTAL_PRIVATE_KEY_PATH','')
    if not key_text and key_path:
        key_text=Path(key_path).read_text()
    if not key_text or not os.getenv('GPORTAL_USER'):
        raise RuntimeError('GPORTAL_PRIVATE_KEY and GPORTAL_USER required')
    key=None
    for cls in (paramiko.Ed25519Key,paramiko.RSAKey,paramiko.ECDSAKey):
        try:
            key=cls.from_private_key(io.StringIO(key_text),password=os.getenv('GPORTAL_KEY_PASSPHRASE') or None)
            break
        except paramiko.SSHException:
            continue
    if key is None:raise ValueError('Unsupported or encrypted private key')
    links=product.get('download_links',[])
    if not links:raise ValueError('No download link')
    remote=links[0]
    if not remote.startswith('standard/'):
        raise ValueError('Unexpected catalogue download path')
    remote='/product/Standard/' + remote[len('standard/'):]
    path=PurePosixPath(remote)
    if urlsplit(remote).scheme or '..' in path.parts or not remote.startswith('/product/Standard/'):
        raise ValueError('Unexpected SFTP path')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    dest=directory/path.name;partial=directory/(path.name+'.part')
    transport=None
    try:
        sock=socket.create_connection((SFTP_HOST,22),timeout=30)
        transport=paramiko.Transport(sock);transport.banner_timeout=30;transport.auth_timeout=30
        transport.start_client(timeout=30)
        fingerprint='SHA256:'+base64.b64encode(hashlib.sha256(transport.get_remote_server_key().asbytes()).digest()).decode().rstrip('=')
        if fingerprint!=SFTP_FINGERPRINT:raise paramiko.SSHException('SFTP host fingerprint mismatch')
        transport.auth_publickey(os.environ['GPORTAL_USER'],key)
        with paramiko.SFTPClient.from_transport(transport) as sftp:
            sftp.get_channel().settimeout(120)
            size=sftp.stat(remote).st_size
            sftp.get(remote,str(partial))
            if size<=0 or partial.stat().st_size!=size:raise ValueError('Incomplete SFTP download')
            with partial.open('rb') as f:
                if f.read(8)!=b'\x89HDF\r\n\x1a\n':raise ValueError('Expected HDF5 data')
            partial.replace(dest)
        return str(dest)
    finally:
        if transport:transport.close()
        if partial.exists():partial.unlink()
