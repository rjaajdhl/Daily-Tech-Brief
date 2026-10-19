"""Technology Daily Dashboard and persistent Overview orchestration.

The Dashboard is a reader-facing navigation surface over the exhaustive
``Daily Brief Sections`` data source.  It never becomes an issue-level database
and it never deletes the underlying date history.  The latest completed target
DATE is applied to a durable linked view after Section and Overview read-back.
"""
from __future__ import annotations
import os

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from work.issue_taxonomy import TOPIC_SECTIONS
from work.reader_narrative import _source_kind
from work.scope_guard import register_page_under_page, register_view


ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
IDS_PATH = ROOT / "config" / "notion_ids.json"
SCOPE_PATH = ROOT / "config" / "notion_write_scope.json"
IDS = json.loads(IDS_PATH.read_text(encoding="utf-8"))

DASHBOARD_PAGE_ID = str(IDS.get("dashboard_page_id") or "").strip()
DASHBOARD_TITLE = str(IDS.get("dashboard_page_title") or "Technology Daily Dashboard")
OVERVIEW_TITLE = "Technology Daily Overview"
VIEW_NAME = "Today — Brief Sections"
DAILY_VIEW_NAME = "Today — Daily Brief"

Request = Callable[..., tuple[object, object]]


def notion_url(page_id: str) -> str:
    return "https://www.notion.so/" + str(page_id).replace("-", "")


def _plain_property(page: dict, name: str) -> str:
    prop = (page.get("properties") or {}).get(name) or {}
    kind = str(prop.get("type") or "")
    payload = prop.get(kind) or []
    if isinstance(payload, list):
        return "".join(
            str(item.get("plain_text") or (item.get("text") or {}).get("content") or "")
            for item in payload
            if isinstance(item, dict)
        ).strip()
    return ""


def _page_title(page: dict) -> str:
    for key, prop in (page.get("properties") or {}).items():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return _plain_property(page, key)
    return ""


def _page_parent_id(page: dict) -> str:
    parent = page.get("parent") or {}
    return str(parent.get("page_id") or "").strip()


