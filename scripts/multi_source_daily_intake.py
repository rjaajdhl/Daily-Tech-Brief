#!/usr/bin/env python3
"""Collect a date-bounded, multi-source technology-intelligence intake.

This is a discovery and coverage layer, not a fact-writing layer. It preserves raw
feed/API responses, normalizes dated metadata, clusters obvious duplicates, and
prints a compact navigation ledger for the daily cron agent. Selected pages must
still be opened and verified before their claims enter the brief.
"""

from __future__ import annotations
import os

import argparse
import concurrent.futures
import datetime as dt
import email.utils
import gzip
import hashlib
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from github_daily_discovery import (
    github_search_url,
    parse_github_repo_search,
    parse_github_trending,
    parse_hf_models,
)

PROFILE_ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
REGISTRY_PATH = PROFILE_ROOT / "config" / "source_registry.json"
DATA_ROOT = PROFILE_ROOT / "data" / "source_intake"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
UTC = dt.timezone.utc
USER_AGENT = "HermesTechIntelligence/1.0 (+personal research; daily bounded fetch)"
MAX_BYTES = 20_000_000
MAX_WORKERS = 10

TOPIC_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "AI industry, deployment, safety and policy",
        re.compile(
            r"\bartificial intelligence\b|\bAI\b|machine learning|AI safety|AI policy|"
            r"AI regulation|AI deployment|AI adoption|AI labor|AI workforce|AI copyright",
            re.I,
        ),
    ),
    (
        "AI agents, harnesses, MCP and tool use",
        re.compile(
            r"\bagent(?:ic|s)?\b|multi[- ]agent|\bmcp\b|model context protocol|"
            r"tool[- ]?(?:use|call|calling)|harness|orchestrat|computer[- ]use|"
            r"browser agent|coding agent|skill(?:s)?\b|plugin|memory system",
            re.I,
        ),
    ),
    (
        "Frontier models and major services",
        re.compile(
            r"\bopenai\b|\bchatgpt\b|\bgpt[- ]?[0-9]|\banthropic\b|\bclaude\b|"
            r"\bgemini\b|deepmind|\bgrok\b|\bxai\b|\bdeepseek\b|\bqwen\b|"
            r"foundation model|frontier model",
            re.I,
        ),
    ),
    (
        "Local/open model capabilities and evaluation",
        re.compile(
            r"local (?:llm|model)|open[- ]weights?|open model|\bqwen\b|\bdeepseek\b|"
            r"\bglm\b|\bz\.ai\b|\bkimi\b|\bmoonshot\b|\bmistral\b|\bllama\b|"
            r"\bgemma\b|model card|model release|benchmark|independent evaluation|"
            r"context window|multimodal|reasoning|coding performance|licens(?:e|ing)|"
            r"parameter(?:s)?|mixture of experts|\bmoe\b",
            re.I,
        ),
    ),
    (
        "AI security and model/agent abuse",
        re.compile(
            r"prompt injection|indirect injection|jailbreak|agent security|model security|"
            r"data exfiltrat|model theft|training data poison|adversarial (?:ai|ml)|"
            r"llm security|ai[- ]assisted (?:attack|exploit)|model supply chain",
            re.I,
        ),
    ),
    (
        "Cybersecurity, offensive security and threat intelligence",
        re.compile(
            r"\bcve[- ]?\d|vulnerab|zero[- ]day|0[- ]day|exploit|ransomware|malware|"
            r"threat actor|intrusion|campaign|command[- ]and[- ]control|\bc2\b|credential|"
            r"phishing|supply chain|backdoor|webshell|red team|pentest|detection rule|"
            r"incident response|known exploited",
            re.I,
        ),
    ),
    (
        "Major cloud/infrastructure events (discovery only)",
        re.compile(
            r"outage|service disruption|downtime|availability incident|data[- ]?center|"
            r"power (?:shortage|failure)|interconnect|hyperscaler (?:strategy|capacity)|"
            r"capacity expansion|cloud (?:outage|incident|strategy|capacity|infrastructure)|"
            r"large[- ]scale (?:cloud|datacenter) investment|network (?:outage|disruption)",
            re.I,
        ),
    ),
    (
        "Semiconductors and AI compute",
        re.compile(
            r"semiconductor|foundry|\btsmc\b|\bnvidia\b|\bamd\b|\bintel\b|\bgpu\b|"
            r"accelerator|\basic\b|hbm|chiplet|wafer|inference chip|training chip|cuda",
            re.I,
        ),
    ),
    (
        "Outside my usual bubble",
        re.compile(
            r"robotics?|autonomous vehicle|drone|spacecraft|satellite|astronomy|rocket|orbital|"
            r"\bnasa\b|biotech|bioinformatics|genomics|protein|quantum|qubit|energy|battery|"
            r"fusion|power grid|materials? science|nanomaterial|network protocol|scientific instrumentation|"
            r"observatory|advanced manufacturing|3d printing|human[- ]computer interaction|accessibility",
            re.I,
        ),
    ),
    (
        "Cross-cutting entity signals (classify by technical topic)",
        re.compile(
            r"\bxai\b|\bgrok\b|spacex|starlink|starship|neuralink|tesla (?:ai|fsd|"
            r"autonomy|robot|optimus|compute)|boring company|\belon musk\b",
            re.I,
        ),
    ),
    (
        "Research, evaluation and benchmarks",
        re.compile(
            r"benchmark|evaluation|dataset|arxiv|paper|method|reasoning|interpretability|"
            r"alignment|robustness|scaling law|long context|retrieval|\brag\b|training|"
            r"\bmath(?:s|ematic\w*)?\b|theorem|\bproofs?\b|\blean ?4?\b|erd[oö]s|conjecture|formal verification",
            re.I,
        ),
    ),
]

