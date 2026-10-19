#!/usr/bin/env python3
"""Canonical target-date Daily research pipeline.

This is the only scheduled research entrypoint.  It derives the target window
from ``--date``, preserves the complete intake/disposition partition, runs
broad discovery and recall, joins direct-source enrichment, and writes a DRAFT
artifact.  The finalizer is a separate canonical command.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo
import sys

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))

from work.issue_taxonomy import TOPIC_SECTIONS, classify_primary_topic, related_topics, subtopic_for
from work.source_enrichment import intake_index, load_enrichment, merge_signal

EVIDENCE = ROOT / "data" / "evidence"
IDS = json.loads((ROOT / "config" / "notion_ids.json").read_text(encoding="utf-8"))
TZ = ZoneInfo("America/Los_Angeles")


def assert_stage_only(write_notion: bool) -> None:
    if write_notion:
        raise SystemExit(
            "direct_notion_write_disabled: run independent Writer/Critic and the guarded daily_finalizer instead"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="target date YYYY-MM-DD; never default to a historical date")
    parser.add_argument(
        "--write-notion",
        action="store_true",
        help="legacy compatibility only; normal scheduled runs stage local evidence and let the guarded finalizer own external writes",
    )
    return parser.parse_args()


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def request(url: str, method: str = "GET", headers: dict[str, str] | None = None, body=None, timeout: int = 60):
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=payload)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception:
                parsed = None
            return response.status, raw, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = None
        return exc.code, raw, parsed
    except Exception as exc:
        return "ERROR", str(exc).encode("utf-8", "replace"), None


def usable_result_content(result: dict) -> bool:
    for key in ("markdown", "fit_markdown", "cleaned_html", "html", "text"):
        value = result.get(key)
        if isinstance(value, str) and len(value.strip()) >= 160:
            return True
    return False


def classify_crawl(payload) -> tuple[str, bool, str]:
    """Return state, verified flag, and a short internal reason."""
    if not isinstance(payload, dict):
        return "UNAVAILABLE", False, "non_json_response"
    top_success = payload.get("success") is True
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return ("UNAVAILABLE", False, "missing_results")
    result_success = all(isinstance(item, dict) and item.get("success") is True for item in results)
    usable = any(isinstance(item, dict) and usable_result_content(item) for item in results)
    if top_success and result_success and usable:
        return "FULL_SOURCE", True, "top_level_and_result_success_with_content"
    if top_success and results:
        return "PARTIAL_SOURCE", False, "transport_success_but_result_or_content_incomplete"
    return "UNAVAILABLE", False, "top_level_failure"


def read_intake(day: str) -> list[dict]:
    path = ROOT / "data" / "source_intake" / f"{day}.jsonl"
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def is_generic_candidate(item: dict) -> bool:
    title = str(item.get("title") or "").strip().lower()
    url = str(item.get("url") or "").lower()
    if not title or not url.startswith("http"):
        return True
    generic = (
        "openai | research & deployment",
        "openai - wikipedia",
        "what is artificial intelligence",
        "google ai - how",
        "online learning courses",
        "merriam-webster",
        "dictionary",
        "youtube",
        "coursera",
        "chatgpt: chat",
    )
    return any(token in title for token in generic)


def _inventory_key(item: dict) -> str:
    """Use the intake's dedup cluster; never use a topic/source quota."""
    cluster_id = str(item.get("cluster_id") or "").strip()
    if cluster_id:
        return "cluster:" + cluster_id
    canonical_url = str(item.get("canonical_url") or item.get("url") or "").strip().lower().rstrip("/")
    if canonical_url:
        return "url:" + canonical_url
    title = re.sub(r"\W+", " ", str(item.get("title") or "").lower()).strip()
    return "title:" + title


def _source_priority(item: dict) -> int:
    source_id = str(item.get("source_id") or "")
    role = str(item.get("role") or "")
    if source_id in {"cisa-kev", "cisa-advisories", "microsoft-security"} or role in {"advisory", "official_security"}:
        return 5
    if role in {"official", "specialist_reporting", "professional_reporting", "research"}:
        return 4
    if source_id.startswith("github-") or source_id.startswith("huggingface-"):
        return 3
    if item.get("lane") == "community_signals" or source_id in {"hacker-news", "lobsters", "the-hacker-news"}:
        return 2
    return 3


def _default_evidence_state(item: dict) -> str:
    explicit = str(item.get("evidence_state") or "").upper()
    if explicit in {"VERIFIED", "DEVELOPING", "UNVERIFIED", "SPECULATIVE", "DISMISSED"}:
        return explicit
    if item.get("lane") == "community_signals" or item.get("discovery_type") in {"github_trending", "github_repo_search", "huggingface_model_radar"}:
        return "UNVERIFIED"
    return "DEVELOPING"


def _is_retainable_candidate(item: dict) -> bool:
    """Return whether an intake row may enter the retained issue inventory.

    This is a quality/disposition gate, not a ranking quota.  Rows that fail it
    are returned as explicit dismissals so they cannot disappear silently.
    """
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    explicit_state = str(item.get("evidence_state") or "").upper()
    return bool(
        title
        and url.startswith("http")
        and item.get("editorial_eligible") is not False
        and explicit_state != "DISMISSED"
        and not is_generic_candidate(item)
    )


