"""v3 cluster-first daily pipeline (test harness, no Notion writes).

Stages per target date (each cached/resumable under data/v3/DATE/):
  extract  -> per-article event facts (gpt-6-luna)
  match    -> attach to persistent event store or create new events
  refresh  -> regenerate canonical summary + embedding for touched events
  novelty  -> did new articles add facts beyond the last written article?
  select   -> exclusion filter + scoring + threshold (every decision logged)
  write    -> one Korean article per selected event (skip allowed)
  validate -> source tag + numeric grounding checks
Usage: python work/v3/pipeline.py --date 2026-09-25 [--stages extract,match,...]
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import llm  # noqa: E402

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[2])
V3 = Path(os.environ.get("V3_DATA_ROOT", str(ROOT / "data" / "v3")))
EVENTS = V3 / "events" / "events.jsonl"
EMBED_URL = os.environ.get("V3_EMBED_URL", "http://localhost:11434/api/embed")
WORKERS = int(os.environ.get("V3_WORKERS", "12"))

ACTIVE_DAYS = 14
CAND_FLOOR = 0.60      # history search floor
GROUP_SIM = 0.72       # intra-day candidate edge
GROUP_SIM_STRONG = 0.84
MAX_GROUP = 10
SELECT_THRESHOLD = 6

SECTIONS = {
    "01": "AI·에이전트·프런티어 모델",
    "02": "사이버보안·오펜시브 시큐리티",
    "03": "로컬·오픈 모델",
    "04": "인프라·클라우드·컴퓨트",
    "05": "반도체·AI 인프라",
    "06": "개발자 도구·오픈소스·GitHub",
    "07": "연구·논문·벤치마크",
    "08": "로봇·응용 기술·오픈 디스커버리",
    "09": "커뮤니티 신호·초기 경보",
    "10": "기술 기업·비즈니스·거버넌스",
}

COMMUNITY = ("news.ycombinator.com", "reddit.com", "twitter.com", "x.com", "youtube.com", "lobste.rs", "bsky.app", "mastodon")
PRIMARY_HINT = ("openai.com", "anthropic.com", "blog.google", "deepmind", "googleblog", "aws.amazon.com", "microsoft.com",
                "nvidia.com", "meta.com", "ai.meta", "x.ai", "mistral.ai", "huggingface.co", "github.com", "github.blog",
                "arxiv.org", "cisa.gov", ".gov", "nist.gov", "status.", "cloudflare.com", "apple.com", "intel.com", "amd.com",
                "tsmc.com", "spacex.com", "tesla.com", "nature.com", "msrc", "chromium.org", "kubernetes.io")


# ----------------------------------------------------------------- utils
def jl_read(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with p.open() as f:
        return [json.loads(l) for l in f if l.strip()]


def jl_write(p: Path, rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(p)


def domain(u: str) -> str:
    return urlsplit(u).netloc.lower().removeprefix("www.")


def domain_is(d: str, hosts) -> bool:
    return any(d == h or d.endswith("." + h) for h in hosts)


def source_class(u: str) -> str:
    d = domain(u)
    if domain_is(d, COMMUNITY):
        return "community"
    if any(d == h.strip(".") or d.endswith("." + h.strip(".")) or (h.endswith(".") and d.startswith(h)) or (not "." in h and h in d)
           for h in PRIMARY_HINT):
        return "primary"
    return "news"


def url_id(u: str) -> str:
    return "a" + hashlib.sha1(u.encode()).hexdigest()[:10]


EMB_CACHE = V3 / "embed_cache.jsonl"
_emb_cache: dict[str, list[float]] | None = None


EMBED_ATTEMPTS = 6          # 1 try + 5 retries with backoff; Ollama is restarted after the first failure
EMBED_BACKOFF = (2.0, 5.0, 15.0, 30.0, 60.0)


def _embed_request(chunk: list[tuple[str, str]]) -> list:
    body = json.dumps({"model": "bge-m3", "input": [t[:6000] for _, t in chunk]}).encode()
    req = urllib.request.Request(EMBED_URL, data=body, headers={"Content-Type": "application/json"})
    last = None
    for attempt in range(EMBED_ATTEMPTS):
        try:
            return json.load(urllib.request.urlopen(req, timeout=600))["embeddings"]
        except Exception as e:  # connection refused while Ollama restarts, timeouts, 5xx
            last = e
            if attempt == 1:  # still failing: make sure the server is actually up (preflight started it once, it may have died since)
                try:
                    import preflight
                    preflight.ensure_ollama(autostart=True, wait=60)
                except Exception:  # noqa: BLE001 - the loop below keeps retrying either way
                    pass
            if attempt + 1 < EMBED_ATTEMPTS:
                time.sleep(EMBED_BACKOFF[min(attempt, len(EMBED_BACKOFF) - 1)])
    raise RuntimeError(f"embedding failed after {EMBED_ATTEMPTS} attempts: {last}")


def embed(texts: list[str]) -> np.ndarray:
    global _emb_cache
    if _emb_cache is None:
        _emb_cache = {r["h"]: r["v"] for r in jl_read(EMB_CACHE)}
    keys = [hashlib.sha256(t.encode()).hexdigest() for t in texts]
    missing = [(k, t) for k, t in zip(keys, texts) if k not in _emb_cache]
    for i in range(0, len(missing), 64):
        chunk = missing[i : i + 64]
        vecs = _embed_request(chunk)
        with EMB_CACHE.open("a") as f:
            for (k, _), v in zip(chunk, vecs):
                _emb_cache[k] = v
                f.write(json.dumps({"h": k, "v": v}) + "\n")
    m = np.array([_emb_cache[k] for k in keys], dtype=np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
    return m


def days_between(a: str, b: str) -> int:
    from datetime import date
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


# ----------------------------------------------------------------- load
def load_articles(date: str) -> list[dict]:
    override = os.environ.get("V3_INPUT_ARTICLES")
    if override:
        return jl_read(Path(override))
    input_root = Path(os.environ.get("V3_INPUT_ROOT", str(ROOT / "data")))
    pkt = json.loads((input_root / f"evidence/{date}/{date}.writer-packet.json").read_text())["issues"]
    enr = {r["source_url"]: r for r in jl_read(input_root / f"evidence/{date}/{date}.source-enrichment.jsonl")}
    out, seen = [], set()
    for it in pkt:
        u = it["source_url"]
        if u in seen:
            continue
        seen.add(u)
        er = enr.get(u)
        if er:  # newer enrichment wins over the stale packet copy
            it = {**it, **{k: er[k] for k in ("source_body_excerpt", "source_facts", "source_description", "source_title", "enrichment_state") if er.get(k)}}
        body = it.get("source_body_excerpt") or ""
        parts_ = [it.get("source_description") or "", body or it.get("source_facts") or it.get("intake_summary") or ""]
        text = "\n".join(p for p in parts_ if p).strip()
        out.append({
            "aid": url_id(u), "url": u, "date": date,
            "title": it.get("source_title") or it.get("title"),
            "legacy_section": it.get("topic_section"), "legacy_issue_id": it.get("issue_id"),
            "enrichment": it.get("enrichment_state"),
            "text": text, "has_body": bool(body),
            "source_class": source_class(u), "domain": domain(u),
        })
    from archived_feed_text import load as load_retained_feed
    recovered = load_retained_feed(input_root / "source_intake" / "raw" / date, {r["url"] for r in out})
    for article in out:
        cached = recovered.get(article["url"])
        if cached and ("arxiv.org/" in article["url"] or len(cached["text"]) > len(article["text"])):
            article["text"] = cached["text"]
            article["has_body"] = True
            article["retained_raw_source"] = {k: cached[k] for k in ("raw_path", "raw_sha256", "retrieval")}
    out.extend(load_reddit(date, seen))
    return out


REDDIT_SNAP = ROOT / "data" / "reddit" / "snapshots"


def load_reddit(date: str, seen: set[str]) -> list[dict]:
    """Reddit posts from the day's snapshot (subreddit list is already limited to AI/agents/local models/security/Musk).
    Only the post title + excerpt/selftext is used -- comments are never collected.  Removed/empty posts carry no signal.
    They enter as community-class articles, so they can corroborate an event but cannot make one 'verified' by themselves."""
    snapshot_root = Path(os.environ["V3_INPUT_ROOT"]) / "reddit" / "snapshots" if os.environ.get("V3_INPUT_ROOT") else REDDIT_SNAP
    path = snapshot_root / f"{date}.jsonl"
    out = []
    for r in jl_read(path):
        u = r.get("url") or ""
        body = (r.get("body_excerpt") or "").strip()
        if not u or u in seen or body in ("[removed]", "[deleted]") or len(body) < 200:
            continue
        seen.add(u)
        out.append({
            "aid": url_id(u), "url": u, "date": date, "title": r.get("title"), "legacy_section": None, "legacy_issue_id": None,
            "enrichment": "REDDIT_EXCERPT", "text": f"r/{r.get('subreddit')} post (excerpt; comments not collected)\n" + body,
            "has_body": True, "source_class": "community", "domain": "reddit.com", "reddit_sub": r.get("subreddit"),
        })
    return out


# ----------------------------------------------------------------- 1 extract
EXTRACT_SYS = """You read one technology article/post/paper/repo and extract the underlying EVENT it reports.
Return JSON only:
{"event_summary": "one English sentence: who did what (the concrete event, not background)",
 "entities": ["main orgs/products/projects/CVE ids/people, max 6"],
 "event_type": one of ["model_release","product_launch","feature_update","security_vuln","security_incident","outage","research_paper","open_source_repo","benchmark","funding_deal","policy_regulation","hardware_chip","business_news","opinion_analysis","community_discussion","other"],
 "event_date": "YYYY-MM-DD if stated else null",
 "is_noise": true only for ads/coupons/deals/job posts/empty pages,
 "key_facts": ["up to 5 concrete facts with numbers/names exactly as in the text"]}"""


def title_only_ok(a: dict) -> bool:
    """Paywalled/blocked pages (WSJ, Bloomberg, NYT, Axios, Reuters ...) arrive with no body.  Their headline still
    reports an event and joins its cluster as another outlet, so it is extracted from the title alone.  Community
    posts and bare pages stay out: a headline is not evidence of a discussion."""
    return a["source_class"] != "community" and len((a.get("title") or "").strip()) >= 25


def usable(a: dict) -> bool:
    """Enriched sources, un-enriched ones that still carry a real description (>=200 chars), or headline-only news."""
    if a["enrichment"] in ("FULL_SOURCE", "PARTIAL_SOURCE"):
        return bool(a["text"].strip())
    if len(a["text"].strip()) >= 200:
        return True
    return title_only_ok(a)


def stage_extract(date: str) -> None:
    arts = load_articles(date)
    out_p = V3 / date / "extracted.jsonl"
    # a row counts as done only when it actually carries an extracted event; needs_enrichment/extract_failed rows are retried
    done = {r["aid"]: r for r in jl_read(out_p) if "_error" not in r and r.get("event_summary")}
    todo = [a for a in arts if a["aid"] not in done and usable(a)]

    def work(a):
        body = a["text"][:5000].strip()
        head_only = len(body) < 200 and a["enrichment"] not in ("FULL_SOURCE", "PARTIAL_SOURCE")
        user = f"URL: {a['url']}\nTITLE: {a['title']}\n\nTEXT:\n" + (
            (body + "\n\n" if body else "") + "(article body unavailable - paywalled or blocked; extract only what the headline states, do not guess)"
            if head_only else body)
        r = llm.call_json(EXTRACT_SYS, user)
        out = {**a, **{k: r.get(k) for k in ("event_summary", "entities", "event_type", "event_date", "is_noise", "key_facts")}}
        if head_only:
            out["head_only"] = True
        return out

    from single_extract import extract_all
    res = extract_all(todo, EXTRACT_SYS, llm.call_json, llm.parallel, min(WORKERS, 6))
    for a, r in zip(todo, res):
        if r.get("event_summary") and "_error" not in r:
            done[a["aid"]] = r
    failed = {a["aid"]: r for a, r in zip(todo, res) if not r.get("event_summary")}
    rows = []
    for a in arts:
        if a["aid"] in done:
            rows.append(done[a["aid"]])
        elif a["aid"] in failed:
            rows.append(failed[a["aid"]])   # status=extract_failed + extract_error (reason kept)
        else:
            rows.append({**a, "status": "needs_enrichment" if not usable(a) else "extract_failed"})
    jl_write(out_p, rows)
    print("extract:", len(rows), "ok", sum(1 for r in rows if r.get("event_summary")), "failed", len(failed), "usage", llm.USAGE)
    for aid, r in list(failed.items())[:20]:
        print(f"  extract_failed {aid}: {r.get('extract_error')}")


def event_text(title: str, summary: str, entities, etype) -> str:
    return f"{title}\n{summary}\nEntities: {', '.join(entities or [])}\nEvent: {etype}"


# ----------------------------------------------------------------- 2 match
JUDGE_SYS = """Decide whether a NEW article belongs to one of the CANDIDATE events (the same real-world story).
SAME story includes: the original announcement/incident, coverage by other outlets, follow-up details (pricing, docs, API, patch,
exploitation, lawsuit filing on it), reactions, analysis, criticism, doubts or denials ABOUT that same happening.
DIFFERENT story: same company/product but a separate happening (a launch vs. a later jailbreak discovery; model A vs. model B release;
two different CVEs; two different papers or repositories; two different court cases).
Return JSON only: {"match": "<event_id or new>", "reason": "short"}"""

PARTITION_SYS = """You get a group of textually similar technology articles. Partition them into real-world STORIES.
Put articles together when they are about the same concrete happening, INCLUDING other outlets' coverage, follow-up details,
reactions, analysis, criticism, doubts or denials about it.
Keep apart: separate happenings that merely share a company/product/field (e.g. launch vs. later vulnerability; two different CVEs,
papers, repositories, court cases, product releases). Different GitHub repositories are always different stories.
Every article id must appear exactly once.
Return JSON only: {"events": [{"label": "short English story name", "article_ids": ["a..."]}]}"""

CANON_SYS = """Write the canonical description of one technology event from its member articles.
Return JSON only: {"title": "short English event title", "summary": "1-3 English sentences with the concrete facts known so far",
"entities": ["max 8"], "event_type": "<same vocabulary as articles>"}"""


def load_events() -> list[dict]:
    return jl_read(EVENTS)


def new_event_id(events: list[dict]) -> str:
    n = max([int(e["event_id"][1:]) for e in events] + [0]) + 1
    return f"E{n:05d}"


def stage_match(date: str) -> None:
    ext = [r for r in jl_read(V3 / date / "extracted.jsonl") if r.get("event_summary") and not r.get("is_noise")]
    events = load_events()
    if any(date in e.get("dates", []) for e in events):
        raise SystemExit(f"{date} already matched into event store; restore events backup before re-running")
    (V3 / "events").mkdir(parents=True, exist_ok=True)
    if EVENTS.exists():
        (V3 / "events" / f"events.before-{date}.jsonl").write_text(EVENTS.read_text())

    # order: primary first, then news, then community
    order = {"primary": 0, "news": 1, "community": 2}
    ext.sort(key=lambda r: order[r["source_class"]])
    A = embed([event_text(r["title"], r["event_summary"], r["entities"], r["event_type"]) for r in ext])
    log: list[dict] = []

    # Phase A: vs existing active events (parallel; events fixed during phase)
    active = [e for e in events if days_between(e["last_seen"], date) <= ACTIVE_DAYS or True]
    attach: dict[str, str] = {}
    if events:
        E = embed([event_text(e["title"], e["summary"], e["entities"], e["event_type"]) for e in events])
        sims = A @ E.T

        def judge_hist(i):
            order_idx = np.argsort(-sims[i])[:6]
            cands = [(events[j], float(sims[i, j])) for j in order_idx if sims[i, j] >= CAND_FLOOR]
            ents = {x.lower() for x in (ext[i]["entities"] or [])}
            gated = []
            for e, s in cands:
                recent = days_between(e["last_seen"], date) <= ACTIVE_DAYS
                overlap = bool(ents & {x.lower() for x in e["entities"]})
                if (recent and (overlap or s >= 0.80)) or (not recent and overlap and s >= 0.80):
                    gated.append((e, s))
            if not gated:
                return {"match": "new", "reason": "no gated candidate", "cands": [(e["event_id"], round(s, 3)) for e, s in cands]}
            r = ext[i]
            user = (f"NEW ARTICLE:\n{r['title']}\n{r['event_summary']}\nEntities: {r['entities']}\nType: {r['event_type']}\n\nCANDIDATES:\n"
                    + "\n".join(f"- {e['event_id']} (last seen {e['last_seen']}): {e['title']} — {e['summary']}" for e, _ in gated))
            out = llm.call_json(JUDGE_SYS, user)
            out["cands"] = [(e["event_id"], round(s, 3)) for e, s in gated]
            return out

        res = llm.parallel(list(range(len(ext))), judge_hist, WORKERS, "match-hist")
        # A call that still failed after llm.call's own retries (typically a 429 burst under 12 workers) gets one
        # slow second pass with few workers. What still fails is kept as a new event (conservative: worst case a
        # duplicate that curate can merge) and flagged llm_failed so the runner can count it against a tolerance.
        failed_idx = [i for i, r in enumerate(res) if not isinstance(r, dict) or "_error" in r]
        if failed_idx:
            print(f"[match-hist] retrying {len(failed_idx)} failed judgments with 2 workers after 30s", flush=True)
            time.sleep(30)
            again = llm.parallel(failed_idx, judge_hist, 2, "match-hist-retry")
            for i, r in zip(failed_idx, again):
                res[i] = r
        valid = {e["event_id"] for e in events}
        for i, r in enumerate(res):
            m = (r or {}).get("match", "new") if isinstance(r, dict) else "new"
            if m in valid:
                attach[ext[i]["aid"]] = m
            failed = not isinstance(r, dict) or "_error" in r
            log.append({"aid": ext[i]["aid"], "phase": "history", "decision": m if m in valid else "new",
                        "reason": (r or {}).get("reason") or (r or {}).get("_error"), "cands": (r or {}).get("cands"),
                        **({"llm_failed": True} if failed else {})})

    # Phase B: remaining articles -> candidate groups -> LLM partition
    rest = [i for i, r in enumerate(ext) if r["aid"] not in attach]
    idx_map = {i: k for k, i in enumerate(rest)}
    R = A[rest]
    S = R @ R.T
    parent = list(range(len(rest)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    ents = [{x.lower() for x in (ext[i]["entities"] or [])} for i in rest]
    for a in range(len(rest)):
        for b in np.where(S[a] >= GROUP_SIM)[0]:
            if b <= a:
                continue
            if S[a, b] >= GROUP_SIM_STRONG or (ents[a] & ents[b]):
                parent[find(a)] = find(int(b))
    comps: dict[int, list[int]] = {}
    for a in range(len(rest)):
        comps.setdefault(find(a), []).append(a)

    def split(members: list[int], th: float) -> list[list[int]]:
        if len(members) <= MAX_GROUP or th > 0.95:
            return [members]
        p = {m: m for m in members}

        def f(x):
            while p[x] != x:
                x = p[x]
            return x
        for x in members:
            for y in members:
                if y > x and S[x, y] >= th:
                    p[f(x)] = f(y)
        g: dict[int, list[int]] = {}
        for m in members:
            g.setdefault(f(m), []).append(m)
        out = []
        for sub in g.values():
            out += split(sub, th + 0.03)
        return out

    groups: list[list[int]] = []
    for c in comps.values():
        groups += split(c, GROUP_SIM + 0.03) if len(c) > MAX_GROUP else [c]
    multi = [g for g in groups if len(g) > 1]

    def partition(g):
        arts = [ext[rest[k]] for k in g]
        user = "\n".join(f"- {a['aid']}: {a['title']} — {a['event_summary']} (entities: {a['entities']})" for a in arts)
        ids = [a["aid"] for a in arts]
        bad = []
        # The model occasionally drops/duplicates/mistypes an id; retry before falling back to one-event-per-article.
        for attempt in range(3):
            prompt = user if not bad else user + f"\n\nYour previous answer listed ids {bad[-1]}; use each of {ids} exactly once."
            out = llm.call_json(PARTITION_SYS, prompt)
            got = [x for ev in out.get("events", []) for x in ev.get("article_ids", [])]
            if sorted(got) == sorted(ids):
                if attempt:
                    out["retries"] = attempt
                return out
            bad.append(got)
        return {"events": [{"label": a["title"], "article_ids": [a["aid"]]} for a in arts], "fallback": True,
                "invalid_attempts": bad}

    parts = llm.parallel(multi, partition, WORKERS, "partition")
    new_groups: list[list[str]] = []
    for g, p in zip(multi, parts):
        if not isinstance(p, dict) or "_error" in p:
            p = {"events": [{"article_ids": [ext[rest[k]]["aid"]]} for k in g], "fallback": True}
        for ev in p["events"]:
            new_groups.append(ev["article_ids"])
        log.append({"phase": "partition", "group": [ext[rest[k]]["aid"] for k in g], "result": p})
    for g in groups:
        if len(g) == 1:
            new_groups.append([ext[rest[g[0]]]["aid"]])

    by_aid = {r["aid"]: r for r in ext}
    ev_by_id = {e["event_id"]: e for e in events}
    touched = set()
    for aid, eid in attach.items():
        e = ev_by_id[eid]
        e["articles"].append(slim(by_aid[aid]))
        e["last_seen"] = date
        e.setdefault("dates", []).append(date) if date not in e.get("dates", []) else None
        e.setdefault("new_today", {})[date] = e.get("new_today", {}).get(date, []) + [aid]
        if e.get("status") == "closed":
            e["status"] = "active"
            e["reopened"] = date
        touched.add(eid)
    for ids in new_groups:
        eid = new_event_id(events)
        first = by_aid[ids[0]]
        e = {"event_id": eid, "title": first["title"], "summary": first["event_summary"],
             "entities": first["entities"] or [], "event_type": first["event_type"],
             "first_seen": date, "last_seen": date, "dates": [date], "status": "active",
             "articles": [slim(by_aid[a]) for a in ids], "new_today": {date: list(ids)},
             "writes": []}
        events.append(e)
        ev_by_id[eid] = e
        touched.add(eid)
    for e in events:
        if days_between(e["last_seen"], date) > ACTIVE_DAYS:
            e["status"] = "closed"
    jl_write(EVENTS, events)
    jl_write(V3 / date / "match_log.jsonl", log)
    json.dump(sorted(touched), open(V3 / date / "touched.json", "w"))
    print(f"match: articles={len(ext)} attached_to_existing={len(attach)} new_events={len(new_groups)} "
          f"multi_groups={len(multi)} touched={len(touched)} total_events={len(events)} usage={llm.USAGE}")


def slim(a: dict) -> dict:
    return {k: a.get(k) for k in ("aid", "url", "date", "title", "domain", "source_class", "event_summary",
                                   "entities", "event_type", "key_facts", "legacy_section", "legacy_issue_id")}


# ----------------------------------------------------------------- failed-item retry
def is_failed(r) -> bool:
    return not isinstance(r, dict) or "_error" in r


def retry_failed(items: list, results: list, fn, label: str, workers: int = 2, wait: float = 30.0) -> list:
    """Second, slow pass over only the items whose LLM call still failed (typically a 429 burst under many workers)."""
    idx = [i for i, r in enumerate(results) if is_failed(r)]
    if not idx:
        return results
    print(f"[{label}] retrying {len(idx)} failed items with {workers} workers after {wait:.0f}s", flush=True)
    time.sleep(wait)
    again = llm.parallel([items[i] for i in idx], fn, workers, label + "-retry")
    for i, r in zip(idx, again):
        results[i] = r
    return results


# ----------------------------------------------------------------- 3 refresh
def stage_refresh(date: str) -> None:
    events = load_events()
    touched = set(json.load(open(V3 / date / "touched.json")))
    todo = [e for e in events if e["event_id"] in touched and len(e["articles"]) > 1]

    def work(e):
        user = "\n".join(f"- [{a['date']}] {a['title']} — {a['event_summary']} facts={a.get('key_facts')}" for a in e["articles"][-15:])
        return llm.call_json(CANON_SYS, user)

    res = llm.parallel(todo, work, WORKERS, "refresh")
    res = retry_failed(todo, res, work, "refresh")
    # A refresh that still failed keeps the event's existing title/summary (they were valid before); it is only logged.
    failed = [e["event_id"] for e, r in zip(todo, res) if is_failed(r)]
    for e, r in zip(todo, res):
        if not is_failed(r):
            e.update({k: r[k] for k in ("title", "summary", "entities", "event_type") if r.get(k)})
    jl_write(EVENTS, events)
    jl_write(V3 / date / "refresh_failed.jsonl", [{"event_id": i, "llm_failed": True} for i in failed])
    print("refresh:", len(todo), "failed", len(failed), "usage", llm.USAGE)


# ----------------------------------------------------------------- 4 novelty
NOVELTY_SYS = """You compare the article we ALREADY PUBLISHED about an event with NEW articles that arrived today.
Did today's articles add anything a reader of the published article does not know yet? Count as new: new numbers, results, benchmarks,
prices, dates, affected versions, exploitation, official confirmation of a previous rumor, a significant new party, a reversal/denial,
new availability/regions. Mere re-reporting or rephrasing is not new. If unsure, answer true.
Return JSON only: {"has_new_facts": true/false, "new_facts": ["..."], "reason": "short"}"""


def stage_novelty(date: str) -> None:
    events = load_events()
    touched = set(json.load(open(V3 / date / "touched.json")))
    todo = [e for e in events if e["event_id"] in touched and e.get("writes")]

    def work(e):
        new_ids = set(e["new_today"].get(date, []))
        new = [a for a in e["articles"] if a["aid"] in new_ids]
        last = e["writes"][-1]
        user = (f"PUBLISHED ({last['date']}):\n{last['headline']}\n{last['body']}\n\nNEW ARTICLES TODAY:\n"
                + "\n".join(f"- {a['title']} — {a['event_summary']} facts={a.get('key_facts')}" for a in new))
        return llm.call_json(NOVELTY_SYS, user)

    res = llm.parallel(todo, work, WORKERS, "novelty")
    res = retry_failed(todo, res, work, "novelty")
    rows = []
    for e, r in zip(todo, res):
        r = r if isinstance(r, dict) else {"_error": "none"}
        if "_error" in r:
            # Still failed after the retry pass: assume there is something new (we would rather over-include than drop it).
            r = {"has_new_facts": True, "reason": "judge failed after retry; assumed new", "llm_failed": True,
                 "llm_error": str(r["_error"])[:200]}
        rows.append({"event_id": e["event_id"], "title": e["title"], **r})
    jl_write(V3 / date / "novelty.jsonl", rows)
    print("novelty:", len(rows), "new_facts", sum(1 for r in rows if r.get("has_new_facts")), "usage", llm.USAGE)


# ----------------------------------------------------------------- 5 select
SELECT_SYS = """You score technology events for one reader's daily brief. Score ONLY interest and impact, using the anchors below. Be strict: most events are 1.
Reader interests (core): AI & agents & frontier models/services; cybersecurity & offensive security; local/open models;
infrastructure/cloud/compute (major outages, k8s, AI platform ops, datacenter/power); semiconductors; Elon Musk-related technology (by actual field).
Hard exclusions (set excluded_by): generic hardware/homelab gadget news; Korean community reactions; routine inference-engine/serving speed tweaks
(unless industry-wide); ads/coupons/deals; routine repetitive product updates with no capability/price/licence/security change.

