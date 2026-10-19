#!/usr/bin/env python3
"""Direct-source enrichment for retained Daily Brief issues.

Discovery metadata is not enough for reader-facing prose.  This module makes a
bounded second pass over the retained issue URLs, stores deterministic source
facts locally, and exposes helpers used by the renderer/finalizer.  It never
fetches Reddit comments: only the submission JSON/selftext and an explicitly
linked external URL may be read.
"""
from __future__ import annotations
import os

import argparse
import concurrent.futures
from contextlib import contextmanager
import fcntl
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

# Keep the canonical direct invocation (`python3 work/source_enrichment.py`)
# importable from the repository root as well as from module execution.
ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from work.feedback import enrichment_priority, update_preference_ledger


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.description = ""
        self.og_description = ""
        self.parts: list[str] = []
        self._skip = 0
        self._content_depth = 0
        self._tag_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {str(k).lower(): str(v or "") for k, v in attrs}
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "template", "button", "input", "select", "option", "textarea", "dialog"}:
            self._skip += 1
        if tag in {"article", "main"}:
            self._content_depth += 1
        if tag == "meta":
            key = (attrs_map.get("name") or attrs_map.get("property") or "").casefold()
            value = attrs_map.get("content", "").strip()
            if key in {"description", "twitter:description"} and value:
                self.description = value
            if key == "og:description" and value:
                self.og_description = value
        self._tag_stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"article", "main"} and self._content_depth:
            self._content_depth -= 1
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "template", "button", "input", "select", "option", "textarea", "dialog"} and self._skip:
            self._skip -= 1
        if self._tag_stack:
            self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = re.sub(r"\s+", " ", html.unescape(data)).strip()
        if not text:
            return
        current = self._tag_stack[-1] if self._tag_stack else ""
        if current == "title":
            self.title.append(text)
        if self._content_depth or current in {"p", "li", "h1", "h2", "h3", "blockquote", "pre"}:
            self.parts.append(text)


def _request(url: str, *, headers: dict[str, str] | None = None, timeout: int = 25) -> tuple[int | str, bytes, str]:
    request_headers = {
        "User-Agent": "DailyTechIntelligence/2026 source-enrichment (research bot)",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5",
    }
    request_headers.update(headers or {})
    req = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read(2_000_000)
            return response.status, raw, response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(200_000), ""
    except Exception as exc:  # kept in the local artifact only
        return "ERROR", str(exc).encode("utf-8", "replace"), ""


_UI_NOISE_PATTERNS = (
    r"link copied to clipboard",
    r"copy link",
    r"share this article",
    r"subscribe(?: to)? (?:our|the) newsletter",
    r"sign up(?: for)? (?:our|the) newsletter",
    r"cookie (?:settings|preferences|policy)",
    r"accept (?:all )?cookies",
    r"advertisement",
    r"sponsored content",
    r"follow us on",
    r"register now for",
    r"you must be signed in to change notification settings",
    r"open more actions menu",
    r"repository files navigation",
    r"skip to (?:content|main content)",
    r"view (?:a|all) file",
    r"sign in(?: to)? (?:change|view|continue)",
    r"enable javascript",
    r"javascript is disabled",
    r"there was an error while loading",
    r"please reload this page",
    r"subscribe to unlock",
    r"\bsubscribe\b",
    r"newsletter signup",
    r"newsletter sign[- ]?up",
    r"back by popular demand",
    r"disrupt\s+20\d{2}",
    r"conference promo(?:tion)?",
    r"read more",
    r"continue reading",
    r"loading\.\.\.",
    r"please wait while we load",
)


def _strip_ui_noise(text: str) -> str:
    cleaned = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    cleaned = re.sub(r"https?://(?:www\.)?httpbin\.org/base64/\S+", " [embedded encoded payload omitted] ", cleaned, flags=re.I)
    cleaned = re.sub(r"https?://\S{300,}", " [long embedded URL omitted] ", cleaned, flags=re.I)
    for pattern in _UI_NOISE_PATTERNS:
        cleaned = re.sub(rf"(?:^|(?<=[.!?。！？]))\s*[^.!?。！？]{{0,220}}{pattern}[^.!?。！？]*[.!?。！？]?", " ", cleaned, flags=re.I)
        cleaned = re.sub(pattern, " ", cleaned, flags=re.I)
    return cleaned


