#!/usr/bin/env python3
"""no_agent cron entry: one-shot queued replay, otherwise the locked daily runner."""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
PY = sys.executable
RETRY_WAIT = 600


def _dates() -> tuple[str, str]:
    """(target day, run date) exactly as run_daily.py would pick them, honouring --date/--run-date."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    today = dt.datetime.now(ZoneInfo("America/Los_Angeles")).date()
    args = sys.argv[1:]
    opt = lambda name: args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None
    return opt("--date") or (today - dt.timedelta(days=1)).isoformat(), opt("--run-date") or today.isoformat()


def post_outage(stage: str, reason: str) -> str:
    """Last-resort Notion notice that this day did not run. Deliberately independent of run_daily.py (which may be the thing
    that is broken): it only needs publish_notion.py. Never raises, never replaces a published brief (publish_notion guards)."""
    try:
        day, run_date = _dates()
        out_dir = ROOT / "data/v3" / day
        out_dir.mkdir(parents=True, exist_ok=True)
        info = out_dir / "outage.json"
        info.write_text(json.dumps({"stage": stage, "reason": reason[:600],
                                    "next": "After the cause is resolved, a successful rerun replaces this notice with the actual brief for the same date."}, ensure_ascii=False))
        rc = 1
        for attempt in range(3):
            r = subprocess.run([PY, "-u", str(ROOT / "work/v3/publish_notion.py"), "--date", day, "--run-date", run_date, "--outage", str(info)],
                               cwd=ROOT, capture_output=True, text=True, timeout=3600)
            rc = r.returncode
            if rc == 0:
                break
            time.sleep(30)
        return f"Outage page: {'posted' if rc == 0 else 'FAILED to post (exit ' + str(rc) + ')'} for {run_date} (stage `{stage}`)."
    except BaseException as exc:  # noqa: BLE001
        return f"Outage page: not posted ({type(exc).__name__}: {str(exc)[:120]})"


def main() -> int:
    # One automatic retry after RETRY_WAIT seconds: finished stages are cached, so only the failed stage and later ones
    # run again. Quota exhaustion (usage_limit_reached) is not retried because waiting 10 minutes does not help.
    cmd = [PY, "-u", str(ROOT / "work/v3/run_daily.py"), *sys.argv[1:]]
    child: list[subprocess.Popen] = []

    def forward(signum, _frame):  # cron cancellation must reach the locked runner (it used to via execv)
        for p in child:
            if p.poll() is None:
                p.send_signal(signum)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    def attempt() -> tuple[int, str]:
        child.clear()
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        child.append(p)
        out, _ = p.communicate()
        sys.stdout.write(out)
        sys.stdout.flush()
        return p.returncode, out

    rc, out = attempt()
    if rc == 0 or "LOCKED" in out:
        return rc
    if "usage_limit_reached" in out:  # waiting does not help; run_daily already posted its own notice, add ours only if it did not
        if "Outage page: posted" not in out:
            print(post_outage("preflight", "Codex usage limit reached (usage_limit_reached)"))
        return rc
    print(f"\n--- first attempt failed (exit {rc}); retrying once in {RETRY_WAIT // 60} min ---\n", flush=True)
    time.sleep(RETRY_WAIT)
    rc, out2 = attempt()
    if rc != 0 and "LOCKED" not in out2 and "Outage page: posted" not in out2:
        # run_daily.py ended without managing to post its own notice (e.g. it crashed on import): do it from here.
        tail = " ".join(out2.strip().splitlines()[-6:])[:500] or "run_daily.py exited without output"
        print(post_outage("runner", f"run_daily.py exit {rc}: {tail}"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