interest (0-3): 3 = squarely a core interest (frontier/agent AI, security/offsec, open models, cloud/infra/chips, Musk-company tech);
  2 = adjacent (AI tooling, AI applied in a domain, developer infrastructure); 1 = general IT/product/business; 0 = unrelated.
impact (0-3), judged by what changes for practitioners or the industry:
  3 = frontier-lab model/service release; actively exploited critical vulnerability in widely used software (CISA KEV, zero-day); major safety incident at a frontier lab;
      major regulation or court ruling on AI; major outage; multi-billion chip/datacenter deal.
  2 = notable open-weights model release; concrete demonstrated attack/exploit/PoC in popular software; significant agent-safety or security research from a major lab;
      meaningful policy/business event (e.g. AI CEO meets the US president, new industry standards body, major partnership); major-vendor change to price/limits/capabilities;
      a tool/repo with supported recent traction or a meaningful new capability/licensing/security change; major-org ownership alone is not news.
      a deep technical write-up of a real system by the people who built or broke it (engineering-blog architecture/training-at-scale posts, incident postmortems,
      vulnerability analyses with exploit or root-cause detail, CVE writeups) = 2, because practitioners learn transferable technique from it.
      Opinion pieces, recaps, tutorials, 'what is X' explainers and personal essays stay 1.
  1 = typical incremental arXiv paper; small SaaS/product landing page, Show HN, docs page, personal blog or opinion; minor feature update; single anecdote.
  Conference agenda / event-registration notices (e.g. Microsoft Ignite pre-day schedule and planned tracks) are impact 1 unless the source actually announces a new product capability, a demonstrated technique, a fix, or a material policy. Do not inflate an agenda into a technology change.
  0 = no substantive change.
