"""Bind published issues to the source-based operational feedback ledger, never sandbox event IDs."""
import json,re
from pathlib import Path
from work import feedback as FB

def plan(rows,day,path=None):
 path=Path(path or FB.ISSUE_LEDGER_PATH)
 ledger=json.loads(path.read_text()) if path.exists() else {'version':1,'issues':{},'dates':{}}
 issues=ledger.get('issues',{});reserved={v['feedback_id'] for v in issues.values() if v.get('feedback_id')}
 maxima={}
 for fid in reserved:
  m=re.fullmatch(r'(\d{2})-(\d+)',fid)
  if m:maxima[m[1]]=max(maxima.get(m[1],0),int(m[2]))
 entries={};ids=set();date_map={}
 for row in rows:
  signal={'title':row['headline'],'primary_topic':{'01':'ai','02':'security','03':'local_models','04':'infrastructure','05':'semiconductors','06':'developer_tools','07':'research','08':'outside_bubble','09':'community','10':'business','11':'review_failed'}[row['_code']], 'url':row['sources'][0]['url']}
  key=FB.stable_issue_key(signal)
  if key in entries:key=key+'#'+str(row.get('event_id'))  # two articles share a first source URL: keep both, give the second its own identity
  entry=dict(issues.get(key,{}));fid=entry.get('feedback_id')
  if not fid:
   code=row['_code'];maxima[code]=maxima.get(code,0)+1;fid=f'{code}-{maxima[code]:03d}'
   while fid in reserved:maxima[code]+=1;fid=f'{code}-{maxima[code]:03d}'
   reserved.add(fid)
  if fid in ids:raise ValueError('duplicate publication feedback id')
  ids.add(fid);row['feedback_id']=fid
  entry.update(feedback_id=fid,stable_issue_key=key,first_seen_date=entry.get('first_seen_date') or day,last_seen_date=day,title=row['headline'],section_code=fid[:2],features=FB.feature_values(signal))
  entries[key]=entry;date_map[key]=fid
 return {'day':day,'entries':entries,'date_map':date_map}

def commit(plan,path=None):
 path=Path(path or FB.ISSUE_LEDGER_PATH)
 current=json.loads(path.read_text()) if path.exists() else {'version':1,'issues':{},'dates':{}}
 issues=current.setdefault('issues',{})
 used={v.get('feedback_id'):k for k,v in issues.items() if v.get('feedback_id')}
 for key,entry in plan['entries'].items():
  other=used.get(entry['feedback_id'])
  if other is not None and other!=key:raise ValueError('concurrent feedback ID conflict')
  issues[key]={**issues.get(key,{}),**entry}
 current.setdefault('dates',{})[plan['day']]=plan['date_map'];current['version']=1
 path.parent.mkdir(parents=True,exist_ok=True)
 tmp=path.with_suffix('.replay.tmp');tmp.write_text(json.dumps(current,ensure_ascii=False,indent=2));tmp.replace(path)
 read=json.loads(path.read_text())
 if read['dates'][plan['day']]!=plan['date_map']:raise ValueError('feedback ledger readback failed')
 return {'date':plan['day'],'registered':len(plan['entries']),'path':str(path)}
