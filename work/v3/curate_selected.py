"""Inventory-wide editorial dedup/noise gate after selection, before writing/publication.
Embeddings group candidates upstream; Luna makes this final event-level inventory check.
"""
import json,sys,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
import pipeline as p
import llm
SYS='''You are the final event-inventory editor for a technology brief. Review ALL selected events below.
Return groups of the SAME concrete happening, choosing one keeper; exclude non-news/noise with a specific evidence-based reason.
Same: original release/incident + other outlets' coverage/reactions/user reports ABOUT that happening. Different: a separate incident, model, CVE, court action, or meaningful new development sharing a company.
Community is corroborating input, not an automatic separate story. Keep a genuinely distinct observed user-experience incident when it adds a concrete new signal; don't make the release another headline.
A perennial repository's README/install instructions are NOT news just because the project is famous, major-org owned, widely starred, or useful. Keep a repository only when a newly rising project, new release, meaningful capability/licensing/security change or concrete traction change is actually supported in this inventory. Do not permanently ban a project name.
Do NOT exclude a meaningful early community signal solely because it lacks primary/independent verification or a screenshot/email is unavailable. Preserve concrete core-interest signals with clear attribution/uncertainty; no claim of consensus or confirmation. A new local-model/agent-harness implementation with concrete mechanisms and a large vendor/self-reported benchmark claim can be useful early radar: label its evaluation limits instead of discarding it solely for lack of independent traction. Static perennial repository descriptions remain excluded.
Conference schedules and product landing pages with no substantive development are excluded. No target story count, quota or filler. Keep every distinct useful event.
Return JSON only: {"groups":[{"ids":["E..."],"keeper":"E...","reason":"specific same-event rationale"}],"exclusions":[{"id":"E...","reason":"specific exclusion rationale"}]}.
EVERY provided ID occurs exactly once in one group or exclusion. A singleton group is normal. Do not invent IDs.'''
def validate_plan(ids,plan):
 problems=plan_problems(ids,plan)
 if problems:raise ValueError('curation IDs not exhaustive/unique: '+problems)
 return True
def plan_problems(ids,plan):
 seen=[]
 for g in plan.get('groups',[]):
  if not g.get('ids') or g.get('keeper') not in g['ids'] or not g.get('reason'):return f'invalid group {json.dumps(g,ensure_ascii=False)[:200]}'
  seen.extend(g['ids'])
 for e in plan.get('exclusions',[]):
  if not e.get('reason'):return f'missing exclusion reason for {e.get("id")}'
  seen.append(e['id'])
 dup=sorted({x for x in seen if seen.count(x)>1});missing=sorted(set(ids)-set(seen));extra=sorted(set(seen)-set(ids))
 if dup or missing or extra:return f'duplicate={dup} missing={missing} unknown={extra}'
 return ''
def apply_plan(date,events,selection,plan):
 import copy
 validate_plan({s['event_id'] for s in selection if s.get('selected')},plan)
 events=copy.deepcopy(events);selection=copy.deepcopy(selection)
 ev={e['event_id']:e for e in events};sc={s['event_id']:s for s in selection};rewrite=[]
 for exclusion in plan['exclusions']:
  s=sc[exclusion['id']];s['selected']=False;s['curation']={'excluded':True,'reason':exclusion['reason']}
 for group in plan['groups']:
  keeper=group['keeper'];k=ev[keeper]
  sc[keeper]['curation']={'kept':True,'members':group['ids'],'reason':group['reason']}
  if len(group['ids'])<2:continue
  k['curation_merged_ids'] = group['ids']
  seen={(a['date'],a['aid']) for a in k['articles']}
  today=set(k.get('new_today',{}).get(date,[]))
  for eid in group['ids']:
   if eid==keeper:continue
   other=ev[eid]
   for a in other['articles']:
    key=a['date'],a['aid']
    if key not in seen:k['articles'].append(a);seen.add(key)
   today.update(other.get('new_today',{}).get(date,[]))
   sc[eid]['selected']=False;sc[eid]['curation']={'absorbed_by':keeper,'reason':group['reason']}
   other.setdefault('absorbed',{})[date]=keeper
  k.setdefault('new_today',{})[date]=sorted(today)
  k['dates']=sorted(set(k.get('dates',[]))|{a['date'] for a in k['articles']})
  rewrite.append(keeper)
 return {'events':events,'selection':selection,'rewrite_ids':rewrite}