MATERIAL_RE = re.compile(
    r"critical|actively exploited|known exploited|zero[- ]day|remote code execution|\brce\b|"
    r"supply chain|data breach|campaign|new attack|bypass|patch|advisory|release|open[- ]weight|"
    r"benchmark|technical report|model card|source code|repository|architecture|incident",
    re.I,
)
NOISE_RE = re.compile(
    r"funding round|raises? \$|valuation|celebrity|rumou?r|giveaway|sponsored|shopping|deal of the day",
    re.I,
)
ROUTINE_INFERENCE_RE = re.compile(
    r"inference engine|inference server|serving runtime|serving infrastructure|"
    r"gpu serving|token(?:-per-second|/s)|throughput optimization|model serving framework|"
    r"\b(?:vllm|sglang|llama\.cpp)\b.*(?:serving|inference|throughput|optimization)",
    re.I,
)
MODEL_EVENT_RE = re.compile(
    r"\b(?:qwen|deepseek|glm|z\.ai|kimi|moonshot|mistral|llama|gemma)\b"
    r".*(?:model card|open[- ]weight|release|benchmark|evaluation|capabilit|context|modalit|license)",
    re.I,
)
MATERIAL_SYSTEM_EVENT_RE = re.compile(
    r"multi[- ]region outage|major cloud outage|power (?:shortage|failure)|"
    r"hyperscaler strategy|capacity expansion|industry[- ]wide|new semiconductor|"
    r"chip architecture|supply disruption|unprecedented|critical incident",
    re.I,
)
SECURITY_ITEM_RE = re.compile(
    r"cybersecurity|cyber security|ai security|offensive security|prompt injection|"
    r"exploit|vulnerability|malware|ransomware|intrusion|attack campaign|\bcve[- ]?\d|"
    r"\bkev\b|sandbox escape|credential theft|supply chain attack|security research",
    re.I,
)
ROLE_WEIGHT = {
    "primary_advisory": 12,
    "primary_security_research": 11,
    "primary": 10,
    "primary_project_source": 10,
    "primary_repository_platform": 10,
    "primary_release_notes": 10,
    "primary_vendor": 10,
    "primary_watchlist": 10,
    "vendor_research": 9,
    "independent_security_research": 9,
    "research": 8,
    "research_curation": 7,
    "vendor_engineering": 7,
    "primary_ecosystem_source": 7,
    "specialist_reporting": 6,
    "independent_reporting": 5,
    "mixed_primary_and_independent": 5,
    "github_trending": 5,
    "repository_discovery": 4,
    "model_community_signal": 4,
    "broad_discovery": 4,
    "community_signal": 2,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="PDT/PST calendar date (YYYY-MM-DD)")
    parser.add_argument("--registry", default=str(REGISTRY_PATH))
    return parser.parse_args()


def target_day(date_text: str | None) -> dt.date:
    if date_text:
        return dt.date.fromisoformat(date_text)
    return dt.datetime.now(LOCAL_TZ).date() - dt.timedelta(days=1)


def local_window(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=LOCAL_TZ)
    end = start + dt.timedelta(days=1)
    return start, end


def local_name(tag: str) -> str:
    return tag.split("}")[-1]


def clean_text(value: str | None, limit: int = 700) -> str:
    if not value:
        return ""
    text = html.unescape(value)
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    return text[:limit]


def parse_timestamp(value: str | None) -> tuple[dt.datetime | None, str]:
    if not value:
        return None, "missing"
    value = value.strip()
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC), "timestamp"
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        iso_value = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(iso_value)
        precision = "date" if len(value) == 10 else "timestamp"
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC), precision
    except ValueError:
        return None, "unparsed"


def node_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for child in list(node):
        if local_name(child.tag) in names:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def node_link(node: ET.Element) -> str:
    candidates: list[str] = []
    for child in list(node):
        if local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href", "").strip()
        rel = child.attrib.get("rel", "alternate")
        if href and rel in ("alternate", ""):
            return href
        text = "".join(child.itertext()).strip()
        if text:
            candidates.append(text)
        elif href:
            candidates.append(href)
    if candidates:
        return candidates[0]
    return node_text(node, ("guid", "id"))


