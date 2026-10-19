#!/usr/bin/env python3
"""Add (or repair) Section 12 for ONE existing Daily without re-publishing the brief.

  add_followup_section.py --day 2026-10-12 [--dry-run]

Creates the day's page 12 (placeholder only) if missing, and adds the Index link if it is absent.  The Index is edited by
appending one bullet after the last section link — the rest of the Index (summary, user feedback text) is not rewritten.
"""
import os
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "work" / "v3"))

from work.interest.notion_io import FollowupScope, NotionIO, plain  # noqa: E402
from work import notion_block_writer as NB  # noqa: E402
import followup_section as FS  # noqa: E402


def token() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("NOTION_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("NOTION_API_KEY missing")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    ids = json.loads((ROOT / "config" / "notion_ids.json").read_text())
    io = NotionIO(token())
    io.scope.databases.update({ids["daily_database_id"].replace("-", "").casefold(), ids["daily_sections_database_id"].replace("-", "").casefold()})
    rows = io.query(ids["daily_data_source_id"], {"property": "Date", "date": {"equals": a.day}})
    if len(rows) != 1:
        raise SystemExit(f"index_not_unique:{len(rows)}")
    index_id = rows[0]["id"]
    sec_rows = io.query(ids["daily_sections_data_source_id"], {"property": "Date", "date": {"equals": a.day}})
    keys = {"".join(t.get("plain_text", "") for t in (r["properties"].get("Section Key") or {}).get("rich_text", [])): r for r in sec_rows}
    print("index", index_id, "| existing sections:", sorted(k for k in keys if not k.startswith("history:")))
    if a.dry_run:
        print("DRY RUN: would", "reuse" if FS.KEY in keys else "create", "section 12 and", "keep" if any(FS.NAME in plain(b) for b in io.children(index_id, register=False)) else "add", "the Index link")
        return 0
    # section 12 through the same pooled helpers the publisher uses (registers the page for the 02:30 scope guard)
    from work import daily_finalizer as F
    from work.scope_guard import register_page
    register_page(index_id, parent_database_id=ids["daily_database_id"])
    stage = (rows[0]["properties"].get("Stage") or {}).get("select", {}).get("name") or "FINAL"
    status = (rows[0]["properties"].get("Brief Status") or {}).get("select", {}).get("name") or "Published"
    res = FS.ensure_followup_section(F.request, io.headers, day=a.day, parent_brief_id=index_id, stage=stage, brief_status=status, review_needed=False)
    print("section12", res)
    io.scope.register_page(index_id)
    blocks = io.children(index_id)
    if any(FS.NAME in plain(b) for b in blocks):
        print("index link already present")
        return 0
    last_link = [b for b in blocks if b["type"] == "bulleted_list_item" and "issues" in plain(b)][-1]
    new = NB.markdown_blocks(f"- [{FS.NAME}]({res['url']}) — answers to your follow-up requests")
    io.call(f"blocks/{index_id}/children", "PATCH", {"children": new, "position": {"type": "after_block", "after_block": {"id": last_link['id']}}})
    print("index link added after:", plain(last_link)[:60])
    return 0


if __name__ == "__main__":
    sys.exit(main())