Papers: impact 2 only if from a major lab (OpenAI, Anthropic, Google/DeepMind, Meta, Microsoft, NVIDIA, DeepSeek, Apple) OR it demonstrates a concrete attack/result on widely used systems
OR it is already widely discussed. Otherwise impact 1, even when on a core topic.
Repos: impact 2 requires an actually supported newly rising project, recent traction change, new release, or meaningful capability/licensing/security change. A perennial repository README, installation guide, or listing is impact 1 even if from a major org or highly starred. State the concrete new development, not that the project is famous or useful.
Credible reports of an unreleased product/strategy from a frontier lab (OpenAI, Anthropic, Google, Meta, xAI) or a Musk company (Tesla, SpaceX, xAI, Neuralink, Starlink) = impact 2;
a strategic pivot, major launch, or major deal by such a company = impact 3.
Examples: 'OpenAI is preparing an always-on ChatGPT assistant' = interest 3, impact 2. 'SpaceX pivots away from space business' = interest 3, impact 3.
'Amodei to meet Trump at White House dinner' = interest 3, impact 2. 'Google/OpenAI/Anthropic form frontier-AI standards body' = interest 3, impact 2-3.
'NVIDIA releases Nemotron 3 open model' = interest 3, impact 2. 'How we built our LLMs with FP8 for training and inference' (company engineering blog) = interest 3, impact 2.
'My Ansible plugin had a jail escape: CVE analysis' = interest 2-3, impact 2. 'What is an agent harness?' (explainer) = impact 1.
'Squint - explain anything on your screen' (indie app) = interest 1, impact 1.
'copyparty portable file server' = interest 1, impact 1. 'Docs overview page' = impact 1. 'Yet another prompt-injection arXiv paper from a university lab' = interest 3, impact 1.
Return JSON only:
{"interest":0-3,"impact":0-3,"section":"01".."10","excluded_by":null or short rule name,"reason":"one short sentence"}
Sections: 01 AI/agents/frontier, 02 security, 03 local/open models, 04 infra/cloud/compute, 05 semiconductors, 06 dev tools/open source/GitHub,
07 research/papers/benchmarks, 08 robotics/applied/other emerging tech, 09 community-only early signals, 10 business/governance."""


def is_paper_or_repo(e: dict) -> bool:
    et = (e.get("event_type") or "").lower()
    return any(k in et for k in ("research", "paper", "repo", "github", "benchmark", "study", "open_source", "open-source", "open source"))


def code_novelty(e: dict, date: str, nv: dict | None) -> int:
    prior = [w for w in e.get("writes", []) if w["date"] < date]
    if prior:
        return 1 if (nv and nv.get("has_new_facts")) else 0
    # arXiv ids encode YYMM: a paper submitted months ago that resurfaced is not news
    for a in e["articles"]:
        m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{2})(\d{2})\.", a["url"])
        if m:
            age = (int(date[2:4]) - int(m.group(1))) * 12 + int(date[5:7]) - int(m.group(2))
            if age >= 3:
                return 0
    return 2 if e["first_seen"] >= date else 1


def code_evidence(e: dict) -> int:
    arts = e["articles"]
    non_comm = {a["domain"] for a in arts if a["source_class"] != "community"}
    if any(a["source_class"] == "primary" for a in arts) or len(non_comm) >= 2:
        return 2
    return 1 if non_comm else 0


def select_rule(ax: dict, excluded, paper_repo: bool) -> bool:
    """interest/impact anchored by the rubric; novelty & evidence are computed from data, not by the model.
    Core-interest events need impact >= 2; adjacent-interest (2) events need impact 3."""
    if excluded or ax["interest"] < 1 or ax["novelty"] < 1:
        return False
    if ax["evidence"] < 1:
        # evidence 0 means community-only sources.  A community-only signal is admitted only when it is a big early issue
        # (core interest AND impact >= 2); it is written as an unverified early signal.  Small threads stay out.
        return ax["interest"] >= 3 and ax["impact"] >= 2
    if ax["impact"] >= 3:
        return ax["interest"] >= 2
    return ax["impact"] >= 2 and ax["interest"] >= 3


def stage_select(date: str) -> None:
    events = load_events()
    touched = set(json.load(open(V3 / date / "touched.json")))
    novelty = {r["event_id"]: r for r in jl_read(V3 / date / "novelty.jsonl")}
    cands, rows = [], []
    for e in events:
        if e["event_id"] not in touched:
            continue
        nv = novelty.get(e["event_id"])
        if nv is not None and not nv.get("has_new_facts"):
            rows.append({"event_id": e["event_id"], "title": e["title"], "selected": False, "stage": "novelty",
                         "reason": nv.get("reason")})
            continue
        cands.append(e)

    def work(e):
        doms = sorted({a["domain"] for a in e["articles"]})
        user = (f"EVENT: {e['title']}\n{e['summary']}\nType: {e['event_type']}\nEntities: {e['entities']}\n"
                f"Sources ({len(doms)} domains): {doms[:10]}\n"
                + ("Continuing event; new facts today: " + json.dumps(novelty[e['event_id']].get('new_facts'), ensure_ascii=False) if e['event_id'] in novelty else "")
                + "\nArticle titles:\n" + "\n".join(f"- {a['title']}" for a in e["articles"][-6:]))
        return {"id": e["event_id"], "prompt": user}

    from batched_steps import batch_score
    packets = [work(e) for e in cands]
    res = batch_score(packets, SELECT_SYS, llm.call_json, llm.parallel, min(WORKERS, 6))
    if any(is_failed(r) for r in res):
        redo = [i for i, r in enumerate(res) if is_failed(r)]
        print(f"[select] retrying {len(redo)} failed items with 2 workers after 30s", flush=True)
        time.sleep(30)
        again = batch_score([packets[i] for i in redo], SELECT_SYS, llm.call_json, llm.parallel, 2)
        for i, r in zip(redo, again):
            res[i] = r
    for e, r in zip(cands, res):
        err = not isinstance(r, dict) or "_error" in r
        r = r if not err else {}
        ax = {"interest": int(r.get("interest") or 0), "impact": int(r.get("impact") or 0),
              "novelty": code_novelty(e, date, novelty.get(e["event_id"])), "evidence": code_evidence(e)}
        total = sum(ax.values())
        pr = is_paper_or_repo(e)
        sel = False if err else select_rule(ax, r.get("excluded_by"), pr)
        section = r.get("section")
        if sel and all(a["source_class"] == "community" for a in e["articles"]):
            section = "09"
        doms = {a["domain"] for a in e["articles"]}
        rows.append({"event_id": e["event_id"], "title": e["title"], "selected": sel, "stage": "score", **ax, "total": total,
                     "final": total, "paper_or_repo": pr, "section": section, "excluded_by": r.get("excluded_by"),
                     "reason": "scoring failed after retry; event excluded" if err else r.get("reason"),
                     **({"llm_failed": True} if err else {}),
                     "n_articles": len(e["articles"]), "n_domains": len(doms)})
    jl_write(V3 / date / "selection.jsonl", rows)
    s = [r for r in rows if r["selected"]]
    from collections import Counter
    print("select:", len(rows), "selected", len(s), Counter(r["section"] for r in s), "usage", llm.USAGE)


# ----------------------------------------------------------------- 6 write (+ style review + fact check, rewrite loop)
def genre(e: dict) -> str:
    if e.get("articles") and all(a.get("source_class") == "community" for a in e["articles"]):
        return "community"
    et = (e.get("event_type") or "").lower()
    # event_type is authoritative when it names a genre; title keywords only break ties
    if any(k in et for k in ("research", "paper", "benchmark", "study", "position")):
        return "paper"
    if "model_release" in et or "model release" in et:
        return "model"
    if any(k in et for k in ("vuln", "cve", "exploit")):
        return "vuln"
    if any(k in et for k in ("incident", "breach", "misbehav", "outage", "hack", "attack", "theft", "leak", "pause", "rogue")):
        return "incident"
    if any(k in et for k in ("repo", "open_source", "open-source", "open source", "github", "software", "release", "project", "feature", "product")):
        return "tool"
    t = et + " " + (e.get("title") or "").lower()
    if any(k in t for k in ("vuln", "cve", "exploit", "rce", "injection flaw", "zero-day", "0-day")):
        return "vuln"
    if any(k in t for k in ("incident", "breach", "misbehav", "outage", "hack", "attack", "theft", "leak", "pause", "rogue")):
        return "incident"
    if "model_release" in t or "model release" in t:
        return "model"
    if any(k in t for k in ("research", "paper", "benchmark", "study", "arxiv")):
        return "paper"
    if any(k in t for k in ("repo", "open_source", "open-source", "open source", "github", "software", "release", "project", "feature", "product")):
        return "tool"
    return "news"


# ---- prompts (v4, 2026-10-11): kept as plain files so they can be edited / rolled back without touching code.
# Previous (v3, Korean) prompts: backups/prompts-20261011-230638/work_v3/pipeline.py
PROMPT_DIR = Path(__file__).parent / "prompts"


def _prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


WRITER_SYS = _prompt("writer_sys.txt")
REVIEW_SYS = _prompt("review_sys.txt")
WRITER_LANGUAGE = "en"
_GENRE_COMMON = _prompt("genre_common.txt")
GENRE_GUIDE = {g: _GENRE_COMMON + "\n\n" + _prompt(f"genre_{g}.txt")
               for g in ("incident", "vuln", "model", "paper", "tool", "community", "news")}

RUN_YEAR: set = set()
BANNED = [r"보여준다", r"시사한다", r"중요하다는 점", r"밝히지 않았", r"드러나지 않았", r"알려지지 않았", r"설명하지 않았",
          r"(?i)\bThis (?:highlights|demonstrates|suggests)\b", r"(?i)\bThe key takeaway\b", r"(?i)\bWhat makes this important\b",
          r"(?i)\bThe common thread\b", r"(?i)\bThis is significant because\b"]
LABEL_LINE = re.compile(r"^\s*(\*\*[^*]{1,30}\*\*|#{1,4}\s.+|[가-힣A-Za-z ]{1,12}:)\s*$", re.M)
PRIMARY_DOC = re.compile(r"^https?://([a-z0-9-]+\.)*(arxiv\.org|github\.com|huggingface\.co|openai\.com|anthropic\.com|deepmind\.google|"
                         r"blog\.google|research\.google|cisa\.gov|nvd\.nist\.gov|msrc\.microsoft\.com|nvidia\.com|meta\.com|ai\.meta\.com|"
                         r"mistral\.ai|x\.ai|googleprojectzero\.blogspot\.com|security\.googleblog\.com|aws\.amazon\.com)(/|$)", re.I)


def style_problems(o: dict) -> list[str]:
    """Style hints only. They are recorded as warnings and never rewrite, fail, or withhold an article."""
    issues = []
    body = (o.get("lead") or "") + "\n" + (o.get("body") or "")
    plain = TAG.sub("", body)
    for pat in BANNED:
        if re.search(pat, plain):
            issues.append(f"Rewrite or remove the filler expression '{pat}'; use a concrete factual sentence.")
    for m in LABEL_LINE.finditer(o.get("body") or ""):
        issues.append(f"Remove the heading/label '{m.group(0).strip()}' and use paragraph transitions.")
    for g in o.get("glossary") or []:
        if [x for x in _claim_values(g.get("definition", "")) if x[1] >= 2]:
            issues.append(f"Remove numbers and event facts from the glossary definition of '{g.get('term')}'.")
    return issues


def fact_problems(o: dict) -> list[dict]:
    global RUN_YEAR
    RUN_YEAR = {float(o["date"][:4])} if o.get("date") else set()
    srcs = {s["tag"]: s for s in o.get("sources", [])}
    # A number missing from the tagged sources but present in another source of this event is a citation slip, not an invented figure.
    all_vals = _source_values(" ".join((s.get("text") or "") + " " + (s.get("title") or "") for s in o.get("sources", [])))
    problems = []
    if not o.get("headline"):
        problems.append({"type": "missing_headline"})
    for field in ("lead", "body"):
        for para in (o.get(field) or "").split("\n"):
            para_n = re.sub(r"^\s*\d+[.)]\s+", "", para)  # list item numbers are not claims
            pending, assigned = [], []
            for snt in _sentences(para_n):
                tags = TAG.findall(snt)
                if tags:
                    assigned += [(q, tags) for q in pending] + [(snt, tags)]
                    pending = []
                else:
                    pending.append(snt)
            for q in pending:
                problems.append({"type": "untagged", "sentence": q[:160]})
            for snt, tags in assigned:
                if any(f"S{t}" not in srcs for t in tags):
                    problems.append({"type": "unknown_tag", "sentence": snt[:160]})
                    continue
                corpus = " ".join((srcs[f"S{t}"]["text"] + " " + (srcs[f"S{t}"]["title"] or "")) for t in tags)
                for clock in re.findall(r"\b\d{1,2}:\d{2}:\d{2}\b", snt):
                    if clock not in corpus:
                        problems.append({"type": "time_not_verbatim", "clock": clock, "sentence": snt[:160]})
                vals = _source_values(corpus) | RUN_YEAR | all_vals
                clean = re.sub(r"`[^`]*`", " ", snt)
                for raw, v in _claim_values(clean):
                    if v < 2 and "." not in raw:
                        continue
                    if not _value_supported(v, vals):
                        problems.append({"type": "number_not_in_source", "number": raw, "sentence": snt[:160]})
                low = re.sub(r"\s+", " ", corpus).lower()
                for code in re.findall(r"`([^`]+)`", snt):
                    if re.sub(r"\s+", " ", code).lower() not in low:
                        problems.append({"type": "code_not_verbatim", "code": code, "sentence": snt[:160]})
    return problems


def fact_feedback(problems: list[dict]) -> list[str]:
    out = []
    for p_ in problems[:12]:
        t = p_["type"]
        if t == "number_not_in_source":
            out.append(f"The number '{p_['number']}' is not in the cited source (sentence: {p_['sentence'][:80]}). Use the value from the source, cite the correct source tag, or remove it.")
        elif t == "time_not_verbatim":
            out.append(f"The time {p_['clock']} is not in the sources. Do not convert a time range into a single timestamp; keep the original time and time zone.")
        elif t == "code_not_verbatim":
            out.append(f"`{p_['code']}` does not match the source text character for character. Quote it exactly as in the source or describe it without backticks.")
        elif t == "untagged":
            out.append(f"Sentence without a source tag: {p_['sentence'][:80]}")
        elif t == "unknown_tag":
            out.append(f"Source tag that does not exist: {p_['sentence'][:80]}")
        elif t == "glossary_has_number":
            out.append(f"Remove numbers and event facts from the glossary definition of '{p_['term']}'.")
        else:
            out.append(t)
    return out


FETCH_CACHE = V3 / "fetch_cache.jsonl"
_fetch_cache: dict | None = None


_env_cache: dict | None = None
_FETCH_LOCK = threading.Lock()


def _env() -> dict:
    global _env_cache
    if _env_cache is None:
        _env_cache = {}
        for l in (ROOT / ".env").read_text().splitlines():
            if "=" in l and not l.strip().startswith("#"):
                k, v = l.strip().split("=", 1)
                _env_cache[k] = v.strip().strip('"').strip("'")
    return _env_cache


def clean_markdown(md: str) -> str:
    """Crawl4AI markdown -> readable text: drop images, unwrap links, drop nav-ish short lines."""
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", md)
    md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)
    lines = []
    for ln in md.splitlines():
        t = ln.strip(" *#>-\t")
        if len(t) < 40 and not re.search(r"[.!?:]$", t):
            continue
        lines.append(re.sub(r"\s+", " ", ln.strip()))
    return "\n".join(lines)


def crawl_text(url: str) -> str:
    env = _env()
    base, tok = env.get("CRAWL4AI_URL", "").rstrip("/"), env.get("CRAWL4AI_API_TOKEN", "")
    if not base:
        return ""
    req = urllib.request.Request(base + "/crawl", data=json.dumps({"urls": [url]}).encode(),
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    for attempt in range(3):  # the service queues heavy pages; retry with backoff before giving up
        try:
            d = json.load(urllib.request.urlopen(req, timeout=120))
            r = (d.get("results") or [{}])[0]
            if r.get("success"):
                m = r.get("markdown")
                raw = m if isinstance(m, str) else ((m or {}).get("fit_markdown") or (m or {}).get("raw_markdown") or "")
                txt = clean_markdown(raw)
                if len(txt) > 300:
                    return txt
            elif "anti-bot" in str(r.get("error_message") or "").lower():
                return ""  # blocked by Cloudflare challenge; retrying will not help
        except Exception:
            pass
        time.sleep(3 * (attempt + 1))
    return ""


def fetch_fuller(url: str, text: str, kind: str = "news") -> str:
    if os.environ.get("V3_OFFLINE_SOURCES") == "1":
        retained = next((r for r in jl_read(V3 / "archived_source_texts.jsonl") if r.get("url") == url), None)
        if retained and ("arxiv.org/" in url or len(retained.get("text", "")) > len(text)):
            return retained["text"]
        return text
    """Replace a truncated extraction with fuller text: arXiv abstract endpoint, else Crawl4AI for short non-community pages."""
    global _fetch_cache
    m = re.match(r"https?://arxiv\.org/(?:abs|pdf|html)/([0-9]{4}\.[0-9]{4,5})", url)
    short = len(text) < 1500 and kind != "community"
    if not (m and len(text) <= 3500) and not short:
        return text
    with _FETCH_LOCK:
        if _fetch_cache is None:
            _fetch_cache = {r["url"]: r["text"] for r in jl_read(FETCH_CACHE) if r["text"]}
    if url not in _fetch_cache:
        got = ""
        if m:
            try:
                req = urllib.request.Request(f"https://export.arxiv.org/abs/{m.group(1)}", headers={"User-Agent": "Mozilla/5.0"})
                page = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
                ab = re.search(r'<blockquote class="abstract[^>]*>(.*?)</blockquote>', page, re.S)
                ti = re.search(r'<h1 class="title[^>]*>(.*?)</h1>', page, re.S)
                if ab:
                    clean = lambda x: html.unescape(re.sub(r"<[^>]+>", " ", x)).replace("Abstract:", "").strip()
                    got = ((clean(ti.group(1)).replace("Title:", "").strip() + "\n") if ti else "") + re.sub(r"\s+", " ", clean(ab.group(1)))
            except Exception:
                got = ""
        else:
            got = crawl_text(url)[:30000]
        if got:  # never cache failures
            with _FETCH_LOCK:
                _fetch_cache[url] = got
                with FETCH_CACHE.open("a") as f:
                    f.write(json.dumps({"url": url, "text": got}, ensure_ascii=False) + "\n")
    got = _fetch_cache.get(url, "")
    return got if got and len(got) > max(len(text) * 1.5, len(text) + 300) else text


SEARCH_CACHE = V3 / "search_cache.jsonl"
_search_cache: dict | None = None
SOCIAL = ("reddit.com", "youtube.com", "youtu.be", "twitter.com", "x.com", "instagram.com", "facebook.com", "linkedin.com",
          "news.ycombinator.com", "tiktok.com", "pinterest.com", "quora.com", "wikipedia.org")


THRESHOLDS = Path(__file__).parent / "thresholds.json"
SAME_SYS = """You label search results for one technology EVENT (a specific real-world happening).
"same" = the page IS about this one happening: the original announcement/incident, another outlet's article reporting it, a follow-up with new details,
analysis or reaction whose main subject is this happening.
"different" =
 - another happening that only shares the company, product or topic (another release, CVE, paper, court case, incident),
 - an aggregate page that merely MENTIONS the event among many items: newsletters/digests/"weekly roundups", tag/category/archive/listing pages, homepages,
   search or "Sign in" pages, generic explainers, link collections,
 - a page whose main subject is something else.