def parse_feed(payload: bytes, source: dict[str, Any]) -> list[dict[str, Any]]:
    root = ET.fromstring(payload)
    nodes = root.findall(".//item")
    if not nodes:
        nodes = [node for node in root.iter() if local_name(node.tag) == "entry"]
    items: list[dict[str, Any]] = []
    for node in nodes:
        title = clean_text(node_text(node, ("title",)), 400)
        url = node_link(node)
        date_text = node_text(node, ("pubDate", "published", "updated", "date"))
        published, precision = parse_timestamp(date_text)
        summary = clean_text(node_text(node, ("description", "summary", "abstract", "encoded")))
        categories: list[str] = []
        for child in list(node):
            if local_name(child.tag) == "category":
                category = clean_text("".join(child.itertext()) or child.attrib.get("term", ""), 100)
                if category:
                    categories.append(category)
        if not title and not url:
            continue
        items.append(
            {
                "source_id": source["id"],
                "source_name": source["name"],
                "lane": source["lane"],
                "role": source["role"],
                "title": title,
                "url": url,
                "summary": summary,
                "categories": categories,
                "published_utc": published.isoformat() if published else None,
                "date_precision": precision,
                "date_raw": date_text,
            }
        )
    return items


def parse_cisa_kev(payload: bytes, source: dict[str, Any]) -> list[dict[str, Any]]:
    data = json.loads(payload)
    items: list[dict[str, Any]] = []
    for entry in data.get("vulnerabilities", []):
        date_text = entry.get("dateAdded", "")
        published, precision = parse_timestamp(date_text)
        cve = entry.get("cveID", "")
        notes = entry.get("notes", "")
        url_match = re.search(r"https?://[^ ;]+", notes)
        url = url_match.group(0) if url_match else f"https://www.cisa.gov/known-exploited-vulnerabilities-catalog?search_api_fulltext={urllib.parse.quote(cve)}"
        items.append(
            {
                "source_id": source["id"],
                "source_name": source["name"],
                "lane": source["lane"],
                "role": source["role"],
                "title": f"{cve}: {entry.get('vulnerabilityName', '')}",
                "url": url,
                "summary": clean_text(
                    f"{entry.get('shortDescription', '')} Required action: {entry.get('requiredAction', '')} "
                    f"Known ransomware use: {entry.get('knownRansomwareCampaignUse', '')}"
                ),
                "categories": [entry.get("vendorProject", ""), entry.get("product", ""), "CISA KEV"],
                "published_utc": published.isoformat() if published else None,
                "date_precision": precision,
                "date_raw": date_text,
                "cve": cve,
                "due_date": entry.get("dueDate"),
            }
        )
    return items


def parse_hf_daily(payload: bytes, source: dict[str, Any]) -> list[dict[str, Any]]:
    data = json.loads(payload)
    items: list[dict[str, Any]] = []
    for entry in data:
        paper = entry.get("paper", {})
        paper_id = paper.get("id") or entry.get("id", "")
        date_text = entry.get("publishedAt") or paper.get("submittedOnDailyAt") or paper.get("publishedAt")
        published, precision = parse_timestamp(date_text)
        items.append(
            {
                "source_id": source["id"],
                "source_name": source["name"],
                "lane": source["lane"],
                "role": source["role"],
                "title": clean_text(entry.get("title") or paper.get("title"), 400),
                "url": f"https://huggingface.co/papers/{paper_id}",
                "summary": clean_text(entry.get("summary") or paper.get("summary")),
                "categories": ["Hugging Face Daily Papers"],
                "published_utc": published.isoformat() if published else None,
                "date_precision": precision,
                "date_raw": date_text,
                "paper_id": paper_id,
                "upvotes": paper.get("upvotes", 0),
                "github_repo": paper.get("githubRepo"),
            }
        )
    return items


def parse_hn(payload: bytes, source: dict[str, Any]) -> list[dict[str, Any]]:
    data = json.loads(payload)
    items: list[dict[str, Any]] = []
    for hit in data.get("hits", []):
        date_text = hit.get("created_at")
        published, precision = parse_timestamp(date_text)
        object_id = str(hit.get("objectID", ""))
        items.append(
            {
                "source_id": source["id"],
                "source_name": source["name"],
                "lane": source["lane"],
                "role": source["role"],
                "title": clean_text(hit.get("title") or hit.get("story_title"), 400),
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}",
                "discussion_url": f"https://news.ycombinator.com/item?id={object_id}",
                "summary": "",
                "categories": [tag for tag in hit.get("_tags", []) if isinstance(tag, str)],
                "published_utc": published.isoformat() if published else None,
                "date_precision": precision,
                "date_raw": date_text,
                "points": hit.get("points") or 0,
                "comments": hit.get("num_comments") or 0,
                "object_id": object_id,
            }
        )
    return items


