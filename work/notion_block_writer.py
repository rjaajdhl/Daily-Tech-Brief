"""Block-level fallback writer for large Notion Markdown pages.

The Notion Markdown replace endpoint can time out on very large pages.  This
module keeps the exact page identity but replaces its top-level blocks in
bounded, read-back-verifiable operations.  It is intentionally independent of
credentials and accepts the finalizer's request function.
"""
from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Callable

from work.issue_taxonomy import TOPIC_SECTIONS
from work.scope_guard import register_block

Request = Callable[..., tuple[object, object]]

_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0
_MIN_REQUEST_INTERVAL = 0.30
_MAX_APPEND_PAYLOAD_BYTES = 180_000
_MAX_INLINE_CHILDREN = 100
_TOPIC_TOGGLE_TITLES = frozenset(name for name, _ in TOPIC_SECTIONS)


def _paced_request(request: Request, url: str, method: str = "GET", headers: dict | None = None, body=None):
    """Keep fallback traffic below Notion's average request-rate limit."""
    global _LAST_REQUEST_AT
    with _REQUEST_LOCK:
        now = time.monotonic()
        scheduled = max(now, _LAST_REQUEST_AT + _MIN_REQUEST_INTERVAL)
        _LAST_REQUEST_AT = scheduled
    wait = scheduled - now
    if wait > 0:
        time.sleep(wait)
    # Do not hold the scheduling lock while network I/O is in flight.  This
    # keeps the aggregate request start rate bounded without serializing all
    # network latency across deletion workers.
    return request(url, method, headers, body)


def _rich_text(value: str) -> list[dict]:
    """Convert simple Markdown links into Notion rich_text segments."""
    value = value.replace("\\_", "_").replace("\\*", "*")
    parts: list[dict] = []
    cursor = 0
    pattern = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
    for match in pattern.finditer(value):
        if match.start() > cursor:
            parts.extend(_plain_segments(value[cursor:match.start()]))
        parts.extend(_link_segments(match.group(1), match.group(2)))
        cursor = match.end()
    if cursor < len(value):
        parts.extend(_plain_segments(value[cursor:]))
    if not parts:
        parts = _plain_segments(value)
    # Notion caps rich_text elements per block at 100. Preserve all visible
    # text while collapsing overflow into one plain-text segment; issue-local
    # source links are emitted separately at the end of each Details toggle.
    if len(parts) > 100:
        overflow = "".join(str(item.get("text", {}).get("content", "")) for item in parts[99:])
        parts = parts[:99] + [{"type": "text", "text": {"content": overflow}}]
    return parts


def _plain_segments(value: str) -> list[dict]:
    """Plain text segments; `backtick` spans become Notion inline-code annotations."""
    out: list[dict] = []
    for i, piece in enumerate(re.split(r"`([^`\n]+)`", value)):
        if not piece:
            continue
        for start in range(0, len(piece), 1800):
            seg = {"type": "text", "text": {"content": piece[start:start + 1800]}}
            if i % 2 == 1:
                seg["annotations"] = {"code": True}
            out.append(seg)
    return out or [{"type": "text", "text": {"content": ""}}]


def _link_segments(label: str, url: str) -> list[dict]:
    result = []
    for start in range(0, len(label), 1800):
        result.append({"type": "text", "text": {"content": label[start:start + 1800], "link": {"url": url}}})
    return result or [{"type": "text", "text": {"content": "", "link": {"url": url}}}]


def _block(block_type: str, payload: dict, children: list[dict] | None = None) -> dict:
    block = {"object": "block", "type": block_type, block_type: payload}
    if children:
        block[block_type]["children"] = children
    return block


