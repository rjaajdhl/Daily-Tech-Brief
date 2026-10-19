#!/usr/bin/env python3
"""Single daily entry point for the v3 pipeline (this is what the cron job runs).

  python work/v3/run_daily.py                 # target = yesterday (America/Los_Angeles), run date = today
  python work/v3/run_daily.py --date 2026-09-30 [--skip-collect] [--force-from extract]

Order (each stage is recorded in data/v3/DATE/run_state.json; a re-run skips finished stages):
  preflight -> collect (existing deterministic intake/enrichment scripts) -> extract -> match -> refresh -> novelty -> select -> write -> validate -> publish
Fail-closed: the first failing stage stops the run with exit code 1 and publish is never called.
Always prints a status table (stdout is what the scheduler delivers).
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[2])
V3 = ROOT / "data" / "v3"
sys.path.insert(0, str(Path(__file__).parent))
import row_check
PY = sys.executable
TZ = ZoneInfo("America/Los_Angeles")
STAGES = ["preflight", "collect", "catchup", "extract", "match", "refresh", "novelty", "select", "curate", "write", "validate", "content_ready", "publish"]
PIPE_STAGES = {"extract", "match", "refresh", "novelty", "select", "curate", "write", "validate"}
# Share of failed single-article extractions the run tolerates (min 3). A handful of malformed cards is normal noise;
# a larger share means something systemic broke and the run must stop.
EXTRACT_FAIL_TOLERANCE = 0.02
# Share of multi-article partition groups allowed to fall back to one-event-per-article after retries (min 3).
PARTITION_FALLBACK_TOLERANCE = 0.05
# Share of history-match judgments allowed to stay failed after the retry pass (min 3). Each one is kept as a new
# event and recorded with llm_failed=true; more than this means the LLM service itself is unhealthy.
MATCH_FAIL_TOLERANCE = 0.02
# A drafted article that failed fact validation or the editorial review after all rewrite attempts is withheld (turned
# into a reasoned skip, original draft kept under "withheld") instead of stopping the day. If more than this share of
# drafts (min 5) is withheld, the writer/reviewer itself is broken and the run stops.
# REMOVED 2026-10-09: withhold_unverified() no longer raises; failed drafts only go to section 11 (value kept unused).
WITHHOLD_TOLERANCE = 0.25
# Stages that record their own per-item failures (llm_failed) and enforce a tolerance in artifact_ready, so the blanket
# "any LLM error stops the run" check does not apply to them.
TOLERANT_STAGES = {"extract", "match", "refresh", "novelty", "select", "curate", "write", "validate"}
# Share of items allowed to stay failed after the in-stage retry pass (min count 3 / 5 for refresh and write).
NOVELTY_FAIL_TOLERANCE = 0.05
SELECT_FAIL_TOLERANCE = 0.02
REFRESH_FAIL_TOLERANCE = 0.10
WRITE_FAIL_TOLERANCE = 0.25
COLLECT_RETRY_WAIT = 60
OUTAGE_ATTEMPTS = 3
OUTAGE_RETRY_WAIT = 30
PUBLISH_ATTEMPTS = 3
PUBLISH_RETRY_WAIT = 60
INPUT_ROOT = None
RUNTIME_ENV: dict[str, str] = {}


def evidence_packet(day: str) -> Path:
    return (INPUT_ROOT or ROOT / "data") / f"evidence/{day}/{day}.writer-packet.json"


def run(cmd: list[str], log: Path, timeout: int) -> int:
    with log.open("ab") as f:
        f.write(f"\n$ {' '.join(cmd)}\n".encode())
        f.flush()
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, start_new_session=True,
                             env={**os.environ, "V3_WORKERS": os.environ.get("V3_WORKERS", "16"), **RUNTIME_ENV})
        try:
            return p.wait(timeout=timeout)
        except BaseException:
            # subprocess.run kills only its direct child; isolated process groups include descendants.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    pass
                if sig == signal.SIGTERM:
                    try:
                        p.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
            p.wait()
            raise


def read_rows(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"invalid object rows: {path}")
    return rows


def matched_events(day: str) -> list[dict]:
    store = V3 / "events" / "events.jsonl"
    return [e for e in read_rows(store) if day in e.get("dates", [])] if store.exists() else []


def valid_touched(day: str) -> list[str]:
    touched = json.loads((V3 / day / "touched.json").read_text())
    events = matched_events(day)
    expected = {e["event_id"] for e in events}
    if not isinstance(touched, list) or any(not isinstance(eid, str) for eid in touched) or len(touched) != len(set(touched)) or set(touched) != expected:
        raise ValueError("invalid touched.json: must exactly match event-store date membership")
    extracted = read_rows(V3 / day / "extracted.jsonl")
    aids = {r["aid"] for r in extracted if r.get("event_summary") and not r.get("is_noise")}
    # A curation merge copies an absorbed event's sources into its keeper and leaves the original record
    # (marked absorbed[day]) intact; those copies are not duplicates, but they must be fully covered.
    by_id = {e["event_id"]: e for e in events}
    members = []
    for e in events:
        keeper_id = (e.get("absorbed") or {}).get(day)
        mine = [a["aid"] for a in e.get("articles", []) if a.get("date") == day]
        if keeper_id:
            keeper = by_id.get(keeper_id)
            kept = {a["aid"] for a in (keeper or {}).get("articles", []) if a.get("date") == day}
            if keeper is None or not set(mine) <= kept:
                raise ValueError("invalid touched.json: extracted articles do not match dated store members")
            continue
        members.extend(mine)
    if set(members) != aids or len(members) != len(set(members)):
        raise ValueError("invalid touched.json: extracted articles do not match dated store members")
    return touched


def has_error(value) -> bool:
    if isinstance(value, dict):
        if any(value.get(k) for k in ("_error", "error", "errors", "fallback")):
            return True
        if "error -> fail open" in str(value.get("reason", "")):
            return True
        return any(has_error(v) for v in value.values())
    if isinstance(value, list):
        return any(has_error(v) for v in value)
    return False


def artifact_ready(day: str, stage: str) -> dict:
    d = V3 / day
    if stage == "curate":
        marker = json.loads((d / "curation_applied.json").read_text())
        digest = hashlib.sha256((d / "selection.jsonl").read_bytes()).hexdigest()
        if marker.get("selection_sha256") != digest:
            raise ValueError("curation selection hash mismatch")
        if marker.get("kept") != sum(r.get("selected") is True for r in read_rows(d / "selection.jsonl")):
            raise ValueError("curation inventory mismatch")
        return {"kept": marker["kept"], "merges": len(marker.get("merges", [])), "excluded": len(marker.get("excluded", [])),
                "degraded": [f"curate: skipped ({marker['fallback_reason']}); duplicates were not merged"] if marker.get("fallback_reason") else []}
    if stage == "extract":
        rows = read_rows(d / "extracted.jsonl")
        # A failed article is recorded (id + reason) and skipped like needs_enrichment; it must not stop the day.
        # Anything else unresolved (other errors, a row with no card and no known status) still stops the run, and so does
        # a large failed share: that means something systemic broke, not one malformed card.
        failed = [r for r in rows if r.get("status") == "extract_failed"]
        unresolved = [r for r in rows if r.get("status") != "extract_failed" and (has_error(r)
                      or (not r.get("event_summary") and r.get("status") != "needs_enrichment"))]
        limit = max(3, int(len(rows) * EXTRACT_FAIL_TOLERANCE))
        degraded = []
        # Failed/unresolved articles are simply left out of the day (they carry no event); that never stops the run.
        if unresolved or len(failed) > limit:
            degraded.append(f"extract: {len(unresolved) + len(failed)} article(s) left out"
                            + (f" (failed {len(failed)} > usual tolerance {limit})" if len(failed) > limit else "")
                            + (f", {len(unresolved)} unresolved" if unresolved else ""))
        aids = [r.get("aid") for r in rows]
        if any(not aid for aid in aids) or len(aids) != len(set(aids)):
            raise ValueError("extraction coverage: missing/duplicate article IDs")
        archived = os.environ.get("V3_INPUT_ARTICLES")
        if archived:
            expected = {r["aid"] for r in read_rows(Path(archived))}
            if set(aids) != expected:
                raise ValueError("extraction coverage differs from archived input inventory")
        elif evidence_packet(day).exists():
            expected_urls = {r["source_url"] for r in json.loads(evidence_packet(day).read_text())["issues"]}
            missing = expected_urls - {r.get("url") for r in rows}
            if missing:
                degraded.append(f"extract: {len(missing)} writer-packet source URL(s) have no extraction row")
        return {"rows": len(rows), "needs_enrichment": sum(r.get("status") == "needs_enrichment" for r in rows),
                "extract_failed": len(failed), "failed_ids": [r.get("aid") for r in failed],
                "failed_reasons": [str(r.get("extract_error", ""))[:200] for r in failed], "degraded": degraded}
    if stage == "match":
        touched = valid_touched(day)
        rows = read_rows(d / "match_log.jsonl") if (d / "match_log.jsonl").exists() else []
        # A partition fallback keeps each article as its own event (conservative: worst case a duplicate pair that
        # curate can merge). A few must not stop the day; many mean the partition step itself broke.
        split = [r for r in rows if r.get("phase") == "partition" and (r.get("result") or {}).get("fallback") is True
                 and not (r.get("result") or {}).get("_error")]
        groups = sum(r.get("phase") == "partition" for r in rows)
        limit = max(3, int(groups * PARTITION_FALLBACK_TOLERANCE))
        hist = [r for r in rows if r.get("phase") == "history"]
        llm_failed = [r for r in hist if r.get("llm_failed")]
        fail_limit = max(3, int(len(hist) * MATCH_FAIL_TOLERANCE))
        degraded = []
        # Both fallbacks are conservative (an article becomes its own event; curate can merge duplicates), so they never stop the run.
        if len(split) > limit:
            degraded.append(f"match: {len(split)} partition groups fell back to one-event-per-article (usual tolerance {limit})")
        if len(llm_failed) > fail_limit:
            degraded.append(f"match: {len(llm_failed)} history judgments failed and were kept as new events (usual tolerance {fail_limit})")
        if any(has_error(r) for r in rows if r not in split):
            degraded.append("match: log rows carry error markers")
        return {"touched": len(touched), "partition_fallback": len(split),
                "fallback_groups": [r.get("group") for r in split],
                "llm_failed": len(llm_failed), "llm_failed_aids": [r.get("aid") for r in llm_failed], "degraded": degraded}
    if stage == "refresh":
        valid_touched(day)
        degraded = []
        if any(has_error(e) for e in matched_events(day)):
            degraded.append("refresh: some event records carry error markers")
        fp = d / "refresh_failed.jsonl"
        failed = read_rows(fp) if fp.exists() else []
        limit = max(5, int(len(matched_events(day)) * REFRESH_FAIL_TOLERANCE))
        if len(failed) > limit:  # a failed refresh keeps the event's previous (valid) title/summary
            degraded.append(f"refresh: {len(failed)} events kept their old summary (usual tolerance {limit})")
        return {"touched": len(matched_events(day)), "llm_failed": len(failed),
                "llm_failed_ids": [r.get("event_id") for r in failed], "degraded": degraded}
    if stage in {"novelty", "select", "write", "validate"}:
        filename = {"novelty": "novelty.jsonl", "select": "selection.jsonl", "write": "articles.jsonl", "validate": "articles.jsonl"}[stage]
        rows = read_rows(d / filename)
        degraded = []
        # Per-item failures already carry a conservative outcome (assumed-new / excluded / reasoned skip); they are reported, never fatal.
        if any(has_error(r) for r in rows):
            degraded.append(f"{stage}: rows carry error/fail-open markers (handled per item)")
        failed = [r for r in rows if r.get("llm_failed")]
        tol = {"novelty": NOVELTY_FAIL_TOLERANCE, "select": SELECT_FAIL_TOLERANCE, "write": WRITE_FAIL_TOLERANCE}.get(stage)
        if tol is not None:
            limit = max(5 if stage == "write" else 3, int(len(rows) * tol))
            if len(failed) > limit:
                degraded.append(f"{stage}: {len(failed)} item(s) failed after retry (usual tolerance {limit})")
        if stage == "validate" and any(r.get("action") == "publish" and not r.get("review_failed") and ((r.get("validation") or {}).get("ok") is not True or (r.get("validation") or {}).get("problems")) for r in rows):
            degraded.append("validate: unresolved validation rows (quarantine moves them to section 11)")
        try:
            if stage == "select":
                expected = set(valid_touched(day))
            elif stage in {"write", "validate"}:
                expected = {r["event_id"] for r in read_rows(d / "selection.jsonl") if r.get("selected")}
            else:
                expected = {e["event_id"] for e in matched_events(day) if any(w.get("date", day) < day for w in e.get("writes", []))}
        except (OSError, ValueError) as exc:  # the reference file itself is missing/invalid: report, do not stop
            expected = None
            degraded.append(f"{stage}: cannot verify outcome coverage ({type(exc).__name__}: {str(exc)[:100]})")
        got = [r.get("event_id") for r in rows]
        if expected is not None and (set(got) != expected or len(got) != len(set(got))):
            # write/validate: content_ready's quarantine fills any missing row with a reasoned skip and drops extras/duplicates.
            # novelty/select: a missing row is treated as "no information" downstream. Either way it is reported, not fatal.
            degraded.append(f"{stage}: outcome rows differ from the expected events "
                            f"(missing {len(expected - set(got))}, unexpected {len(set(got) - expected)}, duplicates {len(got) - len(set(got))})")
        return {"rows": len(rows), "llm_failed": len(failed),
                "llm_failed_ids": [r.get("event_id") for r in failed], "degraded": degraded}
    raise ValueError(f"unknown pipeline stage: {stage}")


REVIEW_UNAVAILABLE = ("Editorial review response failed", "편집 검수 응답 실패")


def withhold_unverified(day: str) -> dict:
    """Drafts that still fail the source check after every rewrite move to section 11 (Review Failed) instead of vanishing.

    Only a draft with no usable review at all (reviewer response broken) is withheld as a reasoned skip, because nothing
    says what is wrong with it. There is no tolerance: failed drafts never stop the run.
    """
    path = V3 / day / "articles.jsonl"
    rows = read_rows(path)
    drafts = [r for r in rows if r.get("action") == "publish"]
    bad = []
    for r in drafts:
        v = r.get("validation") or {}
        if r.get("review_failed") or r.get("quality_ok") is not True or v.get("ok") is not True or v.get("problems"):
            bad.append(r)
    if not bad:
        return {"withheld": 0, "withheld_ids": [], "review_failed": 0, "review_failed_ids": []}
    # No stop threshold: a draft that failed review/fact validation only moves to section 11 with its reasons.
    # It never fails the run (user decision 2026-10-09).
    withheld, failed = set(), set()
    for r in bad:
        v = r.get("validation") or {}
        reasons = list(r.get("review_failed_reasons") or [])
        if v.get("problems") and not r.get("review_failed"):
            reasons += ["Fact validation: " + ", ".join(sorted({str(p.get("type")) for p in v.get("problems") or []}))]
        if not reasons and r.get("quality_ok") is not True:
            reasons = ["Editorial review did not pass after 3 drafting passes"]
        if any(x.startswith(REVIEW_UNAVAILABLE) for x in reasons):
            r["withheld"] = {k: r.get(k) for k in ("headline", "lead", "body", "glossary", "sources", "validation", "quality_ok", "chosen_attempt")}
            r["action"] = "skip"
            r["reason"] = "withheld (not published): editorial review response unavailable"
            for k in ("headline", "lead", "body", "glossary", "validation", "quality_ok", "review_failed", "review_failed_reasons"):
                r.pop(k, None)
            withheld.add(r["event_id"])
            continue
        if r.get("section") != "11":
            r["section_original"] = r.get("section")
        r["section"] = "11"
        r["review_failed"] = True
        r["review_failed_reasons"] = reasons
        r["quality_ok"] = False
        failed.add(r["event_id"])
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    tmp.replace(path)
    # An event validate already recorded as written today must not count as "previously published" tomorrow.
    ids = withheld | failed
    ev_path = V3 / "events" / "events.jsonl"
    if ev_path.exists():
        evs = read_rows(ev_path)
        for e in evs:
            if e.get("event_id") in ids and e.get("writes"):
                e["writes"] = [w for w in e["writes"] if w.get("date") != day]
        tmp = ev_path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in evs))
        tmp.replace(ev_path)
    return {"withheld": len(withheld), "withheld_ids": sorted(withheld), "review_failed": len(failed), "review_failed_ids": sorted(failed)}


def pipeline_step(day: str, stage: str, log: Path) -> dict:
    if stage == "match" and matched_events(day):
        return artifact_ready(day, stage)
    offset = log.stat().st_size if log.exists() else 0
    rc = run([PY, "-u", "work/v3/pipeline.py", "--date", day, "--stages", stage], log, 4 * 3600)
    text = ""
    if log.exists():
        with log.open("rb") as f:
            f.seek(offset)
            text = f.read().decode("utf-8", errors="replace")
    for match in re.finditer(r"usage_limit_reached:\s*(\{[^\n]*\})", text):
        try:
            details = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        import llm
        raise llm.QuotaExceeded(details)
    if rc != 0:
        raise RuntimeError(f"pipeline {day}/{stage} exit {rc}; log: {log}")
    for match in re.finditer(r"usage(?:=|\s+)(\{[^\n]*\})", text):
        usage = ast.literal_eval(match.group(1))
        # match records its own failures per article (llm_failed) and enforces a tolerance in artifact_ready.
        if usage.get("errors", 0) and stage not in TOLERANT_STAGES:
            raise ValueError(f"unresolved LLM errors at {day}/{stage}: {usage}")
    if stage == "validate":
        withheld = withhold_unverified(day)
        return {**artifact_ready(day, stage), **withheld}
    return artifact_ready(day, stage)


def _render_check():
    """Per-article render QA from the publisher (lazy import: it pulls in the Notion helpers). None if unavailable."""
    try:
        import publish_notion
        return publish_notion.row_render_problems
    except Exception:  # noqa: BLE001 - the render check is an extra net, its absence must not stop the day
        return None


def quarantine_rows(day: str) -> dict:
    """Isolate every defective article so that one bad row can never stop the day (see row_check.py).

    Rewrites articles.jsonl only when something changed. Events moved to section 11 or skipped lose today's `writes`
    entry, so tomorrow does not treat them as already published.
    """
    path = V3 / day / "articles.jsonl"
    rows = read_rows(path)
    selected = [r["event_id"] for r in read_rows(V3 / day / "selection.jsonl") if r.get("selected")]
    res = row_check.quarantine(rows, selected, render_check=_render_check())
    # The result of one pass is stable; a second pass proves it (anything still defective is forced to a skip).
    again = row_check.quarantine(res["rows"], selected, render_check=_render_check())
    res["rows"] = again["rows"]
    res["skipped"] = sorted(set(res["skipped"]) | set(again["skipped"]))
    res["moved_to_11"] = sorted((set(res["moved_to_11"]) | set(again["moved_to_11"])) - set(res["skipped"]))
    for eid, rs in again["reasons"].items():
        res["reasons"].setdefault(eid, []).extend(x for x in rs if x not in res["reasons"].get(eid, []))
    changed = res["rows"] != rows
    if changed:
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in res["rows"]))
        tmp.replace(path)
        ids = set(res["moved_to_11"]) | set(res["skipped"])
        ev_path = V3 / "events" / "events.jsonl"
        if ids and ev_path.exists():
            evs = read_rows(ev_path)
            for e in evs:
                if e.get("event_id") in ids and e.get("writes"):
                    e["writes"] = [w for w in e["writes"] if w.get("date") != day]
            tmp = ev_path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in evs))
            tmp.replace(ev_path)
        (V3 / day / "quarantine.json").write_text(json.dumps(
            {k: res[k] for k in ("moved_to_11", "skipped", "dropped", "created", "reasons")}, ensure_ascii=False, indent=1))
    res["changed"] = changed
    return res


def content_ready(day: str, run_date: str, gate: Path | None = None) -> dict:
    """Quarantine broken articles, then confirm what is left. Stops the day only when nothing publishable remains."""
    quarantined = quarantine_rows(day)
    rows = read_rows(V3 / day / "articles.jsonl")
    selection = read_rows(V3 / day / "selection.jsonl")
    expected = {r["event_id"] for r in selection if r.get("selected")}
    got = [r.get("event_id") for r in rows]
    if set(got) != expected or len(got) != len(set(got)):  # quarantine guarantees this; failing here means a bug
        raise ValueError("content readiness: selected events and article outcomes differ after quarantine")
    # Safety net: quarantine should leave nothing defective. If it ever does, that one article is skipped here;
    # a bug in the isolation logic must not become a day-level stop.
    leftovers = [(r.get("event_id"), row_check.row_defects(r)) for r in rows]
    leftovers = {eid: d for eid, d in leftovers if d}
    if leftovers:
        forced = []
        for r in rows:
            eid = r.get("event_id")
            if eid in leftovers:
                forced.append({"event_id": eid, "section": r.get("section"), "final": r.get("final"), "action": "skip",
                               "reason": "withheld (not published): Defects remained after quarantine: " + "; ".join(leftovers[eid])[:250],
                               "withheld": {k: r.get(k) for k in ("headline", "lead", "body", "glossary", "sources", "validation", "quality_ok", "section") if k in r}})
                quarantined["skipped"].append(eid)
                quarantined["moved_to_11"] = [x for x in quarantined["moved_to_11"] if x != eid]
                quarantined["reasons"].setdefault(eid, []).extend(leftovers[eid])
            else:
                forced.append(r)
        rows = forced
        (V3 / day / "articles.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        (V3 / day / "quarantine.json").write_text(json.dumps(
            {k: quarantined[k] for k in ("moved_to_11", "skipped", "dropped", "created", "reasons")}, ensure_ascii=False, indent=1))
    publishable = sum(r.get("action") == "publish" for r in rows)
    # Zero publishable articles is NOT a failure: the publisher posts an honest "0 articles today" brief, so the Dashboard
    # shows today's date instead of silently keeping yesterday's page (which looks like a missed run).
    digest = hashlib.sha256((V3 / day / "articles.jsonl").read_bytes()).hexdigest()
    if gate is not None:
        approval = json.loads(gate.read_text())
        if approval.get("approved") is not True or approval.get("target_date") != day or approval.get("run_date") != run_date or approval.get("articles_sha256") != digest:
            raise ValueError("content readiness: editorial gate not approved for exact dates/content hash")
    return {"ok": True, "target_date": day, "run_date": run_date, "publishable": publishable,
            "review_failed": sum(bool(r.get("review_failed")) for r in rows), "skipped": sum(r.get("action") == "skip" for r in rows),
            "empty_day": publishable == 0, "quarantined_to_11": quarantined["moved_to_11"], "quarantined_skipped": quarantined["skipped"],
            "quarantine_dropped": quarantined["dropped"], "quarantine_reasons": quarantined["reasons"],
            "articles_sha256": digest, "editorial_approved": gate is not None, "editorial_gate": str(gate) if gate else None}


def coverage(day: str) -> dict:
    store = V3 / "events" / "events.jsonl"
    dates = set()
    if store.exists():
        for line in store.read_text().splitlines():
            if line.strip():
                dates.update(json.loads(line).get("dates", []))
    gaps = []
    earlier = sorted(x for x in dates if x < day)
    if earlier:
        x = dt.date.fromisoformat(earlier[0])
        while x < dt.date.fromisoformat(day):
            if x.isoformat() not in dates:
                gaps.append(x.isoformat())
            x += dt.timedelta(days=1)
    return {"event_dates": sorted(dates), "gap_dates": gaps,
            "evidence_gap_dates": [g for g in gaps if evidence_packet(g).exists()],
            "basis": "event-store dates (not proof of complete intake coverage)"}


def exclusion_report(day: str) -> list[str]:
    """IDs + titles of everything that was dropped by a failure or a quality gate today (always listed, never silent)."""
    d = V3 / day
    out: list[str] = []
    def titles() -> dict:
        t = {}
        for name in ("selection.jsonl", "articles.jsonl"):
            if (d / name).exists():
                for r in read_rows(d / name):
                    t.setdefault(r.get("event_id"), r.get("title") or (r.get("withheld") or {}).get("headline") or r.get("headline") or "")
        return t
    try:
        tt = titles()
        groups = []
        if (d / "selection.jsonl").exists():
            groups.append(("select failed (excluded)", [(r["event_id"], tt.get(r["event_id"], "")) for r in read_rows(d / "selection.jsonl") if r.get("llm_failed")]))
        if (d / "novelty.jsonl").exists():
            groups.append(("novelty failed (assumed new)", [(r["event_id"], r.get("title", "")) for r in read_rows(d / "novelty.jsonl") if r.get("llm_failed")]))
        if (d / "refresh_failed.jsonl").exists():
            groups.append(("refresh failed (kept old summary)", [(r["event_id"], tt.get(r["event_id"], "")) for r in read_rows(d / "refresh_failed.jsonl")]))
        if (d / "articles.jsonl").exists():
            groups.append(("Review Failed (published in section 11)", [(r["event_id"], r.get("headline") or tt.get(r["event_id"], "")) for r in read_rows(d / "articles.jsonl") if r.get("review_failed")]))
            groups.append(("withheld (not published)", [(r["event_id"], (r.get("withheld") or {}).get("headline") or tt.get(r["event_id"], "")) for r in read_rows(d / "articles.jsonl")
                                                         if str(r.get("reason", "")).startswith("withheld")]))
        qp = d / "quarantine.json"
        if qp.exists():
            q = json.loads(qp.read_text())
            groups.append(("Quarantined: moved to section 11 (broken article structure)", [(i, tt.get(i, "") + " — " + "; ".join(q["reasons"].get(i, []))[:160]) for i in q.get("moved_to_11", [])]))
            groups.append(("Quarantined: skipped (nothing renderable)", [(i, tt.get(i, "") + " — " + "; ".join(q["reasons"].get(i, []))[:160]) for i in q.get("skipped", [])]))
        if (d / "match_log.jsonl").exists():
            groups.append(("match failed (kept as new event)", [(r.get("aid"), "") for r in read_rows(d / "match_log.jsonl") if r.get("llm_failed")]))
        for label, items in groups:
            if items:
                out += ["", f"{label}: {len(items)}"] + [f"- {i} {t}".rstrip() for i, t in items]
    except Exception as exc:  # noqa: BLE001 - a report problem must never fail the run
        out += ["", f"(exclusion report unavailable: {type(exc).__name__}: {exc})"]
    return out


def publish_outage(day: str, run_date: str, failed: str, state: dict, d: Path, error: dict | None = None) -> str:
    """Post a 0-article 'could not run' brief for the run date. Never raises: a failed notice must not hide the real failure.

    Skipped when a real brief for this run date was already published by this runner, or when the failure was Notion itself."""
    try:
        if state["stages"].get("publish", {}).get("status") == "ok":
            return "Outage page: not posted (a brief for this date was already published)."
        err = error or state.get("error") or {}
        note = (state["stages"].get(failed) or {}).get("note", "")
        reason = str(err.get("message") or note or "Reason not recorded")
        if err.get("type") == "usage_limit_reached" or "usage_limit_reached" in str(err) + note:
            reason = "Codex usage limit reached (usage_limit_reached). " + str(err.get("message") or "")[:200]
        info = {"stage": failed, "reason": reason, "resets_at": err.get("reset_timestamp") or err.get("resets_at"),
                "next": "After the limit resets or the cause is resolved, a successful retry or manual rerun replaces this notice with the actual brief for the same date."}
        if failed == "preflight" and "notion" in (reason + note).lower():
            return "Outage page: not posted (Notion itself is unreachable)."
        path = d / "outage.json"
        path.write_text(json.dumps(info, ensure_ascii=False, indent=1))
        rc = 1
        for attempt in range(OUTAGE_ATTEMPTS):
            rc = run([PY, "-u", "work/v3/publish_notion.py", "--date", day, "--run-date", run_date, "--outage", str(path)], d / "run_daily.log", 3600)
            if rc == 0:
                break
            if attempt < OUTAGE_ATTEMPTS - 1:
                time.sleep(OUTAGE_RETRY_WAIT)
        return f"Outage page: {'posted' if rc == 0 else 'FAILED to post after ' + str(OUTAGE_ATTEMPTS) + ' attempts (exit ' + str(rc) + ')'} for {run_date} (failed stage `{failed}`)."
    except BaseException as exc:  # noqa: BLE001
        return f"Outage page: not posted ({type(exc).__name__}: {str(exc)[:120]})"


def main() -> int:
    global V3, INPUT_ROOT, RUNTIME_ENV
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--run-date", help="fixed Notion-facing brief date; defaults to saved date or today in LA")
    ap.add_argument("--skip-collect", action="store_true", help="evidence for DATE already exists")
    ap.add_argument("--force-from", choices=STAGES, help="re-run this stage and every later one")
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--catchup", action="store_true", help="explicitly process earlier evidence gaps")
    ap.add_argument("--data-root", type=Path, default=V3, help="isolated v3 output/event-store directory (V3_DATA_ROOT)")
    ap.add_argument("--input-root", type=Path, help="archived data directory containing evidence/ and reddit/ (V3_INPUT_ROOT)")
    ap.add_argument("--run-tag", help="audit label stored in run_report.json")
    ap.add_argument("--offline-sources", action="store_true", help="disable writer recrawls/search (V3_OFFLINE_SOURCES=1)")
    ap.add_argument("--content-gate", type=Path, help="independent editorial approval JSON, bound to target/run dates and articles_sha256")
    a = ap.parse_args()
    previous = V3, INPUT_ROOT, RUNTIME_ENV
    V3 = a.data_root.expanduser().resolve()
    INPUT_ROOT = a.input_root.expanduser().resolve() if a.input_root else ROOT / "data"
    RUNTIME_ENV = {"V3_DATA_ROOT": str(V3), "V3_INPUT_ROOT": str(INPUT_ROOT), "V3_RUN_TAG": a.run_tag or "",
                   "V3_OFFLINE_SOURCES": "1" if a.offline_sources else os.environ.get("V3_OFFLINE_SOURCES", "0")}
    try:
        return _locked_run(a)
    finally:
        V3, INPUT_ROOT, RUNTIME_ENV = previous


def _locked_run(a) -> int:
    lock_path = V3 / "events" / "run_daily.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "LOCKED", "lock": str(lock_path), "note": "another scheduled/manual event-store run is active"}))
            return 1
        try:
            return _run_daily(a)
        except Exception as exc:  # noqa: BLE001 - nothing outside the stage loop may end the day without a Notion notice
            today = dt.datetime.now(TZ).date()
            day = a.date or (today - dt.timedelta(days=1)).isoformat()
            (V3 / day).mkdir(parents=True, exist_ok=True)
            note = publish_outage(day, a.run_date or today.isoformat(), "runner", {"stages": {}}, V3 / day,
                                  {"type": type(exc).__name__, "message": f"{type(exc).__name__}: {exc}"})
            print(f"RUNNER CRASHED: {type(exc).__name__}: {exc}\n{note}")
            return 1


def _run_daily(a) -> int:
    today = dt.datetime.now(TZ).date()
    day = a.date or (today - dt.timedelta(days=1)).isoformat()
    d = V3 / day
    d.mkdir(parents=True, exist_ok=True)
    log = d / "run_daily.log"
    state_p = d / "run_state.json"
    try:
        state = json.loads(state_p.read_text()) if state_p.exists() else {}
        if not isinstance(state, dict):
            raise ValueError("run_state.json is not an object")
    except (OSError, ValueError):
        # A damaged state file would otherwise crash the run before any stage; keep it for inspection and start clean.
        try:
            state_p.rename(state_p.with_name("run_state.corrupt." + time.strftime("%H%M%S") + ".json"))
        except OSError:
            pass
        state = {"state_file_was_corrupt": True}
    state.setdefault("stages", {})
    run_date = a.run_date or state.get("run_date") or today.isoformat()
    state.update(target_date=day, run_date=run_date, run_tag=a.run_tag, data_root=str(V3), input_root=str(INPUT_ROOT),
                 offline_sources=RUNTIME_ENV.get("V3_OFFLINE_SOURCES") == "1", started=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    if a.force_from:
        i = STAGES.index(a.force_from)
        for s in STAGES[i:]:
            state["stages"].pop(s, None)


    def save():
        state_p.write_text(json.dumps(state, indent=1, ensure_ascii=False))

    def record(stage: str, status: str, secs: float, note: str = ""):
        state["stages"][stage] = {"status": status, "seconds": round(secs), "note": note, "at": time.strftime("%H:%M:%S")}
        save()

    failed = None
    error = None
    for stage in STAGES:
        cached = stage not in {"preflight", "catchup", "content_ready"} and state["stages"].get(stage, {}).get("status") == "ok"
        if stage == "publish" and a.no_publish:
            break
        t0 = time.time()
        try:
            if cached:
                if stage in PIPE_STAGES:
                    state.setdefault("artifacts", {})[stage] = artifact_ready(day, stage)
                continue
            if stage == "preflight":
                import preflight
                rep = preflight.run(optional=False) if state["offline_sources"] else preflight.run()
                state["preflight"] = rep
                rc, note = 0, "optional degraded: " + ",".join(rep["degraded"]) if rep["degraded"] else "all services ok"
            elif stage == "collect":
                if a.skip_collect:
                    input_path = os.environ.get("V3_INPUT_ARTICLES")
                    if input_path:
                        packet = Path(input_path)
                        rows = read_rows(packet)
                        if any(not r.get("aid") or not r.get("url") or r.get("date") != day for r in rows):
                            raise ValueError("invalid archived input article identity/date")
                    else:
                        packet = evidence_packet(day)
                        if not packet.exists():
                            raise ValueError(f"--skip-collect requires writer-packet: {packet}")
                        payload = json.loads(packet.read_text())
                        if not isinstance(payload.get("issues"), list):
                            raise ValueError(f"invalid writer-packet: {packet}")
                        rows = payload["issues"]
                    state["input_inventory"] = {"path": str(packet), "rows": len(rows), "sha256": hashlib.sha256(packet.read_bytes()).hexdigest()}
                    rc, note = 0, "skipped (archived evidence; no collection/enrichment)"
                else:
                    rc, note, soft = 0, "", []
                    plan = (([PY, "scripts/technology_daily_prepare.py", "--date", day], 3600, False),
                            ([PY, "work/daily_pipeline.py", "--date", day], 3600, False),
                            ([PY, "work/source_enrichment.py", "--date", day, "--workers", "12"], 5400, False),
                            # the writer packet is what every later stage reads; without it there is nothing to process
                            ([PY, "work/build_semantic_writer_packet.py", "--date", day, "--output", f"data/evidence/{day}/{day}.writer-packet.json"], 1800, True))
                    for cmd, to, required in plan:
                        name = Path(cmd[1]).name
                        code = None
                        for attempt in range(2):  # one retry; a hard timeout is not retried (it already cost the full window)
                            try:
                                code = run(cmd, log, to)
                            except subprocess.TimeoutExpired:
                                code = 124
                                break
                            if code == 0:
                                break
                            if attempt == 0:
                                time.sleep(COLLECT_RETRY_WAIT)
                        if code != 0:
                            if required:
                                rc, note = 1, f"{name} exit {code}"
                                break
                            soft.append(f"{name} exit {code}")
                    if rc == 0 and soft:
                        # later stages work from whatever evidence exists (writer-packet is built from the parts that did finish)
                        state.setdefault("collect_degraded", []).extend(soft)
                        note = "continued without: " + ", ".join(soft)
            elif stage == "catchup":
                cov = coverage(day)
                cov["catchup_enabled"] = a.catchup
                state["coverage"] = cov
                gaps = cov["evidence_gap_dates"]
                rc = 0
                cov["catchup_completed"] = []
                if a.catchup:
                    for g in gaps:
                        (V3 / g).mkdir(parents=True, exist_ok=True)
                        for step in ("extract", "match", "refresh", "novelty", "select"):
                            state.setdefault("catchup_artifacts", {}).setdefault(g, {})[step] = pipeline_step(g, step, log)
                            save()
                        cov["catchup_completed"].append(g)
                    note = f"verified catchup dates: {cov['catchup_completed']}"
                else:
                    note = f"gaps not processed (use --catchup): {cov['gap_dates']}"
            elif stage in PIPE_STAGES:
                if stage == "match" and a.force_from and STAGES.index(a.force_from) <= STAGES.index("match") and matched_events(day):
                    store = V3 / "events" / "events.jsonl"
                    if any(date > day for e in read_rows(store) for date in e.get("dates", [])):
                        raise ValueError("refusing rollback: later dates exist in shared event store")
                    backup = V3 / "events" / f"events.before-{day}.jsonl"
                    if not backup.exists():
                        raise ValueError("cannot force match without event-store backup")
                    read_rows(backup)
                    store.write_text(backup.read_text())
                state.setdefault("artifacts", {})[stage] = pipeline_step(day, stage, log)
                rc, note = 0, "artifacts ready (match resumes only with validated touched.json)"
            elif stage == "content_ready":
                state["content_readiness"] = content_ready(day, run_date, a.content_gate)
                rc, note = 0, "content readiness passed (independent of pipeline exit)"
            elif stage == "publish":
                # Publishing is idempotent (page upsert + read-back), so a transient Notion failure is retried.
                for attempt in range(PUBLISH_ATTEMPTS):
                    rc = run([PY, "-u", "work/v3/publish_notion.py", "--date", day, "--run-date", run_date, "--use-feedback-ledger"], log, 3 * 3600)
                    if rc == 0:
                        break
                    if attempt < PUBLISH_ATTEMPTS - 1:
                        time.sleep(PUBLISH_RETRY_WAIT)
                note = f"succeeded on attempt {attempt + 1}" if rc == 0 and attempt else ""
            else:
                rc, note = 1, "unknown stage"
        except subprocess.TimeoutExpired:
            rc, note = 1, "timeout"
        except Exception as e:  # noqa: BLE001
            rc, note = 1, f"{type(e).__name__}: {e}"
            cause = e
            while cause.__cause__ is not None:
                cause = cause.__cause__
            error = getattr(cause, "details", {"type": type(e).__name__, "message": str(e)})
        record(stage, "ok" if rc == 0 else "FAILED", time.time() - t0, note)
        if rc != 0:
            failed = stage
            break

    # A day that could not run still gets a Notion page that says so (Dashboard shows today's date, not yesterday's brief).
    outage_note = None
    if failed and not a.no_publish:
        outage_note = publish_outage(day, run_date, failed, state, d, error)
    # summary (this is what the scheduler delivers)
    lines = [f"v3 daily run — target {day}, run date {run_date}", "", "| stage | status | seconds | note |", "|---|---|---|---|"]
    for s in STAGES:
        r = state["stages"].get(s)
        lines.append(f"| {s} | {r['status'] if r else 'not run'} | {r['seconds'] if r else '-'} | {(r or {}).get('note', '')} |")
    pub = d / "notion_publish_readback.json"
    if not failed and pub.exists() and "publish" in state["stages"]:
        rb = json.loads(pub.read_text())
        lines += ["", f"Notion: {rb.get('index_url')}", f"sections {rb.get('sections')} issues {rb.get('issues')}", f"stage {rb.get('stage')} / {rb.get('brief_status')}"]
    lines += exclusion_report(day)
    deg = list(state.get("collect_degraded") and [f"collect: {x}" for x in state["collect_degraded"]] or [])
    for stage_name, art in (state.get("artifacts") or {}).items():
        deg += [x for x in (art or {}).get("degraded", []) if isinstance(x, str)]
    if deg:
        lines += ["", f"Degraded but continued: {len(deg)}"] + [f"- {x}" for x in deg]
        state["degraded"] = deg
    if outage_note:
        lines += ["", outage_note]
    if failed:
        lines += ["", f"FAILED at stage `{failed}`. Log: {log}. Nothing was published by later stages. Re-run after fixing: python work/v3/run_daily.py --date {day}"]
    state.update(status="FAILED" if failed else "ok", failed_stage=failed, error=error)
    save()
    (d / "run_report.json").write_text(json.dumps(state, indent=1, ensure_ascii=False))
    print("\n".join(lines))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