Judge from the page text when given, otherwise from title and snippet. When the page covers several stories and this event is one of them, answer "different".
Return JSON only: {"labels": [{"i": <int>, "same": true/false}]}"""
_SAME_CACHE = V3 / "same_event_cache.jsonl"
_same_cache: dict | None = None


def load_thresholds() -> dict:
    if not THRESHOLDS.exists():
        raise RuntimeError("thresholds.json missing: run work/v3/threshold_loop.py fit (G3) before enabling extra sources")
    return json.loads(THRESHOLDS.read_text())


def same_event(e: dict, cands: list[dict]) -> list[dict]:
    """Luna makes the final same-event call on candidates the embedding filter let through (cached per event+url)."""
    global _same_cache
    with _FETCH_LOCK:
        if _same_cache is None:
            _same_cache = {r["k"]: r["same"] for r in jl_read(_SAME_CACHE)}
    keys = [e["event_id"] + "|" + c["url"] for c in cands]
    todo = [i for i, k in enumerate(keys) if k not in _same_cache]
    if todo:
        user = f"EVENT: {e['title']}\n{e['summary']}\nEntities: {', '.join(e['entities'] or [])}\n\nRESULTS:\n" + "\n\n".join(
            f"[{n}] {cands[i]['title']} ({cands[i]['domain']})\n{cands[i]['text'][:1800]}" for n, i in enumerate(todo))
        out = llm.call_json(SAME_SYS, user, timeout=900)
        got = {x["i"]: bool(x["same"]) for x in out["labels"]}
        with _FETCH_LOCK:
            with _SAME_CACHE.open("a") as f:
                for n, i in enumerate(todo):
                    v = got.get(n, False)
                    _same_cache[keys[i]] = v
                    f.write(json.dumps({"k": keys[i], "same": v}) + "\n")
    return [c for c, k in zip(cands, keys) if _same_cache.get(k)]


def extra_sources(e: dict, have_urls: set[str], have_domains: set[str], k: int = 3) -> list[dict]:
    """Independent reporting for a thin event: SearXNG -> embedding pre-filter (fitted thresholds) -> crawl -> Luna same-event check."""
    if os.environ.get("V3_OFFLINE_SOURCES") == "1":
        return []
    global _search_cache
    th = load_thresholds()
    with _FETCH_LOCK:
        if _search_cache is None:
            _search_cache = {r["key"]: r["hits"] for r in jl_read(SEARCH_CACHE)}
    key = e["event_id"] + "|" + e["title"][:80]
    if key not in _search_cache:
        hits = []
        base = _env().get("SEARXNG_URL", "").rstrip("/")
        if base:
            q = re.sub(r"\s*[|\-–—]\s*[^|\-–—]{2,40}$", "", e["title"]).strip()
            try:
                url = base + "/search?" + urllib.parse.urlencode({"q": q, "format": "json"})
                d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=40))
                hits = [{"url": r["url"], "title": r.get("title", ""), "snippet": r.get("content", "")} for r in d.get("results", [])[:15] if r.get("url")]
            except Exception:
                hits = []
        with _FETCH_LOCK:
            _search_cache[key] = hits
            if hits:
                with SEARCH_CACHE.open("a") as f:
                    f.write(json.dumps({"key": key, "hits": hits}, ensure_ascii=False) + "\n")
    cands = [h for h in _search_cache[key] if h["url"] not in have_urls and domain(h["url"]) not in have_domains
             and not domain_is(domain(h["url"]), SOCIAL)]
    if not cands:
        return []
    q = embed([event_text(e["title"], e["summary"], e["entities"], e["event_type"])])[0]
    m = embed([(h["title"] + ". " + h["snippet"])[:500] for h in cands])
    sims = m @ q
    order = [i for i in np.argsort(-sims) if sims[i] >= th["level1"]["threshold"]]
    crawled, seen = [], set()
    for i in order[:8]:
        h = cands[i]
        if domain(h["url"]) in seen:
            continue
        txt = fetch_fuller(h["url"], "", "news")
        if len(txt) < 1200:
            continue
        if th["level2"].get("enabled") and float(embed([txt[:1500]])[0] @ q) < th["level2"]["threshold"]:
            continue
        seen.add(domain(h["url"]))
        crawled.append({"url": h["url"], "title": h["title"], "domain": domain(h["url"]), "text": txt})
    return same_event(e, crawled)[:k]


def _searx_hits(query: str) -> list[dict]:
    base = _env().get("SEARXNG_URL", "").rstrip("/")
    if not base:
        return []
    url = base + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=40))
    return [{"url": r["url"], "title": r.get("title", ""), "snippet": r.get("content", "")} for r in d.get("results", [])[:15] if r.get("url")]


GAP_JUDGE_SYS = """너는 기술 브리핑 편집자다. 한 사건의 기사가 어떤 기술적 메커니즘을 언급만 하고 설명하지 않았다. 아래 CANDIDATES가 그 메커니즘을 **실제로 설명하는 페이지**인지 가려낸다.
explains=true: 같은 제품·회사의 메커니즘(작동 방식, 구조, 격리·인증·권한 방식, 설계 근거)을 구체적으로 설명하는 공식 문서·도움말·엔지니어링/보안/연구 블로그, 또는 같은 제품을 직접 분석한 독립 기술 기사.
explains=false: 다른 제품·다른 사건, 광고성 소개나 가격·사용법 위주의 글, 여러 소식을 훑는 모음·뉴스레터·목록 페이지, 메커니즘을 한 줄 언급만 하는 글, 로그인·검색 페이지.
페이지 본문(text)을 기준으로 판단한다. JSON만: {"labels":[{"i":<int>,"explains":true|false}]}"""


def gap_explains(e: dict, cands: list[dict], *, call=None) -> list[dict]:
    """Keep the candidates that explain the gap's mechanism. A failure keeps nothing (and says so)."""
    if not cands:
        return []
    call = call or (lambda system, user, **k: llm.call_json(system, user, **k))
    mech = cands[0].get("mechanism", "")
    user = (f"EVENT: {e['title']}\n{e['summary']}\nEntities: {', '.join(e.get('entities') or [])}\n\n"
            f"MECHANISM TO EXPLAIN: {mech}\n\nCANDIDATES:\n" +
            "\n\n".join(f"[{n}] {c['title']} ({c['domain']})\n{c['text'][:2500]}" for n, c in enumerate(cands)))
    try:
        out = call(GAP_JUDGE_SYS, user, timeout=900)
    except Exception as exc:
        write_progress(e["event_id"], f"gap judge failed: {str(exc)[:120]}")
        return []
    if not isinstance(out, dict) or not isinstance(out.get("labels"), list):
        write_progress(e["event_id"], "gap judge returned no labels")
        return []
    keep = {x["i"] for x in out["labels"] if isinstance(x, dict) and x.get("explains") is True and isinstance(x.get("i"), int)}
    return [c for n, c in enumerate(cands) if n in keep]