def _clean(value: object, limit: int = 5000) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = _strip_ui_noise(text)
    text = re.sub(r"(?:\.\.\.|…|\[truncated\]|\(truncated\))\s*$", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()[:limit].strip()


def _sentences(text: str, limit: int = 6000) -> str:
    text = _clean(text, 24000)
    if not text:
        return ""
    chunks = re.split(r"(?<=[.!?。！？])\s+", text)
    chosen: list[str] = []
    total = 0
    for chunk in chunks:
        if len(chunk) < 20:
            continue
        chosen.append(chunk)
        total += len(chunk) + 1
        if len(chosen) >= 3 or total >= limit:
            break
    return _clean(" ".join(chosen) or text, limit)


def _github_api(url: str) -> dict[str, Any] | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.casefold() not in {"github.com", "www.github.com"}:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0].startswith("topics") or parts[1].startswith("search"):
        return None
    api_url = f"https://api.github.com/repos/{parts[0]}/{parts[1]}"
    status, raw, content_type = _request(api_url, headers={"Accept": "application/vnd.github+json"})
    if status != 200:
        return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("full_name"):
        return None
    readme = _github_readme(str(data.get("full_name")), str(data.get("default_branch") or "main"))
    return {
        "full_name": data.get("full_name"),
        "description": _clean(data.get("description"), 1200),
        "language": data.get("language"),
        "stargazers_count": data.get("stargazers_count"),
        "forks_count": data.get("forks_count"),
        "open_issues_count": data.get("open_issues_count"),
        "pushed_at": data.get("pushed_at"),
        "created_at": data.get("created_at"),
        "topics": data.get("topics") or [],
        "default_branch": data.get("default_branch"),
        "readme_excerpt": readme,
        "api_status": status,
    }


def _github_readme(full_name: str, branch: str) -> str:
    """Read the repository README without treating GitHub UI HTML as evidence."""
    if not full_name or "/" not in full_name:
        return ""
    branch = urllib.parse.quote(branch or "main", safe="")
    for filename in ("README.md", "readme.md", "README.rst"):
        url = f"https://raw.githubusercontent.com/{full_name}/{branch}/{filename}"
        status, raw, _ = _request(url, headers={"Accept": "text/plain"}, timeout=20)
        if status == 200 and raw:
            text = _clean(raw.decode("utf-8", "replace"), 9000)
            if text:
                return text
    return ""


def _reddit_post(url: str) -> dict[str, Any] | None:
    host = urllib.parse.urlparse(url).netloc.casefold()
    if "reddit.com" not in host and "redd.it" not in host:
        return None
    parsed = urllib.parse.urlparse(url)
    json_url = url.rstrip("/") + ".json?raw_json=1"
    status, raw, _ = _request(json_url, headers={"Accept": "application/json"})
    if status != 200:
        return {"status": status, "selftext": "", "title": "", "external_url": ""}
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
        post = payload[0]["data"]["children"][0]["data"]
    except (ValueError, KeyError, IndexError, TypeError):
        return {"status": status, "selftext": "", "title": "", "external_url": ""}
    # Do not traverse payload[1] (comments).  This is a deliberate scope gate.
    external = str(post.get("url_overridden_by_dest") or post.get("url") or "")
    if "reddit.com" in urllib.parse.urlparse(external).netloc.casefold():
        external = ""
    return {
        "status": status,
        "title": _clean(post.get("title"), 500),
        "selftext": _clean(post.get("selftext"), 5000),
        "external_url": external,
        "comment_scope": "not_retrieved",
    }