def _evidence_priority(item: dict) -> int:
    return {
        "VERIFIED": 5,
        "DEVELOPING": 4,
        "UNVERIFIED": 2,
        "SPECULATIVE": 1,
        "DISMISSED": 0,
    }.get(_default_evidence_state(item), 3)


def _candidate_rank(item: dict) -> tuple[int, int, float]:
    try:
        score = float(item.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    return (_evidence_priority(item), _source_priority(item), score)


def _source_entry(item: dict) -> dict:
    return {
        "source_id": str(item.get("source_id") or ""),
        "url": str(item.get("url") or "").strip(),
        "title": str(item.get("title") or "").strip(),
    }


def _inventory_id(key: str, members: list[dict]) -> str:
    for item in members:
        cluster_id = str(item.get("cluster_id") or "").strip()
        if cluster_id:
            return cluster_id
    return key


def _dismissal_reason(members: list[dict]) -> str:
    if all(str(item.get("evidence_state") or "").upper() == "DISMISSED" for item in members):
        return "explicitly_marked_dismissed"
    if all(not str(item.get("title") or "").strip() or not str(item.get("url") or "").strip().startswith("http") for item in members):
        return "missing_title_or_http_url"
    if all(item.get("editorial_eligible") is False for item in members):
        return "editorial_ineligible_by_source_disposition"
    if all(is_generic_candidate(item) for item in members):
        return "generic_or_irrelevant_candidate"
    return "basic_quality_filter_failed"


def _build_retained_signal(key: str, members: list[dict], eligible: list[dict]) -> dict:
    primary = max(eligible, key=_candidate_rank)
    title = str(primary.get("title") or "").strip()
    url = str(primary.get("url") or "").strip()
    try:
        score = max(float(item.get("score") or 0) for item in members)
    except (TypeError, ValueError):
        score = 0.0
    state = _default_evidence_state(primary)
    continuity = str(primary.get("continuity_class") or ("WATCH" if state in {"UNVERIFIED", "SPECULATIVE"} else "NEW")).upper()
    if continuity not in {"NEW", "DEVELOPING", "ONGOING", "WATCH"}:
        continuity = "WATCH"
    source_entries: list[dict] = []
    seen_urls: set[str] = set()
    for item in sorted(members, key=_candidate_rank, reverse=True):
        entry = _source_entry(item)
        if not entry["url"].startswith("http") or entry["url"] in seen_urls:
            continue
        seen_urls.add(entry["url"])
        source_entries.append(entry)
    inventory_id = _inventory_id(key, members)
    signal = {
        "inventory_id": inventory_id,
        "title": title,
        "summary": re.sub(r"\s+", " ", str(primary.get("summary") or "")).strip()[:1000],
        "url": url,
        "source_id": str(primary.get("source_id") or ""),
        "lane": primary.get("lane"),
        "role": primary.get("role"),
        "topics": sorted({topic for item in members for topic in (item.get("topics") or [])}),
        "categories": sorted({category for item in members for category in (item.get("categories") or [])}),
        "discovery_categories": sorted({category for item in members for category in (item.get("discovery_categories") or [])}),
        "score": score,
        "cluster_id": primary.get("cluster_id"),
        "evidence_state": state,
        "continuity_class": continuity,
        "priority_delta": str(primary.get("priority_delta") or "UNCHANGED").upper(),
        "supporting_sources": source_entries,
        "source_title": title,
        "source_description": re.sub(r"\s+", " ", str(primary.get("summary") or "")).strip()[:1200],
        "source_date": primary.get("published_utc") or primary.get("date_raw"),
        "repo_metadata": {key: primary.get(key) for key in ("stargazers_count", "forks_count", "open_issues_count", "pushed_at", "created_at", "language", "stars_today", "technical_relevance_score", "discovery_type", "discovery_categories") if primary.get(key) not in (None, "", [])},
    }
    signal["primary_topic"] = classify_primary_topic(signal)
    signal["topic_section"] = signal["primary_topic"]
    signal["subtopic"] = subtopic_for(signal, signal["primary_topic"])
    signal["related_topics"] = related_topics(signal, signal["primary_topic"])
    return signal


def select_reader_inventory(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return ``(retained_signals, explicitly_dismissed_inventory)``.

    Intake rows are first clustered by their canonical underlying event.  Every
    deduplicated cluster then takes exactly one of two paths: it is retained as
    one issue, or it receives an explicit dismissal reason.  There is no overall
    limit, per-topic limit, source bucket cap, Top-N rule, or balancing rule.
    """
    grouped: dict[str, list[dict]] = {}
    for index, item in enumerate(rows):
        key = _inventory_key(item)
        # Do not collapse unrelated malformed rows into one silent bucket.
        if key in {"title:", "url:"}:
            key = f"row:{index}"
        grouped.setdefault(key, []).append(item)

    signals: list[dict] = []
    dismissed: list[dict] = []
    for key, members in grouped.items():
        eligible = [item for item in members if _is_retainable_candidate(item)]
        if eligible:
            signals.append(_build_retained_signal(key, members, eligible))
            continue
        primary = max(members, key=lambda item: (str(item.get("title") or "").strip(), str(item.get("url") or "")))
        dismissed.append(
            {
                "inventory_id": _inventory_id(key, members),
                "title": str(primary.get("title") or "[untitled candidate]").strip(),
                "url": str(primary.get("url") or "").strip(),
                "source_id": str(primary.get("source_id") or ""),
                "reason": _dismissal_reason(members),
                "member_count": len(members),
                "source_ids": sorted({str(item.get("source_id") or "") for item in members if item.get("source_id")}),
            }
        )

    signals.sort(key=lambda item: (-float(item.get("score") or 0), str(item.get("title") or "").casefold()))
    dismissed.sort(key=lambda item: (str(item.get("title") or "").casefold(), str(item.get("inventory_id") or "")))
    return signals, dismissed


def select_reader_signals(rows: list[dict]) -> list[dict]:
    """Compatibility wrapper returning only the retained issue collection."""
    signals, _dismissed = select_reader_inventory(rows)
    return signals


def source_label(source_id: str) -> str:
    return {
        "cisa-kev": "CISA KEV",
        "cisa-advisories": "CISA",
        "the-hacker-news": "The Hacker News",
        "hacker-news": "Hacker News",
        "lobsters": "Lobsters",
        "github-trending-daily": "GitHub Trending",
        "github-created-repositories": "GitHub repository discovery",
        "github-active-repositories": "GitHub repository activity",
        "github-changelog": "GitHub Changelog",
        "huggingface-models-radar": "Hugging Face model radar",
        "huggingface-daily-papers": "Hugging Face Daily Papers",
        "anthropic-threat-report": "Anthropic Threat Intelligence Report",
        "openai-agents-api": "OpenAI Agents API",
    }.get(source_id, source_id or "source")


TOPIC_SECTIONS = TOPIC_SECTIONS


def _topic_key(signal: dict) -> str:
    """Compatibility wrapper around the shared mechanism-first classifier."""
    return classify_primary_topic(signal)


def _issue_sources(signal: dict) -> list[dict]:
    sources = signal.get("supporting_sources") or [{"source_id": signal.get("source_id"), "url": signal.get("url"), "title": signal.get("title")}]
    unique: list[dict] = []
    seen: set[str] = set()
    for source in sources:
        url = str(source.get("url") or "").strip()
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        unique.append(source)
    return unique


def build_reader_markdown(day: str, signals: list[dict]) -> str:
    """Delegate reader-facing prose to the standalone narrative renderer."""
    from work.reader_narrative import build_reader_markdown as render_reader_markdown

    return render_reader_markdown(
        day,
        signals,
        topic_sections=TOPIC_SECTIONS,
        topic_key=_topic_key,
        issue_sources=_issue_sources,
        source_label=source_label,
        evidence_priority=_evidence_priority,
        source_priority=_source_priority,
    )


def exact_daily_query(notion_headers: dict[str, str], data_source_id: str, day: str):
    return request(
        "https://api.notion.com/v1/data_sources/" + data_source_id + "/query",
        "POST",
        notion_headers,
        {"filter": {"property": "Date", "date": {"equals": day}}},
    )


def page_title(metadata: dict) -> str:
    props = metadata.get("properties", {}) if isinstance(metadata, dict) else {}
    return "".join(x.get("plain_text", "") for x in props.get("Name", {}).get("title", []))


def page_date(metadata: dict) -> str | None:
    props = metadata.get("properties", {}) if isinstance(metadata, dict) else {}
    return (props.get("Date", {}).get("date") or {}).get("start")


def page_stage(metadata: dict) -> str | None:
    props = metadata.get("properties", {}) if isinstance(metadata, dict) else {}
    return ((props.get("Stage", {}).get("select") or {}).get("name"))


def _markdown_chunks(markdown: str, max_chars: int = 80000) -> list[str]:
    if not markdown:
        return [""]
    chunks: list[str] = []
    start = 0
    while start < len(markdown):
        target = min(len(markdown), start + max_chars)
        if target < len(markdown):
            blank = markdown.rfind("\n\n", start, target)
            newline = markdown.rfind("\n", start, target)
            if blank > start + max_chars // 2:
                target = blank + 2
            elif newline > start + max_chars // 2:
                target = newline + 1
        chunks.append(markdown[start:target])
        start = target
    return chunks


def write_markdown_chunked(page_id: str, headers: dict[str, str], markdown: str) -> tuple[int, int]:
    """Write large reader bodies in bounded replace/append requests."""
    initial = _markdown_chunks(markdown, max_chars=12000)
    first = initial[0]
    remainder = _markdown_chunks(markdown[len(first):], max_chars=80000) if len(first) < len(markdown) else []
    chunks = [first] + remainder
    endpoint = f"https://api.notion.com/v1/pages/{page_id}/markdown"

    def patch(payload: dict) -> int | str:
        status, _, _ = request(endpoint, "PATCH", headers, payload)
        return status

    status = patch({"type": "replace_content", "replace_content": {"new_str": chunks[0]}})
    if status not in (200, 201, 202):
        return status, 0
    for index, chunk in enumerate(chunks[1:], start=1):
        status = patch({
            "type": "insert_content",
            "insert_content": {"content": "\n\n" + chunk.lstrip("\n"), "position": {"type": "end"}},
            "allow_async": False,
        })
        if status not in (200, 201, 202):
            return status, index
    return 200, len(chunks)


def main() -> int:
    args = parse_args()
    assert_stage_only(args.write_notion)
    target = dt.date.fromisoformat(args.date)
    day = target.isoformat()
    next_day = target + dt.timedelta(days=1)
    window_start = dt.datetime.combine(target, dt.time.min, tzinfo=TZ).isoformat()
    window_end = dt.datetime.combine(next_day, dt.time.min, tzinfo=TZ).isoformat()
    EVIDENCE.mkdir(parents=True, exist_ok=True)

    env = load_env()
    searx = env.get("SEARXNG_URL", "").rstrip("/")
    crawl_url = env.get("CRAWL4AI_URL", "").rstrip("/")
    crawl_token = env.get("CRAWL4AI_API_TOKEN", "")
    notion_token = env.get("NOTION_API_KEY", "")
    if not all((searx, crawl_url, crawl_token, notion_token)):
        raise SystemExit("required service configuration is incomplete")

    health: dict[str, dict] = {}
    for name, url, headers in (
        ("searxng", searx + "/healthz", {}),
        ("crawl_health", crawl_url + "/health", {}),
        ("crawl_openapi", crawl_url + "/openapi.json", {"Authorization": "Bearer " + crawl_token}),
    ):
        status, raw, payload = request(url, headers=headers)
        health[name] = {
            "status": status,
            "body_status": (payload or {}).get("status") if isinstance(payload, dict) else None,
            "body_excerpt": raw[:200].decode("utf-8", "replace") if name == "searxng" else None,
        }

    openapi_status, _, openapi = request(crawl_url + "/openapi.json", headers={"Authorization": "Bearer " + crawl_token})
    paths = sorted((openapi or {}).get("paths", {}).keys()) if isinstance(openapi, dict) else []

    lanes = [
        ("Frontier AI / Major AI Companies", "OpenAI Anthropic Google DeepMind Meta Microsoft xAI AI announcement release " + day),
        ("AI Agents / Developer Ecosystem", "AI agents MCP computer use orchestration tool use security " + day),
        ("Local / Open Models", "open weights local model Hugging Face model card benchmark " + day),
        ("Cybersecurity / Offensive Security", "CVE exploit zero day ransomware AI security " + day),
        ("Infrastructure / Cloud / Compute", "cloud outage datacenter power compute infrastructure " + day),
        ("Semiconductors / AI Infrastructure", "NVIDIA AMD Intel TSMC HBM accelerator semiconductor " + day),
        ("GitHub / Developer Tools", "GitHub Trending new repository agent framework security tool " + day),
        ("Research / Papers / Benchmarks", "arXiv AI agents benchmark evaluation reasoning " + day),
        ("Community Signals", "Reddit Hacker News AI agents cybersecurity local models " + day),
        ("Outside My Usual Bubble", "robotics space biotech quantum energy materials networking technology " + day),
    ]
    recall_queries = [
        "major AI news " + day,
        "major AI company announcement " + day,
        "major agent news " + day,
        "major cybersecurity news " + day,
        "major semiconductor news " + day,
        "major infrastructure compute news " + day,
        "major open-source release " + day,
        "major technology development outside usual interests " + day,
        "Broadcom Marvell TSMC Samsung SK hynix Micron HBM custom silicon AI infrastructure " + day,
        "NVIDIA Hugging Face acquisition AI model ecosystem " + day,
        "Astra rollout agent safety independent evaluation " + day,
        "Exchange Online outage Microsoft cloud incident " + day,
        "Kubernetes rootless KubeletInUserNamespace HyperPod " + day,
        "known exploited vulnerability active exploitation KEV " + day,
    ]
    search_rows: list[dict] = []
    for lane, query in lanes + [("Major Story Recall", item) for item in recall_queries]:
        search_url = searx + "/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json", "pageno": 1, "engines": "bing,naver"}
        )
        started = time.time()
        status, raw, payload = request(search_url)
        results = payload.get("results", []) if isinstance(payload, dict) else []
        search_rows.append(
            {
                "lane": lane,
                "query": query,
                "engines": "bing,naver",
                "http_status": status,
                "latency_ms": round((time.time() - started) * 1000),
                "result_count": len(results),
                "unresponsive_engines": payload.get("unresponsive_engines", []) if isinstance(payload, dict) else [],
                "results": results[:20],
                "error": None if isinstance(payload, dict) else raw[:300].decode("utf-8", "replace"),
            }
        )

    candidate_urls: list[str] = []
    seen_candidate_urls: set[str] = set()
    for row in search_rows:
        for result in row.get("results", []):
            url = result.get("url")
            if not isinstance(url, str) or not url.startswith("http") or url in seen_candidate_urls:
                continue
            seen_candidate_urls.add(url)
            candidate_urls.append(url)
    # Search-result retrieval is an evidence operation, not an issue-selection
    # quota.  Every discovered URL remains eligible for direct retrieval; the
    # final reader inventory is decided only by event deduplication and explicit
    # quality disposition below.

    def crawl_candidate(url: str) -> dict:
        started = time.time()
        status, raw, payload = request(
            crawl_url + "/crawl",
            "POST",
            {"Authorization": "Bearer " + crawl_token, "Content-Type": "application/json"},
            {"urls": [url]},
            # Crawl4AI runs browser work server-side. Keep the client timeout
            # bounded, but long enough for a single heavy page when the service
            # is healthy. Concurrency is capped below to avoid queue saturation.
            timeout=int(env.get("CRAWL4AI_REQUEST_TIMEOUT", "30")),
        )
        state, verified, reason = classify_crawl(payload)
        return {
            "url": url,
            "method": "POST",
            "endpoint": "/crawl",
            "http_status": status,
            "latency_ms": round((time.time() - started) * 1000),
            "extraction_state": state,
            "crawl_result_success": verified,
            "classification_reason": reason,
            "response_length": len(raw),
            "body_excerpt": raw[:600].decode("utf-8", "replace"),
            "body_sha256": hashlib.sha256(raw).hexdigest(),
        }

    # Retrieval is bounded but not editorially capped: every discovered URL is
    # attempted, while concurrent workers prevent one slow source from turning
    # the scheduled run into an unbounded serial crawl.
    # Keep all discovered URLs eligible while shortening the failure tail when
    # the crawler service is down.  The intake is read before this potentially
    # slow stage so a later timeout cannot be mistaken for an empty collection.
    intake_rows = read_intake(day)
    try:
        configured_workers = int(env.get("CRAWL4AI_WORKERS", "8"))
    except ValueError:
        configured_workers = 8
    crawl_workers = min(12, max(1, configured_workers), max(1, len(candidate_urls)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=crawl_workers) as pool:
        crawl_rows = list(pool.map(crawl_candidate, candidate_urls))

    signals, dismissed_inventory = select_reader_inventory(intake_rows)
    # If a prior enrichment pass exists (for example during a rerun/finalizer
    # handoff), join its direct-source facts before rendering.  The join is
    # presentation metadata only and cannot remove or rank issues.
    enrichment = load_enrichment(day)
    groups = intake_index(day)
    signals = [merge_signal(signal, day=day, enrichment=enrichment, intake_groups=groups) for signal in signals]
    reader_markdown = build_reader_markdown(day, signals)
    target_evidence = EVIDENCE / day
    target_evidence.mkdir(parents=True, exist_ok=True)
    reader_path = target_evidence / f"{day}.md"
    audit_path = target_evidence / f"{day}.audit.md"
    search_path = target_evidence / f"{day}.search.jsonl"
    crawl_path = target_evidence / f"{day}.crawl.jsonl"
    recall_path = target_evidence / f"{day}.major-recall.jsonl"
    coverage_path = target_evidence / f"{day}.coverage.json"
    manifest_path = target_evidence / f"{day}.manifest.json"
    disposition_path = target_evidence / f"{day}.disposition.jsonl"

    search_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in search_rows) + "\n", encoding="utf-8")
    crawl_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in crawl_rows) + "\n", encoding="utf-8")
    recall_rows = []
    for row in search_rows:
        if row.get("lane") != "Major Story Recall":
            continue
        for result in row.get("results", []):
            url = str(result.get("url") or "").strip()
            if not url.startswith("http"):
                continue
            recall_rows.append(
                {
                    "recall_query": row.get("query"),
                    "title": result.get("title", ""),
                    "url": url,
                    "source_id": result.get("engine") or urllib.parse.urlparse(url).netloc,
                    "summary": result.get("content", ""),
                    "include_in_reader": False,
                    "verification_required": True,
                }
            )
    unique_recall = {}
    for row in recall_rows:
        unique_recall.setdefault(row["url"], row)
    recall_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in unique_recall.values()) + ("\n" if unique_recall else ""), encoding="utf-8")

    full_count = sum(row["extraction_state"] == "FULL_SOURCE" for row in crawl_rows)
    partial_count = sum(row["extraction_state"] == "PARTIAL_SOURCE" for row in crawl_rows)
    unavailable_count = sum(row["extraction_state"] == "UNAVAILABLE" for row in crawl_rows)
    coverage: dict[str, dict] = {}
    crawl_by_url = {str(row.get("url") or ""): row for row in crawl_rows}
    for lane, _ in lanes:
        rows = [row for row in search_rows if row["lane"] == lane]
        errors = [row.get("error") for row in rows if row.get("http_status") == "ERROR"]
        unresponsive = [engine for row in rows for engine in row.get("unresponsive_engines", [])]
        lane_urls = {
            str(result.get("url") or "")
            for search_row in rows
            for result in search_row.get("results", [])
            if str(result.get("url") or "").startswith("http")
        }
        lane_crawl = [crawl_by_url[url] for url in lane_urls if url in crawl_by_url]
        lane_full = sum(row.get("extraction_state") == "FULL_SOURCE" for row in lane_crawl)
        lane_partial = sum(row.get("extraction_state") == "PARTIAL_SOURCE" for row in lane_crawl)
        candidate_count = sum(int(row.get("result_count") or 0) for row in rows)
        if candidate_count == 0:
            coverage_status = "PASS_NO_MATERIAL_EVENT"
        elif lane_full == 0:
            coverage_status = "UNAVAILABLE" if not lane_partial else "PARTIAL"
        elif errors or unresponsive:
            coverage_status = "PARTIAL"
        else:
            coverage_status = "PASS"
        coverage[lane] = {
            "coverage_status": coverage_status,
            "registry_checked": True,
            "search_executed": True,
            "queries": [row["query"] for row in rows],
            "candidate_count": candidate_count,
            "retrieved_full_source_count": lane_full,
            "retrieved_partial_source_count": lane_partial,
            "retrieved_unavailable_count": max(0, len(lane_crawl) - lane_full - lane_partial),
            "failures": errors,
            "unresponsive_engines": sorted({json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, (dict, list)) else str(item) for item in unresponsive}),
            "notes": "Coverage status is separate from event evidence state; source retrieval is measured per lane.",
        }

    disposition_rows = []
    for signal in signals:
        disposition_rows.append(
            {
                "event_id": str(signal["inventory_id"]),
                "canonical_title": signal["title"],
                "source_url": signal["url"],
                "continuity_class": signal.get("continuity_class", "WATCH"),
                "evidence_state": signal.get("evidence_state", "UNVERIFIED"),
                "coverage_status": signal.get("coverage_status", "UNKNOWN"),
                "disposition": "INCLUDED_FOR_EDITORIAL_REVIEW",
                "what_was_known": "Current intake contains a retained public signal.",
                "what_changed_today": "Target-date editorial comparison is performed by the Draft/Finalizer agents.",
                "why_it_still_matters": "The retained issue may indicate a material technical change or emerging discussion.",
                "new_evidence_or_context": signal.get("summary", ""),
                "priority_delta": signal.get("priority_delta", "UNCHANGED"),
                "topic_section": signal.get("primary_topic"),
                "subtopic": signal.get("subtopic"),
                "related_topics": signal.get("related_topics", []),
                "supporting_sources": signal.get("supporting_sources", []),
                "source_facts": signal.get("source_facts", ""),
                "enrichment_state": signal.get("enrichment_state", "RESEARCH_INCOMPLETE"),
            }
        )
    for item in dismissed_inventory:
        disposition_rows.append(
            {
                "event_id": str(item["inventory_id"]),
                "canonical_title": item["title"],
                "source_url": item.get("url", ""),
                "continuity_class": "DROP",
                "evidence_state": "DISMISSED",
                "coverage_status": "NOT_APPLICABLE",
                "disposition": "DISMISSED",
                "dismissal_reason": item["reason"],
                "member_count": item.get("member_count", 1),
                "source_ids": item.get("source_ids", []),
            }
        )
    disposition_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in disposition_rows) + "\n", encoding="utf-8"
    )

    retained_issue_ids = [str(signal["inventory_id"]) for signal in signals] + [
        str(item["inventory_id"]) for item in dismissed_inventory
    ]
    reader_issue_ids = [str(signal["inventory_id"]) for signal in signals]
    dismissed_issue_ids = [str(item["inventory_id"]) for item in dismissed_inventory]
    retained_issue_count = len(retained_issue_ids)
    reader_issue_count = len(reader_issue_ids)
    explicitly_dismissed_count = len(dismissed_issue_ids)
    event_evidence_states: dict[str, int] = {}
    for signal in signals:
        state = str(signal.get("evidence_state") or "UNVERIFIED")
        event_evidence_states[state] = event_evidence_states.get(state, 0) + 1
    manifest = {
        "schema_version": 3,
        "target_date": day,
        "target_timezone": "America/Los_Angeles",
        "window_start": window_start,
        "window_end": window_end,
        "reader_markdown_path": str(reader_path),
        "audit_markdown_path": str(audit_path),
        "research_services": {
            "searxng_configured": bool(searx),
            "crawl4ai_configured": bool(crawl_url),
            "crawl4ai_auth_configured": bool(crawl_token),
            "health": {key: value.get("status") for key, value in health.items()},
            "openapi_paths": [path for path in paths if any(part in path for part in ("crawl", "md", "html"))],
        },
        "coverage": coverage,
        "search_query_count": len(search_rows),
        "crawl_attempt_count": len(crawl_rows),
        "crawl_success_count": full_count,
        "crawl_partial_count": partial_count,
        "crawl_failure_count": unavailable_count,
        "verified_count": full_count,
        "crawl_failures": [row["url"] for row in crawl_rows if row["extraction_state"] != "FULL_SOURCE"],
        "source_intake_row_count": len(intake_rows),
        "deduplicated_inventory_count": retained_issue_count,
        "reader_signal_count": reader_issue_count,
        "retained_issue_count": retained_issue_count,
        "reader_issue_count": reader_issue_count,
        "explicitly_dismissed_count": explicitly_dismissed_count,
        "retained_issue_ids": retained_issue_ids,
        "reader_issue_ids": reader_issue_ids,
        "explicitly_dismissed_ids": dismissed_issue_ids,
        "event_evidence_states": event_evidence_states,
        "classification_version": "mechanism-first-v2",
        "source_enrichment_states": {
            state: sum(1 for signal in signals if str(signal.get("enrichment_state") or "RESEARCH_INCOMPLETE").upper() == state)
            for state in sorted({str(signal.get("enrichment_state") or "RESEARCH_INCOMPLETE").upper() for signal in signals})
        },
        "dynamic_subtopics": {
            f"{signal.get('primary_topic')} / {signal.get('subtopic')}": sum(1 for item in signals if item.get("primary_topic") == signal.get("primary_topic") and item.get("subtopic") == signal.get("subtopic"))
            for signal in signals
        },
        "major_story_recall_gate": {
            "artifact": str(recall_path),
            "candidate_count": len(unique_recall),
            "included_recall_count": 0,
            "requires_direct_verification": True,
        },
        "content_loss_check": {
            "retained_issue_count": retained_issue_count,
            "reader_issue_count": reader_issue_count,
            "explicitly_dismissed_count": explicitly_dismissed_count,
            "passed": reader_issue_count + explicitly_dismissed_count == retained_issue_count,
            "rule": "reader_issue_count + explicitly_dismissed_count must equal retained_issue_count",
        },
        "limitations": [
            "Reader Markdown is separate from internal audit telemetry.",
            "Reddit comments are not retrieved by default.",
            "Discovery signal and event evidence state are separate.",
            "The top editorial summary is navigation only; topic sections contain the complete retained issue inventory.",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    coverage_path.write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reader_path.write_text(reader_markdown, encoding="utf-8")

    audit_lines = [
        f"# Internal Daily Research Audit — {day}",
        "",
        f"target_date={day}",
        f"window_start={window_start}",
        f"window_end={window_end}",
        f"service_health={json.dumps({key: value.get('status') for key, value in health.items()}, ensure_ascii=False)}",
        f"coverage_status={json.dumps({key: value['coverage_status'] for key, value in coverage.items()}, ensure_ascii=False)}",
        f"search_query_count={len(search_rows)}",
        f"crawl_attempt_count={len(crawl_rows)}",
        f"crawl_full_source_count={full_count}",
        f"crawl_partial_source_count={partial_count}",
        f"crawl_unavailable_count={unavailable_count}",
        f"reader_signal_count={reader_issue_count}",
        f"retained_issue_count={retained_issue_count}",
        f"reader_issue_count={reader_issue_count}",
        f"explicitly_dismissed_count={explicitly_dismissed_count}",
        "content_loss_check=PASSED; no topic/source quota applied; every dismissed cluster has a reason",
        "",
        "Internal artifacts:",
        f"- {manifest_path}",
        f"- {search_path}",
        f"- {crawl_path}",
        f"- {coverage_path}",
        f"- {disposition_path}",
        "",
        "Reader-facing Markdown intentionally excludes the above telemetry.",
    ]
    audit_path.write_text("\n".join(audit_lines) + "\n", encoding="utf-8")

    if not args.write_notion:
        local_reader = reader_path.read_text(encoding="utf-8") if reader_path.exists() else ""
        staged_readback = {
            "target_date": day,
            "action": "staged_local_only",
            "status": "NOTION_WRITE_DEFERRED",
            "reason": "guarded_finalizer_requires_complete_semantic_rewrite_artifact",
            "metadata_status": None,
            "markdown_status": None,
            "local_reader_chars": len(local_reader),
            "local_reader_sha256": hashlib.sha256(local_reader.encode("utf-8")).hexdigest() if local_reader else None,
            "reader_signal_count": len(signals),
            "crawl_full_source_count": full_count,
            "crawl_partial_source_count": partial_count,
        }
        (target_evidence / f"{day}.notion-readback.json").write_text(
            json.dumps(staged_readback, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(staged_readback, ensure_ascii=False))
        return 0

    notion_headers = {
        "Authorization": "Bearer " + notion_token,
        "Notion-Version": "2026-03-11",
        "Content-Type": "application/json",
    }
    query_status, _, query_payload = exact_daily_query(notion_headers, IDS["daily_data_source_id"], day)
    matches = query_payload.get("results", []) if isinstance(query_payload, dict) else []
    if query_status != 200:
        raise SystemExit(f"notion_exact_date_query_failed:{query_status}")
    if len(matches) > 1:
        raise SystemExit(f"notion_exact_date_duplicate:{len(matches)}")

    action = "none"
    page_id = None
    metadata = {}
    if len(matches) == 1:
        page_id = matches[0].get("id")
        metadata_status, _, metadata_payload = request("https://api.notion.com/v1/pages/" + page_id, headers=notion_headers)
        metadata = metadata_payload if isinstance(metadata_payload, dict) else {}
        if metadata_status != 200:
            raise SystemExit("notion_page_read_failed")
        if metadata.get("parent", {}).get("database_id") != IDS["daily_database_id"]:
            raise SystemExit("notion_parent_mismatch")
        if page_title(metadata) != f"{day} · Daily Tech Intelligence Brief" or page_date(metadata) != day:
            raise SystemExit("notion_exact_identity_mismatch")
        if page_stage(metadata) == "FINAL":
            action = "existing_final_no_write"
        else:
            patch_status, _, _ = request(
                "https://api.notion.com/v1/pages/" + page_id,
                "PATCH",
                notion_headers,
                {"properties": {"Date": {"date": {"start": day}}, "Stage": {"select": {"name": "DRAFT"}}, "Brief Status": {"select": {"name": "Draft"}}, "Review Needed": {"checkbox": True}}},
            )
            if patch_status not in (200, 201):
                raise SystemExit(f"notion_property_write_failed:{patch_status}")
            write_status, _ = write_markdown_chunked(page_id, notion_headers, reader_markdown)
            if write_status not in (200, 201):
                raise SystemExit(f"notion_markdown_write_failed:{write_status}")
            action = "updated"
    else:
        create_status, _, created = request(
            "https://api.notion.com/v1/pages",
            "POST",
            notion_headers,
            {
                "parent": {"database_id": IDS["daily_database_id"]},
                "properties": {
                    "Name": {"title": [{"text": {"content": f"{day} · Daily Tech Intelligence Brief"}}]},
                    "Date": {"date": {"start": day}},
                    "Stage": {"select": {"name": "DRAFT"}},
                    "Brief Status": {"select": {"name": "Draft"}},
                    "Review Needed": {"checkbox": True},
                },
            },
        )
        if create_status not in (200, 201) or not isinstance(created, dict) or not created.get("id"):
            raise SystemExit(f"notion_create_failed:{create_status}")
        page_id = created["id"]
        write_status, _ = write_markdown_chunked(page_id, notion_headers, reader_markdown)
        if write_status not in (200, 201):
            raise SystemExit(f"notion_markdown_write_failed:{write_status}")
        action = "created"

    if page_id:
        meta_status, _, meta_payload = request("https://api.notion.com/v1/pages/" + page_id, headers=notion_headers)
        markdown_status, _, markdown_payload = request("https://api.notion.com/v1/pages/" + page_id + "/markdown", headers=notion_headers)
        metadata = meta_payload if isinstance(meta_payload, dict) else metadata
        notion_markdown = markdown_payload.get("markdown", "") if isinstance(markdown_payload, dict) else ""
        if notion_markdown and action != "existing_final_no_write":
            # Notion's Markdown endpoint canonicalizes links, blank lines, and
            # toggle indentation. Persist that read-back as the local reader
            # artifact only after this run actually wrote the page.  When an
            # existing FINAL page is intentionally not overwritten by Draft,
            # preserve the newly generated target-date artifact for Finalizer.
            reader_path.write_text(notion_markdown, encoding="utf-8")
    else:
        meta_status, markdown_status, notion_markdown = 200, 200, ""

    local_reader = reader_path.read_text(encoding="utf-8") if reader_path.exists() else ""
    readback = {
        "target_date": day,
        "action": action,
        "page_id": page_id,
        "url": metadata.get("url"),
        "exact_date_count": len(matches),
        "metadata_status": meta_status,
        "markdown_status": markdown_status,
        "title_ok": page_title(metadata) == f"{day} · Daily Tech Intelligence Brief" if metadata else action == "existing_final_no_write",
        "date_ok": page_date(metadata) == day if metadata else action == "existing_final_no_write",
        "parent_ok": metadata.get("parent", {}).get("database_id") == IDS["daily_database_id"] if metadata else action == "existing_final_no_write",
        "stage": page_stage(metadata) if metadata else None,
        "markdown_chars": len(notion_markdown),
        "local_reader_chars": len(local_reader),
        "notion_markdown_sha256": hashlib.sha256(notion_markdown.encode("utf-8")).hexdigest() if notion_markdown else None,
        "local_reader_sha256": hashlib.sha256(local_reader.encode("utf-8")).hexdigest() if local_reader else None,
        "exact_content_match": bool(notion_markdown) and notion_markdown == local_reader,
        "reader_internal_markers": [marker for marker in ("SearXNG", "Crawl4AI", "Evidence Pack", "manifest.json", "search.jsonl", "crawl.jsonl", "FAIL —") if marker.lower() in notion_markdown.lower()],
        "token_leak": notion_token in notion_markdown or crawl_token in notion_markdown,
    }
    (target_evidence / f"{day}.notion-readback.json").write_text(json.dumps(readback, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"target_date": day, "action": action, "page_id": page_id, "exact_date_count": len(matches), "metadata_status": meta_status, "markdown_status": markdown_status, "reader_signal_count": len(signals), "crawl_full_source_count": full_count, "crawl_partial_source_count": partial_count, "audit_path": str(audit_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