def stage(date):
 root=p.V3/date;marker=root/'curation_applied.json'
 if marker.exists():
  saved=json.loads(marker.read_text())
  if saved.get('selection_sha256')!=hashlib.sha256((root/'selection.jsonl').read_bytes()).hexdigest():raise ValueError('curation applied selection changed')
  return saved
 plan=review(date);events=p.load_events();selection=p.jl_read(root/'selection.jsonl');result=apply_plan(date,events,selection,plan)
 p.jl_write(root/'selection.pre-curation.jsonl',selection);p.jl_write(root/'events.pre-curation.jsonl',events)
 p.jl_write(p.EVENTS,result['events']);p.jl_write(root/'selection.jsonl',result['selection'])
 # Only retained outcomes remain current; all discarded/merged drafts are preserved in the backup.
 articles=p.jl_read(root/'articles.jsonl');p.jl_write(root/'articles.pre-curation.jsonl',articles)
 wanted={s['event_id'] for s in result['selection'] if s.get('selected')}
 p.jl_write(root/'articles.jsonl',[r for r in articles if r['event_id'] in wanted])
 saved={'kept':len(wanted),'excluded':plan['exclusions'],'merges':[g for g in plan['groups'] if len(g['ids'])>1],'rewrite_ids':result['rewrite_ids'],'selection_sha256':hashlib.sha256((root/'selection.jsonl').read_bytes()).hexdigest()}
 marker.write_text(json.dumps(saved,ensure_ascii=False,indent=2));return saved

def review(date):
 events={e['event_id']:e for e in p.load_events()};selected=[s for s in p.jl_read(p.V3/date/'selection.jsonl') if s.get('selected')]
 articles={r['event_id']:r for r in p.jl_read(p.V3/date/'articles.jsonl')}
 items=[]
 for s in selected:
  e=events[s['event_id']];r=articles.get(s['event_id'],{})
  items.append({'id':s['event_id'],'title':e['title'],'event_summary':e['summary'][:400],'event_type':e['event_type'],'score_reason':s.get('reason'),'draft_headline':r.get('headline'),'lead':(r.get('lead') or '')[:350],'writer_action':r.get('action'),'source_urls':[a['url'] for a in e['articles'][:3]]})
 prompt=json.dumps(items,ensure_ascii=False);digest=hashlib.sha256((SYS+prompt).encode()).hexdigest();cache=p.V3/date/'curation_plan.json'
 if cache.exists():
  old=json.loads(cache.read_text())
  if old.get('input_sha256')==digest:return old['plan']
 ids={s['event_id'] for s in selected};out=None;problems=''
 # The model occasionally drops/duplicates an ID in a long inventory; tell it exactly what was wrong and retry.
 for attempt in range(3):
  ask=prompt if not problems else prompt+f"\n\nYour previous answer was rejected: {problems}. Return the complete plan again; every provided ID exactly once."
  out=llm.call_json(SYS,ask,timeout=600);problems=plan_problems(ids,out)
  if not problems:break
 validate_plan(ids,out)
 cache.write_text(json.dumps({'input_sha256':digest,'plan':out,'model':llm.MODEL,'effort':llm.EFFORT,'usage':llm.USAGE},ensure_ascii=False,indent=2))
 return out
if __name__=='__main__':
 out=review(sys.argv[1]);print(json.dumps({'groups':len(out['groups']),'merges':[g for g in out['groups'] if len(g['ids'])>1],'exclusions':out['exclusions']},ensure_ascii=False,indent=2))


def fallback(date,reason):
 """Curation failed before changing anything: keep the scored selection as it is (duplicates stay, nothing is excluded).
 Refuses (re-raises) when a previous attempt already changed the files, because then the state is no longer the scored one."""
 root=p.V3/date
 if (root/'selection.pre-curation.jsonl').exists() or (root/'events.pre-curation.jsonl').exists():
  raise RuntimeError('curation partially applied; refusing fallback: '+reason)
 sel=root/'selection.jsonl'
 kept=sum(1 for r in p.jl_read(sel) if r.get('selected'))
 saved={'kept':kept,'excluded':[],'merges':[],'rewrite_ids':[],'selection_sha256':hashlib.sha256(sel.read_bytes()).hexdigest(),'fallback_reason':reason}
 (root/'curation_applied.json').write_text(json.dumps(saved,ensure_ascii=False,indent=2));return saved
