#!/usr/bin/env python3
"""Parsers and scoring helpers for broad GitHub/Hugging Face discovery.

This module deliberately separates discovery from editorial truth. Trending and
repository activity are early signals; the Daily writer must still open the
repository, release, paper, or discussion before making detailed claims.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
from typing import Any


CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ai", ("artificial intelligence", "machine learning", "deep learning", "llm", "foundation model")),
    ("agents", ("agent", "mcp", "model context protocol", "orchestration", "tool calling", "computer use")),
    ("developer_tools", ("developer tool", "developer tooling", "ide", "compiler", "debugger", "programming language")),
    ("security", ("security", "cybersecurity", "vulnerability", "exploit", "malware", "penetration test")),
    ("local_models", ("qwen", "deepseek", "glm", "kimi", "mistral", "llama", "gemma", "open-weight", "open weights")),
    ("infrastructure", ("distributed system", "database", "kubernetes", "container", "cloud", "storage", "networking")),
    ("robotics", ("robot", "robotics", "autonomous vehicle", "drone")),
    ("space", ("spacecraft", "satellite", "astronomy", "rocket", "orbital", "nasa", "space")),
    ("biotech", ("biotech", "bioinformatics", "genomics", "protein", "medical device", "healthcare")),
    ("quantum", ("quantum", "qubit")),
    ("energy", ("energy", "battery", "fusion", "power grid", "renewable")),
    ("hardware", ("hardware", "gpu", "cpu", "embedded", "microcontroller", "fpga")),
    ("materials", ("materials science", "material science", "materials database", "nanomaterial")),
    ("networking", ("network protocol", "networking", "router", "internet protocol")),
    ("scientific_instrumentation", ("scientific instrumentation", "laboratory instrument", "measurement system", "observatory")),
    ("advanced_manufacturing", ("advanced manufacturing", "manufacturing", "3d printing", "robotic fabrication")),
    ("human_computer_interaction", ("human-computer interaction", "human computer interaction", "accessibility", "interface design")),
    ("technical_systems", ("simulation", "scientific computing", "data visualization", "operating system", "runtime")),
)


def _clean_text(value: str, limit: int = 1200) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = " ".join(value.split())
    return value[:limit]


def _number(value: str | None) -> int:
    if not value:
        return 0
    match = re.search(r"[\d,]+", value)
    return int(match.group(0).replace(",", "")) if match else 0


def _repo_path(block: str) -> str:
    match = re.search(r'<h2\b[^>]*>.*?<a\b[^>]*href=["\']/(.+?)["\'][^>]*>', block, re.I | re.S)
    if not match:
        match = re.search(r'href=["\']/([\w. -]+/[\w. -]+)["\']', block, re.I)
    return html.unescape(match.group(1).strip("/")) if match else ""


def _href_metric(block: str, suffix: str) -> int | None:
    pattern = rf'href=["\'][^"\']+{re.escape(suffix)}["\'][^>]*>(.*?)</a>'
    match = re.search(pattern, block, re.I | re.S)
    if not match:
        return None
    value = _number(_clean_text(match.group(1)))
    return value if value else None


def _classify_text(text: str, topics: list[str] | None = None) -> tuple[list[str], int]:
    haystack = " ".join([text, " ".join(topics or [])]).casefold()
    categories: list[str] = []
    for category, needles in CATEGORY_RULES:
        if any(needle.casefold() in haystack for needle in needles):
            categories.append(category)
    # A repository can still be an important technical signal without matching
    # the user's usual topics. Keep a small generic technical category rather
    # than treating user-interest matching as a hard inclusion filter.
    if not categories and re.search(
        r"\b(open source|repository|framework|library|dataset|tool|platform|protocol|engine|system)\b",
        haystack,
        re.I,
    ):
        categories.append("technical_systems")
    return categories, min(12, len(categories) * 2)


def technical_relevance(item: dict[str, Any]) -> tuple[list[str], int]:
    topics = [str(topic) for topic in (item.get("topics") or []) if topic]
    text = " ".join(
        [
            str(item.get("full_name") or item.get("name") or item.get("title") or ""),
            str(item.get("description") or item.get("summary") or ""),
            str(item.get("language") or ""),
        ]
    )
    return _classify_text(text, topics)


def parse_github_trending(payload: bytes | str, discovery_date: str, source: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Parse github.com/trending HTML into dated discovery records."""
    raw = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
    blocks = re.findall(r"<article\b[^>]*class=[\"'][^\"']*Box-row[^\"']*[\"'][^>]*>(.*?)</article>", raw, re.I | re.S)
    source = source or {
        "id": "github-trending-daily",
        "name": "GitHub Trending (daily)",
        "lane": "repositories_releases_and_infrastructure",
        "role": "github_trending",
    }
    rows: list[dict[str, Any]] = []
    for block in blocks:
        repo = _repo_path(block)
        if not repo:
            continue
        description_match = re.search(r"<p\b[^>]*>(.*?)</p>", block, re.I | re.S)
        language_match = re.search(r"itemprop=[\"']programmingLanguage[\"'][^>]*>(.*?)</", block, re.I | re.S)
        stars_today_match = re.search(r"([\d,]+)\s+stars\s+today", _clean_text(block), re.I)
        topics, relevance = _classify_text(
            " ".join([repo, _clean_text(description_match.group(1) if description_match else "")]),
        )
        rows.append(
            {
                "source_id": source["id"],
                "source_name": source["name"],
                "lane": source.get("lane", "repositories_releases_and_infrastructure"),
                "role": source.get("role", "github_trending"),
                "title": repo,
                "url": "https://github.com/" + repo,
                "summary": _clean_text(description_match.group(1) if description_match else ""),
                "categories": ["GitHub Trending", *topics],
                "discovery_categories": topics,
                "technical_relevance_score": relevance,
                "published_utc": None,
                "date_precision": "daily_snapshot",
                "date_raw": discovery_date,
                "discovery_date": discovery_date,
                "discovery_type": "github_trending",
                "language": _clean_text(language_match.group(1), 80) if language_match else "",
                "stars_today": _number(stars_today_match.group(1)) if stars_today_match else 0,
                "stargazers_count": _href_metric(block, "/stargazers"),
                "forks_count": _href_metric(block, "/network/members"),
            }
        )
    return rows


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return value


