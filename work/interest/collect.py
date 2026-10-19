"""Stage A of the 11:00 job: checked Daily articles -> Learned Interests.

Reads the last N days of Daily Brief Section pages (only pages whose last_edited_time moved, plus today/yesterday always),
rebuilds the *current* checked set, extracts concepts only for newly checked articles, recomputes scores from scratch, and
syncs the Learned Interests section on 06.01.  Per-article failures are isolated: the article stays "pending" and the run goes on.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Callable

from . import extract, store
from .my_interest import sync_learned
from .notion_io import NotionIO, plain

SECTION_KEYS = {"ai", "security", "local_models", "infrastructure", "semiconductors", "developer_tools", "research", "outside_bubble",
                "community", "business", "review_failed"}
ID_RE = re.compile(r"^(\d{2}-\d{3,})\b")


def _prop_text(page: dict, name: str) -> str:
    return "".join(t.get("plain_text", "") for t in ((page.get("properties") or {}).get(name) or {}).get("rich_text") or [])


def _prop_date(page: dict) -> str | None:
    return (((page.get("properties") or {}).get("Date") or {}).get("date") or {}).get("start")


def _flatten(block: dict) -> str:
    parts = [plain(block)]
    for ch in block.get("_children") or []:
        parts.append(_flatten(ch))
    return "\n".join(p for p in parts if p)


def scan_section_page(io: NotionIO, page_id: str) -> list[dict]:
    """[{fid, checked, title, text}] for every issue checkbox on one Section page.

    Cost control: one call reads the top level (issues are flat to_do blocks); a to_do's nested Details are fetched ONLY
    when that issue is checked, because only checked articles need their text for concept extraction.
    """
    out = []

    def visit(blocks: list[dict]) -> None:
        for b in blocks:
            if b.get("type") == "to_do":
                m = ID_RE.match(plain(b))
                if m:
                    checked = bool(b["to_do"].get("checked"))
                    title = plain(b)[len(m.group(1)):].strip(" ·-—")
                    text = ""
                    if checked:
                        if b.get("has_children"):
                            b["_children"] = io.tree(b["id"])
                        text = _flatten(b)[:5000]
                    out.append({"fid": m.group(1), "checked": checked, "title": title, "text": text})
            elif b.get("has_children") and b.get("type") not in {"child_page", "child_database", "toggle"}:
                visit(io.children(b["id"]))

    visit(io.children(page_id))
    return out


def read_checked(io: NotionIO, state: dict, sections_ds: str, today: str, *, days: int = 30, always_days: int = 2) -> dict:
    t = dt.date.fromisoformat(today)
    since = (t - dt.timedelta(days=days)).isoformat()
    pages = io.query(sections_ds, {"property": "Date", "date": {"on_or_after": since}})
    stats = {"pages": 0, "reread": 0, "cached": 0}
    seen_pages = set()
    for pg in pages:
        key = _prop_text(pg, "Section Key")
        day = _prop_date(pg)
        if not day or key.startswith("history:") or key not in SECTION_KEYS:
            continue
        stats["pages"] += 1
        seen_pages.add(pg["id"])
        cached = state["pages"].get(pg["id"])
        fresh = (t - dt.date.fromisoformat(day)).days < always_days
        if cached and cached["edited"] == pg.get("last_edited_time") and not fresh:
            stats["cached"] += 1
            continue
        issues = scan_section_page(io, pg["id"])
        stats["reread"] += 1
        state["pages"][pg["id"]] = {"day": day, "key": key, "edited": pg.get("last_edited_time"), "checked": sorted(i["fid"] for i in issues if i["checked"])}
        for i in issues:
            ak = store.article_key(day, i["fid"])
            if i["checked"] and ak not in state["articles"]:
                state["articles"][ak] = {"day": day, "fid": i["fid"], "title": i["title"], "text": i["text"], "concepts": [], "status": "pending"}
    for pid in [p for p in state["pages"] if p not in seen_pages]:
        del state["pages"][pid]                       # page left the window (or was removed): its checks no longer count
    cur = {(c["day"], fid) for c in state["pages"].values() for fid in c["checked"]}
    state["checked_now"] = sorted([list(x) for x in cur])
    return stats


def extract_pending(state: dict, *, extract_fn: Callable = extract.extract_concepts, shortlist_fn: Callable | None = None,
                    judge_fn: Callable | None = None) -> dict:
    kw = {k: v for k, v in (("shortlist_fn", shortlist_fn), ("judge_fn", judge_fn)) if v is not None}
    stats = {"extracted": 0, "failed": 0, "new_concepts": 0, "merged": 0}
    checked = {(d, f) for d, f in state["checked_now"]}
    for ak, art in sorted(state["articles"].items()):
        if art["status"] != "pending" or (art["day"], art["fid"]) not in checked:
            continue
        try:
            labels = extract_fn(art["title"], art["text"])
            ids = []
            for label in labels:
                cid, how = extract.resolve_concept(state, label, **kw)
                if cid is None:
                    cid = store.new_concept_id(state)
                    state["concepts"][cid] = {"label": label, "aliases": [], "block_id": None, "active": True, "source": "checkbox", "created": art["day"]}
                    stats["new_concepts"] += 1
                else:
                    stats["merged"] += how in {"judge", "exact"}
                if cid not in ids:
                    ids.append(cid)
            art.update(concepts=ids, status="extracted")
            art.pop("text", None)                       # keep the state file small once extracted
            stats["extracted"] += 1
        except Exception as exc:                        # noqa: BLE001 - one article must not stop the day
            art["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            stats["failed"] += 1
    return stats


def add_request_interest(state: dict, label: str, *, day: str) -> str:
    """A 'show me more of this' feedback becomes a candidate Learned Interest (shown with (요청)); it carries no checkbox score."""
    cid = store.find_concept(state, label)
    if cid:
        return cid
    cid = store.new_concept_id(state)
    state["concepts"][cid] = {"label": label, "aliases": [], "block_id": None, "active": True, "source": "request", "created": day}
    return cid


def run(io: NotionIO, state: dict, *, today: str, sections_ds: str, page_id: str, days: int = 30, **fns) -> dict:
    out = {"read": read_checked(io, state, sections_ds, today, days=days)}
    out["extract"] = extract_pending(state, **{k: v for k, v in fns.items() if k in {"extract_fn", "shortlist_fn", "judge_fn"}})
    scores = store.compute_scores(state, today)
    out["learned"] = sync_learned(io, state, page_id, scores)
    out["checked_now"] = len(state["checked_now"])
    out["concepts"] = len(state["concepts"])
    return out