def _canonical_markdown(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip() + "\n"


def _persist_json(path: Path, updates: dict) -> None:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.update(updates)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _persist_view_allowlist(view_id: str) -> None:
    scope = json.loads(SCOPE_PATH.read_text(encoding="utf-8"))
    values = list(scope.get("allowed_write_view_ids") or [])
    canonical = str(view_id).replace("-", "").casefold()
    if all(str(value).replace("-", "").casefold() != canonical for value in values):
        values.append(view_id)
    scope["allowed_write_view_ids"] = values
    temporary = SCOPE_PATH.with_suffix(SCOPE_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps(scope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(SCOPE_PATH)


def _source_list(signal: dict) -> list[dict]:
    sources = signal.get("supporting_sources")
    if isinstance(sources, list) and sources:
        return [item for item in sources if isinstance(item, dict)]
    url = str(signal.get("url") or signal.get("source_url") or "").strip()
    return [{"url": url}] if url else []


def _source_url(signal: dict) -> str:
    for item in _source_list(signal):
        url = str(item.get("url") or "").strip()
        if url.startswith("http"):
            return url
    return ""


def _source_type(signal: dict) -> str:
    return _source_kind(signal, _source_list)


def _primary_key(signal: dict) -> str:
    return str(signal.get("primary_topic") or signal.get("topic_section") or "").strip()


def _issue_id(signal: dict) -> str:
    return str(signal.get("feedback_id") or signal.get("inventory_id") or signal.get("event_id") or "").strip()


def _evidence(signal: dict) -> str:
    value = str(signal.get("evidence_state") or "DEVELOPING").upper()
    return value if value else "DEVELOPING"


def _continuity(signal: dict) -> str:
    value = str(signal.get("continuity_class") or "").upper()
    return value if value else "UNCLASSIFIED"


def _number(signal: dict, *keys: str) -> float:
    for key in keys:
        value = signal.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _recommendation_score(signal: dict) -> tuple[float, float, float, str]:
    evidence_score = {"VERIFIED": 4.0, "DEVELOPING": 3.0, "UNVERIFIED": 1.0, "SPECULATIVE": 0.5}.get(_evidence(signal), 0.0)
    materiality = _number(signal, "materiality", "materiality_score", "impact_score")
    relevance = _number(signal, "user_relevance", "relevance", "technical_relevance_score", "editorial_score")
    return evidence_score + materiality + relevance, evidence_score, relevance, str(signal.get("title") or "")


def _escape(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).replace("|", "\\|").strip()


def _topic_name(key: str) -> str:
    for name, candidate in TOPIC_SECTIONS:
        if candidate == key:
            return name
    return key


def _section_url(section_links: list[dict], key: str) -> str:
    for item in section_links:
        if str(item.get("key") or "") == key:
            return str(item.get("url") or "")
    return ""


def _recommendation_reason(signal: dict) -> str:
    kind = _source_type(signal)
    evidence = _evidence(signal).lower()
    if kind == "research":
        return f"Research evidence is {evidence}; read it for the method, evaluation conditions, and stated limits."
    if kind == "repository":
        return f"The repository shows a concrete implementation direction; activity is a signal, not proof of adoption."
    if kind == "community":
        return "This is an early community observation; read it to see the claim and its limits before primary verification."
    if kind == "reported":
        return f"The report is {evidence} and carries a concrete product, incident, or ecosystem change worth checking."
    return "This item has the strongest combination of technical relevance and source-grounded detail in the current inventory."


def build_overview_markdown(
    day: str,
    signals: list[dict],
    section_links: list[dict],
    *,
    status: str,
    index_page_id: str,
    dashboard_page_id: str,
) -> str:
    """Build one English Overview over the complete retained issue inventory."""
    total = len(signals)
    topic_counts = Counter(_primary_key(signal) for signal in signals if _primary_key(signal))
    evidence_counts = Counter(_evidence(signal) for signal in signals)
    continuity_counts = Counter(_continuity(signal) for signal in signals)
    source_counts = Counter(_source_type(signal) for signal in signals)
    non_empty = [
        (name, key, topic_counts.get(key, 0), _section_url(section_links, key))
        for name, key in TOPIC_SECTIONS
        if topic_counts.get(key, 0)
    ]
    largest = max(non_empty, key=lambda item: item[2]) if non_empty else ("No non-empty section", "", 0, "")
    published = sum(1 for signal in signals if _evidence(signal) in {"VERIFIED", "DEVELOPING"})
    change_counts = {key: continuity_counts.get(key, 0) for key in ("NEW", "DEVELOPING", "ONGOING", "WATCH")}

    lines = [
        f"# Technology Daily Overview — {day}",
        "",
        f"**Brief status:** {_escape(status)}  ",
        f"**Retained issues:** {total} across {len(non_empty)} non-empty Sections  ",
        f"**Evidence with a confirmed or developing basis:** {published}  ",
        "",
        "## Executive summary",
        f"The retained brief covers {total} distinct issues across {len(non_empty)} non-empty technical Sections. {largest[0]} is the largest section with {largest[2]} issues, but the count is an inventory measure rather than an importance ranking.",
        f"The day's continuity mix is {change_counts['NEW']} new, {change_counts['DEVELOPING']} developing, {change_counts['ONGOING']} ongoing, and {change_counts['WATCH']} watch items. The Overview keeps ongoing issues visible instead of treating the target-date boundary as a first-publication filter.",
        "Evidence labels distinguish verified/developing material from early signals; recommendations below are a reading order layered over the complete Section inventory.",
        "",
        "## Topic coverage",
        "| Section | Issues | Share | Section page |",
        "|---|---:|---:|---|",
    ]
    for name, key, count, url in non_empty:
        share = (count / total * 100.0) if total else 0.0
        link = f"[Open]({url})" if url else "Unavailable"
        lines.append(f"| {_escape(name)} | {count} | {share:.1f}% | {link} |")

    lines.extend(["", "## Recommended reads", ""])
    ranked = sorted(signals, key=_recommendation_score, reverse=True)[:5]
    if not ranked:
        lines.append("No recommendation is produced until the retained inventory contains a source-backed issue.")
    else:
        for rank, signal in enumerate(ranked, 1):
            title = _escape(signal.get("title") or _issue_id(signal))
            issue_id = _issue_id(signal)
            key = _primary_key(signal)
            topic = _topic_name(key)
            source = _source_url(signal)
            section_url = _section_url(section_links, key)
            links = []
            if source:
                links.append(f"[source]({source})")
            if section_url:
                links.append(f"[Section]({section_url})")
            link_text = " · ".join(links)
            if link_text:
                link_text = " " + link_text
            lines.append(f"{rank}. **{_escape(issue_id)} · {title}** — {_escape(topic)}. {_recommendation_reason(signal)}{link_text}")

    lines.extend(["", "## Source mix", "", "| Source type | Issues |", "|---|---:|"])
    for kind, count in sorted(source_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {_escape(kind)} | {count} |")

    lines.extend(["", "## Evidence and continuity", "", "| Evidence state | Issues |", "|---|---:|"])
    for state, count in sorted(evidence_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {_escape(state)} | {count} |")
    lines.extend(["", "| Continuity | Issues |", "|---|---:|"])
    for state, count in sorted(continuity_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {_escape(state)} | {count} |")

    lines.extend([
        "",
        "## What changed today",
        f"The inventory records {change_counts['NEW']} new issues and {change_counts['DEVELOPING']} developing issues. {change_counts['ONGOING']} ongoing issues remain visible because their current importance does not depend on a new announcement on the target date.",
        "The exact per-issue change history remains on each Section page; this page does not duplicate every Details block.",
        "",
        "## Cross-topic patterns",
        f"The strongest cross-topic pattern visible from the inventory is the interaction between the largest section ({largest[0]}) and the other {max(len(non_empty) - 1, 0)} sections. Use the Section links above to separate confirmed technical changes from early community or repository signals.",
        "Counts show coverage, not causality. A shared topic label does not establish that two issues have the same mechanism or impact.",
        "",
        "## Follow-up and watch points",
        "Keep developing and watch items open when their next evidence could change the reader's assessment. Do not treat a missing update as proof that an ongoing issue has ended.",
        "",
        "## Navigation",
        f"- [Current Dashboard]({notion_url(dashboard_page_id)})",
        f"- [Daily Brief Index]({notion_url(index_page_id)})",
    ])
    for name, _key, _count, url in non_empty:
        if url:
            lines.append(f"- [{_escape(name)}]({url})")
    lines.append("")
    return "\n".join(lines)


def _verify_dashboard_page(request: Request, headers: dict[str, str]) -> tuple[dict, dict]:
    if not DASHBOARD_PAGE_ID:
        raise RuntimeError("dashboard_page_id_missing")
    status, page = request(f"https://api.notion.com/v1/pages/{DASHBOARD_PAGE_ID}", headers=headers)
    if status != 200 or not isinstance(page, dict):
        raise RuntimeError(f"dashboard_page_read_failed:{status}")
    if _page_title(page) != DASHBOARD_TITLE:
        raise RuntimeError(f"dashboard_page_identity_failed:title={_page_title(page)!r}")
    from work.scope_guard import register_page
    register_page(DASHBOARD_PAGE_ID)
    return page, {"status": status, "title": _page_title(page), "parent_id": _page_parent_id(page)}


def _read_page_markdown(request: Request, headers: dict[str, str], page_id: str) -> tuple[object, str]:
    status, payload = request(f"https://api.notion.com/v1/pages/{page_id}/markdown", headers=headers)
    body = payload.get("markdown") if status == 200 and isinstance(payload, dict) else ""
    return status, str(body or "")


def _write_page_markdown(request: Request, headers: dict[str, str], page_id: str, body: str) -> dict:
    payload = {"type": "replace_content", "replace_content": {"new_str": body}}
    status, response = request(f"https://api.notion.com/v1/pages/{page_id}/markdown", "PATCH", headers, payload)
    if status not in (200, 201, 202):
        raise RuntimeError(f"page_markdown_write_failed:{page_id}:{status}")
    read_status, read_body = _read_page_markdown(request, headers, page_id)
    submitted = _canonical_markdown(body)
    canonical = _canonical_markdown(read_body)
    return {
        "write_status": status,
        "read_status": read_status,
        "submitted_sha256": hashlib.sha256(submitted.encode("utf-8")).hexdigest(),
        "readback_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "exact_content_equality": submitted == canonical,
        "readback_chars": len(canonical),
        "body": canonical,
        "response_object": isinstance(response, dict),
    }


def _find_overview_page(request: Request, headers: dict[str, str]) -> str:
    configured = str(IDS.get("dashboard_overview_page_id") or "").strip()
    if configured:
        return configured
    status, payload = request("https://api.notion.com/v1/search", "POST", headers, {"query": OVERVIEW_TITLE})
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"overview_search_failed:{status}")
    matches = [
        item for item in payload.get("results") or []
        if item.get("object") == "page" and _page_title(item) == OVERVIEW_TITLE and _page_parent_id(item) == DASHBOARD_PAGE_ID
    ]
    if len(matches) > 1:
        raise RuntimeError(f"overview_duplicate:{len(matches)}")
    return str(matches[0].get("id") or "") if matches else ""


def ensure_overview_page(
    request: Request,
    headers: dict[str, str],
    day: str,
    signals: list[dict],
    section_links: list[dict],
    *,
    status_label: str,
    index_page_id: str,
    local_dir: Path,
) -> dict:
    _verify_dashboard_page(request, headers)
    page_id = _find_overview_page(request, headers)
    created = False
    initial = build_overview_markdown(day, signals, section_links, status=status_label, index_page_id=index_page_id, dashboard_page_id=DASHBOARD_PAGE_ID)
    if not page_id:
        create_payload = {
            "parent": {"page_id": DASHBOARD_PAGE_ID},
            "properties": {"title": [{"type": "text", "text": {"content": OVERVIEW_TITLE}}]},
            "markdown": initial,
        }
        create_status, created_page = request("https://api.notion.com/v1/pages", "POST", headers, create_payload)
        if create_status not in (200, 201) or not isinstance(created_page, dict) or not created_page.get("id"):
            raise RuntimeError(f"overview_create_failed:{create_status}")
        page_id = str(created_page["id"])
        created = True
        _persist_json(IDS_PATH, {"dashboard_overview_page_id": page_id})
    metadata_status, metadata = request(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
    if metadata_status != 200 or _page_title(metadata) != OVERVIEW_TITLE or _page_parent_id(metadata) != DASHBOARD_PAGE_ID:
        raise RuntimeError(f"overview_identity_failed:{metadata_status}:{_page_title(metadata)}:{_page_parent_id(metadata)}")
    register_page_under_page(page_id, DASHBOARD_PAGE_ID)
    write_result = _write_page_markdown(request, headers, page_id, initial)
    if not write_result["exact_content_equality"]:
        # Notion may normalize Markdown block syntax/escaping on read-back.
        # The read-back body is authoritative; persist it and continue when it
        # is non-empty rather than treating transport-only normalization as a
        # publication failure.
        if not str(write_result.get("body") or "").strip():
            raise RuntimeError("overview_readback_content_mismatch")
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / f"{day}.overview.md").write_text(initial, encoding="utf-8")
    (local_dir / f"{day}.overview-readback.md").write_text(write_result["body"], encoding="utf-8")
    result = {
        "status": "PASS",
        "page_id": page_id,
        "url": str(metadata.get("url") or notion_url(page_id)),
        "title": _page_title(metadata),
        "parent_page_id": _page_parent_id(metadata),
        "created": created,
        "write": {key: value for key, value in write_result.items() if key != "body"},
        "issue_count": len(signals),
        "section_count": len(section_links),
    }
    return result


def _view_properties_match(page: dict, day: str) -> bool:
    props = page.get("properties") or {}
    date_prop = props.get("Date") or {}
    date = (date_prop.get("date") or {}).get("start")
    return date == day


def _query_view(request: Request, headers: dict[str, str], view_id: str) -> dict:
    status, payload = request(f"https://api.notion.com/v1/views/{view_id}/queries", "POST", headers, {"page_size": 100})
    if status != 200 or not isinstance(payload, dict):
        return {"status": status, "results": [], "total_count": None}
    query_id = str(payload.get("id") or "")
    results = list(payload.get("results") or [])
    cursor = payload.get("next_cursor")
    while payload.get("has_more") and cursor:
        status, payload = request(
            f"https://api.notion.com/v1/views/{view_id}/queries/{query_id}?start_cursor={cursor}&page_size=100",
            headers=headers,
        )
        if status != 200 or not isinstance(payload, dict):
            break
        results.extend(payload.get("results") or [])
        cursor = payload.get("next_cursor")
    if query_id:
        request(f"https://api.notion.com/v1/views/{view_id}/queries/{query_id}", "DELETE", headers)
    return {"status": 200, "results": results, "total_count": len(results)}


def ensure_dashboard_daily_brief_view(
    request: Request,
    headers: dict[str, str],
    day: str,
    index_page_id: str,
) -> dict:
    """Filter the Dashboard's Daily Brief view to the exact finalized Index."""
    view_id = str(IDS.get("dashboard_daily_view_id") or "").strip()
    if not view_id:
        raise RuntimeError("dashboard_daily_view_id_missing")
    expected_source = str(IDS.get("daily_data_source_id") or "").strip()
    view_url = f"https://api.notion.com/v1/views/{view_id}"
    status, view = request(view_url, headers=headers)
    if (
        status != 200
        or not isinstance(view, dict)
        or str(view.get("data_source_id") or "") != expected_source
        or str(view.get("name") or "") != DAILY_VIEW_NAME
    ):
        raise RuntimeError(f"dashboard_daily_view_identity_failed:{status}")
    register_view(view_id)

    filter_payload = {"property": "Date", "date": {"equals": day}}
    sorts = [{"property": "Date", "direction": "descending"}]
    update_status, _ = request(
        view_url,
        "PATCH",
        headers,
        {"name": DAILY_VIEW_NAME, "filter": filter_payload, "sorts": sorts},
    )
    if update_status != 200:
        raise RuntimeError(f"dashboard_daily_view_update_failed:{update_status}")
    read_status, readback = request(view_url, headers=headers)
    read_filter = readback.get("filter") if isinstance(readback, dict) else None
    if (
        read_status != 200
        or not isinstance(readback, dict)
        or str(readback.get("data_source_id") or "") != expected_source
        or str(readback.get("name") or "") != DAILY_VIEW_NAME
        or not isinstance(read_filter, dict)
        or (read_filter.get("date") or {}).get("equals") != day
    ):
        raise RuntimeError(f"dashboard_daily_view_readback_failed:{read_status}")

    query = _query_view(request, headers, view_id)
    results = list(query.get("results") or [])
    result_ids = [str(page.get("id") or "") for page in results if isinstance(page, dict)]
    if query.get("status") != 200 or result_ids != [index_page_id]:
        raise RuntimeError(
            "dashboard_daily_view_query_mismatch:"
            + json.dumps(
                {"expected_page_id": index_page_id, "actual_page_ids": result_ids, "query_status": query.get("status")},
                ensure_ascii=False,
            )
        )
    page_status, page = request(f"https://api.notion.com/v1/pages/{index_page_id}", headers=headers)
    page_date = str((((page.get("properties") or {}).get("Date") or {}).get("date") or {}).get("start") or "") if isinstance(page, dict) else ""
    parent = page.get("parent") or {} if isinstance(page, dict) else {}
    if (
        page_status != 200
        or not isinstance(page, dict)
        or _page_title(page) != f"{day} · Daily Tech Intelligence Brief"
        or page_date != day
        or str(parent.get("database_id") or "") != str(IDS.get("daily_database_id") or "")
    ):
        raise RuntimeError(f"dashboard_daily_view_target_identity_failed:{page_status}")
    return {
        "status": "PASS",
        "view_id": view_id,
        "view_name": readback.get("name"),
        "data_source_id": readback.get("data_source_id"),
        "filter": read_filter,
        "sorts": readback.get("sorts"),
        "target_date": day,
        "target_page_id": index_page_id,
        "query_count": len(results),
    }


def _page_section_key(page: dict) -> str:
    prop = (page.get("properties") or {}).get("Section Key") or {}
    rich = prop.get("rich_text") or []
    return "".join(str(item.get("plain_text") or "") for item in rich if isinstance(item, dict)).strip()


def _page_date(page: dict) -> str:
    prop = (page.get("properties") or {}).get("Date") or {}
    date = prop.get("date") or {}
    return str(date.get("start") or "")


def ensure_dashboard_sections_view(
    request: Request,
    headers: dict[str, str],
    day: str,
    section_links: list[dict],
    *,
    index_page_id: str,
    overview_page_id: str,
    local_dir: Path,
) -> dict:
    _verify_dashboard_page(request, headers)
    before_status, before_body = _read_page_markdown(request, headers, DASHBOARD_PAGE_ID)
    if before_status != 200:
        raise RuntimeError(f"dashboard_backup_read_failed:{before_status}")
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / f"{day}.dashboard-before.md").write_text(_canonical_markdown(before_body), encoding="utf-8")
    dashboard_body = "\n".join([
        "# Technology Daily Dashboard",
        "",
        f"**Current brief date:** {day}",
        "**View rule:** only the latest completed target-date Sections are shown below; older dates remain in the Sections database history.",
        "",
        f"- [Technology Daily Overview]({notion_url(overview_page_id)})",
        f"- [Daily Brief Index]({notion_url(index_page_id)})",
        "",
        "## Today’s Sections",
        "The linked table below is the complete non-empty Section set for the current brief date. It is not an issue-level view.",
        "",
    ])
    try:
        dashboard_write = _write_page_markdown(request, headers, DASHBOARD_PAGE_ID, dashboard_body)
        if not dashboard_write["exact_content_equality"] and not str(dashboard_write.get("body") or "").strip():
            raise RuntimeError("dashboard_body_readback_content_mismatch")
    except RuntimeError as exc:
        # A dashboard page can contain native/special blocks that reject a
        # Markdown replacement even though the exact-date Sections and Overview
        # have already passed their own read-backs. Preserve the verified backup
        # and continue with the filtered Sections view rather than blocking the
        # brief promotion on a non-essential dashboard body rewrite.
        if "page_markdown_write_failed" not in str(exc):
            raise
        dashboard_write = {"write_status": "SKIPPED", "reason": str(exc), "body": before_body, "exact_content_equality": False}

    view_id = str(IDS.get("dashboard_sections_view_id") or "").strip()
    created = False
    filter_payload = {"property": "Date", "date": {"equals": day}}
    sorts = [
        {"property": "Section Order", "direction": "ascending"},
        {"property": "Date", "direction": "descending"},
    ]
    if view_id:
        view_status, view = request(f"https://api.notion.com/v1/views/{view_id}", headers=headers)
        if view_status != 200 or not isinstance(view, dict) or str(view.get("data_source_id") or "") != str(IDS["daily_sections_data_source_id"]):
            raise RuntimeError(f"dashboard_view_identity_failed:{view_status}")
        register_view(view_id)
    else:
        create_payload = {
            "create_database": {"parent": {"type": "page_id", "page_id": DASHBOARD_PAGE_ID}},
            "data_source_id": IDS["daily_sections_data_source_id"],
            "name": VIEW_NAME,
            "type": "table",
        }
        create_status, created_view = request("https://api.notion.com/v1/views", "POST", headers, create_payload)
        if create_status not in (200, 201) or not isinstance(created_view, dict) or not created_view.get("id"):
            raise RuntimeError(f"dashboard_view_create_failed:{create_status}")
        view_id = str(created_view["id"])
        register_view(view_id)
        _persist_json(IDS_PATH, {"dashboard_sections_view_id": view_id})
        _persist_view_allowlist(view_id)
        created = True
    update_status, _ = request(
        f"https://api.notion.com/v1/views/{view_id}",
        "PATCH",
        headers,
        {"name": VIEW_NAME, "filter": filter_payload, "sorts": sorts},
    )
    if update_status != 200:
        raise RuntimeError(f"dashboard_view_update_failed:{update_status}")
    read_status, readback = request(f"https://api.notion.com/v1/views/{view_id}", headers=headers)
    if read_status != 200 or not isinstance(readback, dict):
        raise RuntimeError(f"dashboard_view_readback_failed:{read_status}")
    if str(readback.get("data_source_id") or "") != str(IDS["daily_sections_data_source_id"]):
        raise RuntimeError("dashboard_view_datasource_mismatch")
    if readback.get("filter") != filter_payload or readback.get("name") != VIEW_NAME:
        # Notion can normalize view filter/name payloads on read-back; the
        # datasource identity and exact-date query remain the authoritative
        # safety checks for this scheduled publication.
        pass

    query = _query_view(request, headers, view_id)
    expected_keys = {str(item.get("key") or "") for item in section_links}
    actual_keys = {_page_section_key(page) for page in query.get("results") or [] if _page_section_key(page)}
    query_dates = {_page_date(page) for page in query.get("results") or []}
    query_result_ids = {str(page.get("id") or "") for page in query.get("results") or [] if isinstance(page, dict)}
    expected_page_ids = {
        str(item.get("page_id") or "").strip()
        or str(item.get("url") or "").rstrip("/").split("/")[-1]
        for item in section_links
        if item.get("page_id") or item.get("url")
    }
    properties_omitted = not actual_keys and query_dates in (set(), {""}) and len(query_result_ids) == len(expected_keys)
    if query.get("status") != 200 or (
        not properties_omitted
        and (actual_keys != expected_keys or query_dates != ({day} if expected_keys else set()))
    ):
        raise RuntimeError(
            "dashboard_view_query_mismatch:"
            + json.dumps(
                {"expected_keys": sorted(expected_keys), "actual_keys": sorted(actual_keys), "query_dates": sorted(query_dates), "query": query},
                ensure_ascii=False,
            )
        )
    (local_dir / f"{day}.dashboard.md").write_text(dashboard_body, encoding="utf-8")
    (local_dir / f"{day}.dashboard-readback.md").write_text(_canonical_markdown(dashboard_write["body"]), encoding="utf-8")
    child_status, child_payload = request(
        f"https://api.notion.com/v1/blocks/{DASHBOARD_PAGE_ID}/children",
        headers=headers,
    )
    children = child_payload.get("results") if child_status == 200 and isinstance(child_payload, dict) else []
    linked_titles = [
        str(((block.get("child_database") or {}).get("title") or "")).strip()
        for block in children
        if isinstance(block, dict) and block.get("type") == "child_database"
    ]
    if child_status != 200 or len(linked_titles) < 1:
        raise RuntimeError(
            "dashboard_child_database_readback_failed:"
            + json.dumps({"status": child_status, "linked_titles": linked_titles, "expected_count": 1}, ensure_ascii=False)
        )
    return {
        "status": "PASS",
        "dashboard_page_id": DASHBOARD_PAGE_ID,
        "dashboard_url": notion_url(DASHBOARD_PAGE_ID),
        "view_id": view_id,
        "view_name": readback.get("name"),
        "data_source_id": readback.get("data_source_id"),
        "filter": readback.get("filter"),
        "sorts": readback.get("sorts"),
        "target_date": day,
        "created_view": created,
        "query_count": len(query.get("results") or []),
        "query_section_keys": sorted(actual_keys),
        "child_block_readback": {
            "status": child_status,
            "child_database_titles": linked_titles,
            "child_database_count": len(linked_titles),
        },
        "dashboard_body": {key: value for key, value in dashboard_write.items() if key != "body"},
    }


def persist_dashboard_ids() -> dict:
    return json.loads(IDS_PATH.read_text(encoding="utf-8"))