def gap_sources(e: dict, gaps: list[dict], have_urls: set[str], have_domains: set[str], *, search=None, fetch=None, judge=None, per_gap: int = 2, total: int = 4) -> list[dict]:
    """Pages that may explain a mechanism the sources only mention. Query = the gap's own search query (not the event
    title); candidates must be a different outlet, long enough to explain anything, and pass the gap judge
    ("does this page explain the mechanism"). Every gap gets a fair share; every failure is logged."""
    if os.environ.get("V3_OFFLINE_SOURCES") == "1" or not gaps:
        return []
    search = search or _searx_hits
    fetch = fetch or (lambda u: fetch_fuller(u, "", "news"))
    judge = judge or gap_explains
    eid = e["event_id"]
    per, taken = [], set(have_domains)
    for g in gaps:
        try:
            hits = search(g["query"])
        except Exception as exc:
            write_progress(eid, f"gap search failed: {str(exc)[:120]} | {g['query'][:80]}")
            per.append([])
            continue
        crawled = []
        for h in hits:
            dm = domain(h["url"])
            if h["url"] in have_urls or dm in taken or domain_is(dm, SOCIAL):
                continue
            txt = fetch(h["url"])
            if len(txt or "") < 1200:
                continue
            taken.add(dm)
            crawled.append({"url": h["url"], "title": h["title"], "domain": dm, "text": txt, "mechanism": g["mechanism"]})
            if len(crawled) >= 4:
                break
        kept = judge(e, crawled)[:per_gap] if crawled else []
        write_progress(eid, f"gap '{g['mechanism'][:40]}': {len(hits)} hit(s), {len(crawled)} fetched, kept {len(kept)}")
        per.append(kept)
    out = []
    for rank in range(per_gap):  # round-robin so the first gap cannot use the whole cap
        for kept in per:
            if rank < len(kept) and len(out) < total:
                out.append(kept[rank])
    return out


