#!/usr/bin/env python3
"""Backfill one PDT calendar day from the Arctic Shift Reddit archive API."""

from __future__ import annotations
import os

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import re
import subprocess
import sys
import time
import urllib.parse
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from reddit_daily_collector import GROUPS  # noqa: E402

PROFILE_ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
SNAPSHOT_ROOT = PROFILE_ROOT / "data" / "reddit" / "snapshots"
BACKFILL_ROOT = PROFILE_ROOT / "data" / "reddit" / "backfills"
API = "https://arctic-shift.photon-reddit.com/api/posts/search"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


def all_subreddits() -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for group in GROUPS.values():
        for name in group:
            if name.lower() not in seen:
                seen.add(name.lower())
                names.append(name)
    return names


def clean(value: str, limit: int = 1200) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def fetch_json(params: dict[str, str | int]) -> dict:
    url = API + "?" + urllib.parse.urlencode(params)
    last_error = ""
    for attempt in range(6):
        completed = subprocess.run(
            [
                "curl",
                "-L",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "120",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=140,
        )
        if completed.returncode == 0:
            return json.loads(completed.stdout)
        last_error = completed.stderr.strip() or f"curl exit {completed.returncode}"
        if attempt < 5:
            time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(last_error)


def collect_subreddit(
    subreddit: str, start_epoch: int, end_epoch: int
) -> tuple[str, list[dict], str | None]:
    rows_by_id: dict[str, dict] = {}
    before = end_epoch
    try:
        for _page in range(10):
            payload = fetch_json(
                {
                    "subreddit": subreddit,
                    "after": start_epoch,
                    "before": before,
                    "limit": "auto",
                }
            )
            rows = payload.get("data", [])
            if not isinstance(rows, list):
                raise RuntimeError("API data field is not a list")
            for row in rows:
                post_id = row.get("id")
                created = row.get("created_utc")
                if not post_id or not isinstance(created, (int, float)):
                    continue
                if not (start_epoch < created < end_epoch):
                    continue
                permalink = row.get("permalink") or ""
                url = (
                    "https://www.reddit.com" + permalink
                    if permalink.startswith("/")
                    else permalink or f"https://www.reddit.com/comments/{post_id}/"
                )
                rows_by_id[post_id] = {
                    "id": post_id,
                    "subreddit": row.get("subreddit") or subreddit,
                    "title": clean(row.get("title", ""), 500),
                    "author": row.get("author") or "",
                    "published_utc": dt.datetime.fromtimestamp(
                        created, tz=dt.timezone.utc
                    ).isoformat(),
                    "url": url,
                    "body_excerpt": clean(row.get("selftext", "")),
                    "archive_source": "Arctic Shift",
                    "score_at_archive": row.get("score"),
                    "comment_count_at_archive": row.get("num_comments"),
                }
            if len(rows) < 1000:
                break
            oldest = min(
                (row.get("created_utc") for row in rows if row.get("created_utc")),
                default=None,
            )
            if oldest is None or oldest >= before:
                raise RuntimeError("archive pagination stalled")
            before = int(oldest)
        else:
            raise RuntimeError("archive pagination exceeded 10 pages")
    except Exception as exc:
        return subreddit, [], str(exc)
    records = sorted(rows_by_id.values(), key=lambda item: item["published_utc"])
    return subreddit, records, None


def load_existing(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    records = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("id"):
            records[item["id"]] = item
    return records


def main() -> int:
    args = parse_args()
    day = dt.date.fromisoformat(args.date)
    start_local = dt.datetime.combine(day, dt.time.min, tzinfo=LOCAL_TZ)
    end_local = start_local + dt.timedelta(days=1)
    start_epoch = int(start_local.astimezone(dt.timezone.utc).timestamp())
    end_epoch = int(end_local.astimezone(dt.timezone.utc).timestamp())
    subreddits = all_subreddits()

    results: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(collect_subreddit, name, start_epoch, end_epoch): name
            for name in subreddits
        }
        for future in concurrent.futures.as_completed(futures):
            subreddit, records, error = future.result()
            if error:
                errors[subreddit] = error
                print(f"ERROR r/{subreddit}: {error}", flush=True)
            else:
                results[subreddit] = records
                print(f"r/{subreddit}: {len(records)}", flush=True)

    snapshot_path = SNAPSHOT_ROOT / f"{day.isoformat()}.jsonl"
    merged = load_existing(snapshot_path)
    for records in results.values():
        for item in records:
            merged[item["id"]] = item
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    with snapshot_path.open("w", encoding="utf-8") as handle:
        for item in sorted(merged.values(), key=lambda row: row["published_utc"]):
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")

    counts = {name: len(results.get(name, [])) for name in subreddits if results.get(name)}
    report = {
        "date_pdt": day.isoformat(),
        "source": "Arctic Shift exact-window API",
        "status": "complete" if not errors else "partial",
        "complete_to_window_start": not errors,
        "target_post_count": sum(len(items) for items in results.values()),
        "archive_post_count": len(merged),
        "subreddits_requested": subreddits,
        "subreddits_completed": sorted(results),
        "counts_by_subreddit": dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].lower()))),
        "errors": errors,
        "window_epoch": {"after_exclusive": start_epoch, "before_exclusive": end_epoch},
        "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    BACKFILL_ROOT.mkdir(parents=True, exist_ok=True)
    report_path = BACKFILL_ROOT / f"{day.isoformat()}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
