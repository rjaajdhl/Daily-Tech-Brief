"""06.01. My Interest page: creation (idempotent) and the Learned Interests section sync.

Rules enforced here:
  * New concepts are created as checked ``[x]``.  Existing blocks are never created again and their ``checked`` is never sent.
  * Refreshing a score PATCHes only ``to_do.rich_text``.  After every refresh the section is re-read and compared with the
    states captured before; a changed checkbox raises (and the state file is not advanced).
  * A block the user typed by hand is adopted as a concept (source "user"); nothing is deleted.
  * Base Topics / Research Sources are written once at creation and are display-only (not read by the 02:30 pipeline).
"""
from __future__ import annotations
import os

import json
import re
from pathlib import Path

from . import store
from .notion_io import NotionIO, plain, rt

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[2])
ROOT_PAGE_ID = os.environ.get("TI_NOTION_ROOT_PAGE_ID") or "00000000-0000-0000-0000-000000000000"      # the Notion page that holds your Dashboard (set TI_NOTION_ROOT_PAGE_ID in .env)
TITLE_06 = "06. Research Controls"
TITLE_0601 = "06.01. My Interest"
HEAD_LEARNED, HEAD_TOPICS, HEAD_SOURCES = "Learned Interests", "Base Topics", "Research Sources"
CREATED_PATH = ROOT / "data" / "interest" / "created_pages.json"

INTRO = ("Learned Interests are filled by the 11:00 job from the Daily articles you check. Leave an item checked to keep it active, "
         "uncheck it to turn it off — the job never switches it back on. Base Topics and Research Sources are shown here for reference only: "
         "editing them does not change the 02:30 collection or selection yet.")
LEARNED_HINT = ("The number is the recent-check strength (21-day half-life). Articles you did not check never count against a topic. "
                "\"(요청)\" marks a topic you asked for in feedback.")


def todo(text: str, checked: bool = True, url: str | None = None) -> dict:
    return {"object": "block", "type": "to_do", "to_do": {"rich_text": rt(text, url), "checked": checked}}


def heading(level: int, text: str) -> dict:
    return {"object": "block", "type": f"heading_{level}", f"heading_{level}": {"rich_text": rt(text)}}


def paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rt(text)}}


def base_topics_from_prompt() -> list[str]:
    """The real core-interest list from the canonical selection prompt (v3/pipeline.py SELECT_SYS), not a retyped copy."""
    src = (ROOT / "work" / "v3" / "pipeline.py").read_text(encoding="utf-8")
    m = re.search(r"Reader interests \(core\): (.*?)\.\nHard exclusions", src, re.S)
    if not m:
        raise RuntimeError("base_topics_not_found_in_select_prompt")
    parts = [p.strip() for p in re.split(r";\s*", " ".join(m.group(1).split())) if p.strip()]
    return [p[0].upper() + p[1:] for p in parts]


def research_sources() -> list[tuple[str, list[dict]]]:
    reg = json.loads((ROOT / "config" / "source_registry.json").read_text(encoding="utf-8"))
    lanes: dict[str, list[dict]] = {}
    for s in reg["sources"]:
        lanes.setdefault(s["lane"], []).append(s)
    return list(lanes.items())


def body_blocks() -> list[dict]:
    blocks = [{"object": "block", "type": "callout", "callout": {"rich_text": rt(INTRO), "icon": {"type": "emoji", "emoji": "🎯"}}},
              heading(2, HEAD_LEARNED), paragraph(LEARNED_HINT),
              heading(2, HEAD_TOPICS)]
    blocks += [todo(t) for t in base_topics_from_prompt()]
    blocks.append(heading(2, HEAD_SOURCES))
    for lane, items in research_sources():
        blocks.append(heading(3, lane.replace("_", " ").title().replace(" Ai ", " AI ").replace("Ai ", "AI ", 1) if lane.startswith("official_ai") else lane.replace("_", " ").title()))
        for s in items:
            label = f"{s['name']} ({s['kind']})"
            blocks.append(todo(label, True, s["url"] if str(s.get("url", "")).startswith("http") else None))
    return blocks


def _record_created(entry: dict, path: Path = CREATED_PATH) -> None:
    data = json.loads(path.read_text()) if path.exists() else {"pages": []}
    if all(p["id"] != entry["id"] for p in data["pages"]):
        data["pages"].append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1))


def _child_page(io: NotionIO, parent_id: str, title: str) -> str | None:
    hits = [b for b in io.children(parent_id, register=False) if b.get("type") == "child_page" and (b["child_page"].get("title") == title)]
    if len(hits) > 1:
        raise RuntimeError(f"duplicate_child_page:{title}")
    return hits[0]["id"] if hits else None