def parse_github_repo_search(payload: bytes | str, source: dict[str, Any], discovery_date: str) -> list[dict[str, Any]]:
    """Parse GitHub repository search results for created/pushed activity."""
    raw = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
    data = json.loads(raw)
    rows: list[dict[str, Any]] = []
    use_push_timestamp = "active" in str(source.get("id", "")).casefold() or "pushed" in str(source.get("query", "")).casefold()
    for item in data.get("items", []):
        topics = [str(topic) for topic in (item.get("topics") or []) if topic]
        categories, relevance = technical_relevance(item)
        event_timestamp = (
            item.get("pushed_at") if use_push_timestamp else item.get("created_at")
        ) or item.get("created_at") or item.get("pushed_at")
        rows.append(
            {
                "source_id": source["id"],
                "source_name": source["name"],
                "lane": source.get("lane", "repositories_releases_and_infrastructure"),
                "role": source.get("role", "repository_discovery"),
                "title": item.get("full_name", ""),
                "url": item.get("html_url", ""),
                "summary": _clean_text(item.get("description", "")),
                "categories": topics,
                "discovery_categories": categories,
                "technical_relevance_score": relevance,
                "published_utc": _iso_date(event_timestamp),
                "date_precision": "repository_timestamp",
                "date_raw": event_timestamp,
                "discovery_date": discovery_date,
                "discovery_type": "github_repo_search",
                "language": item.get("language") or "",
                "stargazers_count": int(item.get("stargazers_count") or 0),
                "forks_count": int(item.get("forks_count") or 0),
                "open_issues_count": int(item.get("open_issues_count") or 0),
                "created_at": item.get("created_at"),
                "pushed_at": item.get("pushed_at"),
            }
        )
    return rows


def parse_hf_models(payload: bytes | str, source: dict[str, Any], discovery_date: str) -> list[dict[str, Any]]:
    """Parse recent Hugging Face model activity as a model/capability radar."""
    raw = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
    data = json.loads(raw)
    rows: list[dict[str, Any]] = []
    for item in data if isinstance(data, list) else []:
        model_id = item.get("modelId") or item.get("id") or ""
        if not model_id:
            continue
        tags = [str(tag) for tag in (item.get("tags") or []) if tag]
        categories, relevance = _classify_text(" ".join([model_id, " ".join(tags), str(item.get("pipeline_tag") or "")]), tags)
        last_modified = item.get("lastModified") or item.get("createdAt")
        rows.append(
            {
                "source_id": source["id"],
                "source_name": source["name"],
                "lane": source.get("lane", "official_ai_and_model_sources"),
                "role": source.get("role", "model_community_signal"),
                "title": model_id,
                "url": "https://huggingface.co/" + model_id,
                "summary": _clean_text(
                    " ".join(
                        part
                        for part in [
                            str(item.get("pipeline_tag") or ""),
                            str(item.get("library_name") or ""),
                            "tags=" + ", ".join(tags[:20]) if tags else "",
                        ]
                        if part
                    )
                ),
                "categories": ["Hugging Face model activity", *tags[:20]],
                "discovery_categories": categories,
                "technical_relevance_score": relevance,
                "published_utc": _iso_date(last_modified),
                "date_precision": "last_modified",
                "date_raw": last_modified,
                "discovery_date": discovery_date,
                "discovery_type": "huggingface_model_radar",
                "pipeline_tag": item.get("pipeline_tag"),
                "downloads": int(item.get("downloads") or 0),
                "likes": int(item.get("likes") or 0),
            }
        )
    return rows


def github_search_url(source: dict[str, Any], day: str) -> str:
    query = str(source.get("query", "")).format(date=day, next_date=(dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat())
    params = {
        "q": query,
        "sort": source.get("sort", "stars"),
        "order": source.get("order", "desc"),
        "per_page": str(source.get("per_page", 100)),
    }
    from urllib.parse import urlencode

    return source["url"] + "?" + urlencode(params)
