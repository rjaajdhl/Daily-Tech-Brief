"""Per-article defect check shared by the runner's content gate and the Notion publisher.

One malformed article must never stop the whole day.  `row_defects` names every reason a single article cannot be
published as written; `quarantine` then isolates ONLY that article:

  * still has a headline, lead, body and at least one usable source  -> moved to section 11 (Review Failed) with the reasons
  * nothing renderable left (empty body, no source, error row, ...)   -> reasoned skip, original row kept under `withheld`
  * the selected event has no article row at all                      -> reasoned skip row is created
  * duplicate rows for one event                                      -> the best one is kept
  * rows for events that were not selected                            -> dropped

Zero publishable articles is a valid empty brief, not a day-level failure.
"""
from __future__ import annotations

import re
from typing import Iterable

SECTION_CODES = {f"{i:02}" for i in range(1, 12)}
TAGID = re.compile(r"\[S(\d+)\]")
FEEDBACK_ID = re.compile(r"\d{2}-\d{3,}")


def _text_ok(value) -> bool:
    return isinstance(value, str) and bool(TAGID.sub("", value).strip())


def usable_sources(row: dict) -> list[dict]:
    """Sources that have a tag and an http(s) URL; the others cannot be rendered as links."""
    return [s for s in (row.get("sources") or []) if isinstance(s, dict) and s.get("tag")
            and str(s.get("url") or "").startswith(("https://", "http://"))]


def renderable(row: dict) -> bool:
    """True when the row has enough content to be shown (so it is worth keeping in section 11)."""
    return (row.get("action") == "publish" and not row.get("_error")
            and all(_text_ok(row.get(k)) for k in ("headline", "lead", "body")) and bool(usable_sources(row)))


def structural_defects(row: dict) -> list[str]:
    """Reasons the row is structurally unusable, independent of editorial quality."""
    out: list[str] = []
    if row.get("_error"):
        out.append(f"Writer error: {str(row['_error'])[:120]}")
    if row.get("action") not in {"publish", "skip"}:
        out.append(f"Unknown action (action={row.get('action')!r})")
        return out
    if row.get("action") == "skip":
        if not str(row.get("reason") or "").strip():
            out.append("Skip reason missing")
        return out
    for key in ("headline", "lead", "body"):
        if not _text_ok(row.get(key)):
            out.append(f"{key} is empty")
    if not row.get("sources"):
        out.append("Sources missing")
    elif not usable_sources(row):
        out.append("No valid source URL")
    if row.get("section") not in SECTION_CODES:
        out.append(f"Invalid section number({row.get('section')!r})")
    # An unknown citation tag is recorded as a reason, but once the article already sits in section 11 with that reason
    # it renders fine (unknown tags are simply not linked), so it must not be flagged again.
    already_isolated = row.get("review_failed") is True and row.get("section") == "11" and bool(row.get("review_failed_reasons"))
    tags = {s.get("tag") for s in (row.get("sources") or []) if isinstance(s, dict)}
    cited = {"S" + t for t in TAGID.findall(str(row.get("lead") or "") + str(row.get("body") or ""))}
    if cited - tags and not already_isolated:
        out.append("Citation tags absent from source list: " + ", ".join(sorted(cited - tags)))
    return out


def quality_defects(row: dict) -> list[str]:
    """Editorial/fact failures on a publishable row that is not already flagged review_failed."""
    if row.get("action") != "publish" or row.get("review_failed") is True:
        return []
    v = row.get("validation") or {}
    out: list[str] = []
    if row.get("quality_ok") is not True:
        out.append("Editorial review did not pass")
    if v.get("ok") is not True or v.get("problems"):
        kinds = sorted({str(p.get("type")) for p in (v.get("problems") or []) if isinstance(p, dict)})
        out.append("Fact validation failed" + (": " + ", ".join(kinds) if kinds else ""))
    return out


def row_defects(row: dict) -> list[str]:
    """Every reason this single row cannot go out as it is (empty list = fine)."""
    out = structural_defects(row)
    if row.get("action") == "publish" and row.get("review_failed") is True:
        if row.get("section") != "11":
            out.append("Review Failed article is not in section 11")
        if not row.get("review_failed_reasons"):
            out.append("Review Failed reason missing")
    out += quality_defects(row)
    return out