GAP_SYS = """너는 기술 브리핑 편집자다. 아래 SOURCES는 한 사건에 대한 기사 원문이다.
소스가 어떤 기술적 메커니즘(작동 방식, 구조, 프로토콜, 격리·인증 방식 등)을 **언급만 하고 설명하지 않은** 곳을 찾는다.
기술 독자가 '그게 어떻게 되는 건데?'라고 물을 만한 것만 고른다. 가격, 순위, 일정, 인물 발언, 이미 설명된 내용은 제외한다.
각 항목은 소스에 실제로 있는 문장을 quote에 그대로 옮기고, 공식 발표·기술 문서·기술 블로그에서 답을 찾을 검색어(query)를 영어로 쓴다.
없으면 빈 목록. 지어내지 마라. JSON만: {"gaps":[{"mechanism":"...","quote":"소스 원문 그대로","query":"..."}]}"""


def find_mechanism_gaps(srcs: list[dict], *, call=None, max_gaps: int = 2) -> list[dict]:
    """Mechanisms a non-community source mentions without explaining. A failure means 'no gaps', never a crash."""
    usable = [s for s in srcs if s.get("kind") != "community" and (s.get("text") or "").strip()]
    if not usable:
        return []
    call = call or (lambda system, user, **k: llm.call_json(system, user, **k))
    user = "SOURCES:\n\n" + "\n\n".join(f"[{s['tag']}] {s.get('domain', '')}\n{s['text'][:12000]}" for s in usable)
    try:
        out = call(GAP_SYS, user, timeout=600)
    except Exception:
        return []
    if not isinstance(out, dict) or "_error" in out or not isinstance(out.get("gaps"), list):
        return []
    haystack = re.sub(r"\s+", " ", " ".join(s["text"] for s in usable))
    grounded = []
    for g in out["gaps"]:
        if not isinstance(g, dict) or not all(isinstance(g.get(k), str) and g[k].strip() for k in ("mechanism", "quote", "query")):
            continue
        if re.sub(r"\s+", " ", g["quote"]).strip() not in haystack:
            continue  # the model must point at a real sentence
        grounded.append({"mechanism": g["mechanism"].strip(), "quote": g["quote"].strip(), "query": g["query"].strip()})
    return grounded[:max_gaps]


def related_primary_docs(e: dict, events: list[dict], date: str, ext_cache: dict, k: int = 2) -> list[dict]:
    """Primary technical documents already in the store under a *different* event (e.g. an incident report
    split from the news coverage). Requires high embedding similarity and entity overlap."""
    mine = {a["url"] for a in e["articles"]}
    ents = {x.lower() for x in e.get("entities") or []}
    pool = []
    for o in events:
        if o["event_id"] == e["event_id"] or o["first_seen"] > date:
            continue
        if not ({x.lower() for x in o.get("entities") or []} & ents):
            continue
        prim = [a for a in o["articles"] if a["url"] not in mine and a["date"] <= date and PRIMARY_DOC.search(a["url"])
                and a["source_class"] != "community"]
        if prim:
            pool.append((o, prim[0]))
    if not pool:
        return []
    q = embed([event_text(e["title"], e["summary"], e["entities"], e["event_type"])])[0]
    m = embed([event_text(o["title"], o["summary"], o["entities"], o["event_type"]) for o, _ in pool])
    sims = m @ q
    out = []
    for i in np.argsort(-sims)[:k]:
        if sims[i] < 0.80:
            break
        a = pool[i][1]
        full = ext_cache(a)
        if full and len(full.get("text") or "") > 800:
            out.append({**a, "_text": full["text"], "_via": pool[i][0]["event_id"], "_sim": round(float(sims[i]), 3)})
    return out


def gap_sources_enabled() -> bool:
    """Opt-in (V3_GAP_SOURCES=1). Production stays on the verified path until the gap flow passes its acceptance test."""
    return os.environ.get("V3_GAP_SOURCES") == "1"


def write_progress(event_id: str, what: str, *, seconds: float | None = None) -> None:
    """One flushed line per step so a long write run shows where it is (and a hang shows where it stopped)."""
    t = f" {int(seconds)}s" if seconds is not None else ""
    print(f"[write:{event_id}] {what}{t}", flush=True)