def _chunks(items: list, n: int = 90):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def ensure_pages(io: NotionIO, state: dict, *, created_path: Path = CREATED_PATH) -> dict:
    """Create 06 and 06.01 if (and only if) they do not exist.  Never touches any other page."""
    io.scope.parent_pages.add(store_canon(ROOT_PAGE_ID))
    result = {"created": []}
    p06 = _child_page(io, ROOT_PAGE_ID, TITLE_06)
    if not p06:
        p06 = io.call("pages", "POST", {"parent": {"page_id": ROOT_PAGE_ID}, "properties": {"title": [{"type": "text", "text": {"content": TITLE_06}}]},
                                         "children": [paragraph("Controls for what the research pipeline pays attention to.")]})["id"]
        _record_created({"id": p06, "title": TITLE_06, "parent": ROOT_PAGE_ID}, created_path)
        result["created"].append(TITLE_06)
    io.scope.register_page(p06)
    io.scope.parent_pages.discard(store_canon(ROOT_PAGE_ID))
    p0601 = _child_page(io, p06, TITLE_0601)
    if not p0601:
        blocks = body_blocks()
        p0601 = io.call("pages", "POST", {"parent": {"page_id": p06}, "properties": {"title": [{"type": "text", "text": {"content": TITLE_0601}}]},
                                           "children": blocks[:90]})["id"]
        io.scope.register_page(p0601)
        _record_created({"id": p0601, "title": TITLE_0601, "parent": p06}, created_path)
        for chunk in _chunks(blocks[90:]):
            io.call(f"blocks/{p0601}/children", "PATCH", {"children": chunk})
        result["created"].append(TITLE_0601)
    io.scope.register_page(p0601)
    state["page_ids"].update({"p06": p06, "p0601": p0601})
    result.update(p06=p06, p0601=p0601)
    return result


def store_canon(x: str) -> str:
    from .notion_io import canon
    return canon(x)


# ------------------------------------------------------------------------------------------------ Learned section
def learned_section(io: NotionIO, page_id: str) -> tuple[list[dict], str | None]:
    """(to_do blocks of the Learned Interests section, id of the last block of that section's head [hint paragraph])"""
    blocks = io.children(page_id)
    items, anchor, inside = [], None, False
    for b in blocks:
        t = b.get("type")
        if t == "heading_2":
            inside = plain(b).strip() == HEAD_LEARNED
            if inside:
                anchor = b["id"]
            continue
        if inside:
            if t == "to_do":
                items.append(b)
            elif not items:
                anchor = b["id"]          # the hint paragraph: new items go right after it
    return items, anchor


def _checked_map(items: list[dict]) -> dict[str, bool]:
    return {b["id"]: bool(b["to_do"].get("checked")) for b in items}


def sync_learned(io: NotionIO, state: dict, page_id: str, scores: dict[str, float]) -> dict:
    items, anchor = learned_section(io, page_id)
    if anchor is None:
        raise RuntimeError("learned_section_missing")
    before = _checked_map(items)
    stats = {"adopted": 0, "text_updated": 0, "created": 0, "active": 0, "inactive": 0}
    by_block = {c.get("block_id"): cid for cid, c in state["concepts"].items() if c.get("block_id")}
    # 1) read Notion -> state (Notion is the source of truth for on/off; hand-typed blocks are adopted)
    for b in items:
        label, is_req, _ = store.parse_block_text(plain(b))
        cid = by_block.get(b["id"]) or store.find_concept(state, label)
        if cid is None:
            cid = store.new_concept_id(state)
            state["concepts"][cid] = {"label": label, "aliases": [], "block_id": b["id"], "active": True, "source": "user", "created": None}
            stats["adopted"] += 1
        c = state["concepts"][cid]
        c["block_id"] = b["id"]
        c["active"] = bool(b["to_do"].get("checked"))
        if is_req:
            c["source"] = "request"
    # 2) refresh the number on existing blocks: text only, never `checked`
    for b in items:
        cid = by_block.get(b["id"]) or store.find_concept(state, store.parse_block_text(plain(b))[0])
        c = state["concepts"][cid]
        want = store.block_text(c["label"], scores.get(cid, 0.0), request=(c.get("source") == "request"))
        if plain(b) != want:
            io.call(f"blocks/{b['id']}", "PATCH", {"to_do": {"rich_text": rt(want)}})
            stats["text_updated"] += 1
    # 3) new concepts -> new [x] blocks, placed by score (existing blocks are never moved)
    existing = [(store.parse_block_text(plain(b))[2] or 0.0, b["id"]) for b in items]
    for cid, c in sorted(state["concepts"].items(), key=lambda kv: -scores.get(kv[0], 0.0)):
        if c.get("block_id") or c.get("source") == "base":
            continue
        s = scores.get(cid, 0.0)
        after = anchor
        for sc, bid in existing:
            if sc >= round(s, 1):
                after = bid
        res = io.call(f"blocks/{page_id}/children", "PATCH", {"children": [todo(store.block_text(c["label"], s, request=(c.get("source") == "request")), True)],
                                                                  "position": {"type": "after_block", "after_block": {"id": after}}})
        new_id = res["results"][0]["id"]
        c["block_id"], c["active"] = new_id, True
        io.scope.register_block(new_id)
        existing.append((round(s, 1), new_id))
        existing.sort(key=lambda x: -x[0])
        stats["created"] += 1
    # 4) prove no user checkbox moved
    items_after, _ = learned_section(io, page_id)
    after_map = _checked_map(items_after)
    for bid, was in before.items():
        if after_map.get(bid) is not was:
            raise RuntimeError(f"learned_checkbox_changed:{bid}:{was}->{after_map.get(bid)}")
    stats["active"] = sum(1 for b in items_after if b["to_do"].get("checked"))
    stats["inactive"] = len(items_after) - stats["active"]
    return stats