def render_defects(row: dict, render_check) -> list[str]:
    """Run the section renderer/QA on this one row alone; any exception or FAIL is a defect of this row only.

    `render_check(row) -> list[str]` is supplied by the publisher (it knows the markdown/native-block layout).
    """
    try:
        return list(render_check(row) or [])
    except Exception as exc:  # noqa: BLE001 - a render problem belongs to this row, not to the day
        return [f"Rendering check failed: {type(exc).__name__}: {str(exc)[:120]}"]


def _score(row: dict) -> tuple:
    """Prefer the row that is publishable, then renderable, then has fewer defects."""
    d = row_defects(row)
    return (not d, renderable(row), -len(d))


def quarantine(rows: list[dict], selected_ids: Iterable[str], *, render_check=None) -> dict:
    """Isolate defective rows.  Returns {"rows": [...], "moved_to_11": [...], "skipped": [...], "dropped": [...],
    "created": [...], "reasons": {event_id: [reason, ...]}}.  Pure function: never raises on row content."""
    selected = list(dict.fromkeys(selected_ids))
    sel = set(selected)
    by_id: dict[str, list[dict]] = {}
    dropped: list[str] = []
    for r in rows:
        eid = r.get("event_id")
        if eid in sel:
            by_id.setdefault(eid, []).append(r)
        else:
            dropped.append(str(eid))
    out, moved, skipped, created = [], [], [], []
    reasons: dict[str, list[str]] = {}
    for eid in selected:
        cands = by_id.get(eid) or []
        if not cands:
            out.append({"event_id": eid, "action": "skip", "reason": "withheld (not published): No article result for the selected event"})
            created.append(eid)
            skipped.append(eid)
            reasons[eid] = ["Article result missing"]
            continue
        row = dict(max(cands, key=_score))
        if len(cands) > 1:
            reasons.setdefault(eid, []).append(f"Kept one of {len(cands)} article rows")
        # A few bad source URLs must not cost the article: drop only those sources while a usable one remains.
        good_src = usable_sources(row)
        if row.get("action") == "publish" and good_src and len(good_src) != len(row.get("sources") or []):
            reasons.setdefault(eid, []).append(f"Removed {len(row['sources']) - len(good_src)} invalid sources")
            row["sources"] = good_src
        defects = row_defects(row)
        rendered_bad = False
        if row.get("action") == "skip" and not defects:
            out.append(row)  # an already-reasoned skip is final: nothing to render, and its original reason must survive
            continue
        if not defects and render_check is not None:
            defects = render_defects(row, render_check)
            rendered_bad = bool(defects)
        if not defects:
            out.append(row)
            continue
        reasons.setdefault(eid, []).extend(defects)
        # A row that already sits in section 11 and still cannot be rendered has nowhere left to go: skip it.
        if renderable(row) and not (rendered_bad and row.get("section") == "11" and row.get("review_failed") is True):
            # Content is complete but editorially/structurally flagged: keep it visible in section 11 with the reasons.
            if row.get("section") != "11":
                row["section_original"] = row.get("section_original") or row.get("section")
            row["section"] = "11"
            row["review_failed"] = True
            old = list(row.get("review_failed_reasons") or [])
            row["review_failed_reasons"] = old + [d for d in defects if d not in old]
            row["quality_ok"] = False
            out.append(row)
            moved.append(eid)
        else:
            keep = {k: row.get(k) for k in ("headline", "lead", "body", "glossary", "sources", "validation", "quality_ok",
                                            "section", "action", "_error") if k in row}
            out.append({"event_id": eid, "section": row.get("section"), "final": row.get("final"), "action": "skip",
                        "reason": "withheld (not published): " + "; ".join(defects)[:300], "withheld": keep})
            skipped.append(eid)
    return {"rows": out, "moved_to_11": moved, "skipped": skipped, "dropped": dropped, "created": created, "reasons": reasons}