def stage_write(date: str) -> None:
    events_list = load_events()
    events = {e["event_id"]: e for e in events_list}
    sel = [r for r in jl_read(V3 / date / "selection.jsonl") if r["selected"]]
    only = {x for x in os.environ.get("V3_ONLY", "").split(",") if x}
    if only:
        sel = [r for r in sel if r["event_id"] in only]
    _ext: dict[str, dict] = {}

    def ext_for(d: str) -> dict:
        if d not in _ext:
            _ext[d] = {r["aid"]: r for r in jl_read(V3 / d / "extracted.jsonl")}
        return _ext[d]

    def ext_article(a: dict):
        return ext_for(a["date"]).get(a["aid"])

    def build(e: dict) -> tuple[list[dict], str]:
        def _gaps_logged(ev, srcs_, genre_):
            if genre_ == "paper":
                return []
            t0 = time.time()
            write_progress(ev["event_id"], "mechanism-gap check start")
            gaps = find_mechanism_gaps(srcs_)
            write_progress(ev["event_id"], f"mechanism-gap check done: {len(gaps)} gap(s) {[g['mechanism'][:50] for g in gaps]}", seconds=time.time() - t0)
            return gaps
        today = set(e["new_today"].get(date, []))
        seen, arts = set(), []
        for a in sorted((a for a in e["articles"] if a["date"] <= date), key=lambda a: a["date"], reverse=True):
            if a["url"] not in seen:  # the same URL can be collected on several days
                seen.add(a["url"])
                arts.append(a)
        is_prim = lambda a: bool(PRIMARY_DOC.search(a["url"])) or source_class(a["url"]) == "primary"
        arts = sorted(arts, key=lambda a: (not is_prim(a), source_class(a["url"]) == "community", a["aid"] not in today))
        if not e.get("curation_merged_ids"):
            arts = arts[:6]  # merged events retain every absorbed source, including useful community observations
        srcs = []
        for a in arts:
            full = ext_article(a) or {}
            prim = is_prim(a)
            kind = "primary" if prim else source_class(a["url"])
            text = full.get("text") or a.get("event_summary") or ""
            text = fetch_fuller(a["url"], text, kind)
            srcs.append({"tag": f"S{len(srcs)+1}", "aid": a["aid"], "url": a["url"], "title": a["title"], "domain": a["domain"],
                         "kind": kind, "text": text[: 3000 if kind == "community" else 20000]})
        genre_now = genre(e)
        non_comm = [s_ for s_ in srcs if s_["kind"] != "community"]
        thin = sum(len(s_["text"]) for s_ in non_comm) < 6000 or len({s_["domain"] for s_ in non_comm}) < 2
        extras = []
        if thin and genre_now != "paper":
            try:
                extras = extra_sources(e, {s_["url"] for s_ in srcs}, {s_["domain"] for s_ in srcs})
            except llm.QuotaExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 - extra sources are optional; the article is written from its own sources
                write_progress(e["event_id"], f"extra sources unavailable ({type(exc).__name__}: {str(exc)[:80]}); writing from its own sources")
            for x in extras:
                if len(srcs) >= 9:
                    break
                srcs.append({"tag": f"S{len(srcs)+1}", "aid": "extra", "url": x["url"], "title": x["title"], "domain": x["domain"],
                             "kind": "primary" if PRIMARY_DOC.search(x["url"]) else "news", "text": x["text"][:20000], "via": "search"})
        for x in (gap_sources(e, _gaps_logged(e, srcs, genre_now), {s_["url"] for s_ in srcs}, {s_["domain"] for s_ in srcs}) if gap_sources_enabled() else []):
            if len(srcs) >= 9:
                break
            srcs.append({"tag": f"S{len(srcs)+1}", "aid": "extra", "url": x["url"], "title": x["title"], "domain": x["domain"],
                         "kind": "primary" if PRIMARY_DOC.search(x["url"]) else "news", "text": x["text"][:20000], "via": "gap:" + x.get("mechanism", "")[:80]})
        try:
            related = related_primary_docs(e, events_list, date, ext_article)
        except llm.QuotaExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - optional enrichment
            related = []
            write_progress(e["event_id"], f"related primary docs unavailable ({type(exc).__name__}: {str(exc)[:80]})")
        for a in related:
            if len(srcs) >= 8:
                break
            srcs.append({"tag": f"S{len(srcs)+1}", "aid": a["aid"], "url": a["url"], "title": a["title"], "domain": a["domain"],
                         "kind": "primary", "via": a["_via"], "text": a["_text"][:14000]})
        prev = [w for w in e.get("writes", []) if w["date"] < date]
        user = f"OUTPUT_LANGUAGE: English. Previous coverage may be in another language; retain it as context and write only the new article in English.\n\nGENRE: {GENRE_GUIDE[genre(e)]}\n\n"
        if prev:
            w = prev[-1]
            user += f"PREVIOUSLY PUBLISHED ({w['date']}):\n{w['headline']}\n{w['lead']}\n{w['body']}\n\n"
        user += f"EVENT: {e['title']}\n{e['summary']}\n\nSOURCES:\n" + "\n\n".join(
            f"[{s['tag']}] ({s['kind']}) {s['title']} ({s['domain']})\n{s['text']}" for s in srcs)
        return srcs, user

    feedback_path = V3 / date / "writer_feedback.json"
    editorial = json.loads(feedback_path.read_text()) if feedback_path.exists() else {}
    saved = {r["event_id"]: r for r in jl_read(V3 / date / "articles.jsonl")}
    checkpoint_lock = threading.Lock()

    def draft_work(r):
        e = events[r["event_id"]]
        srcs, user = build(e)
        editor_feedback = editorial.get(r["event_id"], "")
        if editor_feedback:
            user += "\n\nEDITOR SOURCE CHECK (use only facts supported by SOURCES):\n" + editor_feedback
        prompt_hash = hashlib.sha256((WRITER_LANGUAGE + "\0" + WRITER_SYS + REVIEW_SYS + user).encode()).hexdigest()
        previous = saved.get(r["event_id"])
        same_prompt = previous and previous.get("writer_prompt_sha256") == prompt_hash and previous.get("writer_language") == WRITER_LANGUAGE
        if same_prompt and (previous.get("quality_ok") is True or previous.get("action") == "skip"):
            return previous
        digest = "\n".join(f"[{s['tag']}] ({s['kind']}) {s['title']}: {s['text']}" for s in srcs)
        # Re-review only a draft from this exact language/prompt. Equal sources do not make a legacy Korean draft a new English article.
        if not editor_feedback and same_prompt and previous.get("action") == "publish" and previous.get("sources") == srcs:
            fp = fact_problems(previous)
            if not fp:
                rv = llm.call_json(REVIEW_SYS, f"GENRE: {GENRE_GUIDE[genre(e)]}\n\nDRAFT:\n{previous['headline']}\n{previous['lead']}\n\n{previous['body']}\n\nSOURCE DIGEST:\n{digest}", timeout=600)
                if isinstance(rv, dict) and not rv.get("_error") and rv.get("blocking") == [] and not rv.get("missing") and not rv.get("editorial"):
                    previous = dict(previous)
                    previous["quality_ok"] = True
                    previous["writer_prompt_sha256"] = prompt_hash
                    previous.setdefault("attempts", []).append({"attempt": len(previous.get("attempts", [])) + 1, "style": [], "review": [], "missing": rv.get("missing", []), "editorial": rv.get("editorial", []), "minor": rv.get("minor", []), "facts": [], "cached_draft_review": True})
                    return previous
        history, feedback, out, drafts = [], [], None, []
        for attempt in range(3):
            prompt = user if not feedback else (
                user + "\n\nPREVIOUS DRAFT:\n" + json.dumps({k: out.get(k) for k in ("headline", "lead", "body", "glossary")}, ensure_ascii=False)
                + "\n\nFIX ALL OF THESE AND RETURN THE FULL REVISED ARTICLE:\n- " + "\n- ".join(feedback))
            out = llm.call_json(WRITER_SYS, prompt, timeout=900)
            write_progress(r["event_id"], f"draft {attempt + 1} written")
            out = dict(out) if isinstance(out, dict) else out
            if not isinstance(out, dict) or "_error" in out:
                return {"_error": (out or {}).get("_error", "none") if isinstance(out, dict) else "none", "sources": srcs, "attempts": history}
            out["sources"] = srcs
            out["date"] = date
            out["genre"] = genre(e)
            out["writer_prompt_sha256"] = prompt_hash
            out["writer_language"] = WRITER_LANGUAGE
            if out.get("action") != "publish":
                out["attempts"] = history
                return out
            out["headline"] = TAG.sub("", out.get("headline") or "").strip()
            sp = style_problems(out)
            rv = llm.call_json(REVIEW_SYS, f"GENRE: {GENRE_GUIDE[genre(e)]}\n\nDRAFT:\n{out['headline']}\n{out['lead']}\n\n{out['body']}\n\n"
                               f"GLOSSARY: {json.dumps(out.get('glossary'), ensure_ascii=False)}\n\nSOURCE DIGEST:\n{digest}", timeout=600)
            if isinstance(rv, dict) and "_error" not in rv:
                rv_issues = [str(x) for x in (rv.get("blocking") or [])]
                missing = [str(x) for x in (rv.get("missing") or [])]
                editorial_notes = [str(x) for x in (rv.get("editorial") or [])]
                minor = [str(x) for x in (rv.get("minor") or [])]
            else:
                rv_issues, missing, editorial_notes, minor = ["Editorial review response failed: the draft cannot be treated as passed"], [], [], []
            fp = fact_problems(out)
            history.append({"attempt": attempt + 1, "style": sp, "review": rv_issues, "missing": missing, "editorial": editorial_notes, "minor": minor, "facts": fp})
            write_progress(r["event_id"], f"review {attempt + 1}: blocking={len(rv_issues)} missing={len(missing)} editorial={len(editorial_notes)} facts={len(fp)} minor={len(minor)} style_hints={len(sp)}")
            # v4 loop: rewrite feedback = blocking + missing + editorial + deterministic fact problems. Minor and style hints never trigger a rewrite.
            feedback = rv_issues + missing + editorial_notes + fact_feedback(fp)
            drafts.append((len(fp), len(rv_issues), len(missing), len(editorial_notes), attempt, out))
            if not feedback:
                break
        # keep the best draft: fewest fact problems, then fewest source contradictions, then fewest omissions, then latest
        best = sorted(drafts, key=lambda d: (d[0], d[1], d[2], d[3], -d[4]))[0]
        out = best[5]
        out["attempts"] = history
        out["chosen_attempt"] = best[4] + 1
        # Only a remaining fact error (code check or source contradiction) fails review. Leftover missing/editorial/minor notes and style hints still publish normally.
        chosen = history[best[4]]
        reasons = list(chosen["review"]) + fact_feedback(chosen["facts"])
        out["review_failed"] = bool(reasons)
        out["review_failed_reasons"] = reasons
        out["quality_ok"] = not reasons
        return out

    def work(r):
        out = draft_work(r)
        row = {"event_id": r["event_id"], "section": r["section"], "final": r["final"], **out}
        with checkpoint_lock:
            saved[r["event_id"]] = row
            jl_write(V3 / date / "articles.jsonl", list(saved.values()))
        return out

    res = llm.parallel(sel, work, min(WORKERS, 4), "write")
    res = retry_failed(sel, res, work, "write")
    rows = []
    for r, o in zip(sel, res):
        if is_failed(o):
            # Still failed after the retry pass: withheld as a reasoned skip so one article cannot block the brief.
            err = str((o or {}).get("_error", "none"))[:200] if isinstance(o, dict) else "none"
            o = {"action": "skip", "reason": f"withheld (not published): writer LLM failed after retry ({err})", "llm_failed": True}
        rows.append({"event_id": r["event_id"], "section": r["section"], "final": r["final"], **o})
    if only:
        keep = [r for r in jl_read(V3 / date / "articles.jsonl") if r["event_id"] not in only]
        jl_write(V3 / date / "articles.jsonl", keep + rows)
    else:
        jl_write(V3 / date / "articles.jsonl", rows)
    pub = [x for x in rows if x.get("action") == "publish"]
    from collections import Counter
    print("write:", len(rows), "publish", len(pub), "skip", sum(1 for x in rows if x.get("action") == "skip"),
          "err", sum(1 for x in rows if x.get("llm_failed")), "quality_ok", sum(1 for x in pub if x.get("quality_ok")),
          "attempts", Counter(len(x.get("attempts") or []) for x in pub), "usage", llm.USAGE)


