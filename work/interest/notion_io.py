"""Notion access for the 11:00 follow-up job, behind its own fail-closed write guard.

The 02:30 publisher keeps using work.scope_guard.  This job gets a *separate* allow-list
(config/notion_write_scope_followup.json) so a bug here cannot write anywhere the job was not given.
Two hard guarantees live in the guard, not in the callers:
  * a to_do block can only have its text changed (``rich_text``); ``checked`` is never sent, so Notion keeps the user's state;
  * blocks can only be deleted/edited when this job read them under a page it registered.
"""
from __future__ import annotations
import os

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[2])
SCOPE_PATH = ROOT / "config" / "notion_write_scope_followup.json"
Request = Callable[..., tuple]


class ScopeViolation(RuntimeError):
    pass


def canon(value: object) -> str:
    return str(value or "").replace("-", "").casefold()


class FollowupScope:
    def __init__(self, config_path: Path = SCOPE_PATH, extra_parent_pages: tuple = ()) -> None:
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.databases = {canon(v) for v in cfg.get("allowed_write_database_ids", [])}
        self.parent_pages = {canon(v) for v in (*cfg.get("allowed_write_parent_page_ids", []), *extra_parent_pages)}
        self.pages: set[str] = set()      # pages this job may PATCH / append to
        self.blocks: set[str] = set()     # blocks read under those pages (editable text / deletable)
        self.created: list[dict] = []     # every page created in this process (for created_pages.json)

    def register_page(self, page_id: object) -> None:
        self.pages.add(canon(page_id))

    def register_block(self, block_id: object) -> None:
        if block_id:
            self.blocks.add(canon(block_id))

    def guard(self, url: str, method: str = "GET", body: object | None = None) -> None:
        verb = str(method or "GET").upper()
        if verb in {"GET", "HEAD", "OPTIONS"}:
            return
        parts = urlsplit(url).path.rstrip("/").split("/")
        if len(parts) >= 5 and parts[1] == "v1" and parts[2] in {"data_sources", "databases"} and parts[4] == "query":
            return
        if "/".join(parts[:3]) == "/v1/search" and verb == "POST":
            return
        b = body if isinstance(body, dict) else {}
        if parts[1:] == ["v1", "pages"] and verb == "POST":
            parent = b.get("parent") or {}
            if canon(parent.get("database_id")) in self.databases and parent.get("database_id"):
                return
            if parent.get("page_id") and (canon(parent["page_id"]) in self.parent_pages or canon(parent["page_id"]) in self.pages):
                return
            raise ScopeViolation(f"SCOPE_BLOCKED:create_page_parent_not_allowed:{parent}")
        if len(parts) == 4 and parts[2] == "pages":
            if canon(parts[3]) not in self.pages:
                raise ScopeViolation(f"SCOPE_BLOCKED:unregistered_page:{parts[3]}")
            if verb != "PATCH" or not set(b) <= {"properties", "in_trash", "archived"}:
                raise ScopeViolation(f"SCOPE_BLOCKED:page_write_shape:{verb}:{sorted(b)}")
            return
        if len(parts) == 5 and parts[2] == "blocks" and parts[4] == "children":
            if canon(parts[3]) not in self.pages and canon(parts[3]) not in self.blocks:
                raise ScopeViolation(f"SCOPE_BLOCKED:unregistered_block_parent:{parts[3]}")
            if verb != "PATCH":
                raise ScopeViolation(f"SCOPE_BLOCKED:children_verb:{verb}")
            return
        if len(parts) == 4 and parts[2] == "blocks":
            if canon(parts[3]) not in self.blocks:
                raise ScopeViolation(f"SCOPE_BLOCKED:unregistered_block:{parts[3]}")
            if verb == "DELETE":
                return
            if verb == "PATCH":
                kinds = [k for k in b if k not in {"archived", "in_trash"}]
                if len(kinds) != 1:
                    raise ScopeViolation(f"SCOPE_BLOCKED:block_patch_shape:{sorted(b)}")
                inner = b[kinds[0]]
                if kinds[0] == "to_do" and set(inner) - {"rich_text"}:
                    raise ScopeViolation("SCOPE_BLOCKED:to_do_patch_may_only_change_text")
                return
        raise ScopeViolation(f"SCOPE_BLOCKED:unknown_write:{verb}:{'/'.join(parts)}")