def _markdown_blocks_from_lines(lines: list[str]) -> list[dict]:
    """Parse the renderer subset into native Notion blocks.

    Task items become ``to_do`` blocks.  Topic ``<details>`` becomes a native
    Toggle Heading (``heading_2`` with ``is_toggleable=true``), while issue
    Details become generic ``toggle`` blocks.  This is used only by the
    bounded block fallback, while the Markdown endpoint receives the same
    semantic tags.
    """
    blocks: list[dict] = []
    index = 0
    while index < len(lines):
        raw = lines[index].rstrip("\n")
        line = raw.strip()
        if not line:
            index += 1
            continue

        if re.fullmatch(r'<table_of_contents(?:\s+color="[^"]+")?\s*/?>', line, re.I):
            blocks.append(_block("table_of_contents", {"color": "gray"}))
            index += 1
            continue

        if line.casefold() == "<details>":
            index += 1
            toggle_title = "▶ Details"
            child_lines: list[str] = []
            depth = 1
            while index < len(lines):
                inner = lines[index].rstrip("\n")
                stripped = inner.strip()
                if stripped.casefold() == "<details>":
                    depth += 1
                    child_lines.append(re.sub(r"^(?:\t| {1,4})", "", inner))
                    index += 1
                    continue
                if stripped.casefold() == "</details>":
                    depth -= 1
                    if depth == 0:
                        break
                    child_lines.append(re.sub(r"^(?:\t| {1,4})", "", inner))
                    index += 1
                    continue
                summary = re.fullmatch(r"<summary>(.*?)</summary>", stripped, re.I)
                if summary and depth == 1:
                    toggle_title = re.sub(r"^\s+|\s+$", "", summary.group(1)) or toggle_title
                else:
                    child_lines.append(re.sub(r"^(?:\t| {1,4})", "", inner))
                index += 1
            children = _markdown_blocks_from_lines(child_lines)
            if toggle_title in _TOPIC_TOGGLE_TITLES:
                # Notion's visual "Toggle heading" is a heading block with
                # is_toggleable=true, not a generic toggle/list block.  Topic
                # sections use this native type; issue Details remain generic
                # toggles.
                blocks.append(
                    _block(
                        "heading_2",
                        {"rich_text": _rich_text(toggle_title), "is_toggleable": True},
                        children,
                    )
                )
            else:
                blocks.append(_block("toggle", {"rich_text": _rich_text(toggle_title)}, children))
            index += 1
            continue

        task = re.fullmatch(r"-\s+\[([ xX])\]\s+(\d{2}-\d{3,})(?:\s+·)?\s+(.+?)", line)
        if task:
            feedback_id, title = task.group(2), task.group(3).strip()
            # Lines indented under the task belong to it (lead, body, glossary, sources), so one
            # checkbox owns the whole issue.  Legacy pages have un-indented lead lines and are unaffected.
            child_lines: list[str] = []
            j = index + 1
            while j < len(lines):
                nxt = lines[j].rstrip("\n")
                if nxt.strip() == "":
                    k = j + 1
                    while k < len(lines) and lines[k].strip() == "":
                        k += 1
                    if k < len(lines) and re.match(r"^(?:\t| {4})", lines[k]):
                        child_lines.append("")
                        j += 1
                        continue
                    break
                if re.match(r"^(?:\t| {4})", nxt):
                    child_lines.append(re.sub(r"^(?:\t| {4})", "", nxt, count=1))
                    j += 1
                    continue
                break
            blocks.append(
                _block(
                    "to_do",
                    {"rich_text": _rich_text(f"{feedback_id} · {title}"), "checked": task.group(1).casefold() == "x"},
                    _markdown_blocks_from_lines(child_lines) if child_lines else None,
                )
            )
            index = j
            continue

        # Notion's Markdown read-back represents a native Toggle Heading as
        # an indented heading subtree with a transport marker.  Accept that
        # form as input too, so a read-back/fallback cycle cannot flatten a
        # topic heading into an ordinary heading or generic toggle.
        readback_toggle_heading = re.fullmatch(
            r"##\s+(.+?)\s+\{toggle=\"true\"\}", line, re.I
        )
        if readback_toggle_heading:
            toggle_title = readback_toggle_heading.group(1).strip()
            if toggle_title in _TOPIC_TOGGLE_TITLES:
                index += 1
                child_lines: list[str] = []
                while index < len(lines):
                    inner = lines[index].rstrip("\n")
                    if inner.strip() and not re.match(r"^(?:\t| {1,4})", inner):
                        break
                    child_lines.append(re.sub(r"^(?:\t| {1,4})", "", inner))
                    index += 1
                blocks.append(
                    _block(
                        "heading_2",
                        {"rich_text": _rich_text(toggle_title), "is_toggleable": True},
                        _markdown_blocks_from_lines(child_lines),
                    )
                )
                continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            level = min(len(heading.group(1)), 4)
            kind = f"heading_{level}"
            blocks.append(_block(kind, {"rich_text": _rich_text(heading.group(2).strip())}))
            index += 1
            continue

        bullet = re.match(r"^-\s+(.*)$", line)
        if bullet:
            blocks.append(_block("bulleted_list_item", {"rich_text": _rich_text(bullet.group(1).strip())}))
            index += 1
            continue

        blocks.append(_block("paragraph", {"rich_text": _rich_text(line)}))
        index += 1
    return blocks


