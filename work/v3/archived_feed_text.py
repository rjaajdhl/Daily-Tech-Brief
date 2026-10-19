"""Read source text from retained gzip RSS/Atom captures; zero network calls."""
from pathlib import Path
from html.parser import HTMLParser
from xml.etree import ElementTree as ET
import gzip,hashlib
class Text(HTMLParser):
 def __init__(self):super().__init__();self.parts=[];self.ignore=0
 def handle_starttag(self,tag,attrs):
  if tag in ('script','style'):self.ignore+=1
  if tag in ('p','div','br','li','tr','td','th','pre'):self.parts.append('\n')
 def handle_endtag(self,tag):
  if tag in ('script','style'):self.ignore=max(0,self.ignore-1)
  if tag in ('p','div','li','tr','td','th','pre'):self.parts.append('\n')
 def handle_data(self,data):
  if not self.ignore:self.parts.append(data)
def plain(s):
 p=Text();p.feed(s);return '\n'.join(' '.join(line.split()) for line in ''.join(p.parts).splitlines() if line.strip())
def load(directory,urls):
 out={}
 for path in sorted(Path(directory).glob('*.gz')):
  try:raw=gzip.decompress(path.read_bytes());root=ET.fromstring(raw)
  except (OSError,ET.ParseError):continue
  for e in root.iter():
   if e.tag.rsplit('}',1)[-1] not in ('item','entry'):continue
   links=[];texts=[]
   for c in e:
    tag=c.tag.rsplit('}',1)[-1]
    if tag=='link':links.append(c.attrib.get('href') or (c.text or '').strip())
    if tag in ('description','summary','content','encoded'):
     text=plain(''.join(c.itertext()))
     if text:texts.append(text)
   if not texts:continue
   best=max(texts,key=len)
   for url in links:
    if url in urls and len(best)>len(out.get(url,{}).get('text','')):
     out[url]={'url':url,'text':best,'raw_path':str(path),'raw_sha256':hashlib.sha256(raw).hexdigest(),'retrieval':'retained RSS/Atom original; no recrawl'}
 return out
