#!/usr/bin/env python3
"""Render yesterday's archived Reddit snapshots as bounded cron prompt context."""

from __future__ import annotations
import os

import argparse
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from reddit_daily_collector import GROUPS, candidate_score  # noqa: E402

PROFILE_ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
SNAPSHOT_ROOT = PROFILE_ROOT / "data" / "reddit" / "snapshots"
RUN_LOG = PROFILE_ROOT / "data" / "reddit" / "snapshot-runs.jsonl"
BACKFILL_ROOT = PROFILE_ROOT / "data" / "reddit" / "backfills"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
# Community samples are navigation aids only. Preserve the full archive, but keep
# pre-prompt volume below the primary evidence intake so Reddit cannot dominate.
PER_SUBREDDIT = 0
PER_TOPIC = 2
CROSSPOST_PRINT = 8
SPECIALIST_SUBREDDITS = {
    "blueteamsec",
    "netsec",
    "redteamsec",
    "reverseengineering",
    "malware",
    "threatintel",
}

# This is a deterministic discovery index, not a final editorial taxonomy.
# Rules intentionally overlap: a Claude MCP security post should appear in
# both the Claude and agent-security ledgers rather than disappear into one.
TOPIC_RULES = [
    (
        "Local/open models and capabilities",
        re.compile(
            r"qwen|deepseek|glm|z\.ai|kimi|moonshot|mistral|llama|gemma|"
            r"open[- ]weights?|local (?:llm|model)|model card|benchmark|evaluation|"
            r"context window|multimodal|reasoning|coding model|licens(?:e|ing)",
            re.IGNORECASE,
        ),
    ),
    (
        "DeepSeek Harness / DSH plugins / durable state",
        re.compile(
            r"deepseek harness|\bdsh\b|cordis|plugin bench|session memory|"
            r"persistent (?:agent )?memory|forgetting everything",
            re.IGNORECASE,
        ),
    ),
    (
        "Agent runtime / MCP / skills / orchestration",
        re.compile(
            r"\bagent(?:s|ic)?\b|\bmcp\b|\bskills?\b|orchestrat|tool[- ]?call|"
            r"multi[- ]?agent|harness|workflow",
            re.IGNORECASE,
        ),
    ),
    (
        "Prompt injection / agent permissions / AI security",
        re.compile(
            r"prompt(?:ing)? (?:the )?(?:payload|injection)|indirect injection|"
            r"agent compromise|credential is not authori[sz]ation|ai security|"
            r"sensitive data|supply chain attack|tool access",
            re.IGNORECASE,
        ),
    ),
    (
        "Offensive security / exploitation / malware",
        re.compile(
            r"nmap|metasploit|pentest|red ?team|exploit|bug bounty|malware|"
            r"reverse engineer|vulnerabilit|webshell|backdoor|c2\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Detection engineering / blue team / threat intelligence",
        re.compile(
            r"blue ?team|threat intel|yara[- ]?l|secops|chronicle|detection rule|"
            r"siem|ueba|o365.*rule|curated detection",
            re.IGNORECASE,
        ),
    ),
    ("Claude / Anthropic", re.compile(r"claude|anthropic", re.IGNORECASE)),
    ("ChatGPT / OpenAI", re.compile(r"chatgpt|openai|codex", re.IGNORECASE)),
    ("Gemini / Google AI", re.compile(r"gemini|google ai|aistudio", re.IGNORECASE)),
    ("Grok / xAI", re.compile(r"\bgrok\b|\bxai\b|x\.ai", re.IGNORECASE)),
    (
        "Entity-linked applied systems (classify by technical topic)",
        re.compile(
            r"spacex|starlink|tesla|cybercab|neuralink|boring company|elon musk|"
            r"super heavy|starship",
            re.IGNORECASE,
        ),
    ),
]


def target_day(date_text: str | None = None) -> dt.date:
    if date_text:
        return dt.date.fromisoformat(date_text)
    return dt.datetime.now(LOCAL_TZ).date() - dt.timedelta(days=1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="PDT/PST calendar date (YYYY-MM-DD)")
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def coverage_for(day: dt.date) -> dict:
    backfill_path = BACKFILL_ROOT / f"{day.isoformat()}.json"
    backfill = None
    if backfill_path.exists():
        try:
            backfill = json.loads(backfill_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backfill = None
    runs = load_jsonl(RUN_LOG)
    start_local = dt.datetime.combine(day, dt.time.min, tzinfo=LOCAL_TZ)
    # Include the first post-midnight snapshot, which captures late posts.
    end_local = start_local + dt.timedelta(days=1, hours=1)
    relevant = []
    for run in runs:
        stamp = run.get("fetched_at_utc")
        if not stamp or run.get("status") != "ok":
            continue
        fetched = dt.datetime.fromisoformat(stamp).astimezone(LOCAL_TZ)
        if start_local <= fetched <= end_local:
            oldest = dt.datetime.fromisoformat(run["feed_oldest_utc"])
            newest = dt.datetime.fromisoformat(run["feed_newest_utc"])
            relevant.append((fetched, oldest, newest))
    relevant.sort(key=lambda item: item[0])
    fetched_times = [item[0] for item in relevant]
    gaps = [
        int((right - left).total_seconds() // 60)
        for left, right in zip(fetched_times, fetched_times[1:])
    ]
    continuity_gaps = []
    for previous, current in zip(relevant, relevant[1:]):
        previous_newest = previous[2]
        current_oldest = current[1]
        if current_oldest > previous_newest:
            continuity_gaps.append(int((current_oldest - previous_newest).total_seconds()))
    complete_backfill = bool(backfill and backfill.get("complete_to_window_start"))
    return {
        "successful_snapshots": len(relevant),
        "first_snapshot_local": fetched_times[0].isoformat() if relevant else None,
        "last_snapshot_local": fetched_times[-1].isoformat() if relevant else None,
        "largest_gap_minutes": max(gaps) if gaps else None,
        "source_continuity_gap_count": len(continuity_gaps),
        "largest_source_gap_seconds": max(continuity_gaps) if continuity_gaps else 0,
        "coverage_quality": (
            "complete-backfill"
            if complete_backfill
            else "good"
            if relevant and fetched_times[0] <= start_local + dt.timedelta(minutes=30)
            and fetched_times[-1] >= start_local + dt.timedelta(days=1)
            and (max(gaps) if gaps else 10_000) <= 30
            and not continuity_gaps
            else "partial"
        ),
        "backfill": backfill,
    }


def subreddit_scope() -> list[str]:
    names = []
    seen = set()
    for group in GROUPS.values():
        for name in group:
            if name.lower() not in seen:
                seen.add(name.lower())
                names.append(name)
    return names


def normalize_title(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[^a-z0-9가-힣]+", " ", value)
    return " ".join(value.split())


def searchable_text(item: dict) -> str:
    return f"{item.get('title', '')}\n{item.get('body_excerpt', '')}"


def is_specialist_signal(item: dict) -> bool:
    return item.get("subreddit", "").casefold() in SPECIALIST_SUBREDDITS


def print_source_coverage(coverage: dict, by_subreddit: dict[str, list[dict]]) -> None:
    backfill = coverage.get("backfill") or {}
    requested = backfill.get("subreddits_requested") or subreddit_scope()
    completed = backfill.get("subreddits_completed") or []
    errors = backfill.get("errors") or {}
    active_names = {name.casefold() for name in by_subreddit}
    zero_posts = [name for name in completed if name.casefold() not in active_names]
    print("\nSOURCE COVERAGE LEDGER")
    print(
        f"- configured={len(requested)}; completed={len(completed)}; "
        f"active={len(active_names)}; zero-post={len(zero_posts)}; failed={len(errors)}"
    )
    if zero_posts:
        print("- completed with zero posts: " + ", ".join("r/" + name for name in zero_posts))
    if errors:
        print("- failed sources: " + ", ".join(f"r/{name}: {message}" for name, message in errors.items()))


def print_crosspost_ledger(records: list[dict]) -> None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in records:
        key = normalize_title(item.get("title", ""))
        if key:
            groups[key].append(item)
    repeated = [
        items
        for items in groups.values()
        if len(items) > 1 and len({item.get("subreddit", "").casefold() for item in items}) > 1
    ]
    repeated.sort(
        key=lambda items: (len({item.get("subreddit", "").casefold() for item in items}), max(candidate_score(item) for item in items)),
        reverse=True,
    )
    print("\nEXACT-TITLE / CROSSPOST DEDUP LEDGER")
    print(f"- cross-community repeated-title groups: {len(repeated)}")
    for items in repeated[:CROSSPOST_PRINT]:
        subreddits = sorted({"r/" + item.get("subreddit", "unknown") for item in items})
        representative = max(items, key=candidate_score)
        print(
            f"- {representative.get('title', '')} | posts={len(items)} | "
            f"communities={', '.join(subreddits)} | {representative.get('url', '')}"
        )


def print_topic_ledger(records: list[dict]) -> None:
    print("\nTOPIC COVERAGE INDEX — OVERLAPPING DISCOVERY TAXONOMY, NOT AN INCLUSION QUOTA")
    print(
        "Every matching post remains in the full archive. Counts overlap across topics. "
        "Review every listed topic with material candidates; do not reduce the final brief to a fixed count."
    )
    for label, pattern in TOPIC_RULES:
        matched = [item for item in records if pattern.search(searchable_text(item))]
        if not matched:
            continue
        communities = sorted({item.get("subreddit", "unknown") for item in matched}, key=str.casefold)
        unique_by_title: dict[str, dict] = {}
        for item in matched:
            key = normalize_title(item.get("title", "")) or item.get("id", "")
            current = unique_by_title.get(key)
            if (
                current is None
                or candidate_score(item) > candidate_score(current)
                or (
                    candidate_score(item) == candidate_score(current)
                    and is_specialist_signal(item)
                    and not is_specialist_signal(current)
                )
            ):
                unique_by_title[key] = item
        ranked = sorted(
            unique_by_title.values(),
            key=lambda item: (candidate_score(item), item.get("published_utc", "")),
            reverse=True,
        )
        print(
            f"\n### {label}: posts={len(matched)}; unique-titles={len(unique_by_title)}; "
            f"communities={len(communities)} ({', '.join('r/' + name for name in communities)})"
        )
        selected = ranked[:PER_TOPIC]
        specialist = next((item for item in ranked if is_specialist_signal(item)), None)
        if specialist is not None and specialist not in selected and selected:
            selected[-1] = specialist
        for item in selected:
            print(
                f"- [r/{item.get('subreddit', 'unknown')}] {item.get('title', '')} "
                f"(score={candidate_score(item)}; {item.get('published_utc', '')})\n"
                f"  {item.get('url', '')}"
            )


def print_authoring_gate(path: Path) -> None:
    print("\nMANDATORY COMMUNITY + DEEP ANALYSIS AUTHORING GATE")
    print(
        "1. Before drafting, review the SOURCE COVERAGE, CROSSPOST, and every TOPIC COVERAGE "
        "ledger above. The taxonomy overlaps and is not a story quota."
    )
    print(
        "2. Build an internal disposition ledger for every materially relevant candidate topic: "
        "include, exclude as noise/duplicate, or hold as unverified. Do not expose a giant rejected-post "
        "list, but do expose material coverage limits and representative disqualified high-impact claims."
    )
    print(
        "3. The per-topic and per-subreddit samples are navigation aids only. If a topic is material, "
        f"inspect additional records in the complete archive {path}; never treat the printed samples as "
        "an inclusion cap."
    )
    print(
        "4. Final Community Signals must split distinct claims into separate subsections. A technically relevant "
        "conversation may be retained even before primary-source verification as `COMMUNITY SIGNAL — UNVERIFIED`. "
        "Each retained signal needs representative thread links, repeated-vs-single-source classification, what users "
        "actually reported, primary/repository comparison where available, uncertainty, and user relevance. Low "
        "points or comment counts are not an automatic exclusion threshold."
    )
    print(
        "5. Before writing the morning brief, use the fixed 00–10 sections and build a disposition ledger for material claims. "
        "Keep the morning brief lightweight: add an item-specific Deep Analysis only when the morning story needs it "
        "and only for a genuinely new, hard-to-explain non-security mechanism; zero is acceptable. Route long security "
        "threat models, PoC validation, and detailed architecture analysis to the separate Deep Dive Queue. Related "
        "items may share a toggle only when they share the same concrete mechanism."
    )
    print(
        "6. Daily default scope: comments not retrieved. Treat a Reddit score or comment count as metadata only; "
        "use the available post excerpt/full body and linked-source fetch status explicitly. Retrieve comments "
        "only in a separately requested authorized Deep Dive, and record that expanded evidence scope. For an "
        "applicable Deep Analysis, record objective, authorized scope, environment/versions, evidence status, "
        "baseline/metrics, limitations, cleanup/rollback/idempotency, and execution status."
    )
    print(
        "7. Explain every acronym or specialist term actually used in a plain-language glossary. Deep Analysis, "
        "PoC and glossary text must use default high-contrast formatting; do not emit gray color attributes or "
        "low-contrast custom spans."
    )
    print(
        "8. Do not mark Published or Reddit Included=YES until a post-write Notion fetch confirms: the "
        "Community Signals section distinguishes signal from fact; representative links and excerpt/full-body "
        "status exist; the inclusion/exclusion Coverage Map matches the final item list; any Deep Analysis is "
        "directly attached to the item that needs it; and no unrelated page-end bundle was created. If validation "
        "fails, repair and fetch again."
    )


def main() -> int:
    args = parse_args()
    day = target_day(args.date)
    path = SNAPSHOT_ROOT / f"{day.isoformat()}.jsonl"
    records_by_id = {item.get("id"): item for item in load_jsonl(path) if item.get("id")}
    records = list(records_by_id.values())
    coverage = coverage_for(day)

    print("REDDIT SNAPSHOT DIGEST — COMMUNITY SIGNALS ARE UNVERIFIED UNTIL CROSS-CHECKED")
    print(f"Target PDT date: {day.isoformat()}")
    print(f"Archive: {path}")
    print(f"Coverage quality: {coverage['coverage_quality']}")
    print(f"Successful snapshots: {coverage['successful_snapshots']}")
    print(f"Largest snapshot gap: {coverage['largest_gap_minutes']} minutes")
    print(
        f"Source continuity gaps: {coverage['source_continuity_gap_count']}; "
        f"largest={coverage['largest_source_gap_seconds']} seconds"
    )
    if coverage.get("backfill"):
        backfill = coverage["backfill"]
        print(
            f"Backfill: {backfill.get('status')}; pages={backfill.get('pages_fetched')}; "
            f"posts={backfill.get('target_post_count')}"
        )
    print(f"Unique archived posts: {len(records)}")
    print("Configured communities: " + ", ".join("r/" + name for name in subreddit_scope()))

    if not records:
        print(
            "No archived posts are available for this date. Do not claim comprehensive Reddit "
            "coverage; use web/RSS fallbacks and label the coverage limitation internally."
        )
        return 0

    by_subreddit: dict[str, list[dict]] = defaultdict(list)
    for item in records:
        enriched = dict(item)
        enriched["candidate_score"] = candidate_score(enriched)
        by_subreddit[enriched.get("subreddit", "unknown")].append(enriched)

    print("\nACTIVE SUBREDDIT COUNTS")
    print(
        "Per-subreddit counts are preserved in the backfill report and full archive but suppressed here "
        "because platform volume is not editorial importance."
    )

    print_source_coverage(coverage, by_subreddit)
    print_crosspost_ledger(records)
    print_topic_ledger(records)
    print_authoring_gate(path)

    if PER_SUBREDDIT:
        candidates: list[dict] = []
        for subreddit, items in by_subreddit.items():
            items.sort(
                key=lambda item: (item["candidate_score"], item.get("published_utc", "")),
                reverse=True,
            )
            candidates.extend(items[:PER_SUBREDDIT])
        candidates.sort(
            key=lambda item: (item["candidate_score"], item.get("published_utc", "")),
            reverse=True,
        )

        print("\nBOUNDED REVIEW QUEUE")
        print(
            "This queue is only a compact first-pass index and is NOT an inclusion cap. The complete "
            f"source archive is {path}. Use file tools to inspect all posts for active subreddits, "
            "especially whenever a subreddit has more posts than shown here or several distinct themes. "
            "Cluster repeated claims across communities, inspect representative threads, distinguish "
            "user reports from facts, and cross-check official/primary sources. There is no numeric "
            "quota for final Community Signals or Deep Analysis items."
        )
        for item in candidates:
            print(
                f"- [r/{item.get('subreddit', 'unknown')}] {item.get('title', '')} "
                f"(discovery score={item['candidate_score']}; {item.get('published_utc', '')})\n"
                f"  {item.get('url', '')}"
            )
            excerpt = item.get("body_excerpt", "")
            if excerpt:
                print(f"  excerpt: {excerpt[:250]}")
    else:
        print("\nPER-SUBREDDIT SAMPLES: suppressed to prevent community prompt-volume bias.")
        print(
            "Use the topic and cross-post ledgers above for navigation, then inspect the complete "
            f"archive {path} only for materially relevant or ambiguous topics."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
