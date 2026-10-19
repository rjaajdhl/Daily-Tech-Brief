#!/usr/bin/env python3
"""Helpers the 11:00 follow-up agent calls instead of touching Notion/state by hand.

  followup_tools.py publish --day D --title T --file report.md [--request-key K]
      Creates (or updates, if a page with that exact title already exists) a CHILD PAGE of that day's "12. Follow-up & Deep Dives"
      page, writes the Markdown as native blocks, reads it back and compares.  Creates section 12 first if the day has none.
  followup_tools.py interest --label "Agent Security" --day D
      "Show me more of this" feedback -> a Learned Interest candidate shown as "Label (요청)" (checked).  No score is added.
  followup_tools.py section12 --day D
      Print the page id / url of that day's section 12 (creating it if needed).
"""
import os
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "work" / "v3"))

from work.interest import collect, my_interest as MI, store  # noqa: E402
from work.interest.notion_io import NotionIO, plain, rt  # noqa: E402


def token() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("NOTION_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("NOTION_API_KEY missing")


def make_io(request=None) -> tuple[NotionIO, dict]:
    ids = json.loads((ROOT / "config" / "notion_ids.json").read_text())
    io = NotionIO(token() if request is None else "tok", request=request)
    io.scope.databases.update({ids["daily_database_id"].replace("-", "").casefold(), ids["daily_sections_database_id"].replace("-", "").casefold()})
    return io, ids


def find_section12(io: NotionIO, ids: dict, day: str, *, create: bool = True) -> dict:
    idx = io.query(ids["daily_data_source_id"], {"property": "Date", "date": {"equals": day}})
    if len(idx) != 1:
        raise RuntimeError(f"daily_index_not_unique:{day}:{len(idx)}")
    index_id = idx[0]["id"]
    rows = io.query(ids["daily_sections_data_source_id"], {"property": "Date", "date": {"equals": day}})
    for r in rows:
        key = "".join(t.get("plain_text", "") for t in (r["properties"].get("Section Key") or {}).get("rich_text", []))
        if key == "followup":
            io.scope.register_page(r["id"])
            return {"page_id": r["id"], "url": r.get("url"), "index_id": index_id, "created": False}
    if not create:
        raise RuntimeError(f"section12_missing:{day}")
    import followup_section as FS
    from work import daily_finalizer as F
    from work.scope_guard import register_page
    register_page(index_id, parent_database_id=ids["daily_database_id"])
    stage = (idx[0]["properties"].get("Stage") or {}).get("select", {}).get("name") or "FINAL"
    status = (idx[0]["properties"].get("Brief Status") or {}).get("select", {}).get("name") or "Published"
    res = FS.ensure_followup_section(F.request, io.headers, day=day, parent_brief_id=index_id, stage=stage, brief_status=status, review_needed=False)
    io.scope.register_page(res["page_id"])
    # link it from the Index (append only, after the last section link)
    blocks = io.children(index_id)
    if not any(FS.NAME in plain(b) for b in blocks):
        from work import notion_block_writer as NB
        io.scope.register_page(index_id)
        last = [b for b in blocks if b["type"] == "bulleted_list_item" and "issues" in plain(b)][-1]
        io.call(f"blocks/{index_id}/children", "PATCH", {"children": NB.markdown_blocks(f"- [{FS.NAME}]({res['url']}) — answers to your follow-up requests"),
                                                          "position": {"type": "after_block", "after_block": {"id": last["id"]}}})
    return {"page_id": res["page_id"], "url": res["url"], "index_id": index_id, "created": res["created"]}


def publish_report(io: NotionIO, ids: dict, day: str, title: str, markdown: str) -> dict:
    from work import notion_block_writer as NB
    sec = find_section12(io, ids, day)
    page_id = sec["page_id"]
    blocks = NB.markdown_blocks(markdown)
    if not blocks:
        raise RuntimeError("empty_report")
    existing = [b for b in io.children(page_id, register=False) if b.get("type") == "child_page" and b["child_page"].get("title") == title]
    if len(existing) > 1:
        raise RuntimeError(f"duplicate_report_pages:{title}")
    if existing:                                    # same title = same request: replace its body, never create a second page
        rid = existing[0]["id"]
        io.scope.register_page(rid)
        for b in io.children(rid):
            io.call(f"blocks/{b['id']}", "DELETE")
        created = False
    else:
        rid = io.call("pages", "POST", {"parent": {"page_id": page_id}, "properties": {"title": [{"type": "text", "text": {"content": title}}]}})["id"]
        io.scope.register_page(rid)
        created = True
    for i in range(0, len(blocks), 90):
        io.call(f"blocks/{rid}/children", "PATCH", {"children": blocks[i:i + 90]})
    got = io.tree(rid)
    want_headings = [plain(b) for b in blocks if b["type"].startswith("heading")]
    got_headings = [plain(b) for b in got if b["type"].startswith("heading")]
    ok = want_headings == got_headings and len(got) >= len(blocks) * 0.99
    return {"ok": ok, "report_page_id": rid, "created": created, "top_level_blocks": len(got), "expected_blocks": len(blocks),
            "section12": sec["url"], "headings": got_headings[:12]}


def add_interest(label: str, day: str, io: NotionIO | None = None) -> dict:
    state = store.load_state()
    page_id = state["page_ids"]["p0601"]
    if io is None:
        io, _ = make_io()
    io.scope.register_page(page_id)
    cid = collect.add_request_interest(state, label, day=day)
    scores = store.compute_scores(state, day)
    stats = MI.sync_learned(io, state, page_id, scores)
    store.save_state(state)
    return {"concept": cid, "label": state["concepts"][cid]["label"], "sync": stats}


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish"); p.add_argument("--day", required=True); p.add_argument("--title", required=True); p.add_argument("--file", required=True)
    i = sub.add_parser("interest"); i.add_argument("--label", required=True); i.add_argument("--day", required=True)
    s = sub.add_parser("section12"); s.add_argument("--day", required=True)
    a = ap.parse_args()
    if a.cmd == "interest":
        print(json.dumps(add_interest(a.label, a.day), ensure_ascii=False, indent=1)); return 0
    io, ids = make_io()
    if a.cmd == "section12":
        print(json.dumps(find_section12(io, ids, a.day), indent=1)); return 0
    res = publish_report(io, ids, a.day, a.title, Path(a.file).read_text(encoding="utf-8"))
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