def fetch_source(url: str) -> dict[str, Any]:
    url = str(url or "").strip()
    result: dict[str, Any] = {"source_url": url, "enrichment_state": "RESEARCH_INCOMPLETE"}
    if not url.startswith("http"):
        result["reason"] = "missing_http_url"
        return result

    repo = _github_api(url)
    if repo:
        repo_description = _clean(repo.get("description"), 1200)
        readme = _clean(repo.get("readme_excerpt"), 6000)
        source_text = " ".join(part for part in (repo_description, readme) if part)
        result.update(
            {
                "enrichment_state": "FULL_SOURCE" if source_text else "PARTIAL_SOURCE",
                "source_title": str(repo.get("full_name") or ""),
                "source_description": repo_description,
                "source_body_excerpt": readme[:10000],
                "source_facts": _sentences(source_text, 6000),
                "repo_metadata": repo,
                "retrieved_from": "github_api",
            }
        )
        return result

    reddit = _reddit_post(url)
    if reddit is not None:
        selftext = str(reddit.get("selftext") or "")
        title = str(reddit.get("title") or "")
        state = "FULL_SOURCE" if len(selftext) >= 160 else ("PARTIAL_SOURCE" if title else "RESEARCH_INCOMPLETE")
        result.update(
            {
                "http_status": reddit.get("status"),
                "enrichment_state": state,
                "source_title": title,
                "source_description": selftext[:1500],
                "source_body_excerpt": selftext[:10000],
                "source_facts": _sentences(selftext, 6000),
                "retrieved_from": "reddit_post_json",
                "comment_scope": "not_retrieved",
                "external_url": reddit.get("external_url", ""),
            }
        )
        return result

    status, raw, content_type = _request(url)
    result["http_status"] = status
    if status != 200:
        result["reason"] = "http_or_transport_failure"
        return result
    raw_text = raw.decode("utf-8", "replace")
    if "json" in content_type or raw_text.lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            text = _clean(payload.get("text") or payload.get("content") or payload.get("description"), 5000)
            result.update(
                {
                    "enrichment_state": "FULL_SOURCE" if len(text) >= 160 else "PARTIAL_SOURCE",
                    "source_title": _clean(payload.get("title"), 500),
                    "source_description": _clean(payload.get("description"), 1200),
                    "source_body_excerpt": text[:12000],
                    "source_facts": _sentences(text, 6000),
                    "retrieved_from": "json_source",
                }
            )
            return result

    parser = _HTMLText()
    try:
        parser.feed(raw_text)
    except Exception:
        pass
    title = _clean(" ".join(parser.title), 500)
    description = _clean(parser.og_description or parser.description, 1500)
    body_parts: list[str] = []
    seen: set[str] = set()
    for part in parser.parts:
        part = _clean(part, 1200)
        if len(part) < 20 or part in seen:
            continue
        seen.add(part)
        body_parts.append(part)
    body = _clean(" ".join(body_parts), 24000)
    # Meta description alone is not a full source.  It is useful context but
    # must not be laundered as a fully read article.
    state = "FULL_SOURCE" if len(body) >= 300 else ("PARTIAL_SOURCE" if len(description) >= 80 or len(body) >= 80 else "RESEARCH_INCOMPLETE")
    result.update(
        {
            "enrichment_state": state,
            "source_title": title,
            "source_description": description,
            "source_body_excerpt": body[:20000],
            "source_facts": _sentences(body or description, 6000),
            "retrieved_from": "html_source",
            "content_type": content_type,
        }
    )
    return result


def read_intake(day: str) -> list[dict]:
    path = ROOT / "data" / "source_intake" / f"{day}.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def source_enrichment_path(day: str) -> Path:
    return ROOT / "data" / "evidence" / day / f"{day}.source-enrichment.jsonl"


def _primary_urls(rows: list[dict]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for row in rows:
        url = str(row.get("url") or row.get("canonical_url") or "").strip()
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def retained_intake(day: str) -> list[dict]:
    """Use the complete intake only when no disposition ledger exists."""
    intake = read_intake(day)
    disposition = ROOT / "data" / "evidence" / day / f"{day}.disposition.jsonl"
    if not disposition.exists():
        return intake
    included_ids: set[str] = set()
    for row in _load_jsonl(disposition):
        if str(row.get("disposition") or "").casefold().startswith("dismissed"):
            continue
        value = str(row.get("event_id") or row.get("inventory_id") or "").strip()
        if value:
            included_ids.add(value)
    if not included_ids:
        return intake
    retained = [row for row in intake if str(row.get("cluster_id") or "").strip() in included_ids]
    return retained or intake


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _enrich_day_unlocked(day: str, *, max_workers: int = 8) -> list[dict]:
    intake = retained_intake(day)
    urls = _primary_urls(intake)
    preference_ledger = update_preference_ledger(day)
    row_by_url = {
        str(row.get("url") or row.get("canonical_url") or "").strip(): row
        for row in intake
        if str(row.get("url") or row.get("canonical_url") or "").strip()
    }
    # Every URL remains in the collection. Preference only controls which
    # preferred issues enter the bounded worker queue first.
    urls.sort(key=lambda url: (-enrichment_priority(row_by_url.get(url, {}), preference_ledger), url))
    output: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_source, url): url for url in urls}
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {"source_url": url, "enrichment_state": "RESEARCH_INCOMPLETE", "reason": str(exc)}
            row["feedback_enrichment_priority"] = enrichment_priority(row_by_url.get(url, {}), preference_ledger)
            output.append(row)
    output.sort(key=lambda row: str(row.get("source_url") or ""))
    path = source_enrichment_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in output) + "\n", encoding="utf-8")
    return output


