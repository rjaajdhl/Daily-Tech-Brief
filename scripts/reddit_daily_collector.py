#!/usr/bin/env python3
"""Collect a complete PDT calendar day from selected Reddit communities via RSS.

Reddit's Atom RSS supports 100 entries per page but does not emit a next link.
This collector derives the last submission id and supplies it as `after=t3_<id>`
until the requested day is fully covered. It writes the complete result to JSON.
The stdout candidate list is deliberately only a bounded navigation/review queue;
`posts` in the JSON archive is the complete source inventory and is not subject
to the per-subreddit display limit.
"""

from __future__ import annotations
import os

import argparse
import datetime as dt
import html
import json
import re
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

PROFILE_ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
OUTPUT_ROOT = PROFILE_ROOT / "data" / "reddit"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
)
ATOM = {"a": "http://www.w3.org/2005/Atom"}

# Reddit scope follows the user's durable preferences: AI/agents/local models,
# frontier model services, cybersecurity/offensive security, and Musk-related
# technology. Generic developer-tool/open-source and hardware/homelab feeds are
# deliberately not included.
GROUPS: dict[str, list[str]] = {
    "ai_general_local": [
        "LocalLLaMA",
        "MachineLearning",
        "artificial",
        "singularity",
        "LLM",
    ],
    "frontier_models_services": [
        "ChatGPT",
        "OpenAI",
        "ChatGPTPro",
        "ClaudeAI",
        "Anthropic",
        "Claude",
        "GeminiAI",
        "Bard",
        "Perplexity_AI",
        "DeepSeek",
        "Qwen_AI",
        "Grok",
        "xAI",
    ],
    "agents": [
        "AI_Agents",
        "AutoGPT",
        "AgenticAI",
        "aiagents",
    ],
    "security_research": [
        "netsec",
        "cybersecurity",
        "blueteamsec",
        "redteamsec",
        "ReverseEngineering",
        "Malware",
        "threatintel",
    ],
    "offensive_security": [
        "hacking",
        "Pentesting",
        "bugbounty",
        "ExploitDev",
        "AskNetsec",
        "oscp",
    ],
    "entity_linked_technology": [
        "elonmusk",
        "spacex",
        "SpaceXLounge",
        "Starlink",
        "teslamotors",
        "neuralink",
        "BoringCompany",
    ],
}

HIGH_SIGNAL_TERMS = {
    "agent": 4,
    "agentic": 5,
    "mcp": 5,
    "tool calling": 5,
    "prompt injection": 7,
    "jailbreak": 4,
    "local": 2,
    "llama": 3,
    "qwen": 4,
    "deepseek": 4,
    "chatgpt": 2,
    "openai": 3,
    "claude": 3,
    "anthropic": 3,
    "gemini": 3,
    "grok": 3,
    "xai": 3,
    "open-weight": 4,
    "context window": 4,
    "multimodal": 4,
    "reasoning": 3,
    "coding model": 4,
    "benchmark": 4,
    "eval": 3,
    "model card": 5,
    "security": 3,
    "vulnerability": 6,
    "cve": 6,
    "exploit": 6,
    "malware": 6,
    "supply chain": 6,
    "breach": 5,
    "ransomware": 5,
    "red team": 5,
    "pentest": 5,
    "reverse engineering": 5,
    "c2": 5,
    "credential": 5,
    "starlink": 4,
    "spacex": 4,
    "neuralink": 4,
    "tesla": 2,
}

LOW_SIGNAL_TERMS = {
    "meme": -5,
    "shitpost": -6,
    "rate my": -3,
    "homework": -4,
    "beginner question": -2,
    "which laptop": -4,
    "buying gpu": -4,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date",
        help="PDT/PST calendar date (YYYY-MM-DD). Defaults to yesterday in America/Los_Angeles.",
    )
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--per-subreddit", type=int, default=5)
    parser.add_argument("--sleep", type=float, default=0.7)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def target_window(date_text: str | None) -> tuple[dt.date, dt.datetime, dt.datetime]:
    tz = ZoneInfo("America/Los_Angeles")
    if date_text:
        day = dt.date.fromisoformat(date_text)
    else:
        day = dt.datetime.now(tz).date() - dt.timedelta(days=1)
    start_local = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    end_local = start_local + dt.timedelta(days=1)
    return day, start_local.astimezone(dt.timezone.utc), end_local.astimezone(dt.timezone.utc)


