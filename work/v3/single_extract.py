"""One article per extraction call.

Replaces the 16-per-call batch: there, one malformed card made the whole batch fail validation twice and all 16 articles
became `extract_failed`, which stopped the day's publication. Here a bad card costs only itself.
Validation rules are the unchanged batched_steps ones (entities<=6, key_facts<=5, event_type in the fixed set, ...).
A rejected card is retried once WITH the rejection reason; if it still fails, the row keeps `status=extract_failed`
and `extract_error` (so the cause is never lost again). Quota errors always propagate."""
from __future__ import annotations

import llm
from batched_steps import EXTRACT_FIELDS, EXTRACT_TEXT_LIMIT, _validate_fields


def user_message(item: dict) -> str:
    body = (item.get("text") or "")[:EXTRACT_TEXT_LIMIT].strip()
    head_only = len(body) < 200 and item.get("enrichment") not in ("FULL_SOURCE", "PARTIAL_SOURCE")
    tail = ((body + "\n\n" if body else "") + "(article body unavailable - paywalled or blocked; extract only what the "
            "headline states, do not guess)") if head_only else body
    return f"URL: {item['url']}\nTITLE: {item['title']}\n\nTEXT:\n{tail}"


def check_card(card) -> str | None:
    """None when valid, otherwise the exact rejection reason."""
    if not isinstance(card, dict) or "_error" in card:
        return f"response is not a JSON object with the extraction fields: {str(card)[:120]}"
    try:
        _validate_fields(card, EXTRACT_FIELDS)
    except ValueError as exc:
        return str(exc)
    return None


def extract_one(item: dict, system: str, call_json) -> dict:
    original = {k: v for k, v in item.items() if k not in EXTRACT_FIELDS and k not in ("_error", "head_only", "extract_error", "status")}
    user, reason = user_message(item), ""
    for attempt in range(2):
        prompt = user if attempt == 0 else (user + f"\n\nYour previous answer was rejected: {reason}. "
                                            "Return the complete corrected JSON object only.")
        try:
            card = call_json(system, prompt)
        except llm.QuotaExceeded:
            raise
        except Exception as exc:  # transport/parse problem belongs to THIS article only
            reason = f"call failed: {str(exc)[:160]}"
            continue
        reason = check_card(card) or ""
        if not reason:
            row = {**original, **{k: card[k] for k in EXTRACT_FIELDS}}
            if len((item.get("text") or "")[:EXTRACT_TEXT_LIMIT].strip()) < 200 and item.get("enrichment") not in ("FULL_SOURCE", "PARTIAL_SOURCE"):
                row["head_only"] = True
            return row
    return {**original, "status": "extract_failed", "extract_error": reason or "unknown"}


def extract_all(items, system, call_json, parallel, workers) -> list[dict]:
    """Ordered rows, one per input item. `parallel` is llm.parallel-compatible."""
    items = list(items)
    if not items:
        return []
    results = parallel(items, lambda it: extract_one(it, system, call_json), workers, "single-extract")
    out = []
    for item, res in zip(items, results):
        if isinstance(res, dict) and "_error" in res and "aid" not in res:   # worker-level failure from parallel()
            original = {k: v for k, v in item.items() if k not in EXTRACT_FIELDS}
            out.append({**original, "status": "extract_failed", "extract_error": f"worker failed: {res['_error'][:160]}"})
        else:
            out.append(res)
    return out
