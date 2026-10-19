"""Per-topic Daily Brief section pages for the Notion archive.

The Daily Briefs database keeps one lightweight Index page per date.  This
module owns the second database: one page per non-empty topic section and date.
All external writes are exact-date/section-key idempotent and are followed by a
recursive native-block read-back.
"""
from __future__ import annotations
import os

import hashlib
import json
import re
from pathlib import Path
from typing import Callable

from work.issue_taxonomy import TOPIC_SECTIONS
from work.notion_block_writer import _children, replace_page_resumable
from work.scope_guard import register_page

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
IDS = json.loads((ROOT / "config" / "notion_ids.json").read_text(encoding="utf-8"))
Request = Callable[..., tuple[object, object]]

SECTION_DATABASE_ID = IDS["daily_sections_database_id"]
SECTION_DATA_SOURCE_ID = IDS["daily_sections_data_source_id"]


def section_page_title(day: str, section_key: str) -> str:
    order = next((name.split(".", 1)[0] for name, key in TOPIC_SECTIONS if key == section_key), {"review_failed": "11", "followup": "12"}.get(section_key, section_key))
    return f"{day} · Brief Section {order}"


def _plain(block: dict) -> str:
    kind = str(block.get("type") or "")
    payload = block.get(kind) or {}
    values = payload.get("rich_text") or payload.get("title") or []
    return "".join(
        str(item.get("plain_text") or (item.get("text") or {}).get("content") or "")
        for item in values
        if isinstance(item, dict)
    )


def _page_title(metadata: dict) -> str:
    props = metadata.get("properties") or {}
    values = (props.get("Name") or {}).get("title") or []
    return "".join(str(item.get("plain_text") or "") for item in values if isinstance(item, dict))


def _page_date(metadata: dict) -> str | None:
    return ((metadata.get("properties") or {}).get("Date") or {}).get("date", {}).get("start")


def _select(metadata: dict, name: str) -> str | None:
    return (((metadata.get("properties") or {}).get(name) or {}).get("select") or {}).get("name")


def _query(
    request: Request,
    headers: dict[str, str],
    day: str,
    section_key: str | None = None,
) -> tuple[object, list[dict]]:
    filters: list[dict] = [{"property": "Date", "date": {"equals": day}}]
    if section_key is not None:
        filters.append({"property": "Section Key", "rich_text": {"equals": section_key}})
    body = {"filter": filters[0] if len(filters) == 1 else {"and": filters}, "page_size": 100}
    status, payload = request(
        f"https://api.notion.com/v1/data_sources/{SECTION_DATA_SOURCE_ID}/query",
        "POST",
        headers,
        body,
    )
    rows = payload.get("results") or [] if status == 200 and isinstance(payload, dict) else []
    return status, rows