def fetch_bytes(url: str, timeout: int = 45) -> bytes:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/json, text/xml, */*"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(MAX_BYTES + 1)
            if len(payload) > MAX_BYTES:
                raise ValueError(f"response exceeded {MAX_BYTES} bytes")
            return payload
        except Exception as exc:  # network boundary: retain exact diagnostic in report
            last_error = exc
            if attempt == 0:
                time.sleep(0.7)
    assert last_error is not None
    raise last_error


def hn_url(source: dict[str, Any], start: dt.datetime, end: dt.datetime, page: int) -> str:
    query = {
        "tags": "story",
        "numericFilters": f"created_at_i>={int(start.timestamp())},created_at_i<{int(end.timestamp())}",
        "hitsPerPage": "100",
        "page": str(page),
    }
    return f"{source['url']}?{urllib.parse.urlencode(query)}"


def source_fetch_plan(source: dict[str, Any], day: dt.date, start: dt.datetime, end: dt.datetime) -> list[str]:
    kind = source["kind"]
    if kind == "hf_daily_papers":
        return [f"{source['url']}?{urllib.parse.urlencode({'date': day.isoformat(), 'limit': 100})}"]
    if kind == "hf_models":
        return [
            f"{source['url']}?{urllib.parse.urlencode({'sort': 'lastModified', 'direction': '-1', 'limit': 100, 'full': 'true'})}"
        ]
    if kind == "hn_algolia":
        return [hn_url(source, start, end, 0)]
    if kind == "github_trending":
        return [f"{source['url']}?{urllib.parse.urlencode({'since': 'daily'})}"]
    if kind == "github_repo_search":
        return [github_search_url(source, day.isoformat())]
    return [source["url"]]


def fetch_and_parse(source: dict[str, Any], day: dt.date, start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
    started = time.monotonic()
    raw_payloads: list[bytes] = []
    try:
        urls = source_fetch_plan(source, day, start, end)
        payload = fetch_bytes(urls[0])
        raw_payloads.append(payload)
        kind = source["kind"]
        if kind == "feed":
            items = parse_feed(payload, source)
        elif kind == "cisa_kev":
            items = parse_cisa_kev(payload, source)
        elif kind == "hf_daily_papers":
            items = parse_hf_daily(payload, source)
        elif kind == "hf_models":
            items = parse_hf_models(payload, source, day.isoformat())
        elif kind == "hn_algolia":
            items = parse_hn(payload, source)
            first = json.loads(payload)
            nb_pages = min(int(first.get("nbPages", 1)), 20)
            for page in range(1, nb_pages):
                page_payload = fetch_bytes(hn_url(source, start, end, page))
                raw_payloads.append(page_payload)
                items.extend(parse_hn(page_payload, source))
        elif kind == "openai_misalignment_reports":
            items = parse_openai_misalignment_reports(payload, source)
        elif kind == "github_trending":
            items = parse_github_trending(payload, day.isoformat(), source)
        elif kind == "github_repo_search":
            items = parse_github_repo_search(payload, source, day.isoformat())
        else:
            raise ValueError(f"unsupported source kind: {kind}")
        return {
            "source": source,
            "status": "ok",
            "items": items,
            "payloads": raw_payloads,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {
            "source": source,
            "status": "failed",
            "items": [],
            "payloads": raw_payloads,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }


def canonical_url(value: str) -> str:
    if not value:
        return ""
    try:
        parts = urllib.parse.urlsplit(value)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        query = [(k, v) for k, v in query if not k.lower().startswith("utm_") and k.lower() not in {"ref", "source", "output"}]
        path = re.sub(r"/+", "/", parts.path).rstrip("/") or "/"
        return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urllib.parse.urlencode(query), ""))
    except ValueError:
        return value


def canonical_title(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"[^\w\s]", " ", value)
    return " ".join(value.split())


def parse_openai_misalignment_reports(payload: bytes, source: dict[str, Any]) -> list[dict[str, Any]]:
    """alignment.openai.com/misalignment-reports has no feed (the site RSS carries research posts only), so read its table.

    Each `<tbody class="report-entry" data-title data-incident data-created ...>` is one report; `data-created` is the
    first-posted date. The date has no time, so pin it to local noon to land in the right America/Los_Angeles day.
    """
    page = payload.decode("utf-8", errors="replace")
    items: list[dict[str, Any]] = []
    for block in re.findall(r"<tbody class=\"report-entry\".*?</tbody>", page, re.S):
        attrs = dict(re.findall(r"data-([a-z]+)=\"([^\"]*)\"", block.split(">", 1)[0]))
        href = re.search(r"class=\"report-title\" href=\"([^\"]+)\"", block)
        title = html.unescape(attrs.get("title", "")).strip()
        created = attrs.get("created", "")
        if not (title and href and re.fullmatch(r"\d{4}-\d{2}-\d{2}", created)):
            continue
        desc = re.search(r"<p>(.*?)</p>", block.split("entry-detail", 1)[-1], re.S)
        summary = clean_text(re.sub(r"<[^>]+>", " ", desc.group(1)) if desc else "")
        published = dt.datetime.combine(dt.date.fromisoformat(created), dt.time(12), LOCAL_TZ).astimezone(UTC)
        items.append(
            {
                "source_id": source["id"],
                "source_name": source["name"],
                "lane": source["lane"],
                "role": source["role"],
                "title": clean_text(title, 400),
                "url": urllib.parse.urljoin(source["url"], href.group(1)),
                "summary": f"{summary} (incident date {attrs.get('incident', 'unknown')})".strip(),
                "categories": [html.unescape(t) for t in re.findall(r"&quot;(.*?)&quot;", attrs.get("topics", ""))],
                "published_utc": published.isoformat(),
                "date_precision": "date",
                "date_raw": created,
            }
        )
    return items


def topics_for(item: dict[str, Any]) -> list[str]:
    haystack = " ".join(
        [item.get("title", ""), item.get("summary", ""), " ".join(item.get("categories", []))]
    )
    return [name for name, pattern in TOPIC_RULES if pattern.search(haystack)]


def item_score(item: dict[str, Any]) -> int:
    text = f"{item.get('title', '')} {item.get('summary', '')}"
    score = ROLE_WEIGHT.get(item.get("role", ""), 3)
    score += min(8, 2 * len(item.get("topics", [])))
    if MATERIAL_RE.search(text):
        score += 4
    if NOISE_RE.search(text):
        score -= 5
    if item.get("source_id") == "hacker-news":
        score += min(4, int(item.get("points", 0)) // 30)
        score += min(2, int(item.get("comments", 0)) // 25)
    if item.get("source_id") == "huggingface-daily-papers":
        score += min(5, int(item.get("upvotes", 0)) // 10)
    if item.get("discovery_type") == "github_trending":
        score += min(8, int(item.get("stars_today", 0)) // 100)
        score += min(4, int(item.get("stargazers_count", 0)) // 1000)
    if item.get("discovery_type") == "github_repo_search":
        score += min(6, int(item.get("stargazers_count", 0)) // 500)
        score += min(3, int(item.get("forks_count", 0)) // 100)
    if item.get("discovery_type") == "huggingface_model_radar":
        score += min(4, int(item.get("downloads", 0)) // 1000)
        score += min(3, int(item.get("likes", 0)) // 100)
    return score


def is_routine_inference_candidate(item: dict[str, Any]) -> bool:
    """Return True for ordinary serving/inference chatter, not model events."""
    text = f"{item.get('title', '')} {item.get('summary', '')}"
    if not ROUTINE_INFERENCE_RE.search(text):
        return False
    if MODEL_EVENT_RE.search(text) or MATERIAL_SYSTEM_EVENT_RE.search(text):
        return False
    return True


def should_attach_deep_analysis(item: dict[str, Any]) -> bool:
    """Select rare, non-security technical explainers for the morning brief."""
    text = f"{item.get('title', '')} {item.get('summary', '')} {item.get('category', '')}"
    if SECURITY_ITEM_RE.search(text) or is_routine_inference_candidate(item):
        return False
    novel = bool(
        item.get('technically_novel')
        or item.get('new_architecture')
        or item.get('unusual_mechanism')
        or re.search(r"new architecture|novel mechanism|unusual system|interconnect", text, re.I)
    )
    understanding_gap = bool(
        item.get('understanding_gap')
        or item.get('mechanism_unclear')
        or item.get('needs_explanation')
    )
    return novel and understanding_gap


def needs_balance_review(section_counts: dict[str, int], available_nonsecurity_candidates: int) -> bool:
    """Advisory gate: flag security-heavy drafts when material alternatives exist."""
    total = sum(max(0, int(value)) for value in section_counts.values())
    security = max(0, int(section_counts.get('security', 0)))
    if total == 0 or available_nonsecurity_candidates <= 0:
        return False
    return security / total >= 0.60


def editorially_eligible(item: dict[str, Any]) -> bool:
    """Separate preserved source volume from items eligible for editorial review."""
    if is_routine_inference_candidate(item):
        return False
    if item.get("discovery_type") in {"github_trending", "github_repo_search"}:
        # GitHub discovery must not be a hidden AI-only filter. Keep a
        # technically relevant repository when its description/topics identify
        # any technical or outside-bubble domain, or when the activity itself
        # is unusually strong enough for human review.
        return bool(
            item.get("discovery_categories")
            or int(item.get("technical_relevance_score", 0)) > 0
            or int(item.get("stars_today", 0)) >= 20
            or int(item.get("stargazers_count", 0)) >= 100
        )
    lane = item.get("lane", "")
    text = f"{item.get('title', '')} {item.get('summary', '')}"
    topics = item.get("topics") or topics_for(item)
    if lane == "community_signals":
        # Recall-first: volume is metadata, not a truth or inclusion threshold.
        return bool(topics)
    if lane in {
        "independent_technology_reporting",
        "papers_and_research",
        "repositories_releases_and_infrastructure",
    }:
        return bool(topics)
    if lane == "official_ai_and_model_sources":
        return bool(topics) or bool(MATERIAL_RE.search(text))
    # Security-specialist, advisory, semiconductor and watchlist sources are
    # already narrowly scoped. Preserve their dated items as candidates, then
    # let the source disposition ledger reject routine/noise items.
    return True


def in_window(item: dict[str, Any], start: dt.datetime, end: dt.datetime, day: dt.date) -> bool:
    if item.get("discovery_type") == "github_trending":
        return item.get("discovery_date") == day.isoformat()
    raw = item.get("published_utc")
    if not raw:
        return False
    timestamp = dt.datetime.fromisoformat(raw)
    if item.get("source_id") == "cisa-kev":
        return item.get("date_raw") == day.isoformat()
    return start.astimezone(UTC) <= timestamp < end.astimezone(UTC)


def write_raw_payloads(day: dt.date, result: dict[str, Any]) -> list[str]:
    source_id = result["source"]["id"]
    raw_dir = DATA_ROOT / "raw" / day.isoformat()
    raw_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for index, payload in enumerate(result.get("payloads", [])):
        suffix = f"-{index + 1}" if len(result.get("payloads", [])) > 1 else ""
        path = raw_dir / f"{source_id}{suffix}.bin.gz"
        with gzip.open(path, "wb", compresslevel=6) as handle:
            handle.write(payload)
        paths.append(str(path))
    return paths


def cluster_items(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        url_key = canonical_url(item.get("url", ""))
        title_key = canonical_title(item.get("title", ""))
        key_material = url_key if url_key else title_key
        if not key_material:
            key_material = json.dumps(item, sort_keys=True, ensure_ascii=False)
        key = hashlib.sha1(key_material.encode("utf-8")).hexdigest()[:16]
        item["canonical_url"] = url_key
        item["cluster_id"] = key
        clusters[key].append(item)
    return clusters


def build_discovery_contract(source_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize discovery passes without conflating fetches and web follow-up.

    ``search_required`` is intentionally not treated as an accessible source.
    The Daily agent must execute those queries and record the result separately.
    """

    by_id = {row.get("id"): row for row in source_rows if row.get("id")}

    def aggregate(source_ids: list[str]) -> dict[str, Any]:
        rows = [by_id.get(source_id) for source_id in source_ids]
        statuses = [row.get("status", "unavailable") if row else "unavailable" for row in rows]
        if statuses and all(status == "ok" for status in statuses):
            status = "accessible"
            accessible = True
        elif any(status == "ok" for status in statuses):
            status = "partial"
            accessible = False
        elif any(status == "failed" for status in statuses):
            status = "unavailable"
            accessible = False
        else:
            status = "manual_followup_required"
            accessible = False
        return {
            "source_ids": source_ids,
            "status": status,
            "accessible": accessible,
            "raw_statuses": statuses,
        }

    community_ids = [
        "hacker-news",
        "lobsters",
        "community-platform-search",
        "github-discussions-search",
        "huggingface-discussions-search",
    ]
    manual_followup_source_ids = sorted(
        row["id"]
        for row in source_rows
        if row.get("status") in {"search_required", "failed"}
    )
    return {
        "github_trending": aggregate(["github-trending-daily"]),
        "github_repository_activity": aggregate(
            ["github-created-repositories", "github-active-repositories"]
        ),
        "model_radar": aggregate(["huggingface-models-radar"]),
        "community_ecosystems": aggregate(community_ids),
        "outside_bubble": aggregate(["outside-bubble-discovery"]),
        "manual_followup_source_ids": manual_followup_source_ids,
    }


def print_coverage(report: dict[str, Any]) -> None:
    print("MULTI-SOURCE EVIDENCE INTAKE")
    print(f"TARGET WINDOW: {report['window_start']} to {report['window_end']} (end exclusive)")
    print(f"ARCHIVE: {report['archive_path']}")
    print(f"REPORT: {report['report_path']}")
    print(
        "Registry coverage: "
        f"configured={report['configured_sources']}; fetched={report['fetched_sources']}; "
        f"search-required={report['search_only_sources']}; failed={report['failed_sources']}; "
        f"preserved-window-items={report['window_items']}; editorial-candidates={report['eligible_items']}; "
        f"dedup-clusters={report['dedup_clusters']}"
    )
    print("\nSOURCE COVERAGE BY EVIDENCE LANE")
    for lane, lane_data in report["lanes"].items():
        print(
            f"\n### {lane}: configured={lane_data['configured']}; fetched={lane_data['fetched']}; "
            f"search-required={lane_data['search_only']}; failed={lane_data['failed']}; "
            f"preserved-window-items={lane_data['window_items']}; "
            f"editorial-candidates={lane_data['eligible_items']}; clusters={lane_data['clusters']}"
        )
        for source in lane_data["sources"]:
            detail = (
                f"raw={source.get('raw_items', 0)}; window={source.get('window_items', 0)}; "
                f"eligible={source.get('eligible_items', 0)}; "
                f"undated={source.get('undated_items', 0)}; checked={source.get('checked', False)}"
            )
            if source["status"] == "failed":
                detail = f"FAILED: {source.get('error', '')}"
            elif source["status"] == "search_required":
                detail = f"WEB SEARCH REQUIRED: {source.get('query', '')}"
            print(f"- {source['name']} [{source['role']}] — {detail}")
    contract = report.get("discovery_contract", {})
    if contract:
        print("\nDISCOVERY PASS STATUS")
        for label in (
            "github_trending",
            "github_repository_activity",
            "model_radar",
            "community_ecosystems",
            "outside_bubble",
        ):
            state = contract[label]
            print(
                f"- {label}: {state['status']}; accessible={state['accessible']}; "
                f"sources={','.join(state['source_ids'])}"
            )
        print(
            "- manual_followup_source_ids: "
            + (", ".join(contract["manual_followup_source_ids"]) or "none")
        )


def print_topic_ledger(items: list[dict[str, Any]], clusters: dict[str, list[dict[str, Any]]]) -> None:
    print("\nTOPIC COVERAGE LEDGER (overlapping discovery index; not story quotas)")
    for topic, _pattern in TOPIC_RULES:
        matched = [item for item in items if topic in item.get("topics", [])]
        cluster_ids = {item["cluster_id"] for item in matched}
        lanes = sorted({item["lane"] for item in matched})
        roles = Counter(item["role"] for item in matched)
        print(
            f"\n### {topic}: items={len(matched)}; clusters={len(cluster_ids)}; "
            f"lanes={len(lanes)}; roles={dict(roles)}"
        )
        representatives: list[dict[str, Any]] = []
        for cluster_id in cluster_ids:
            members = clusters[cluster_id]
            representative = max(members, key=lambda item: (item["score"], item.get("published_utc") or ""))
            representatives.append(representative)
        representatives.sort(key=lambda item: (item["score"], item.get("published_utc") or ""), reverse=True)
        # Every deduplicated cluster remains visible in the topic ledger. This
        # is an inventory, not a representative-story preview.
        for item in representatives:
            print(
                f"- [{item['source_name']} / {item['role']}] {item['title']} "
                f"(score={item['score']}; {item.get('published_utc')})\n  {item.get('url', '')}"
            )


def print_lane_candidates(items: list[dict[str, Any]], clusters: dict[str, list[dict[str, Any]]]) -> None:
    print("\nCANDIDATE INVENTORY BY EVIDENCE LANE")
    print("All deduplicated clusters are printed; this output is not a Top-N preview and imposes no inclusion cap.")
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for cluster_id, members in clusters.items():
        representative = max(members, key=lambda item: (item["score"], item.get("published_utc") or ""))
        key = (representative["lane"], cluster_id)
        if key in seen:
            continue
        seen.add(key)
        by_lane[representative["lane"]].append(representative)
    for lane in sorted(by_lane):
        ranked = sorted(
            by_lane[lane],
            key=lambda item: (item["score"], item.get("published_utc") or ""),
            reverse=True,
        )
        print(f"\n### {lane}: candidates={len(ranked)}")
        for item in ranked:
            metrics = ""
            if item.get("source_id") == "hacker-news":
                metrics = f"; points={item.get('points', 0)}; comments={item.get('comments', 0)}"
            if item.get("source_id") == "huggingface-daily-papers":
                metrics = f"; upvotes={item.get('upvotes', 0)}"
            print(
                f"- [{item['source_name']} / {item['role']}] {item['title']} "
                f"(score={item['score']}{metrics})\n  {item.get('url', '')}"
            )


def print_authoring_contract(report: dict[str, Any]) -> None:
    print("\n다중 출처 작성 계약")
    print(
        "1. 이 수집은 1차 발견 계층입니다. Reddit·Hacker News는 보조 커뮤니티 계층이며 게시물 수나 참여량이 "
        "브리핑 전체 비중을 결정하지 않습니다."
    )
    print(
        "2. 초안 전에 위의 모든 evidence lane을 확인하고 WEB SEARCH REQUIRED 쿼리를 정확한 날짜로 실행합니다. "
        "결과가 0인 lane은 커버리지 상태이지 사건이 없었다는 증명이 아닙니다."
    )
    print(
        "3. claim은 공식·권고·모델 카드·원 저장소, 논문·기술 보고서, 독립 보도, 커뮤니티 경험 순의 근거 역할로 "
        "평가합니다. 출처별 기사 quota는 없습니다."
    )
    print(
        "4. feed/API metadata는 발견 근거일 뿐입니다. mechanism·숫자·날짜·영향을 쓰기 전에 선택한 원문 기사, "
        "논문/모델 카드, 권고 또는 저장소를 열어 내용을 추출합니다. 검색 snippet·feed 요약만으로 상세 주장을 만들지 않습니다."
    )
    print(
        "5. URL이 아니라 underlying event 또는 claim으로 중복 제거합니다. 독립 확인, 구현 세부, 공격자 증거 또는 "
        "별도 커뮤니티 경험을 더하는 출처만 함께 보존합니다."
    )
    print(
        "6. 논문은 preprint/peer-review 상태, 방법, 데이터셋, baseline, 측정 결과와 한계를 기록합니다. 보안은 "
        "권고 사실, vendor telemetry, 독립 보도와 exploitation 주장을 분리합니다."
    )
    print(
        "7. 내부 source disposition ledger를 included·duplicate·irrelevant/noise·weak/unverified·coverage failure로 "
        "작성합니다. 모든 lane을 대표하려고 약한 기사를 추가하지 않습니다."
    )
    print(
        "8. 로컬·오픈 모델 lane은 모델 출시·능력·평가·라이선스·실제 반응을 우선합니다. 일반 inference·serving·"
        "token/s·GPU serving·inference-engine 릴리스는 특별한 산업 사건이 아니면 제외합니다."
    )
    print(
        "9. 클라우드·인프라는 독립 고정 범주가 아닙니다. 대형 장애·AI 데이터센터/전력·hyperscaler 전략처럼 "
        "광범위한 사건만 실제 기술 주제에 배치합니다."
    )
    if report["failed_sources"]:
        print(
            "10. 자동 수집 출처 중 하나 이상이 실패했습니다. 표적 웹 검색·원문 직접 조회로 material coverage를 "
            "복구하고, 실패하면 partial coverage로 남겨 조용히 성공 처리하지 않습니다."
        )
    else:
        print(
            "10. 자동 feed가 성공해도 search-only/watchlist 출처는 직접 웹 검색과 선택 원문 추출 전까지 완료로 보지 않습니다."
        )


def main() -> int:
    args = parse_args()
    day = target_day(args.date)
    start, end = local_window(day)
    registry_path = Path(args.registry).expanduser()
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    sources = registry.get("sources", [])
    fetchable = [source for source in sources if source["kind"] != "search_only"]
    search_only = [source for source in sources if source["kind"] == "search_only"]

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fetch_and_parse, source, day, start, end) for source in fetchable]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    result_by_id = {result["source"]["id"]: result for result in results}

    all_window_items: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    for source in sources:
        if source["kind"] == "search_only":
            query = source.get("query", "").format(date=day.isoformat(), next_date=(day + dt.timedelta(days=1)).isoformat())
            source_rows.append(
                {
                    "id": source["id"],
                    "name": source["name"],
                    "lane": source["lane"],
                    "role": source["role"],
                    "kind": source["kind"],
                    "status": "search_required",
                    "checked": False,
                    "query": query,
                    "url": source["url"],
                    "raw_items": 0,
                    "window_items": 0,
                    "eligible_items": 0,
                    "undated_items": 0,
                }
            )
            continue
        result = result_by_id[source["id"]]
        raw_paths = write_raw_payloads(day, result) if result.get("payloads") else []
        items = result.get("items", [])
        window_items = [item for item in items if in_window(item, start, end, day)]
        for item in window_items:
            item["topics"] = topics_for(item)
            item["score"] = item_score(item)
            item["editorial_eligible"] = editorially_eligible(item)
        all_window_items.extend(window_items)
        source_rows.append(
            {
                "id": source["id"],
                "name": source["name"],
                "lane": source["lane"],
                "role": source["role"],
                "kind": source["kind"],
                "status": result["status"],
                "checked": result["status"] == "ok",
                "error": result.get("error"),
                "raw_items": len(items),
                "window_items": len(window_items),
                "eligible_items": sum(1 for item in window_items if item["editorial_eligible"]),
                "undated_items": sum(
                    1
                    for item in items
                    if not item.get("published_utc") and not item.get("discovery_date")
                ),
                "raw_paths": raw_paths,
                "elapsed_ms": result.get("elapsed_ms"),
            }
        )

    all_clusters = cluster_items(all_window_items)
    for item in all_window_items:
        item["duplicate_sources"] = sorted({member["source_name"] for member in all_clusters[item["cluster_id"]]})
        item["duplicate_count"] = len(all_clusters[item["cluster_id"]])
    eligible_items = [item for item in all_window_items if item["editorial_eligible"]]
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in eligible_items:
        clusters[item["cluster_id"]].append(item)

    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    archive_path = DATA_ROOT / f"{day.isoformat()}.jsonl"
    with archive_path.open("w", encoding="utf-8") as handle:
        for item in sorted(all_window_items, key=lambda entry: (entry.get("published_utc") or "", entry["source_name"])):
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")

    lane_rows: dict[str, dict[str, Any]] = {}
    for lane in sorted({source["lane"] for source in sources}):
        lane_sources = [row for row in source_rows if row["lane"] == lane]
        lane_items = [item for item in all_window_items if item["lane"] == lane]
        lane_eligible = [item for item in eligible_items if item["lane"] == lane]
        lane_rows[lane] = {
            "configured": len(lane_sources),
            "fetched": sum(1 for row in lane_sources if row["status"] == "ok"),
            "search_only": sum(1 for row in lane_sources if row["status"] == "search_required"),
            "failed": sum(1 for row in lane_sources if row["status"] == "failed"),
            "window_items": len(lane_items),
            "eligible_items": len(lane_eligible),
            "clusters": len({item["cluster_id"] for item in lane_eligible}),
            "sources": lane_sources,
        }

    report_path = DATA_ROOT / f"{day.isoformat()}.report.json"
    report = {
        "date": day.isoformat(),
        "timezone": str(LOCAL_TZ),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "registry_path": str(registry_path),
        "configured_sources": len(sources),
        "fetched_sources": sum(1 for row in source_rows if row["status"] == "ok"),
        "search_only_sources": len(search_only),
        "failed_sources": sum(1 for row in source_rows if row["status"] == "failed"),
        "window_items": len(all_window_items),
        "eligible_items": len(eligible_items),
        "dedup_clusters": len(clusters),
        "archive_path": str(archive_path),
        "report_path": str(report_path),
        "lanes": lane_rows,
        "sources": source_rows,
    }
    report["discovery_contract"] = build_discovery_contract(source_rows)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    print_coverage(report)
    print_topic_ledger(eligible_items, clusters)
    print_lane_candidates(eligible_items, clusters)
    print_authoring_contract(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