def fetch_rss(url: str) -> str:
    command = [
        "curl",
        "-L",
        "--fail",
        "--silent",
        "--show-error",
        "--retry",
        "3",
        "--retry-all-errors",
        "--retry-delay",
        "2",
        "--max-time",
        "40",
        "-A",
        USER_AGENT,
        "-H",
        "Accept: application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
        url,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if completed.returncode != 0:
        message = completed.stderr.strip() or f"curl exit {completed.returncode}"
        raise RuntimeError(message)
    return completed.stdout


def text_of(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def clean_html(value: str, limit: int = 1200) -> str:
    value = html.unescape(value)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</p\s*>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def submission_id(link: str, fallback: str = "") -> str:
    match = re.search(r"/comments/([a-z0-9]+)/", link, flags=re.I)
    if match:
        return match.group(1).lower()
    if fallback.startswith("t3_"):
        return fallback[3:].lower()
    return fallback.rsplit("_", 1)[-1].lower()


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    records: list[dict] = []
    for entry in root.findall("a:entry", ATOM):
        link_node = entry.find("a:link", ATOM)
        link = link_node.attrib.get("href", "") if link_node is not None else ""
        id_text = text_of(entry.find("a:id", ATOM))
        category = entry.find("a:category", ATOM)
        subreddit = category.attrib.get("term", "") if category is not None else ""
        author = text_of(entry.find("a:author/a:name", ATOM))
        published_text = text_of(entry.find("a:published", ATOM)) or text_of(
            entry.find("a:updated", ATOM)
        )
        if not published_text:
            continue
        published = dt.datetime.fromisoformat(published_text.replace("Z", "+00:00"))
        title = html.unescape(text_of(entry.find("a:title", ATOM)))
        content = text_of(entry.find("a:content", ATOM))
        records.append(
            {
                "id": submission_id(link, id_text),
                "subreddit": subreddit,
                "title": title,
                "author": author,
                "published_utc": published.astimezone(dt.timezone.utc).isoformat(),
                "url": link,
                "body_excerpt": clean_html(content),
            }
        )
    return records


def new_feed_url(subreddits: list[str], after: str | None, count: int) -> str:
    joined = "+".join(subreddits)
    params = {"limit": "100"}
    if after:
        params.update({"count": str(count), "after": f"t3_{after}"})
    return f"https://www.reddit.com/r/{joined}/new/.rss?{urllib.parse.urlencode(params)}"


def top_feed_url(subreddits: list[str]) -> str:
    joined = "+".join(subreddits)
    return f"https://www.reddit.com/r/{joined}/top/.rss?t=day&limit=100"


def collect_group(
    name: str,
    subreddits: list[str],
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    max_pages: int,
    sleep_seconds: float,
) -> tuple[list[dict], dict]:
    target: dict[str, dict] = {}
    after: str | None = None
    seen_page_ends: set[str] = set()
    oldest_seen: dt.datetime | None = None
    newest_seen: dt.datetime | None = None
    errors: list[str] = []
    complete = False
    pages_fetched = 0

    for page_index in range(max_pages):
        url = new_feed_url(subreddits, after, page_index * 100)
        try:
            records = parse_feed(fetch_rss(url))
        except Exception as exc:  # diagnostics must survive partial source failure
            errors.append(f"new page {page_index + 1}: {exc}")
            break
        pages_fetched += 1
        if not records:
            complete = True
            break

        page_dates = [dt.datetime.fromisoformat(item["published_utc"]) for item in records]
        page_oldest = min(page_dates)
        page_newest = max(page_dates)
        oldest_seen = page_oldest if oldest_seen is None else min(oldest_seen, page_oldest)
        newest_seen = page_newest if newest_seen is None else max(newest_seen, page_newest)

        for item, published in zip(records, page_dates):
            if start_utc <= published < end_utc:
                target[item["id"]] = item

        if page_oldest <= start_utc:
            complete = True
            break

        # Multireddit Atom entries are not guaranteed to be emitted in strict
        # timestamp order. Page from the chronologically oldest record rather
        # than assuming the final XML entry is the oldest.
        last_id = records[page_dates.index(page_oldest)]["id"]
        if not last_id or last_id in seen_page_ends:
            errors.append("pagination stalled: repeated or missing last submission id")
            break
        seen_page_ends.add(last_id)
        after = last_id
        time.sleep(sleep_seconds)

    top_rank: dict[str, int] = {}
    try:
        top_records = parse_feed(fetch_rss(top_feed_url(subreddits)))
        for rank, item in enumerate(top_records, start=1):
            top_rank[item["id"]] = rank
    except Exception as exc:
        errors.append(f"top/day: {exc}")

    records = sorted(target.values(), key=lambda item: item["published_utc"], reverse=True)
    for item in records:
        item["top_day_rank"] = top_rank.get(item["id"])

    coverage = {
        "group": name,
        "subreddits_requested": subreddits,
        "pages_fetched": pages_fetched,
        "target_post_count": len(records),
        "oldest_seen_utc": oldest_seen.isoformat() if oldest_seen else None,
        "newest_seen_utc": newest_seen.isoformat() if newest_seen else None,
        "complete_to_window_start": complete,
        "errors": errors,
    }
    return records, coverage


def candidate_score(item: dict) -> float:
    haystack = f"{item['title']} {item['body_excerpt']}".lower()
    score = 0.0
    for term, weight in HIGH_SIGNAL_TERMS.items():
        if term in haystack:
            score += weight
    for term, weight in LOW_SIGNAL_TERMS.items():
        if term in haystack:
            score += weight
    rank = item.get("top_day_rank")
    if rank:
        score += max(0.0, 12.0 - (rank - 1) * 0.12)
    if item.get("body_excerpt"):
        score += min(2.0, len(item["body_excerpt"]) / 600.0)
    return round(score, 2)


def select_candidates(records: list[dict], per_subreddit: int) -> list[dict]:
    by_subreddit: dict[str, list[dict]] = defaultdict(list)
    for item in records:
        enriched = dict(item)
        enriched["candidate_score"] = candidate_score(enriched)
        by_subreddit[enriched["subreddit"]].append(enriched)

    candidates: list[dict] = []
    for subreddit, items in sorted(by_subreddit.items(), key=lambda pair: pair[0].lower()):
        items.sort(
            key=lambda item: (
                item["candidate_score"],
                -(item["top_day_rank"] or 10_000),
                item["published_utc"],
            ),
            reverse=True,
        )
        candidates.extend(items[:per_subreddit])
    candidates.sort(
        key=lambda item: (item["candidate_score"], item["published_utc"]),
        reverse=True,
    )
    return candidates


def print_digest(payload: dict, output_path: Path) -> None:
    print("REDDIT DAILY COLLECTION — SOURCE DATA, NOT VERIFIED FACT")
    print(f"PDT date: {payload['date_pdt']}")
    print(f"UTC window: {payload['window_utc']['start']} to {payload['window_utc']['end_exclusive']}")
    print(f"Full JSON: {output_path}")
    print(f"Unique posts in window: {payload['post_count']}")
    print()
    print("COVERAGE")
    for coverage in payload["coverage"]:
        status = "complete" if coverage["complete_to_window_start"] else "PARTIAL"
        print(
            f"- {coverage['group']}: {status}; pages={coverage['pages_fetched']}; "
            f"posts={coverage['target_post_count']}; communities="
            f"{', '.join('r/' + name for name in coverage['subreddits_requested'])}"
        )
        if coverage["errors"]:
            print(f"  collection errors: {' | '.join(coverage['errors'])}")

    print()
    print("POST COUNTS BY SUBREDDIT")
    for subreddit, count in payload["counts_by_subreddit"].items():
        print(f"- r/{subreddit}: {count}")

    print()
    print("CANDIDATES FOR HUMAN/AGENT REVIEW — BOUNDED NAVIGATION QUEUE")
    print(
        "This list is only a bounded review queue based on interest keywords and top/day order. "
        "It is not the final issue inventory and it imposes no topic-section inclusion cap. "
        "The complete `posts` array in the JSON archive must be scanned for materially useful distinct issues; "
        "open representative threads and cross-check primary sources before writing the brief."
    )
    for item in payload["candidates"]:
        rank = f"; top/day rank={item['top_day_rank']}" if item.get("top_day_rank") else ""
        print(
            f"- [r/{item['subreddit']}] {item['title']} "
            f"(score={item['candidate_score']}{rank}; {item['published_utc']})\n"
            f"  {item['url']}"
        )
        if item.get("body_excerpt"):
            print(f"  excerpt: {item['body_excerpt'][:500]}")


def main() -> int:
    args = parse_args()
    day, start_utc, end_utc = target_window(args.date)
    all_records: dict[str, dict] = {}
    coverage: list[dict] = []

    for index, (name, subreddits) in enumerate(GROUPS.items()):
        records, group_coverage = collect_group(
            name,
            subreddits,
            start_utc,
            end_utc,
            args.max_pages,
            args.sleep,
        )
        coverage.append(group_coverage)
        for item in records:
            all_records[item["id"]] = item
        if index + 1 < len(GROUPS):
            time.sleep(args.sleep)

    records = sorted(all_records.values(), key=lambda item: item["published_utc"], reverse=True)
    counts: dict[str, int] = defaultdict(int)
    for item in records:
        counts[item["subreddit"]] += 1

    candidates = select_candidates(records, args.per_subreddit)
    payload = {
        "date_pdt": day.isoformat(),
        "window_utc": {
            "start": start_utc.isoformat(),
            "end_exclusive": end_utc.isoformat(),
        },
        "scope_note": (
            "AI/agents/local models, frontier model services, cybersecurity/offensive "
            "security, and entity-linked technology classified by its technical topic. "
            "Generic developer tools/open source and hardware/homelab communities are intentionally excluded."
        ),
        "coverage": coverage,
        "post_count": len(records),
        "counts_by_subreddit": dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].lower()))),
        "candidate_count": len(candidates),
        "candidate_queue_is_not_inventory": True,
        "inventory_field": "posts",
        "inventory_note": (
            "The candidates list is a bounded navigation queue only. The complete posts array is the source inventory; "
            "all materially useful distinct issues must be dispositioned and may enter the Daily Brief."
        ),
        "candidates": candidates,
        "posts": records,
    }

    output_path = args.output or (OUTPUT_ROOT / f"{day.isoformat()}.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print_digest(payload, output_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"COLLECTOR ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
