#!/usr/bin/env python3
"""One-shot, idempotent: create 06. Research Controls / 06.01. My Interest under the Tech Intelligence root page.

  setup_my_interest.py --dry-run     print what would be created (no Notion call that writes)
  setup_my_interest.py               create (skips anything that already exists), record ids in data/interest/created_pages.json
Only these two pages are created.  The write scope is limited to the root page as a *parent* for exactly this run.
"""
import os
import json
import sys
from pathlib import Path

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))

from work.interest import my_interest as MI, store  # noqa: E402
from work.interest.notion_io import NotionIO, plain  # noqa: E402


def token() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("NOTION_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("NOTION_API_KEY missing")


def main() -> int:
    if "--dry-run" in sys.argv:
        blocks = MI.body_blocks()
        kinds = {}
        for b in blocks:
            kinds[b["type"]] = kinds.get(b["type"], 0) + 1
        print(json.dumps({"would_create": [MI.TITLE_06, MI.TITLE_0601], "parent": MI.ROOT_PAGE_ID, "block_counts": kinds, "total_blocks": len(blocks)}, indent=1))
        print("--- 06.01 outline ---")
        for b in blocks:
            t = b["type"]
            txt = "".join(x["text"]["content"] for x in b[t]["rich_text"])
            if t.startswith("heading") or t == "callout":
                print(f"[{t}] {txt[:110]}")
        topics = [b for b in blocks if b["type"] == "to_do"][:6]
        print("--- Base Topics (from the selection prompt) ---")
        for b in topics:
            print("  [x]", b["to_do"]["rich_text"][0]["text"]["content"])
        return 0
    io = NotionIO(token())
    state = store.load_state()
    res = MI.ensure_pages(io, state)
    store.save_state(state)
    print(json.dumps(res, indent=1))
    items = io.children(res["p0601"], register=False)
    print("06.01 top-level blocks:", len(items), "| headings:", [plain(b) for b in items if b["type"] == "heading_2"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
