#!/usr/bin/env python3
"""Prepare exact-window Reddit data, then emit the daily digest for cron injection."""

from __future__ import annotations
import os

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

PROFILE_ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
SCRIPT_ROOT = PROFILE_ROOT / "scripts"
BACKFILL_ROOT = PROFILE_ROOT / "data" / "reddit" / "backfills"
SNAPSHOT_ROOT = PROFILE_ROOT / "data" / "reddit" / "snapshots"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="PDT/PST calendar date (YYYY-MM-DD)")
    return parser.parse_args()


def target_day(date_text: str | None) -> dt.date:
    if date_text:
        return dt.date.fromisoformat(date_text)
    return dt.datetime.now(LOCAL_TZ).date() - dt.timedelta(days=1)


def complete_backfill_exists(day: dt.date) -> bool:
    report_path = BACKFILL_ROOT / f"{day.isoformat()}.json"
    archive_path = SNAPSHOT_ROOT / f"{day.isoformat()}.jsonl"
    if not report_path.exists() or not archive_path.exists():
        return False
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return bool(report.get("complete_to_window_start"))


def main() -> int:
    args = parse_args()
    day = target_day(args.date)
    preparation_note = ""

    if complete_backfill_exists(day):
        preparation_note = "Exact-window archive backfill already complete; reused local archive."
    else:
        backfill = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_ROOT / "reddit_archive_backfill.py"),
                "--date",
                day.isoformat(),
                "--workers",
                "4",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if backfill.returncode == 0:
            preparation_note = "Exact-window archive backfill completed successfully."
        else:
            tail = (backfill.stderr or backfill.stdout)[-1000:].replace("\n", " | ")
            preparation_note = (
                "Exact-window archive backfill failed or was partial; continue with local snapshots "
                f"and web fallbacks. Diagnostic: {tail}"
            )

    digest = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_ROOT / "reddit_daily_digest.py"),
            "--date",
            day.isoformat(),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if digest.returncode != 0:
        print(digest.stderr or digest.stdout, file=sys.stderr)
        return digest.returncode

    print(f"REDDIT DATA PREPARATION: {preparation_note}")
    print(digest.stdout, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