@contextmanager
def _enrichment_lock(day: str):
    lock_path = source_enrichment_path(day).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def enrich_day(day: str, *, max_workers: int = 8) -> list[dict]:
    """Serialize same-date writers so stdout matches the final artifact."""
    with _enrichment_lock(day):
        return _enrich_day_unlocked(day, max_workers=max_workers)


def load_enrichment(day: str) -> dict[str, dict]:
    path = source_enrichment_path(day)
    if not path.exists():
        return {}
    result: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("source_url"):
            result[str(row["source_url"])] = row
    return result


def intake_index(day: str) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in read_intake(day):
        cluster = str(row.get("cluster_id") or "").strip()
        if cluster:
            grouped.setdefault(cluster, []).append(row)
    return grouped


def _repo_fields(row: dict) -> dict:
    keys = ("stargazers_count", "forks_count", "open_issues_count", "pushed_at", "created_at", "language", "stars_today", "technical_relevance_score", "discovery_type", "discovery_categories")
    return {key: row.get(key) for key in keys if row.get(key) not in (None, "", [])}


def merge_signal(signal: dict, *, day: str, enrichment: dict[str, dict] | None = None, intake_groups: dict[str, list[dict]] | None = None) -> dict:
    """Attach original source facts and repository metadata without selecting."""
    from work.issue_taxonomy import classify_primary_topic, related_topics, subtopic_for

    merged = dict(signal)
    cluster = str(signal.get("inventory_id") or signal.get("cluster_id") or "").strip()
    members = (intake_groups or {}).get(cluster, [])
    if not members:
        url = str(signal.get("url") or "").strip()
        members = [row for rows in (intake_groups or {}).values() for row in rows if str(row.get("url") or row.get("canonical_url") or "").strip() == url]
    primary = next((row for row in members if str(row.get("url") or "").strip() == str(signal.get("url") or "").strip()), members[0] if members else {})
    if primary:
        merged["source_title"] = primary.get("title") or merged.get("title")
        merged["source_description"] = primary.get("summary") or merged.get("summary")
        merged["repo_metadata"] = _repo_fields(primary)
        merged["source_date"] = primary.get("published_utc") or primary.get("date_raw")
        merged["source_role"] = primary.get("role")
        merged["source_lane"] = primary.get("lane")
        merged["all_source_urls"] = sorted({str(row.get("url") or "").strip() for row in members if str(row.get("url") or "").startswith("http")})
    url = str(merged.get("url") or "").strip()
    if enrichment and url in enrichment:
        fetched = enrichment[url]
        for key in ("enrichment_state", "source_title", "source_description", "source_body_excerpt", "source_facts", "retrieved_from", "http_status", "reason", "comment_scope", "external_url", "content_type", "feedback_enrichment_priority"):
            if fetched.get(key) not in (None, "", []):
                merged[key] = fetched[key]
        if fetched.get("repo_metadata"):
            merged["repo_metadata"] = {**(merged.get("repo_metadata") or {}), **fetched["repo_metadata"]}
    merged.setdefault("enrichment_state", "PARTIAL_SOURCE" if merged.get("summary") else "RESEARCH_INCOMPLETE")
    merged["primary_topic"] = classify_primary_topic(merged)
    merged["topic_section"] = merged["primary_topic"]
    merged["subtopic"] = subtopic_for(merged, merged["primary_topic"])
    merged["related_topics"] = related_topics(merged, merged["primary_topic"])
    return merged


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    rows = enrich_day(args.date, max_workers=max(1, min(args.workers, 16)))
    states: dict[str, int] = {}
    for row in rows:
        state = str(row.get("enrichment_state") or "RESEARCH_INCOMPLETE")
        states[state] = states.get(state, 0) + 1
    print(json.dumps({"date": args.date, "source_count": len(rows), "states": states, "path": str(source_enrichment_path(args.date))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