def markdown_blocks(markdown: str) -> list[dict]:
    """Map the renderer's Markdown subset to native Notion blocks."""
    return _markdown_blocks_from_lines(markdown.splitlines())


def _children(request: Request, page_id: str, headers: dict[str, str]) -> list[dict]:
    result: list[dict] = []
    cursor = None
    for _ in range(200):
        url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
        if cursor:
            url += "&start_cursor=" + cursor
        status, payload = _paced_request(request, url, "GET", headers)
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeError(f"children_read_failed:{status}")
        result.extend(payload.get("results") or [])
        if not payload.get("has_more"):
            for block in result:
                register_block(block.get("id"))
            return result
        cursor = payload.get("next_cursor")
    raise RuntimeError("children_read_pagination_limit")


def _delete_one(request: Request, block_id: str, headers: dict[str, str]) -> tuple[str, object]:
    url = f"https://api.notion.com/v1/blocks/{block_id}"
    last = None
    for attempt in range(10):
        status, payload = _paced_request(request, url, "DELETE", headers)
        last = status
        if status in (200, 201, 202, 204, 404):
            return block_id, status
        if status in (429, 500, 502, 503, 504, "ERROR"):
            time.sleep(min(30.0, 3.0 * (attempt + 1)))
    return block_id, last


def archive_top_level(request: Request, page_id: str, headers: dict[str, str], max_workers: int = 1) -> int:
    blocks = _children(request, page_id, headers)
    ids = [str(block.get("id")) for block in blocks if block.get("id")]
    failures: list[tuple[str, object]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for block_id, status in pool.map(lambda value: _delete_one(request, value, headers), ids):
            if status not in (200, 201, 202, 204, 404):
                failures.append((block_id, status))
    if failures:
        raise RuntimeError(f"block_delete_failed:{len(failures)}:{failures[:3]}")
    for _ in range(8):
        if not _children(request, page_id, headers):
            return len(ids)
        time.sleep(1.0)
    raise RuntimeError("block_delete_readback_failed")


def archive_top_level_resumable(
    request: Request,
    page_id: str,
    headers: dict[str, str],
    progress_path: Path | None,
    source_sha256: str,
    expected_blocks: int,
    chunk_size: int = 25,
    max_workers: int = 3,
) -> int:
    """Archive existing top-level blocks with an observable recovery checkpoint.

    A page may be interrupted during archival.  Re-read the remaining children
    after every bounded chunk and persist the observed remaining IDs/count so a
    rerun never has to trust an unrecorded delete prefix.
    """
    archived = 0
    while True:
        current = _children(request, page_id, headers)
        ids = [str(block.get("id")) for block in current if block.get("id")]
        if not ids:
            _save_progress(
                progress_path,
                {
                    "phase": "archive",
                    "page_id": page_id,
                    "source_sha256": source_sha256,
                    "expected_blocks": expected_blocks,
                    "archived_blocks": archived,
                    "remaining_blocks": 0,
                    "chunk_size": chunk_size,
                },
            )
            return archived
        failures: list[tuple[str, object]] = []
        chunk = ids[:chunk_size]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            for block_id, status in pool.map(lambda value: _delete_one(request, value, headers), chunk):
                if status not in (200, 201, 202, 204, 404):
                    failures.append((block_id, status))
        if failures:
            raise RuntimeError(f"block_delete_failed:{len(failures)}:{failures[:3]}")
        remaining = _children(request, page_id, headers)
        archived += len(ids) - len(remaining)
        _save_progress(
            progress_path,
            {
                "phase": "archive",
                "page_id": page_id,
                "source_sha256": source_sha256,
                "expected_blocks": expected_blocks,
                "archived_blocks": archived,
                "remaining_blocks": len(remaining),
                "chunk_size": chunk_size,
            },
        )
        if len(remaining) >= len(ids):
            raise RuntimeError(f"block_archive_no_progress:{len(remaining)}")


def append_blocks(request: Request, page_id: str, headers: dict[str, str], blocks: list[dict], chunk_size: int = 50) -> int:
    endpoint = f"https://api.notion.com/v1/blocks/{page_id}/children"
    sent = 0
    for start in range(0, len(blocks), chunk_size):
        chunk = blocks[start:start + chunk_size]
        last = None
        for attempt in range(4):
            status, payload = _paced_request(request, endpoint, "PATCH", headers, {"children": chunk})
            last = status
            if status in (200, 201, 202):
                sent += len(chunk)
                break
            if attempt < 3:
                time.sleep(2.0 * (attempt + 1))
        else:
            raise RuntimeError(f"block_append_failed:{start}:{last}")
        time.sleep(0.5)
    return sent


def clear_page_fast(request: Request, page_id: str, headers: dict[str, str]) -> tuple[bool, int]:
    """Clear a page in one Markdown operation when the endpoint supports it.

    Notion has no bulk block-archive endpoint.  A page-level Markdown replace
    is materially safer for large briefs: it avoids a long sequence of DELETE
    calls and is followed by a children read-back before any native blocks are
    appended.  The caller falls back to bounded per-block archival on any
    endpoint or read-back failure.
    """
    existing = _children(request, page_id, headers)
    if not existing:
        return True, 0
    endpoint = f"https://api.notion.com/v1/pages/{page_id}/markdown"
    status, _ = _paced_request(
        request,
        endpoint,
        "PATCH",
        headers,
        {"type": "replace_content", "replace_content": {"new_str": ""}},
    )
    if status not in (200, 201, 202):
        return False, len(existing)
    for attempt in range(8):
        if not _children(request, page_id, headers):
            return True, len(existing)
        if attempt < 7:
            time.sleep(1.0)
    return False, len(existing)


def _block_plain(block: dict) -> str:
    payload = block.get(block.get("type"), {}) or {}
    values = payload.get("rich_text") or payload.get("title") or []
    return "".join(
        item.get("plain_text") or (item.get("text") or {}).get("content") or ""
        for item in values
        if isinstance(item, dict)
    )


def _comparison_plain(block: dict) -> str:
    """Return Notion-canonical text for semantic read-back comparisons.

    Notion may strip zero-width/format characters while preserving the visible
    text. Treating those transport-only differences as content loss aborts a
    large nested write even though the rendered block is equivalent.
    """
    text = unicodedata.normalize("NFKC", _block_plain(block))
    return "".join(char for char in text if unicodedata.category(char) != "Cf")


def _block_matches(actual: dict, expected: dict) -> bool:
    """Compare the stable top-level semantics used by the resumable writer."""
    if actual.get("type") != expected.get("type"):
        return False
    if _comparison_plain(actual) != _comparison_plain(expected):
        return False
    if actual.get("type") == "to_do":
        return bool((actual.get("to_do") or {}).get("checked")) == bool(
            (expected.get("to_do") or {}).get("checked")
        )
    if str(actual.get("type") or "").startswith("heading_"):
        return bool((actual.get(actual.get("type")) or {}).get("is_toggleable")) == bool(
            (expected.get(expected.get("type")) or {}).get("is_toggleable")
        )
    return True


def _is_prefix(actual: list[dict], expected: list[dict]) -> bool:
    return len(actual) <= len(expected) and all(
        _block_matches(left, right) for left, right in zip(actual, expected)
    )


def _without_children(block: dict) -> dict:
    """Return a safe block shell, retaining small inline descendants."""
    shell = copy.deepcopy(block)
    block_type = shell.get("type")
    payload = shell.get(block_type) if block_type else None
    if not isinstance(payload, dict) or "children" not in payload:
        return shell
    if len(payload.get("children") or []) > _MAX_INLINE_CHILDREN:
        payload.pop("children", None)
        return shell
    size = len(
        json.dumps({"children": [shell]}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if size > _MAX_APPEND_PAYLOAD_BYTES:
        payload.pop("children", None)
    return shell


def _payload_chunks(blocks: list[dict], start: int, chunk_size: int) -> list[dict]:
    """Choose a count- and byte-bounded append chunk."""
    selected: list[dict] = []
    for block in blocks[start:]:
        candidate = selected + [block]
        size = len(
            json.dumps({"children": candidate}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if selected and (len(selected) >= chunk_size or size > _MAX_APPEND_PAYLOAD_BYTES):
            break
        if not selected and size > _MAX_APPEND_PAYLOAD_BYTES:
            raise RuntimeError(f"single_block_append_payload_too_large:{size}")
        selected.append(block)
        if len(selected) >= chunk_size:
            break
    return selected


def _append_chunk_readback(
    request: Request,
    parent_id: str,
    headers: dict[str, str],
    current: list[dict],
    expected_shells: list[dict],
    chunk: list[dict],
) -> list[dict]:
    """Append one bounded chunk and wait for a verified child prefix."""
    endpoint = f"https://api.notion.com/v1/blocks/{parent_id}/children"
    target_count = len(current) + len(chunk)
    transient = {429, 500, 502, 503, 504, "ERROR"}
    last_status: object = None
    sent = False
    for attempt in range(8):
        status, response_payload = _paced_request(request, endpoint, "PATCH", headers, {"children": chunk})
        last_status = status
        if status in (200, 201, 202):
            sent = True
            break
        if status == 413:
            raise RuntimeError(f"block_append_failed:{len(current)}:{status}")
        if status in transient:
            after = _children(request, parent_id, headers)
            if len(after) >= target_count and _is_prefix(after, expected_shells):
                return after
            if attempt + 1 < 8:
                time.sleep(min(30.0, 1.5 * (attempt + 1)))
                continue
        break
    if not sent:
        raise RuntimeError(f"block_append_failed:{len(current)}:{last_status}:{json.dumps(response_payload, ensure_ascii=False)[:2000]}")

    # A successful response may be asynchronous. Poll without re-sending:
    # retrying a successful append would duplicate children.
    for attempt in range(8):
        after = _children(request, parent_id, headers)
        if len(after) >= target_count and _is_prefix(after, expected_shells):
            return after
        if attempt + 1 < 8:
            time.sleep(min(30.0, 1.5 * (attempt + 1)))
    raise RuntimeError(f"block_append_readback_failed:{len(current)}:{last_status}:{len(after)}:{target_count}")


def _append_tree_resumable(
    request: Request,
    parent_id: str,
    headers: dict[str, str],
    expected_blocks: list[dict],
    current: list[dict],
    progress_path: Path | None,
    source_sha256: str,
    root_expected_count: int,
    chunk_size: int,
    node_path: str = "root",
) -> tuple[list[dict], int]:
    """Write a block tree without putting descendants in a large parent payload."""
    expected_shells = [_without_children(block) for block in expected_blocks]
    if len(current) > len(expected_blocks) or not _is_prefix(current, expected_shells):
        raise RuntimeError(f"block_tree_prefix_failed:{node_path}:{len(current)}:{len(expected_blocks)}")

    def populate(index: int, actual: dict) -> None:
        block_type = expected_blocks[index].get("type")
        payload = expected_blocks[index].get(block_type) or {}
        children = payload.get("children") or []
        if not children:
            return
        actual_id = actual.get("id")
        if not actual_id:
            raise RuntimeError(f"block_tree_missing_id:{node_path}/{index}")
        child_current = _children(request, str(actual_id), headers)
        _append_tree_resumable(
            request,
            str(actual_id),
            headers,
            children,
            child_current,
            progress_path,
            source_sha256,
            root_expected_count,
            chunk_size,
            f"{node_path}/{index}",
        )

    cursor = len(current)
    for index in range(cursor):
        populate(index, current[index])

    while cursor < len(expected_blocks):
        chunk = _payload_chunks(expected_shells, cursor, chunk_size)
        after = _append_chunk_readback(request, parent_id, headers, current, expected_shells, chunk)
        target = cursor + len(chunk)
        if len(after) < target:
            raise RuntimeError(f"block_tree_short_readback:{node_path}:{len(after)}:{target}")
        for index in range(cursor, target):
            populate(index, after[index])
        current = after
        cursor = target
        if node_path == "root":
            _save_progress(
                progress_path,
                {
                    "phase": "append",
                    "page_id": parent_id,
                    "source_sha256": source_sha256,
                    "expected_blocks": root_expected_count,
                    "appended_blocks": cursor,
                    "chunk_size": chunk_size,
                },
            )
    return current, len(current)


def _save_progress(path: Path | None, payload: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _load_progress(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _append_resumable(
    request: Request,
    page_id: str,
    headers: dict[str, str],
    expected_blocks: list[dict],
    current: list[dict],
    progress_path: Path | None,
    source_sha256: str,
    chunk_size: int,
) -> tuple[list[dict], int]:
    endpoint = f"https://api.notion.com/v1/blocks/{page_id}/children"
    transient = {429, 500, 502, 503, 504, "ERROR"}
    chunk_number = 0
    while len(current) < len(expected_blocks):
        start = len(current)
        chunk = expected_blocks[start : start + chunk_size]
        target_count = start + len(chunk)
        last_status: object = None
        applied = False
        after: list[dict] = current
        for attempt in range(8):
            status, _ = _paced_request(request, endpoint, "PATCH", headers, {"children": chunk})
            last_status = status
            if status in (200, 201, 202):
                # Keep the hot path cheap.  A confirmed success is recorded
                # locally; periodic and final full read-backs verify that
                # asynchronous Notion writes did not reorder or disappear.
                current = current + chunk
                applied = True
                break
            if status in transient:
                try:
                    after = _children(request, page_id, headers)
                except Exception:
                    after = current
                if _is_prefix(after, expected_blocks) and len(after) >= target_count:
                    current = after
                    applied = True
                    break
                time.sleep(min(30.0, 1.5 * (attempt + 1)))
                continue
            break
        if not applied:
            raise RuntimeError(f"block_append_failed:{start}:{last_status}")
        chunk_number += 1
        if chunk_number % 10 == 0 or len(current) == len(expected_blocks):
            after = _children(request, page_id, headers)
            if not _is_prefix(after, expected_blocks) or len(after) < target_count:
                raise RuntimeError(f"block_append_readback_failed:{start}:{last_status}:{len(after)}:{target_count}")
            current = after
        _save_progress(
            progress_path,
            {
                "phase": "append",
                "page_id": page_id,
                "source_sha256": source_sha256,
                "expected_blocks": len(expected_blocks),
                "appended_blocks": len(current),
                "chunk_size": chunk_size,
            },
        )
    return current, len(current)


def replace_page_resumable(
    request: Request,
    page_id: str,
    headers: dict[str, str],
    markdown: str,
    *,
    progress_path: str | Path | None = None,
    chunk_size: int = 25,
) -> dict:
    """Replace a page with native blocks without leaving an unrecoverable gap.

    Notion block writes are not atomic.  The old implementation deleted the
    page and appended the entire body without a checkpoint, so an execution
    timeout left a partial page and the next run could destroy the partial
    state again.  This writer records the intended source hash and confirms
    every append.  A later invocation resumes only when the page is a verified
    prefix of the same source; otherwise it archives the current tree and
    starts from an empty page.
    """
    expected_blocks = markdown_blocks(markdown)
    source_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    checkpoint = Path(progress_path) if progress_path is not None else None
    prior = _load_progress(checkpoint)
    same_intent = (
        prior.get("page_id") == page_id
        and prior.get("source_sha256") == source_sha256
        and prior.get("expected_blocks") == len(expected_blocks)
        and prior.get("phase") in {"archive", "append", "done"}
    )
    current = _children(request, page_id, headers)
    deleted = 0
    resumed = False

    if same_intent and prior.get("phase") in {"append", "done"} and _is_prefix(current, expected_blocks):
        resumed = bool(current)
    else:
        _save_progress(
            checkpoint,
            {
                "phase": "archive",
                "page_id": page_id,
                "source_sha256": source_sha256,
                "expected_blocks": len(expected_blocks),
                "chunk_size": chunk_size,
            },
        )
        if current:
            # A verified page-level Markdown clear is much faster for very
            # large pages.  It is safe here because clear_page_fast returns
            # success only after a children read-back proves the page empty;
            # otherwise fall back to bounded per-block archival.
            try:
                fast_cleared, fast_deleted = clear_page_fast(request, page_id, headers)
            except Exception:
                fast_cleared, fast_deleted = False, 0
            if fast_cleared:
                deleted = fast_deleted
            else:
                deleted = archive_top_level_resumable(
                    request,
                    page_id,
                    headers,
                    checkpoint,
                    source_sha256,
                    len(expected_blocks),
                    chunk_size=chunk_size,
                )
            current = _children(request, page_id, headers)
            if current:
                raise RuntimeError(f"block_archive_readback_failed:{len(current)}")
        _save_progress(
            checkpoint,
            {
                "phase": "append",
                "page_id": page_id,
                "source_sha256": source_sha256,
                "expected_blocks": len(expected_blocks),
                "appended_blocks": 0,
                "chunk_size": chunk_size,
            },
        )

    try:
        current, appended = _append_tree_resumable(
            request,
            page_id,
            headers,
            expected_blocks,
            current,
            checkpoint,
            source_sha256,
            len(expected_blocks),
            chunk_size,
        )
    except RuntimeError as exc:
        # A same-intent top-level prefix can still contain a stale or reordered
        # nested Details subtree.  Do not keep retrying the corrupted prefix;
        # clear it once, checkpoint the reset, and rebuild from an empty page.
        if not str(exc).startswith("block_tree_prefix_failed:"):
            raise
        current_now = _children(request, page_id, headers)
        if current_now:
            fast_cleared, fast_deleted = clear_page_fast(request, page_id, headers)
            if fast_cleared:
                deleted += fast_deleted
            else:
                deleted += archive_top_level_resumable(
                    request,
                    page_id,
                    headers,
                    checkpoint,
                    source_sha256,
                    len(expected_blocks),
                    chunk_size=chunk_size,
                )
            current_now = _children(request, page_id, headers)
            if current_now:
                raise RuntimeError(f"block_tree_retry_reset_failed:{len(current_now)}") from exc
        _save_progress(
            checkpoint,
            {
                "phase": "append",
                "page_id": page_id,
                "source_sha256": source_sha256,
                "expected_blocks": len(expected_blocks),
                "appended_blocks": 0,
                "chunk_size": chunk_size,
                "restarted_after_nested_prefix_failure": True,
            },
        )
        current, appended = _append_tree_resumable(
            request,
            page_id,
            headers,
            expected_blocks,
            [],
            checkpoint,
            source_sha256,
            len(expected_blocks),
            chunk_size,
        )
    final = _children(request, page_id, headers)
    if len(final) != len(expected_blocks) or not _is_prefix(final, expected_blocks):
        raise RuntimeError(f"block_count_readback_failed:{len(final)}:{len(expected_blocks)}")
    _save_progress(
        checkpoint,
        {
            "phase": "done",
            "page_id": page_id,
            "source_sha256": source_sha256,
            "expected_blocks": len(expected_blocks),
            "appended_blocks": len(final),
            "chunk_size": chunk_size,
        },
    )
    return {
        "deleted_blocks": deleted,
        "appended_blocks": appended,
        "expected_blocks": len(expected_blocks),
        "resumed": resumed,
        "progress_path": str(checkpoint) if checkpoint else None,
    }


def replace_page(request: Request, page_id: str, headers: dict[str, str], markdown: str) -> dict:
    """Backward-compatible entry point using the resumable native writer."""
    return replace_page_resumable(request, page_id, headers, markdown)
