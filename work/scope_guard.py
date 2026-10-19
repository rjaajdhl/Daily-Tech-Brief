"""Fail-closed write boundary for the technology Daily Notion pipeline.

Only the databases, Dashboard page, and Dashboard view declared in
``config/notion_write_scope.json`` may be mutated by this job.  Unlisted
Notion targets remain readable for reference but are never writable from
this pipeline.
"""
from __future__ import annotations
import os

import json
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
SCOPE_PATH = ROOT / "config" / "notion_write_scope.json"


class ScopeViolation(RuntimeError):
    """Raised before an out-of-scope external write is sent."""


def _canonical(value: object) -> str:
    return str(value or "").replace("-", "").casefold()


class NotionWriteScope:
    def __init__(self, config_path: Path = SCOPE_PATH) -> None:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.config_path = config_path
        self.allowed_database_ids = {
            _canonical(value) for value in config.get("allowed_write_database_ids", []) if value
        }
        self.allowed_view_ids = {
            _canonical(value) for value in config.get("allowed_write_view_ids", []) if value
        }
        self.allowed_parent_page_ids = {
            _canonical(value)
            for value in config.get("allowed_write_parent_page_ids", [])
            if value
        }
        self.registered_page_ids: set[str] = set()
        self.registered_block_ids: set[str] = set()

    def register_page(
        self,
        page_id: object,
        *,
        parent_database_id: object | None = None,
        parent_page_id: object | None = None,
    ) -> None:
        page = _canonical(page_id)
        if not page:
            raise ScopeViolation("SCOPE_BLOCKED:empty_page_id")
        if parent_database_id is not None and _canonical(parent_database_id) not in self.allowed_database_ids:
            raise ScopeViolation(
                "SCOPE_BLOCKED:page_parent_not_allowed:"
                f"parent={parent_database_id}"
            )
        if parent_page_id is not None and _canonical(parent_page_id) not in self.allowed_parent_page_ids:
            raise ScopeViolation(
                "SCOPE_BLOCKED:page_parent_page_not_allowed:"
                f"parent={parent_page_id}"
            )
        self.registered_page_ids.add(page)

    def register_view(self, view_id: object) -> None:
        view = _canonical(view_id)
        if not view:
            raise ScopeViolation("SCOPE_BLOCKED:empty_view_id")
        self.allowed_view_ids.add(view)

    def register_block(self, block_id: object) -> None:
        block = _canonical(block_id)
        if block:
            self.registered_block_ids.add(block)

    def _guard_page(self, page_id: str, operation: str) -> None:
        if _canonical(page_id) not in self.registered_page_ids:
            raise ScopeViolation(
                "SCOPE_BLOCKED:unregistered_page:"
                f"operation={operation}:page={page_id}"
            )

    def guard(self, url: str, method: str = "GET", body: object | None = None) -> None:
        """Reject a mutating request whose target is not in this job's scope."""
        verb = str(method or "GET").upper()
        if verb in {"GET", "HEAD", "OPTIONS"}:
            return

        path = urlsplit(url).path.rstrip("/")
        parts = path.split("/")
        # Read-only database/data-source queries use POST but do not mutate the
        # external target.  They remain available for reference/read-back.
        if (
            len(parts) >= 5
            and parts[1] == "v1"
            and parts[2] in {"data_sources", "databases"}
            and parts[4] == "query"
        ):
            return

        # Search is a read-only discovery endpoint even though it uses POST.
        if path == "/v1/search" and verb == "POST":
            return

        # View queries create/read/delete a short-lived server-side result
        # cache. They do not mutate the database or the saved view definition.
        if (
            len(parts) >= 5
            and parts[1] == "v1"
            and parts[2] == "views"
            and parts[4] == "queries"
        ):
            return

        if path == "/v1/pages" and verb == "POST":
            parent = body.get("parent") if isinstance(body, dict) else None
            database_id = parent.get("database_id") if isinstance(parent, dict) else None
            page_id = parent.get("page_id") if isinstance(parent, dict) else None
            if (
                _canonical(database_id) not in self.allowed_database_ids
                and _canonical(page_id) not in self.allowed_parent_page_ids
            ):
                raise ScopeViolation(
                    "SCOPE_BLOCKED:create_page_parent_not_allowed:"
                    f"database={database_id}:page={page_id}"
                )
            return

        if path == "/v1/views" and verb == "POST":
            create_database = body.get("create_database") if isinstance(body, dict) else None
            parent = create_database.get("parent") if isinstance(create_database, dict) else None
            page_id = parent.get("page_id") if isinstance(parent, dict) else None
            if _canonical(page_id) not in self.allowed_parent_page_ids:
                raise ScopeViolation(
                    "SCOPE_BLOCKED:create_view_parent_not_allowed:"
                    f"page={page_id}"
                )
            return

        if len(parts) >= 4 and parts[1] == "v1" and parts[2] == "pages":
            page_id = parts[3]
            self._guard_page(page_id, f"{verb} {path}")
            if verb not in {"PATCH", "DELETE"}:
                raise ScopeViolation(f"SCOPE_BLOCKED:unsupported_page_write:{verb}:{path}")
            return

        if len(parts) >= 5 and parts[1] == "v1" and parts[2] == "blocks":
            block_id = parts[3]
            if parts[4] == "children":
                if (
                    _canonical(block_id) not in self.registered_page_ids
                    and _canonical(block_id) not in self.registered_block_ids
                ):
                    raise ScopeViolation(
                        "SCOPE_BLOCKED:unregistered_block_parent:"
                        f"operation={verb} {path}:parent={block_id}"
                    )
                if verb != "PATCH":
                    raise ScopeViolation(f"SCOPE_BLOCKED:unsupported_children_write:{verb}:{path}")
                return
            raise ScopeViolation(f"SCOPE_BLOCKED:unknown_block_write:{verb}:{path}")

        if len(parts) == 4 and parts[1] == "v1" and parts[2] == "blocks":
            block_id = _canonical(parts[3])
            if block_id not in self.registered_block_ids:
                raise ScopeViolation(
                    "SCOPE_BLOCKED:unregistered_block:"
                    f"operation={verb} {path}"
                )
            if verb != "DELETE":
                raise ScopeViolation(f"SCOPE_BLOCKED:unsupported_block_write:{verb}:{path}")
            return

        if len(parts) == 4 and parts[1] == "v1" and parts[2] == "views":
            if _canonical(parts[3]) not in self.allowed_view_ids:
                raise ScopeViolation(
                    "SCOPE_BLOCKED:view_not_allowed:"
                    f"view={parts[3]}"
                )
            if verb != "PATCH":
                raise ScopeViolation(f"SCOPE_BLOCKED:unsupported_view_write:{verb}:{path}")
            return

        raise ScopeViolation(f"SCOPE_BLOCKED:unknown_write_target:{verb}:{path}")


_SCOPE = NotionWriteScope()


def scope() -> NotionWriteScope:
    return _SCOPE


def guard_notion_request(url: str, method: str = "GET", body: object | None = None) -> None:
    _SCOPE.guard(url, method, body)


def register_page(page_id: object, *, parent_database_id: object | None = None) -> None:
    _SCOPE.register_page(page_id, parent_database_id=parent_database_id)


def register_page_under_page(page_id: object, parent_page_id: object) -> None:
    _SCOPE.register_page(page_id, parent_page_id=parent_page_id)


def register_block(block_id: object) -> None:
    _SCOPE.register_block(block_id)


def register_view(view_id: object) -> None:
    _SCOPE.register_view(view_id)