def _rich_text_property(value: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": str(value)}}]}


def _properties(
    day: str,
    section_key: str,
    section_name: str,
    parent_brief_id: str,
    section_order: int,
    issue_count: int,
    *,
    stage: str,
    brief_status: str,
    coverage_status: str,
    review_needed: bool,
    quality_note: str,
) -> dict:
    return {
        "Name": {"title": [{"type": "text", "text": {"content": section_page_title(day, section_key)}}]},
        "Date": {"date": {"start": day}},
        "Section Key": _rich_text_property(section_key),
        "Section Name": _rich_text_property(section_name),
        "Section Order": {"number": int(section_order)},
        "Parent Brief ID": _rich_text_property(parent_brief_id),
        "Stage": {"select": {"name": stage}},
        "Brief Status": {"select": {"name": brief_status}},
        "Issue Count": {"number": int(issue_count)},
        "Coverage Status": {"select": {"name": coverage_status}},
        "Review Needed": {"checkbox": bool(review_needed)},
        "Quality Note": {"rich_text": [{"type": "text", "text": {"content": quality_note}}]},
    }


def resolve_section_page(
    request: Request,
    headers: dict[str, str],
    *,
    day: str,
    section_key: str,
    section_name: str,
    parent_brief_id: str,
    section_order: int,
    issue_count: int,
    stage: str = "DRAFT",
    brief_status: str = "Draft",
    coverage_status: str = "PASS",
    review_needed: bool = True,
    quality_note: str = "Section page; body-derived native read-back required.",
) -> tuple[str, dict, bool]:
    status, matches = _query(request, headers, day, section_key)
    if status != 200:
        raise RuntimeError(f"section_exact_query_failed:{status}")
    if len(matches) > 1:
        raise RuntimeError(f"section_exact_duplicate:{day}:{section_key}:{len(matches)}")

    created = False
    if matches:
        page_id = str(matches[0].get("id") or "")
        if not page_id:
            raise RuntimeError(f"section_page_id_missing:{day}:{section_key}")
        metadata_status, metadata = request(
            f"https://api.notion.com/v1/pages/{page_id}",
            "GET",
            headers,
        )
        if metadata_status != 200 or not isinstance(metadata, dict):
            raise RuntimeError(f"section_metadata_read_failed:{day}:{section_key}:{metadata_status}")
    else:
        create_status, created_page = request(
            "https://api.notion.com/v1/pages",
            "POST",
            headers,
            {
                "parent": {"database_id": SECTION_DATABASE_ID},
                "properties": _properties(
                    day,
                    section_key,
                    section_name,
                    parent_brief_id,
                    section_order,
                    issue_count,
                    stage=stage,
                    brief_status=brief_status,
                    coverage_status=coverage_status,
                    review_needed=review_needed,
                    quality_note=quality_note,
                ),
            },
        )
        if create_status not in (200, 201) or not isinstance(created_page, dict) or not created_page.get("id"):
            raise RuntimeError(f"section_create_failed:{day}:{section_key}:{create_status}")
        page_id = str(created_page["id"])
        created = True
        metadata_status, metadata = request(
            f"https://api.notion.com/v1/pages/{page_id}",
            "GET",
            headers,
        )
        if metadata_status != 200 or not isinstance(metadata, dict):
            raise RuntimeError(f"section_created_metadata_read_failed:{day}:{section_key}:{metadata_status}")

    parent = metadata.get("parent") or {}
    if parent.get("database_id") != SECTION_DATABASE_ID:
        raise RuntimeError(f"section_parent_mismatch:{day}:{section_key}")
    register_page(page_id, parent_database_id=SECTION_DATABASE_ID)
    if _page_title(metadata) != section_page_title(day, section_key) or _page_date(metadata) != day:
        raise RuntimeError(f"section_identity_mismatch:{day}:{section_key}")

    patch_status, _ = request(
        f"https://api.notion.com/v1/pages/{page_id}",
        "PATCH",
        headers,
        {
            "properties": _properties(
                day,
                section_key,
                section_name,
                parent_brief_id,
                section_order,
                issue_count,
                stage=stage,
                brief_status=brief_status,
                coverage_status=coverage_status,
                review_needed=review_needed,
                quality_note=quality_note,
            )
        },
    )
    if patch_status not in (200, 201):
        raise RuntimeError(f"section_metadata_write_failed:{day}:{section_key}:{patch_status}")
    read_status, readback = request(f"https://api.notion.com/v1/pages/{page_id}", "GET", headers)
    if read_status != 200 or _page_title(readback) != section_page_title(day, section_key) or _page_date(readback) != day:
        raise RuntimeError(f"section_metadata_readback_failed:{day}:{section_key}:{read_status}")
    return page_id, readback, created


def _walk_blocks(request: Request, headers: dict[str, str], parent_id: str, stats: dict) -> None:
    blocks = _children(request, parent_id, headers)
    for block in blocks:
        kind = str(block.get("type") or "")
        payload = block.get(kind) or {}
        text = _plain(block)
        if kind == "heading_2" and payload.get("is_toggleable") is True:
            stats["topic_toggle_heading"] += 1
        if kind == "toggle":
            stats["generic_toggle"] += 1
            if text in {"▶ Details", "세부 내용 · 출처"}:
                stats["details_toggle"] += 1
        if kind == "to_do":
            stats["todo"] += 1
            match = re.search(r"\b(\d{2}-\d{3,})\b", text)
            if match:
                stats["issue_ids"].append(match.group(1))
        if kind in {"bulleted_list_item", "numbered_list_item"}:
            stats["source_links"] += sum(1 for item in payload.get("rich_text") or [] if isinstance(item, dict) and item.get("href"))
        if block.get("has_children") or payload.get("children"):
            _walk_blocks(request, headers, str(block.get("id")), stats)


def verify_section_page(request: Request, headers: dict[str, str], page_id: str, expected_issue_count: int) -> dict:
    """Section pages are flat lists of checkbox issues; the page title is the section name.  Legacy pages that use a
    topic Toggle Heading + per-issue Details toggles still verify under the original rule."""
    stats = {
        "top_level_blocks": len(_children(request, page_id, headers)),
        "topic_toggle_heading": 0,
        "generic_toggle": 0,
        "details_toggle": 0,
        "todo": 0,
        "todo_with_children": 0,
        "issue_ids": [],
        "source_links": 0,
    }
    _walk_blocks(request, headers, page_id, stats)
    for block in _children(request, page_id, headers):
        if block.get("type") == "to_do" and block.get("has_children"):
            stats["todo_with_children"] += 1
    stats["unique_issue_ids"] = len(set(stats["issue_ids"]))
    errors: list[str] = []
    flat = stats["topic_toggle_heading"] == 0
    if flat:
        if stats["todo_with_children"] != expected_issue_count:
            errors.append(f"todo_with_children={stats['todo_with_children']}:{expected_issue_count}")
        if stats["generic_toggle"] != 0:
            errors.append(f"unexpected_generic_toggle={stats['generic_toggle']}")
    else:
        if stats["topic_toggle_heading"] != 1:
            errors.append(f"topic_toggle_heading={stats['topic_toggle_heading']}")
        if stats["details_toggle"] != expected_issue_count:
            errors.append(f"details={stats['details_toggle']}:{expected_issue_count}")
        if stats["generic_toggle"] != expected_issue_count:
            errors.append(f"generic_toggle={stats['generic_toggle']}:{expected_issue_count}")
    if stats["todo"] != expected_issue_count:
        errors.append(f"todo={stats['todo']}:{expected_issue_count}")
    if stats["unique_issue_ids"] != expected_issue_count:
        errors.append(f"unique_issue_ids={stats['unique_issue_ids']}:{expected_issue_count}")
    stats["layout"] = "flat" if flat else "legacy_toggle"
    stats["status"] = "PASS" if not errors else "FAIL"
    stats["errors"] = errors
    return stats


def write_section_page(
    request: Request,
    headers: dict[str, str],
    *,
    day: str,
    section_key: str,
    section_name: str,
    section_order: int,
    parent_brief_id: str,
    markdown: str,
    issue_count: int,
    stage: str,
    brief_status: str,
    coverage_status: str,
    review_needed: bool,
    progress_path: str | Path | None = None,
) -> dict:
    page_id, metadata, created = resolve_section_page(
        request,
        headers,
        day=day,
        section_key=section_key,
        section_name=section_name,
        parent_brief_id=parent_brief_id,
        section_order=section_order,
        issue_count=issue_count,
        stage=stage,
        brief_status=brief_status,
        coverage_status=coverage_status,
        review_needed=review_needed,
    )
    source_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    write_result = replace_page_resumable(
        request,
        page_id,
        headers,
        markdown,
        progress_path=progress_path,
    )
    read_status, body = request(f"https://api.notion.com/v1/pages/{page_id}/markdown", "GET", headers)
    if read_status != 200 or not isinstance(body, dict) or not isinstance(body.get("markdown"), str):
        raise RuntimeError(f"section_body_readback_failed:{day}:{section_key}:{read_status}")
    native = verify_section_page(request, headers, page_id, issue_count)
    if native.get("status") != "PASS":
        raise RuntimeError(f"section_native_readback_failed:{day}:{section_key}:{json.dumps(native, ensure_ascii=False)}")
    return {
        "page_id": page_id,
        "url": metadata.get("url") or "",
        "title": _page_title(metadata),
        "date": _page_date(metadata),
        "section_key": section_key,
        "section_name": section_name,
        "issue_count": issue_count,
        "created": created,
        "source_sha256": source_sha256,
        "body_status": read_status,
        "body_chars": len(body.get("markdown") or ""),
        "write": write_result,
        "native": native,
    }


def list_section_pages(request: Request, headers: dict[str, str], day: str) -> list[dict]:
    status, rows = _query(request, headers, day)
    if status != 200:
        raise RuntimeError(f"section_date_query_failed:{day}:{status}")
    return rows