# ----------------------------------------------------------------- 7 validate
NUM = re.compile(r"\d+(?:[.,]\d+)*")
TAG = re.compile(r"\[S(\d+)\]")
TAGRUN = re.compile(r"(?:\s*\[S\d+\])+")
EN_MULT = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "mn": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9,
           "trillion": 1e12, "tn": 1e12, "t": 1e12}
KO_MULT = {"천": 1e3, "만": 1e4, "억": 1e8, "조": 1e12}
NUM_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
             "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
             "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "fifty": 50, "hundred": 100,
             "dozen": 12, "half": 50, "twice": 2, "double": 2, "triple": 3, "third": 3, "quarter": 25, "thousand": 1000}
ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
EN_MULT.update({"tr": 1e12, "trn": 1e12})
_WORD_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
               "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
               "seventeen": 17, "eighteen": 18, "nineteen": 19, **TENS}
_WORD_SCALES = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}
_WORD_RUN = re.compile(r"\b(?:(?:a|an)\s+(?=hundred|thousand|million|billion|trillion)|(?:%s|hundred|thousand|million|billion|trillion|and)(?:[-\s]+|\b))+"
                       % "|".join(_WORD_UNITS))


def _spoken_numbers(low: str) -> set[float]:
    """Values of spelled-out English numbers: 'fifteen million', 'one hundred million', 'three hundred and fifty thousand'."""
    vals: set[float] = set()
    for m in _WORD_RUN.finditer(low):
        total, cur, seen = 0.0, 0.0, False
        for w in re.findall(r"[a-z]+", m.group(0)):
            if w in _WORD_UNITS:
                cur += _WORD_UNITS[w]; seen = True
            elif w == "hundred":
                cur = (cur or 1) * 100; seen = True
            elif w in _WORD_SCALES:
                total += (cur or 1) * _WORD_SCALES[w]; cur = 0.0; seen = True
        if seen:
            vals.add(total + cur)
    return vals


def _sentences(text: str) -> list[str]:
    """Split into sentences, keeping trailing [S#] tags with the sentence they follow."""
    out, buf = [], ""
    for para in text.split("\n"):
        pos = 0
        for m in re.finditer(r"[.!?](?=\s|$|\[)", para):
            end = m.end()
            t = TAGRUN.match(para, end)
            if t:
                end = t.end()
            out.append(para[pos:end].strip())
            pos = end
        if para[pos:].strip():
            out.append(para[pos:].strip())
    return [x for x in out if x and not TAGRUN.fullmatch(x)]


def _source_values(corpus: str) -> set[float]:
    vals: set[float] = set()
    low = corpus.lower()
    for m in re.finditer(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(thousand|million|billion|trillion|bn|mn|tn|trn|tr(?![a-z])|[kmbt](?![a-z]))?", low):
        v = float(m.group(1).replace(",", ""))
        vals.add(v)
        if m.group(2):
            vals.add(v * EN_MULT[m.group(2)])
    vals |= _spoken_numbers(low)
    if re.search(r"\bdecades?(?:-long|[- ]old)?\b", low) and not re.search(r"\b(several|multiple|many|two|three|four|five|six|seven|eight|nine)\s+decades\b", low):
        vals.add(10.0)  # "decade-long" == 10 years
    for w, n in NUM_WORDS.items():
        if re.search(rf"\b{w}\b", low):
            vals.add(float(n))
    # spelled-out compounds: "eighty-five", "twenty five", "three dozen", "forty-seven times"
    for m in re.finditer(rf"\b({'|'.join(TENS)})[-\s]({'|'.join(ONES)})\b", low):
        vals.add(float(TENS[m.group(1)] + ONES[m.group(2)]))
    for m in re.finditer(rf"\b({'|'.join(ONES)}|{'|'.join(TENS)}|a|one|a few)\s+(thousand|million|billion|trillion)\b", low):
        base = ONES.get(m.group(1)) or TENS.get(m.group(1)) or 1
        vals.add(float(base * EN_MULT[m.group(2)]))
    for m in re.finditer(rf"\b({'|'.join(ONES)}|a)\s+dozen\b", low):
        vals.add(float(ONES.get(m.group(1), 1) * 12))
    for t, n in TENS.items():
        if re.search(rf"\b{t}\b", low):
            vals.add(float(n))
    for o, n in ONES.items():
        if re.search(rf"\b{o}\b", low):
            vals.add(float(n))
    if re.search(r"\b(tripl\w*|threefold|three-fold)", low):
        vals.add(3.0)
    if re.search(r"\b(doubl\w*|twofold|two-fold)", low):
        vals.add(2.0)
    if "quadratic" in low or "second-order" in low or "second order" in low:
        vals.add(2.0)
    for i, mo in enumerate(MONTHS, 1):
        if mo in low or re.search(rf"\b{mo[:3]}t?\.?\s", low):
            vals.add(float(i))
    denominators = {"third": 3, "quarter": 4, "half": 2}
    for match in re.finditer(r"\b(a|one|two|three|four)[ -]+(thirds?|quarters?|half|halves)\b", low):
        numerator = 1 if match.group(1) == "a" else ONES[match.group(1)]
        unit = match.group(2)
        denominator = 2 if unit in ("half", "halves") else denominators[unit.rstrip("s")]
        vals.add(numerator / denominator)
        vals.add(100.0 * numerator / denominator)
    return vals


def _ko_number(tok: str) -> float:
    """'1억2천만' -> 120000000, '3천6백만' -> 36000000, '9650억' -> 965000000000."""
    tok = tok.replace(",", "").replace(" ", "")
    total, section, cur = 0.0, 0.0, 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)?([조억만천백]?)", tok):
        if not num and not unit:
            continue
        n = float(num) if num else 1.0
        if unit in ("천", "백"):
            section += n * (1000 if unit == "천" else 100)
        elif unit in ("만", "억", "조"):
            section += n if num else 0
            total += (section or 1) * KO_MULT[unit]
            section = 0
        else:
            section += n
    return total + section


def _claim_values(sentence: str) -> list[tuple[str, float]]:
    out = []
    s = TAG.sub("", sentence)
    s = re.sub(r"제\s?\d+", "", s)
    s = re.sub(r"[A-Za-z_\-]*[A-Za-z][A-Za-z_\-]*\d[\w.\-]*|\b\d+x\b", " ", s)  # identifiers: Paper2Agent, L0, GPT-5, 20x
    s = re.sub(r"\d+\s*차(?=[\s,.)])", " ", s)  # '2차' (second-order) is wording, not a figure
    def fraction_value(match):
        denominator, numerator = map(float, match.groups())
        if denominator:
            out.append((match.group(0), 100.0 * numerator / denominator))
            return " "
        return match.group(0)
    s = re.sub(r"(\d+)\s*분의\s*(\d+)", fraction_value, s)
    pat = re.compile(r"(\d+(?:,\d{3})*(?:\.\d+)?(?:\s*[조억만천백]\s*(?:\d+(?:,\d{3})*(?:\.\d+)?)?)*)")
    for m in pat.finditer(s):
        raw = m.group(1).strip()
        if re.search(r"[조억만천백]", raw):
            out.append((raw, _ko_number(raw)))
        else:
            out.append((raw, float(raw.replace(",", ""))))
    return out


def _value_supported(v: float, vals: set[float]) -> bool:
    exact_only = float(v).is_integer() and abs(v) < 1e5  # counts, years, versions: must match exactly
    for x in vals:
        if x == v:
            return True
        if not exact_only and x and abs(x - v) / max(abs(x), abs(v)) < 0.011:  # rounding of large/decimal figures
            return True
        if x and (abs(x * 100 - v) < 1e-6 or abs(x - v * 100) < 1e-6):  # 0.4 vs 40%
            return True
    return False


def stage_validate(date: str) -> None:
    rows = jl_read(V3 / date / "articles.jsonl")
    events = {e["event_id"]: e for e in load_events()}
    report = []
    for r in rows:
        if r.get("action") != "publish":
            continue
        problems = fact_problems(r)
        style = style_problems(r)
        r["headline"] = TAG.sub("", r.get("headline") or "").strip()
        r["validation"] = {"ok": not problems, "problems": problems, "style_warnings": style}
        if problems:
            r["review_failed"] = True
            r["review_failed_reasons"] = list(r.get("review_failed_reasons") or []) + fact_feedback(problems)
            r["quality_ok"] = False
        report.append({"event_id": r["event_id"], "ok": not problems, "n_problems": len(problems)})
        if not problems and not r.get("review_failed"):
            e = events[r["event_id"]]
            e.setdefault("writes", [])
            e["writes"] = [w for w in e["writes"] if w["date"] != date] + [
                {"date": date, "headline": r["headline"], "lead": TAG.sub("", r["lead"]), "body": TAG.sub("", r["body"])}]
    jl_write(V3 / date / "articles.jsonl", rows)
    jl_write(EVENTS, list(events.values()))
    from collections import Counter
    print("validate:", len(report), "ok", sum(1 for x in report if x["ok"]),
          Counter(p["type"] for r in rows if r.get("validation") for p in r["validation"]["problems"]))


def stage_curate(date: str) -> None:
    from curate_selected import stage, fallback
    try:
        result = stage(date)
    except llm.QuotaExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - curation is an editorial dedup pass; without it the selection stands as scored
        result = fallback(date, f"{type(exc).__name__}: {str(exc)[:160]}")
    print("curate:", json.dumps(result, ensure_ascii=False), "usage", llm.USAGE)


STAGES = {"extract": stage_extract, "match": stage_match, "refresh": stage_refresh, "novelty": stage_novelty,
          "select": stage_select, "curate": stage_curate, "write": stage_write, "validate": stage_validate}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--stages", default=",".join(STAGES))
    a = ap.parse_args()
    (V3 / a.date).mkdir(parents=True, exist_ok=True)
    for st in a.stages.split(","):
        t = time.time()
        STAGES[st](a.date)
        print(f"== {st} done in {time.time()-t:.0f}s", flush=True)
    json.dump(llm.USAGE, open(V3 / a.date / f"usage.{int(time.time())}.json", "w"))