def http_request(url: str, method: str = "GET", headers: dict | None = None, body=None):
    """Same contract as work.daily_finalizer.request: (status, parsed_json). Reads retry; writes never auto-retry."""
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=payload)
    reads = method.upper() in {"GET", "HEAD"} or (method.upper() == "POST" and ("/query" in url or url.endswith("/search")))
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                try:
                    return r.status, json.loads(raw.decode("utf-8"))
                except Exception:
                    return r.status, None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception:
                parsed = None
            if exc.code in {429, 500, 502, 503, 504} and reads and attempt < 5:
                time.sleep(min(30.0, max(2.0, float(exc.headers.get("Retry-After", "5") or 5))))
                continue
            return exc.code, parsed
        except Exception:
            if reads and attempt < 5:
                time.sleep(min(30.0, 2.0 * (2 ** attempt)))
                continue
            return "ERROR", None
    return "ERROR", None


class NotionIO:
    """Thin client. ``request`` is injectable so tests run against an in-memory fake Notion."""

    def __init__(self, token: str = "", *, request: Request | None = None, scope: FollowupScope | None = None) -> None:
        self.headers = {"Authorization": "Bearer " + token, "Notion-Version": "2026-03-11", "Content-Type": "application/json"}
        self._request = request or http_request
        self.scope = scope or FollowupScope()

    def request(self, url: str, method: str = "GET", headers: dict | None = None, body=None):
        """Signature compatible with the publisher's helpers (append_blocks etc.), but guarded by this job's scope."""
        self.scope.guard(url, method, body)
        return self._request(url, method, headers or self.headers, body)

    def call(self, path: str, method: str = "GET", body=None, ok: tuple = (200, 201)):
        st, data = self.request("https://api.notion.com/v1/" + path.lstrip("/"), method, self.headers, body)
        if st not in ok:
            raise RuntimeError(f"notion_{method}_{path.split('?')[0]}_failed:{st}:{json.dumps(data, ensure_ascii=False)[:200] if data else ''}")
        return data

    # -- reads --------------------------------------------------------------------------------------------------
    def page(self, page_id: str) -> dict:
        return self.call(f"pages/{page_id}")

    def children(self, block_id: str, *, register: bool = True) -> list[dict]:
        out, cursor = [], None
        for _ in range(500):
            data = self.call(f"blocks/{block_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else ""))
            out.extend(data.get("results") or [])
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        if register:
            for blk in out:
                self.scope.register_block(blk.get("id"))
        return out

    def tree(self, block_id: str, *, depth: int = 0) -> list[dict]:
        """Blocks with ``_children`` filled recursively (read-only)."""
        blocks = self.children(block_id)
        if depth > 12:
            raise RuntimeError("tree_depth_limit")
        for blk in blocks:
            if blk.get("has_children"):
                blk["_children"] = self.tree(blk["id"], depth=depth + 1)
        return blocks

    def query(self, data_source_id: str, flt: dict | None = None) -> list[dict]:
        rows, cursor = [], None
        for _ in range(500):
            body = {"page_size": 100}
            if flt:
                body["filter"] = flt
            if cursor:
                body["start_cursor"] = cursor
            data = self.call(f"data_sources/{data_source_id}/query", "POST", body)
            rows.extend(data.get("results") or [])
            if not data.get("has_more"):
                return rows
            cursor = data.get("next_cursor")
        raise RuntimeError("query_pagination_limit")


def plain(block: dict) -> str:
    payload = block.get(block.get("type")) or {}
    return "".join(str(t.get("plain_text") or (t.get("text") or {}).get("content") or "") for t in payload.get("rich_text") or payload.get("title") or [])


def rt(text: str, url: str | None = None) -> list[dict]:
    t: dict = {"content": text}
    if url:
        t["link"] = {"url": url}
    return [{"type": "text", "text": t}]
