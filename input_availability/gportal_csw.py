"""Public G-Portal CSW search (October 2026 service). No authentication needed.
Downloads are not claimed: the new SFTP key route is not yet configured/tested.
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
