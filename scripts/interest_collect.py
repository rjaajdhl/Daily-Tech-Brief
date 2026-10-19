#!/usr/bin/env python3
"""11:00 job, stage A: checked Daily articles -> Learned Interests (06.01).  Prints a short report (cron delivers it).

  interest_collect.py [--today YYYY-MM-DD] [--days 30] [--dry-run]
--dry-run reads Notion and shows what would be extracted; it calls no LLM and writes nothing (state file included).
"""
import os
import argparse
import datetime as dt
import json
import sys
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))

from work.interest import collect, store  # noqa: E402
from work.interest.notion_io import NotionIO  # noqa: E402


def token() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("NOTION_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("NOTION_API_KEY missing")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    today = a.today or dt.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    ids = json.loads((ROOT / "config" / "notion_ids.json").read_text())
    state = store.load_state()
    page_id = state.get("page_ids", {}).get("p0601")
    if not page_id:
        print("FAILED: 06.01 My Interest page id is missing from data/interest/state.json (run scripts/setup_my_interest.py)")
        return 1
    io = NotionIO(token())
    io.scope.register_page(page_id)
    if a.dry_run:
        read = collect.read_checked(io, state, ids["daily_sections_data_source_id"], today, days=a.days)
        pending = [(k, v["title"]) for k, v in sorted(state["articles"].items()) if v["status"] == "pending"]
        print(json.dumps({"today": today, "read": read, "checked_now": state["checked_now"], "pending_extraction": pending}, ensure_ascii=False, indent=1))
        return 0
    try:
        out = collect.run(io, state, today=today, sections_ds=ids["daily_sections_data_source_id"], page_id=page_id, days=a.days)
    except Exception:
        store.save_state(state)                      # keep whatever progress was made (extractions are per-article)
        print("FAILED:\n" + traceback.format_exc()[-1500:])
        return 1
    store.save_state(state)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
