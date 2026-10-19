#!/usr/bin/env python3
"""Canonical target-date Daily writer/finalizer.

Consumes the same-date disposition and enrichment artifacts produced by
``daily_pipeline.py``; it never selects a smaller representative set.  It
reclassifies semantically, checks event-specific analysis binding, recomputes
body metadata, and performs exact-date Notion read-back before FINAL.
"""
from __future__ import annotations
import os

import argparse
import datetime as dt
import hashlib
import json
import re
from collections import Counter
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
IDS = json.loads((ROOT / "config" / "notion_ids.json").read_text(encoding="utf-8"))
sys.path.insert(0, str(ROOT))

from work.issue_taxonomy import TOPIC_SECTIONS, classification_confidence, classify_primary_topic, related_topics, subtopic_for
from work.reader_brief_contract import validate_reader_markdown
from work.daily_pipeline import (
    _evidence_priority,
    _issue_sources,
    _source_priority,
    build_reader_markdown as pipeline_build_reader_markdown,
    source_label,
)
from work.reader_narrative import (
    _clean_text,
    _deep_analysis,
    build_brief_index_markdown,
    build_topic_section_markdown,
    deep_analysis_binding,
)
from work.reader_quality import audit_reader, quality_manifest
from work.notion_block_writer import markdown_blocks, replace_page_resumable as replace_page_blocks
from work.daily_brief_sections import list_section_pages, resolve_section_page, write_section_page
from work.source_enrichment import intake_index, load_enrichment, merge_signal
from work.feedback import ISSUE_LEDGER_PATH, PREFERENCE_LEDGER_PATH, apply_preferences, assign_feedback_ids, feedback_states
from work.scope_guard import guard_notion_request, register_page
from work.notion_dashboard import (
    ensure_dashboard_daily_brief_view,
    ensure_dashboard_sections_view,
    ensure_overview_page,
)


def args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def notion_write_progress_path(day: str) -> Path:
    return ROOT / "data" / "evidence" / day / f"{day}.notion-block-write-progress.json"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def reader_rewrite_path(day: str) -> Path:
    return ROOT / "data" / "quality_rewrite_batches" / f"{day}.reader-rewrites.jsonl"


def evidence_urls_by_issue(signals: list[dict]) -> dict[str, set[str]]:
    allowed: dict[str, set[str]] = {}
    for signal in signals:
        issue_id = str(signal.get("feedback_id") or "").strip()
        if not issue_id:
            continue
        urls: set[str] = set()
        for value in [signal.get("url"), signal.get("source_url"), *(signal.get("source_urls") or [])]:
            if isinstance(value, str) and value.strip().startswith("http"):
                urls.add(value.strip().rstrip("/"))
        for item in signal.get("supporting_sources") or []:
            if isinstance(item, dict):
                value = str(item.get("url") or "").strip()
                if value.startswith("http"):
                    urls.add(value.rstrip("/"))
        allowed[issue_id] = urls
    return allowed


def inspect_reader_rewrite_artifact(
    path: Path,
    expected_ids: set[str],
    allowed_source_urls: dict[str, set[str]] | None = None,
) -> dict:
    """Validate the complete semantic rewrite handoff before any external write.

    A rewrite artifact must cover the exact issue inventory, preserve a natural
    reader lead and source-grounded Details, and retain at least one source URL
    for every issue. This deterministic gate does not assess editorial quality.
    """
    expected = {str(item).strip() for item in expected_ids if str(item).strip()}
    if not path.exists():
        return {
            "status": "MISSING",
            "external_write_allowed": False,
            "path": str(path),
            "expected_count": len(expected),
            "row_count": 0,
        }

    rows = load_jsonl(path)
    ids = [str(row.get("issue_id") or "").strip() for row in rows]
    counts = Counter(ids)
    duplicate_ids = sorted(issue_id for issue_id, count in counts.items() if issue_id and count > 1)
    actual = {issue_id for issue_id in ids if issue_id}
    missing_ids = sorted(expected - actual)
    extra_ids = sorted(actual - expected)
    if duplicate_ids or missing_ids or extra_ids or len(ids) != len(actual):
        return {
            "status": "INVENTORY_MISMATCH",
            "external_write_allowed": False,
            "path": str(path),
            "expected_count": len(expected),
            "row_count": len(rows),
            "missing_ids": missing_ids,
            "extra_ids": extra_ids,
            "duplicate_ids": duplicate_ids,
        }

    invalid_rows: list[dict] = []
    for row in rows:
        issue_id = str(row.get("issue_id") or "").strip()
        summary = row.get("summary") or row.get("summary_lines") or [row.get("summary_1"), row.get("summary_2")]
        if isinstance(summary, str):
            summary_text = re.sub(r"\s+", " ", summary).strip()
        elif isinstance(summary, (list, tuple)):
            summary_text = re.sub(r"\s+", " ", " ".join(str(value or "").strip() for value in summary)).strip()
        else:
            summary_text = ""
        detail = str(row.get("detail_markdown") or "").strip()
        sources = row.get("sources")
        source_urls = [
            str(item.get("url") or "").strip()
            for item in sources
            if isinstance(item, dict) and str(item.get("url") or "").strip().startswith("http")
        ] if isinstance(sources, list) else []
        reasons: list[str] = []
        if not summary_text:
            reasons.append("reader_summary_missing")
        reader_text = summary_text + "\n" + detail
        if not re.search(r"[가-힣]", reader_text):
            reasons.append("reader_language_not_korean")
        if "[truncated]" in reader_text.lower() or "… [truncated]" in reader_text.lower():
            reasons.append("reader_artifact_truncated")
        source_understanding = str(row.get("source_understanding") or "").strip().upper()
        if not detail:
            reasons.append("reader_details_missing")
        if not source_urls:
            reasons.append("source_url_missing")
        if allowed_source_urls is not None:
            allowed = {
                str(url).strip().rstrip("/")
                for url in (allowed_source_urls.get(issue_id) or set())
                if str(url).strip().startswith("http")
            }
            cited = {url.rstrip("/") for url in source_urls}
            if not allowed or not (cited & allowed):
                reasons.append("source_url_not_in_evidence_packet")
        if source_understanding not in {"FULL_SOURCE", "PARTIAL_SOURCE", "UNRESOLVED"}:
            reasons.append("source_understanding_missing")

        if reasons:
            invalid_rows.append({"issue_id": issue_id, "reasons": reasons})

    if invalid_rows:
        return {
            "status": "ROW_VALIDATION_FAILED",
            "external_write_allowed": False,
            "path": str(path),
            "expected_count": len(expected),
            "row_count": len(rows),
            "invalid_rows": invalid_rows[:25],
            "invalid_row_count": len(invalid_rows),
        }
    return {
        "status": "PASS",
        "external_write_allowed": True,
        "path": str(path),
        "expected_count": len(expected),
        "row_count": len(rows),
    }


def apply_reader_rewrites(day: str, signals: list[dict]) -> int:
    """Attach verified source-aware prose without changing reader inclusion.

    The batch writer is deliberately an explicit artifact.  A partial batch,
    unknown ID, malformed summary, or source-less detail aborts before any
    Markdown/Notion write; the old generic renderer is never silently mixed
    with a partial rewrite.
    """
    path = reader_rewrite_path(day)
    if not path.exists():
        return 0
    signal_ids = {str(signal.get("feedback_id") or "").strip() for signal in signals}
    rewrite_gate = inspect_reader_rewrite_artifact(path, signal_ids, evidence_urls_by_issue(signals))
    if not rewrite_gate.get("external_write_allowed"):
        raise SystemExit("reader_rewrite_gate_failed:" + json.dumps(rewrite_gate, ensure_ascii=False))
    rows = load_jsonl(path)
    by_id = {str(row.get("issue_id") or "").strip(): row for row in rows}
    if len(by_id) != len(rows) or set(by_id) != signal_ids:
        missing = sorted(signal_ids - set(by_id))[:10]
        extra = sorted(set(by_id) - signal_ids)[:10]
        raise SystemExit(f"reader_rewrite_inventory_mismatch:missing={missing}:extra={extra}")
    valid_topics = {item[1] for item in TOPIC_SECTIONS}
    for signal in signals:
        issue_id = str(signal.get("feedback_id") or "")
        row = by_id[issue_id]
        summary = row.get("summary") or row.get("summary_lines") or [row.get("summary_1"), row.get("summary_2")]
        if isinstance(summary, str):
            summary_text = re.sub(r"\s+", " ", summary).strip()
        elif isinstance(summary, (list, tuple)):
            summary_text = re.sub(r"\s+", " ", " ".join(str(value or "").strip() for value in summary)).strip()
        else:
            summary_text = ""
        if not summary_text:
            raise SystemExit(f"reader_rewrite_summary_missing:{issue_id}")
        detail = str(row.get("detail_markdown") or "").strip()
        if not detail:
            raise SystemExit(f"reader_rewrite_details_missing:{issue_id}")
        sources = row.get("sources")
        if not isinstance(sources, list) or not any(str(item.get("url") or "").startswith("http") for item in sources if isinstance(item, dict)):
            raise SystemExit(f"reader_rewrite_sources_missing:{issue_id}")
        corrected = str(row.get("corrected_primary_topic") or signal.get("primary_topic") or "").strip()
        if corrected not in valid_topics:
            raise SystemExit(f"reader_rewrite_topic_invalid:{issue_id}:{corrected}")
        signal["reader_summary"] = summary_text
        signal.pop("reader_summary_lines", None)
        signal["reader_detail_markdown"] = detail
        signal["reader_deep_analysis"] = str(row.get("deep_analysis") or "").strip()
        reader_title = str(row.get("reader_title") or "").strip()
        if reader_title:
            if "\n" in reader_title or "\r" in reader_title:
                raise SystemExit(f"reader_rewrite_title_invalid:{issue_id}")
            signal["title"] = reader_title
            signal["canonical_title"] = reader_title
        signal["reader_sources"] = [
            {
                "source_id": str(item.get("source_id") or "").strip(),
                "source_name": str(item.get("source_name") or item.get("publisher") or "").strip(),
                "publisher": str(item.get("publisher") or item.get("source_name") or "").strip(),
                "url": str(item.get("url") or "").strip(),
                "title": str(item.get("title") or "").strip(),
            }
            for item in sources if isinstance(item, dict) and str(item.get("url") or "").startswith("http")
        ]
        signal["primary_topic"] = corrected
        signal["topic_section"] = corrected
        signal["subtopic"] = str(row.get("corrected_subtopic") or signal.get("subtopic") or "").strip()
        if not bool(row.get("manual_override")):
            classified = classify_primary_topic(signal)
            if classified in valid_topics:
                signal["primary_topic"] = classified
                signal["topic_section"] = classified
        signal["reader_rewrite_source_status"] = str(row.get("source_understanding") or "UNRESOLVED")
    return len(signals)


def is_dismissed(row: dict) -> bool:
    return str(row.get("disposition") or "").casefold().startswith("dismissed")


def meaningful(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or text.casefold() in {"none", "n/a", "unknown"}:
        return ""
    return text


def source_id_for(url: str, category: str = "") -> str:
    host = urlparse(url).netloc.casefold()
    if "reddit.com" in host:
        return "reddit"
    if "github.com" in host:
        return "github-trending-daily"
    if "arxiv.org" in host:
        return "arxiv"
    if "thehackernews.com" in host:
        return "the-hacker-news"
    if "cisa.gov" in host:
        return "cisa-advisories"
    if "broadcom.com" in host:
        return "broadcom"
    if "openai.com" in host:
        return "openai"
    if "nvidia.com" in host or "huggingface.co" in host:
        return "official-model-compute"
    return host or category or "source"


def _supporting_sources(row: dict) -> list[dict]:
    sources: list[dict] = []
    seen: set[str] = set()
    raw = row.get("supporting_sources")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            sources.append({"source_id": str(item.get("source_id") or source_id_for(url)), "url": url, "title": str(item.get("title") or row.get("canonical_title") or "")})
    primary = str(row.get("source_url") or row.get("primary_source") or "").strip()
    if primary.startswith("http") and primary not in seen:
        sources.insert(0, {"source_id": str(row.get("source_id") or source_id_for(primary)), "url": primary, "title": str(row.get("canonical_title") or "")})
        seen.add(primary)
    for url in row.get("independent_sources") or []:
        url = str(url).strip()
        if url.startswith("http") and url not in seen:
            sources.append({"source_id": source_id_for(url), "url": url, "title": str(row.get("canonical_title") or "")})
            seen.add(url)
    return sources


def _major_recall_path(day: str) -> Path:
    return ROOT / "data" / "evidence" / day / f"{day}.major-recall.jsonl"


def load_recall_candidates(day: str, existing_ids: set[str]) -> list[dict]:
    """Load only candidates explicitly marked include=true by the recall review."""
    path = _major_recall_path(day)
    result: list[dict] = []
    for row in load_jsonl(path):
        event_id = str(row.get("event_id") or row.get("inventory_id") or "").strip()
        if not event_id or event_id in existing_ids or row.get("include_in_reader") is not True:
            continue
        result.append(row)
    return result


def _previous_day(day: str) -> str:
    return (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()


def load_prior_major(day: str) -> list[dict]:
    """Return prior event bindings used by the continuity gate."""
    prior = _previous_day(day)
    manifest_path = ROOT / "data" / "evidence" / prior / f"{prior}.manifest.json"
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    result: list[dict] = []
    for binding in manifest.get("deep_analysis_bindings") or []:
        if isinstance(binding, dict) and binding.get("title"):
            result.append(
                {
                    "event_id": binding.get("event_id"),
                    "title": binding.get("title"),
                    "source_urls": binding.get("source_urls") or [],
                    "binding": binding.get("binding"),
                }
            )
    return result


def load_continuity_rows(day: str, rows: list[dict]) -> list[dict]:
    """Restore prior major issues absent from today's ledger unless duplicated.

    This is a recall-preserving bridge, not a ranking quota.  It copies only
    prior issues that had an explicit Deep Analysis binding, and gives them a
    new deterministic event id while retaining the prior source URL/title.
    """
    prior = _previous_day(day)
    prior_major = load_prior_major(day)
    if not prior_major:
        return []
    prior_rows = load_jsonl(ROOT / "data" / "evidence" / prior / f"{prior}.disposition.jsonl")
    by_id = {str(row.get("event_id") or row.get("inventory_id") or ""): row for row in prior_rows}
    current_urls = {str(row.get("source_url") or row.get("url") or "").rstrip("/").casefold() for row in rows}
    current_titles = {re.sub(r"[^a-z0-9가-힣]+", "", str(row.get("canonical_title") or row.get("title") or "").casefold()) for row in rows}
    restored: list[dict] = []
    for major in prior_major:
        title = str(major.get("title") or "").strip()
        urls = [str(url).rstrip("/").casefold() for url in major.get("source_urls") or [] if str(url).startswith("http")]
        norm_title = re.sub(r"[^a-z0-9가-힣]+", "", title.casefold())
        if norm_title in current_titles or any(url in current_urls for url in urls):
            continue
        prior_row = by_id.get(str(major.get("event_id") or ""))
        if not prior_row:
            prior_row = next((row for row in prior_rows if re.sub(r"[^a-z0-9가-힣]+", "", str(row.get("canonical_title") or row.get("title") or "").casefold()) == norm_title), None)
        if not prior_row or is_dismissed(prior_row):
            continue
        row = dict(prior_row)
        seed = f"{day}|{major.get('binding')}|{title}"
        new_id = "continuity:" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]
        row["event_id"] = new_id
        row["inventory_id"] = new_id
        row["continuity_class"] = "ONGOING"
        row["continuity_restored"] = True
        row["deep_analysis_event_id"] = ""
        row["what_was_known"] = "이 사건의 핵심 내용은 이전 날짜에도 확인됐다"
        row["what_changed_today"] = "새 발표가 없더라도 이전 major binding과 같은 사건이어서 현재 brief에서 계속 추적한다"
        restored.append(row)
    return restored


def normalize(row: dict, *, day: str, enrichment: dict[str, dict], intake_groups: dict[str, list[dict]]) -> dict:
    title = meaningful(row.get("canonical_title") or row.get("title")) or "기술 이슈"
    url = meaningful(row.get("source_url") or row.get("url") or row.get("primary_source"))
    sources = _supporting_sources(row)
    signal = {
        "inventory_id": str(row.get("event_id") or row.get("inventory_id") or title),
        "title": title,
        "summary": meaningful(row.get("new_evidence_or_context") or row.get("summary") or row.get("source_description")),
        "url": url,
        "source_id": str(row.get("source_id") or (sources[0]["source_id"] if sources else source_id_for(url))),
        "supporting_sources": sources,
        "role": row.get("role") or ("community_signal" if "community" in str(row.get("category") or "").casefold() else "specialist_reporting"),
        "lane": row.get("lane") or ("community_signals" if "community" in str(row.get("category") or "").casefold() else ""),
        "categories": [str(row.get("category") or "")] if row.get("category") else [],
        "topics": row.get("topics") or [],
        "discovery_categories": row.get("discovery_categories") or [],
        "score": row.get("score", 0),
        "cluster_id": str(row.get("inventory_id") or row.get("event_id") or ""),
        "continuity_class": str(row.get("continuity_class") or "WATCH").upper(),
        "continuity_restored": bool(row.get("continuity_restored")),
        "evidence_state": str(row.get("evidence_state") or "DEVELOPING").upper(),
        "priority_delta": str(row.get("priority_delta") or "UNCHANGED").upper(),
        "what_was_known": row.get("what_was_known", ""),
        "what_changed_today": row.get("what_changed_today", ""),
        "why_it_still_matters": row.get("why_it_still_matters", ""),
        "enrichment_state": row.get("enrichment_state", ""),
        "source_title": row.get("source_title", title),
        "source_description": row.get("source_description", ""),
        "source_facts": row.get("source_facts", ""),
        "source_body_excerpt": row.get("source_body_excerpt", ""),
        "deep_analysis": row.get("deep_analysis", ""),
        "reader_narrative": row.get("reader_narrative", ""),
        "repo_metadata": row.get("repo_metadata") or {},
        "related_topics": row.get("related_topics") or [],
        "comment_scope": row.get("comment_scope"),
        "deep_analysis_event_id": row.get("deep_analysis_event_id"),
    }
    signal = merge_signal(signal, day=day, enrichment=enrichment, intake_groups=intake_groups)
    # Editorial overrides may come from deliberate recall records, but stale
    # disposition source text must never overwrite a successful direct-source
    # enrichment result.
    for key in ("deep_analysis", "reader_narrative", "what_was_known", "what_changed_today", "why_it_still_matters", "comment_scope", "deep_analysis_event_id"):
        if row.get(key) not in (None, "", []):
            signal[key] = row[key]
    primary = classify_primary_topic(signal)
    signal["primary_topic"] = primary
    signal["topic_section"] = primary
    signal["subtopic"] = subtopic_for(signal, primary)
    signal["related_topics"] = row.get("related_topics") or related_topics(signal, primary)
    for key in ("source_title", "source_description", "source_facts", "source_body_excerpt", "summary"):
        if signal.get(key) not in (None, "", []):
            signal[key] = _clean_text(signal[key], 9000 if key in {"source_facts", "source_body_excerpt"} else 1800)
    signal["classification_confidence"] = classification_confidence(signal, primary)
    return signal


def _storage_row(signal: dict, original: dict) -> dict:
    row = dict(original)
    row.update(
        {
            "event_id": str(signal["inventory_id"]),
            "inventory_id": str(signal["inventory_id"]),
            "canonical_title": signal.get("title"),
            "source_url": signal.get("url"),
            "source_id": signal.get("source_id"),
            "topic_section": signal.get("primary_topic"),
            "subtopic": signal.get("subtopic"),
            "feedback_id": signal.get("feedback_id"),
            "feedback_state": signal.get("feedback_state", "[ ]"),
            "feedback_rank_score": signal.get("feedback_rank_score", 0.0),
            "feedback_features": signal.get("feedback_features", {}),
            "classification_confidence": signal.get("classification_confidence"),
            "related_topics": signal.get("related_topics", []),
            "source_title": signal.get("source_title", signal.get("title")),
            "source_description": signal.get("source_description", ""),
            "source_facts": signal.get("source_facts", ""),
            "source_body_excerpt": signal.get("source_body_excerpt", ""),
            "enrichment_state": signal.get("enrichment_state", "RESEARCH_INCOMPLETE"),
            "repo_metadata": signal.get("repo_metadata", {}),
            "supporting_sources": signal.get("supporting_sources", []),
            "new_evidence_or_context": signal.get("summary", ""),
        }
    )
    for key in ("deep_analysis", "reader_narrative", "what_was_known", "what_changed_today", "why_it_still_matters", "comment_scope", "deep_analysis_event_id"):
        if signal.get(key) not in (None, "", []):
            row[key] = signal[key]
    return row


def derive_metadata(signals: list[dict], body: str) -> dict:
    topic_labels = {
        "01. AI·에이전트·프런티어 모델": "AI", "02. 사이버보안·오펜시브 시큐리티": "Cybersecurity", "03. 로컬·오픈 모델": "Open Models",
        "04. 인프라·클라우드·컴퓨트": "Infrastructure", "05. 반도체·AI 인프라": "Semiconductors", "06. 개발자 도구·오픈소스·GitHub": "Developer Tools",
        "07. 연구·논문·벤치마크": "Research", "08. 로봇·응용 기술·오픈 디스커버리": "Applied Technology", "09. 커뮤니티 신호·초기 경보": "Community", "10. 기술 기업·비즈니스·거버넌스": "Business",
    }
    present = [label for heading, label in topic_labels.items() if re.search(r"^## " + re.escape(heading) + r"(?:\n|$)", body, re.M)]
    reddit = bool(re.search(r"reddit\.com", body, re.I))
    research = bool(re.search(r"^## 07\. 연구·논문·벤치마크(?:\n|$)|arxiv\.org", body, re.I | re.M))
    major = len(re.findall(r"^\s*(?:#{4,5}\s+)?Deep Analysis\s*$", body, re.M))
    changes = len(re.findall(r"Status: (?:NEW|DEVELOPING)\b", body, re.I))
    return {
        "Research Included": research,
        "Reddit Included": reddit,
        "Top Topics": present[:10],
        "Major Story Count": major,
        "Watchlist Changes": changes,
        "body_issue_count": len(re.findall(r"^(?:-\s+\[[ xX]\]\s+\d{2}-\d{3,}\s+|####\s+\d{2}-\d{3,}\s+)", body, re.M)),
    }


def evaluate_publication_gate(
    day: str,
    base_manifest: dict,
    original_rows: list[dict],
    included_rows: list[dict],
    signals: list[dict],
) -> dict:
    """Reject a false-success publication when collection/retrieval collapsed.

    The 2026-09-07 failure had plausible-looking PASS coverage labels, but all
    362 source retrievals failed and the reader body consisted only of three
    continuity-restored rows.  Metadata/quality checks alone cannot detect
    that condition, so this gate combines the independent telemetry with the
    actual row provenance before allowing FINAL.
    """
    reasons: list[str] = []
    coverage = base_manifest.get("coverage") or {}
    allowed = {"PASS", "PASS_NO_MATERIAL_EVENT", "PASS_NO_MATERIAL_EVENT_FOUND"}
    unresolved_coverage: list[str] = []
    for area, entry in coverage.items():
        status = str(entry.get("coverage_status") if isinstance(entry, dict) else entry or "").strip().upper()
        normalized = re.sub(r"[^A-Z]+", "_", status).strip("_")
        if normalized not in allowed:
            unresolved_coverage.append(f"{area}:{status or 'MISSING'}")
    if unresolved_coverage:
        reasons.append("unresolved_coverage=" + ",".join(unresolved_coverage))

    def integer(name: str) -> int:
        try:
            return int(base_manifest.get(name) or 0)
        except (TypeError, ValueError):
            return 0

    candidate_count = sum(
        int(entry.get("candidate_count") or 0)
        for entry in coverage.values()
        if isinstance(entry, dict)
    )
    attempts = integer("crawl_attempt_count")
    successes = integer("crawl_success_count")
    partial = integer("crawl_partial_count")
    verified = integer("verified_count")
    if candidate_count and attempts and successes == 0 and partial == 0 and verified == 0:
        reasons.append("source_retrieval_zero_with_candidates")

    all_continuity = bool(signals) and all(bool(signal.get("continuity_restored")) for signal in signals)
    if all_continuity and candidate_count and verified == 0:
        reasons.append("continuity_only_fallback")

    enrichment_path = ROOT / "data" / "evidence" / day / f"{day}.source-enrichment.jsonl"
    enrichment_rows = load_jsonl(enrichment_path)
    if candidate_count and not enrichment_rows:
        reasons.append("source_enrichment_empty_with_candidates")
    if not included_rows:
        reasons.append("no_reader_issues")

    return {
        "status": "PASS" if not reasons else "BLOCKED",
        "reasons": reasons,
        "candidate_count": candidate_count,
        "original_row_count": len(original_rows),
        "included_row_count": len(included_rows),
        "continuity_only": all_continuity,
        "crawl_attempt_count": attempts,
        "crawl_success_count": successes,
        "crawl_partial_count": partial,
        "verified_count": verified,
        "source_enrichment_row_count": len(enrichment_rows),
        "rule": "FINAL is forbidden when source retrieval collapses or the body is continuity-only fallback",
    }


def enrichment_status_from_states(states: dict) -> str:
    """Report enrichment quality, not merely artifact existence."""
    normalized = {
        str(key).strip().upper(): int(value or 0)
        for key, value in (states or {}).items()
        if int(value or 0) > 0
    }
    if not normalized:
        return "BLOCKED"
    if any(key != "FULL_SOURCE" for key in normalized):
        return "PARTIAL"
    return "PASS"


def prepare(day: str) -> tuple[list[dict], list[dict], list[dict], list[dict], str, dict]:
    evidence = ROOT / "data" / "evidence" / day
    manifest_path = evidence / f"{day}.manifest.json"
    disposition_path = evidence / f"{day}.disposition.jsonl"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    original_rows = load_jsonl(disposition_path)
    existing_ids = {str(row.get("event_id") or row.get("inventory_id") or "") for row in original_rows}
    recall_rows = load_recall_candidates(day, existing_ids)
    rows = original_rows + recall_rows
    continuity_rows = load_continuity_rows(day, rows)
    rows.extend(continuity_rows)
    included_rows = [row for row in rows if not is_dismissed(row)]
    dismissed_rows = [row for row in rows if is_dismissed(row)]
    enrichment = load_enrichment(day)
    groups = intake_index(day)
    signals = [normalize(row, day=day, enrichment=enrichment, intake_groups=groups) for row in included_rows]
    existing_reader = ROOT / "data" / "evidence" / day / f"{day}.md"
    checked_states = feedback_states(existing_reader.read_text(encoding="utf-8", errors="replace")) if existing_reader.exists() else {}
    current_states, current_feedback_sync = current_notion_feedback(day)
    checked_states.update(current_states)
    assign_feedback_ids(day, signals, checked_states)
    rewritten_count = apply_reader_rewrites(day, signals)
    prior_feedback_markdowns, preference_sync = prior_notion_feedback(day)
    preference_ledger = apply_preferences(day, signals, additional_markdowns=prior_feedback_markdowns)
    for signal in signals:
        signal["feedback_features"] = signal.get("feedback_features") or {}
    markdown = pipeline_build_reader_markdown(day, signals)
    binding_rows = []
    analysis_texts = []
    for signal in signals:
        binding = deep_analysis_binding(signal)
        if binding:
            analysis = _deep_analysis(signal).strip()
            title = str(signal.get("title") or "").strip()
            if not analysis or (not binding.startswith("custom:") and not binding.startswith("reader:") and title and title not in analysis):
                raise SystemExit(f"deep_analysis_event_mismatch:{signal.get('inventory_id')}")
            analysis_texts.append(analysis)
            binding_rows.append({
                "event_id": str(signal.get("inventory_id") or signal.get("event_id") or ""),
                "binding": binding,
                "title": title,
                "source_urls": [str(item.get("url") or "") for item in _supporting_sources(signal) if str(item.get("url") or "").startswith("http")],
            })
    if len(analysis_texts) != len(set(analysis_texts)):
        raise SystemExit("deep_analysis_duplicate_text")
    prior_major = load_prior_major(day)
    quality = audit_reader(markdown, signals, prior_major=prior_major)
    if not quality.passed and rewritten_count:
        raise SystemExit("reader_quality_failed:" + json.dumps(quality_manifest(quality), ensure_ascii=False))
    metadata = derive_metadata(signals, markdown)
    section_specs = _section_page_specs(day, signals)
    section_inventory = {
        "expected_section_keys": [str(spec["key"]) for spec in section_specs],
        "expected_section_count": len(section_specs),
        "expected_issue_counts": {str(spec["key"]): int(spec["issue_count"]) for spec in section_specs},
        "expected_issue_total": sum(int(spec["issue_count"]) for spec in section_specs),
        "no_section_quota": True,
    }
    if metadata.get("Major Story Count") != len(binding_rows):
        raise SystemExit(f"deep_analysis_binding_mismatch:body={metadata.get('Major Story Count')}:bindings={len(binding_rows)}")
    retained_ids = [str(row.get("event_id") or row.get("inventory_id")) for row in rows]
    reader_ids = [str(row.get("event_id") or row.get("inventory_id")) for row in included_rows]
    dismissed_ids = [str(row.get("event_id") or row.get("inventory_id")) for row in dismissed_rows]
    states: dict[str, int] = {}
    enrichment_states: dict[str, int] = {}
    subtopics: dict[str, int] = {}
    for signal in signals:
        state = str(signal.get("evidence_state") or "DEVELOPING").upper()
        states[state] = states.get(state, 0) + 1
        estate = str(signal.get("enrichment_state") or "RESEARCH_INCOMPLETE").upper()
        enrichment_states[estate] = enrichment_states.get(estate, 0) + 1
        key = f"{signal.get('primary_topic')} / {signal.get('subtopic')}"
        subtopics[key] = subtopics.get(key, 0) + 1
    recall_catalog = load_jsonl(_major_recall_path(day))
    recall_ids = {str(row.get("event_id") or row.get("inventory_id") or "") for row in recall_catalog}
    included_recall_count = sum(1 for row in included_rows if str(row.get("event_id") or row.get("inventory_id") or "") in recall_ids)
    recall_included_candidates = [row for row in recall_catalog if row.get("include_in_reader") is True]
    recall_gate_status = "PASS" if recall_included_candidates or included_recall_count else "PASS_NO_MATERIAL_EVENT"
    publication_gate = evaluate_publication_gate(day, manifest, original_rows, included_rows, signals)
    updated_manifest = dict(manifest)
    updated_manifest.update(
        {
            "schema_version": 3,
            "reader_narrative_renderer": "work/reader_narrative.py",
            "classification_version": "mechanism-first-v3-event-aware",
            "writing_version": "event-distiller-v2-native-toggle-heading",
            "quality_gate_version": "reader-quality-v3-native-toggle-heading-layout",
            "reader_rewrite_artifact": str(reader_rewrite_path(day)) if rewritten_count else None,
            "reader_rewritten_issue_count": rewritten_count,
            "reader_quality": quality_manifest(quality),
            "reader_quality_gate_status": "PASS" if quality.passed else ("DEFERRED_UNREVIEWED_STAGING" if not rewritten_count else "FAILED"),
            "source_enrichment_artifact": str(evidence / f"{day}.source-enrichment.jsonl"),
            "source_enrichment_states": enrichment_states,
            "dynamic_subtopics": subtopics,
            "section_inventory": section_inventory,
            "event_evidence_states": states,
            "retained_issue_count": len(retained_ids),
            "reader_issue_count": len(reader_ids),
            "explicitly_dismissed_count": len(dismissed_ids),
            "retained_issue_ids": retained_ids,
            "reader_issue_ids": reader_ids,
            "explicitly_dismissed_ids": dismissed_ids,
            "content_loss_check": {
                "retained_issue_count": len(retained_ids),
                "reader_issue_count": len(reader_ids),
                "explicitly_dismissed_count": len(dismissed_ids),
                "passed": len(reader_ids) + len(dismissed_ids) == len(retained_ids),
                "rule": "reader_issue_count + explicitly_dismissed_count must equal retained_issue_count",
            },
            "major_story_recall_gate": {
                "artifact": str(_major_recall_path(day)),
                "candidate_count": len(recall_catalog),
                "reviewed_candidate_count": len(recall_catalog),
                "include_candidate_count": len(recall_included_candidates),
                "included_recall_count": included_recall_count,
                "status": recall_gate_status,
                "rule": "PASS_NO_MATERIAL_EVENT is valid only when the independent recall artifact was reviewed and no candidate was marked include_in_reader",
            },
            "publication_gate": publication_gate,
            "body_metadata_recomputed": metadata,
            "feedback": {
                "issue_id_ledger": str(ISSUE_LEDGER_PATH),
                "preference_ledger": str(PREFERENCE_LEDGER_PATH),
                "rendered_issue_id_count": len({str(signal.get("feedback_id")) for signal in signals}),
                "checked_prior_observation_count": len(preference_ledger.get("observations") or []),
                "eligible_feature_count": sum(1 for count in (preference_ledger.get("feature_counts") or {}).values() if int(count) >= 2),
                "unchecked_is_not_negative": True,
                "minimum_repeated_signals": 2,
                "notion_prior_sync": preference_sync,
                "notion_current_sync": current_feedback_sync,
            },
            "deep_analysis_bindings": binding_rows,
            "reader_quality": quality_manifest(quality),
            "continuity_review": {
                "prior_major_count": len(prior_major),
                "restored_count": len(continuity_rows),
                "restored_titles": [str(row.get("canonical_title") or row.get("title") or "") for row in continuity_rows],
                "passed": not quality.continuity_loss,
            },
        }
    )
    return rows, included_rows, dismissed_rows, signals, markdown, updated_manifest


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _native_checked_feedback(page_id: str, headers: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    states: dict[str, str] = {}
    errors: list[str] = []

    def text_of(payload: dict) -> str:
        return "".join(str(item.get("plain_text") or "") for item in payload.get("rich_text") or [] if isinstance(item, dict))

    def walk(block_id: str) -> None:
        status, payload = request(f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100", headers=headers)
        if status != 200 or not isinstance(payload, dict):
            errors.append(f"{block_id}:children_status={status}")
            return
        for block in payload.get("results") or []:
            kind = str(block.get("type") or "")
            value = block.get(kind) or {}
            if kind == "to_do" and value.get("checked") is True:
                text = text_of(value)
                match = re.search(r"\b(\d{2}-\d{3,})\b", text)
                if match:
                    states[match.group(1)] = "[x]"
            if block.get("has_children"):
                walk(str(block.get("id") or ""))

    walk(page_id)
    return states, errors


def _split_notion_feedback(day: str, headers: dict[str, str]) -> tuple[dict[str, str] | None, dict]:
    """Read checked issue states from split topic pages, if that date uses them."""
    try:
        pages = list_section_pages(request, headers, day)
    except Exception as exc:
        return None, {"status": "ERROR", "reason": f"section_query_failed:{exc}"}
    if not pages:
        return None, {"status": "NOT_SPLIT", "section_count": 0}
    states: dict[str, str] = {}
    errors: list[str] = []
    for page in pages:
        page_id = str(page.get("id") or "")
        if not page_id:
            errors.append("page_id_missing")
            continue
        native_states, native_errors = _native_checked_feedback(page_id, headers)
        if native_errors:
            errors.extend(native_errors)
            body_status, body = request(f"https://api.notion.com/v1/pages/{page_id}/markdown", headers=headers)
            if body_status == 200 and isinstance(body, dict) and isinstance(body.get("markdown"), str):
                states.update(feedback_states(body["markdown"]))
            else:
                errors.append(f"{page_id}:markdown_status={body_status}")
        else:
            states.update(native_states)
    return states, {"status": "PASS" if not errors else "PARTIAL", "section_count": len(pages), "checked_count": len(states), "errors": errors}


def prior_notion_feedback(target_date: str) -> tuple[dict[str, str], dict]:
    """Read prior exact-date FINAL pages for checked feedback only."""
    values = load_env()
    token = values.get("NOTION_API_KEY")
    if not token:
        return {}, {"attempted": 0, "read": 0, "skipped": [], "status": "UNAVAILABLE"}
    headers = {"Authorization": "Bearer " + token, "Notion-Version": "2026-03-11", "Content-Type": "application/json"}
    try:
        target = dt.date.fromisoformat(target_date)
    except ValueError:
        return {}, {"attempted": 0, "read": 0, "skipped": ["invalid_target_date"], "status": "UNAVAILABLE"}
    result: dict[str, str] = {}
    skipped: list[str] = []
    days: list[str] = []
    for path in (ROOT / "data" / "evidence").glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"):
        try:
            if dt.date.fromisoformat(path.name) < target:
                days.append(path.name)
        except ValueError:
            continue
    for day in sorted(days):
        query_status, query = request(
            f"https://api.notion.com/v1/data_sources/{IDS['daily_data_source_id']}/query",
            "POST", headers, {"filter": {"property": "Date", "date": {"equals": day}}},
        )
        matches = query.get("results", []) if query_status == 200 and isinstance(query, dict) else []
        if query_status != 200 or len(matches) != 1:
            skipped.append(f"{day}:exact_count={len(matches)}:status={query_status}")
            continue
        page_id = matches[0].get("id")
        meta_status, metadata = request(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
        if meta_status != 200 or select_prop(metadata, "Stage") != "FINAL" or select_prop(metadata, "Brief Status") != "Published":
            skipped.append(f"{day}:not_final")
            continue
        split_states, split_info = _split_notion_feedback(day, headers)
        if split_states is not None:
            if split_info.get("status") == "PASS":
                result[day] = "\n".join(f"- {state} {issue_id} section-feedback" for issue_id, state in sorted(split_states.items()))
            else:
                skipped.append(f"{day}:split_feedback_{split_info.get('status')}")
        else:
            body_status, body = request(f"https://api.notion.com/v1/pages/{page_id}/markdown", headers=headers)
            if body_status == 200 and isinstance(body, dict) and isinstance(body.get("markdown"), str):
                result[day] = body["markdown"]
            else:
                skipped.append(f"{day}:markdown_status={body_status}")
    return result, {"attempted": len(days), "read": len(result), "skipped": skipped, "status": "PASS" if not skipped else "PARTIAL"}


def current_notion_feedback(target_date: str) -> tuple[dict[str, str], dict]:
    """Read checked states from the exact current page to preserve them."""
    values = load_env()
    token = values.get("NOTION_API_KEY")
    if not token:
        return {}, {"status": "UNAVAILABLE", "reason": "missing_key"}
    headers = {"Authorization": "Bearer " + token, "Notion-Version": "2026-03-11", "Content-Type": "application/json"}
    status, query = request(
        f"https://api.notion.com/v1/data_sources/{IDS['daily_data_source_id']}/query",
        "POST", headers, {"filter": {"property": "Date", "date": {"equals": target_date}}},
    )
    matches = query.get("results", []) if status == 200 and isinstance(query, dict) else []
    if status != 200 or len(matches) != 1:
        return {}, {"status": "SKIPPED", "exact_count": len(matches), "query_status": status}
    page_id = matches[0].get("id")
    split_states, split_info = _split_notion_feedback(target_date, headers)
    if split_states is not None:
        return split_states, split_info
    body_status, body = request(f"https://api.notion.com/v1/pages/{page_id}/markdown", headers=headers)
    if body_status != 200 or not isinstance(body, dict) or not isinstance(body.get("markdown"), str):
        return {}, {"status": "SKIPPED", "markdown_status": body_status}
    return feedback_states(body["markdown"]), {"status": "PASS", "checked_count": len(feedback_states(body["markdown"]))}


def request(url: str, method: str = "GET", headers: dict[str, str] | None = None, body=None):
    import urllib.error
    import urllib.request
    guard_notion_request(url, method, body)
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=payload)
    for _attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                raw = response.read()
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except Exception:
                    parsed = None
                return response.status, parsed
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception:
                parsed = None
            safe_read_post = method.upper() == "POST" and ("/query" in url or url.endswith("/search"))
            if exc.code in {429, 500, 502, 503, 504} and (method.upper() in {"GET", "HEAD", "OPTIONS"} or safe_read_post) and _attempt < 5:
                import time
                retry_after = exc.headers.get("Retry-After", "5")
                try:
                    delay = min(30.0, max(2.0, float(retry_after)))
                except (TypeError, ValueError):
                    delay = 5.0
                time.sleep(delay)
                continue
            return exc.code, parsed
        except Exception:
            # Timeouts / connection resets are transient. Reads (and /query POSTs) are safe to repeat; a write whose
            # outcome is unknown is not retried here (the caller's read-back decides whether it landed).
            safe = method.upper() in {"GET", "HEAD", "OPTIONS"} or (method.upper() == "POST" and ("/query" in url or url.endswith("/search")))
            if safe and _attempt < 5:
                import time
                time.sleep(min(30.0, 2.0 * (2 ** _attempt)))
                continue
            return "ERROR", None
    return "ERROR", None


def update_dashboard_today_view(
    headers: dict[str, str],
    target_date: str,
    section_links: list[dict],
    *,
    index_page_id: str,
    overview_page_id: str,
    local_dir: Path,
) -> dict:
    """Update the Sections linked view only after the brief is published.

    The old implementation patched a Daily Briefs view using wall-clock today.
    That was the wrong data source and could expose a date with no completed
    brief.  The durable view now points at Daily Brief Sections and uses the
    exact completed TARGET_DATE supplied by the Finalizer.
    """
    daily_brief_view = ensure_dashboard_daily_brief_view(
        request,
        headers,
        target_date,
        index_page_id,
    )
    sections_view = ensure_dashboard_sections_view(
        request,
        headers,
        target_date,
        section_links,
        index_page_id=index_page_id,
        overview_page_id=overview_page_id,
        local_dir=local_dir,
    )
    return {
        "status": "PASS" if daily_brief_view.get("status") == "PASS" and sections_view.get("status") == "PASS" else "FAILED",
        "daily_brief_view": daily_brief_view,
        "sections_view": sections_view,
    }


def notion_top_level_block_counts(page_id: str, headers: dict[str, str]) -> tuple[object, dict[str, int]]:
    """Read native top-level block types for publication verification."""
    counts: dict[str, int] = {}
    total = 0
    cursor = None
    for _ in range(50):
        url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
        if cursor:
            url += "&start_cursor=" + str(cursor)
        status, payload = request(url, headers=headers)
        if status != 200 or not isinstance(payload, dict):
            return status, counts
        for block in payload.get("results") or []:
            kind = str(block.get("type") or "")
            payload_for_type = block.get(kind) or {}
            count_key = (
                f"{kind}_toggle"
                if kind.startswith("heading_") and payload_for_type.get("is_toggleable") is True
                else kind
            )
            counts[count_key] = counts.get(count_key, 0) + 1
            total += 1
        if not payload.get("has_more"):
            return 200, counts
        cursor = payload.get("next_cursor")
        if not cursor:
            return "ERROR", counts
    return "ERROR", counts


def ensure_native_layout(page_id: str, headers: dict[str, str], markdown: str, expected_count: int, progress_path: Path | None = None) -> tuple[dict, str]:
    """Use native Notion blocks when Markdown conversion did not do so.

    The reader layout has topic-level Toggle Headings containing issue to-dos
    and generic Details toggles.  Only topic Toggle Headings are top-level, so the old check that
    expected ``expected_count`` top-level toggles is invalid for this layout.
    Recursive content equality is performed by ``verify_notion_daily_layout``;
    this function only decides whether the bounded native fallback is needed.
    """
    expected_top = markdown_blocks(markdown)
    expected_top_todos = sum(block.get("type") == "to_do" for block in expected_top)
    expected_topic_toggles = sum(
        block.get("type") == "heading_2"
        and (block.get("heading_2") or {}).get("is_toggleable") is True
        for block in expected_top
    )
    expected_toc = sum(block.get("type") == "table_of_contents" for block in expected_top)

    def top_layout_ok(counts: dict[str, int]) -> bool:
        return (
            counts.get("to_do", 0) == expected_top_todos
            and counts.get("heading_2_toggle", 0) == expected_topic_toggles
            and counts.get("table_of_contents", 0) == expected_toc
        )

    status, counts = notion_top_level_block_counts(page_id, headers)
    if status == 200 and top_layout_ok(counts):
        return {"status": status, "counts": counts, "fallback": False}, "markdown"
    block_result = replace_page_blocks(request, page_id, headers, markdown, progress_path=progress_path)
    status, counts = notion_top_level_block_counts(page_id, headers)
    if status != 200:
        raise SystemExit(f"notion_native_block_readback_failed:{status}")
    if not top_layout_ok(counts):
        raise SystemExit(
            "notion_native_layout_failed:"
            f"top_todo={counts.get('to_do', 0)}:{expected_top_todos}:"
            f"topic_toggle_heading={counts.get('heading_2_toggle', 0)}:{expected_topic_toggles}:"
            f"toc={counts.get('table_of_contents', 0)}:{expected_toc}"
        )
    return {"status": status, "counts": counts, "fallback": True, "block_result": block_result}, "blocks"


def page_title(metadata: dict) -> str:
    props = metadata.get("properties", {})
    return "".join(item.get("plain_text", "") for item in props.get("Name", {}).get("title", []))


def page_date(metadata: dict) -> str | None:
    return (metadata.get("properties", {}).get("Date", {}).get("date") or {}).get("start")


def select_prop(metadata: dict, name: str) -> str | None:
    return (metadata.get("properties", {}).get(name, {}).get("select") or {}).get("name")


def rich_text_prop(metadata: dict, name: str) -> str:
    values = (metadata.get("properties", {}).get(name, {}).get("rich_text") or [])
    return "".join(
        str(item.get("plain_text") or (item.get("text") or {}).get("content") or "")
        for item in values
        if isinstance(item, dict)
    ).strip()


def _properties_for_metadata(meta: dict, derived: dict, *, stage: str, status: str, review: bool) -> dict:
    props: dict = {
        "Stage": {"select": {"name": stage}},
        "Brief Status": {"select": {"name": status}},
        "Review Needed": {"checkbox": review},
        "Research Included": {"checkbox": bool(derived.get("Research Included"))},
        "Reddit Included": {"checkbox": bool(derived.get("Reddit Included"))},
        "Major Story Count": {"number": int(derived.get("Major Story Count") or 0)},
        "Watchlist Changes": {"number": int(derived.get("Watchlist Changes") or 0)},
        "Top Topics": {"multi_select": [{"name": str(x)} for x in derived.get("Top Topics") or []]},
        "Quality Note": {"rich_text": [{"type": "text", "text": {"content": "Body-derived metadata; dynamic subtopics, source enrichment, recall gate, and reader read-back checked."}}]},
    }
    return {key: value for key, value in props.items() if key in meta.get("properties", {})}


def _section_page_specs(day: str, signals: list[dict]) -> list[dict]:
    """Build one render spec per non-empty primary topic, without a quota.

    The expected set is derived from the complete signal inventory, not from a
    shortlist or a fixed section count.  A mismatch is a hard failure before
    any Index write so a five-section prefix cannot masquerade as a complete
    brief.
    """
    valid_keys = {key for _name, key in TOPIC_SECTIONS}
    expected_counts: dict[str, int] = {}
    unknown: dict[str, int] = {}
    for signal in signals:
        key = str(signal.get("primary_topic") or signal.get("topic_section") or "").strip()
        if key not in valid_keys:
            unknown[key] = unknown.get(key, 0) + 1
            continue
        expected_counts[key] = expected_counts.get(key, 0) + 1
    if unknown:
        raise SystemExit("section_unknown_topic_keys:" + json.dumps(unknown, ensure_ascii=False, sort_keys=True))

    specs: list[dict] = []
    for section_name, section_key in TOPIC_SECTIONS:
        issue_count = expected_counts.get(section_key, 0)
        if not issue_count:
            continue
        bucket = [
            signal
            for signal in signals
            if str(signal.get("primary_topic") or signal.get("topic_section") or "") == section_key
        ]
        coverage = "PASS"
        if any(str(signal.get("enrichment_state") or "").upper() != "FULL_SOURCE" for signal in bucket):
            coverage = "PARTIAL"
        specs.append(
            {
                "name": section_name,
                "key": section_key,
                "order": int(section_name.split(".", 1)[0]),
                "signals": bucket,
                "issue_count": issue_count,
                "coverage_status": coverage,
            }
        )

    actual_counts = {str(spec["key"]): int(spec["issue_count"]) for spec in specs}
    if actual_counts != expected_counts or sum(actual_counts.values()) != len(signals):
        raise SystemExit(
            "section_inventory_mismatch:"
            + json.dumps(
                {
                    "signal_count": len(signals),
                    "expected_counts": expected_counts,
                    "actual_counts": actual_counts,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return specs


def _write_split_brief_pages(
    day: str,
    index_page_id: str,
    headers: dict[str, str],
    full_markdown: str,
    signals: list[dict],
    *,
    stage: str,
    brief_status: str,
    review_needed: bool,
    progress_path: Path | None = None,
) -> dict:
    """Write topic pages first, then replace the date Index with navigation."""
    specs = _section_page_specs(day, signals)
    section_results: list[dict] = []
    section_links: list[dict] = []
    for spec in specs:
        section_markdown = build_topic_section_markdown(
            day,
            spec["signals"],
            section_name=spec["name"],
            section_key=spec["key"],
            issue_sources=_issue_sources,
            source_label=source_label,
            evidence_priority=_evidence_priority,
            source_priority=_source_priority,
        )
        section_progress = (
            ROOT / "data" / "evidence" / day / f"{day}.section-{spec['key']}.notion-progress.json"
        )
        result = write_section_page(
            request,
            headers,
            day=day,
            section_key=spec["key"],
            section_name=spec["name"],
            section_order=spec["order"],
            parent_brief_id=index_page_id,
            markdown=section_markdown,
            issue_count=spec["issue_count"],
            stage=stage,
            brief_status=brief_status,
            coverage_status=spec["coverage_status"],
            review_needed=review_needed,
            progress_path=section_progress,
        )
        result["section_key"] = spec["key"]
        result["section_name"] = spec["name"]
        result["section_order"] = spec["order"]
        result["coverage_status"] = spec["coverage_status"]
        section_results.append(result)
        section_links.append(
            {
                "key": spec["key"],
                "name": spec["name"],
                "url": result.get("url") or "",
                "issue_count": spec["issue_count"],
            }
        )

    expected_section_keys = {str(spec["key"]) for spec in specs}
    expected_section_counts = {str(spec["key"]): int(spec["issue_count"]) for spec in specs}
    existing_section_pages = list_section_pages(request, headers, day)
    actual_section_keys = {
        rich_text_prop(page, "Section Key")
        for page in existing_section_pages
        if rich_text_prop(page, "Section Key")
    }
    if actual_section_keys != expected_section_keys:
        raise SystemExit(
            "split_section_inventory_readback_failed:"
            + json.dumps(
                {
                    "expected_keys": sorted(expected_section_keys),
                    "actual_keys": sorted(actual_section_keys),
                    "missing": sorted(expected_section_keys - actual_section_keys),
                    "extra": sorted(actual_section_keys - expected_section_keys),
                },
                ensure_ascii=False,
            )
        )
    actual_section_counts = {
        rich_text_prop(page, "Section Key"): int(
            (page.get("properties", {}).get("Issue Count") or {}).get("number") or 0
        )
        for page in existing_section_pages
        if rich_text_prop(page, "Section Key")
    }
    if actual_section_counts != expected_section_counts:
        raise SystemExit(
            "split_section_count_readback_failed:"
            + json.dumps(
                {"expected": expected_section_counts, "actual": actual_section_counts},
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    index_markdown = build_brief_index_markdown(day, full_markdown, section_links)
    index_progress = progress_path or notion_write_progress_path(day)
    index_write = replace_page_blocks(
        request,
        index_page_id,
        headers,
        index_markdown,
        progress_path=index_progress,
    )
    body_status, body_payload = request(
        f"https://api.notion.com/v1/pages/{index_page_id}/markdown",
        "GET",
        headers,
    )
    index_body = body_payload.get("markdown") if body_status == 200 and isinstance(body_payload, dict) else ""
    if body_status != 200 or not isinstance(index_body, str) or not index_body.strip():
        raise SystemExit(f"split_index_body_readback_failed:{body_status}")
    missing_links = [
        item["name"]
        for item in section_links
        if item["name"] not in index_body
    ]
    if missing_links:
        raise SystemExit("split_index_section_links_missing:" + ",".join(missing_links))

    expected_ids = {
        str(signal.get("feedback_id") or signal.get("inventory_id") or signal.get("event_id") or "").strip()
        for signal in signals
        if str(signal.get("feedback_id") or signal.get("inventory_id") or signal.get("event_id") or "").strip()
    }
    actual_ids = {
        issue_id
        for result in section_results
        for issue_id in (result.get("native") or {}).get("issue_ids", [])
    }
    if actual_ids != expected_ids:
        raise SystemExit(
            "split_issue_id_readback_failed:"
            + json.dumps({"expected": len(expected_ids), "actual": len(actual_ids), "missing": sorted(expected_ids - actual_ids)[:20], "extra": sorted(actual_ids - expected_ids)[:20]}, ensure_ascii=False)
        )
    return {
        "index_page_id": index_page_id,
        "index_markdown": index_markdown,
        "index_body": index_body,
        "index_body_status": body_status,
        "index_write": index_write,
        "section_count": len(section_results),
        "section_issue_count": sum(int(item.get("issue_count") or 0) for item in section_results),
        "section_results": section_results,
        "section_links": section_links,
        "issue_ids": sorted(actual_ids),
    }


def _set_split_section_status(
    day: str,
    headers: dict[str, str],
    index_page_id: str,
    section_results: list[dict],
    *,
    stage: str,
    brief_status: str,
    review_needed: bool,
) -> list[dict]:
    updated: list[dict] = []
    for result in section_results:
        page_id, metadata, _ = resolve_section_page(
            request,
            headers,
            day=day,
            section_key=str(result["section_key"]),
            section_name=str(result["section_name"]),
            parent_brief_id=index_page_id,
            section_order=int(result["section_order"]),
            issue_count=int(result["issue_count"]),
            stage=stage,
            brief_status=brief_status,
            coverage_status=str(result.get("coverage_status") or "PASS"),
            review_needed=review_needed,
        )
        updated.append({"page_id": page_id, "status": select_prop(metadata, "Brief Status") if isinstance(metadata, dict) else None})
    return updated


def keep_notion_draft(day: str, markdown: str, signals: list[dict], gate: dict) -> dict:
    """Keep the exact page visibly non-final when publication is blocked.

    A blocked publication must not replace the page body with the generic
    renderer.  The page is only eligible for a Draft body after the complete
    semantic rewrite handoff has passed its inventory/content gate.
    """
    expected_ids = {str(signal.get("feedback_id") or "").strip() for signal in signals}
    rewrite_gate = inspect_reader_rewrite_artifact(
        reader_rewrite_path(day),
        expected_ids,
        evidence_urls_by_issue(signals),
    )
    if not rewrite_gate.get("external_write_allowed"):
        return {
            "status": "SKIPPED",
            "reason": "semantic_rewrite_gate_blocked",
            "rewrite_gate": rewrite_gate,
        }
    values = load_env()
    token = values.get("NOTION_API_KEY")
    if not token:
        return {"status": "SKIPPED", "reason": "NOTION_API_KEY_missing"}
    headers = {"Authorization": "Bearer " + token, "Notion-Version": "2026-03-11", "Content-Type": "application/json"}
    query_status, query = request(
        f"https://api.notion.com/v1/data_sources/{IDS['daily_data_source_id']}/query",
        "POST",
        headers,
        {"filter": {"property": "Date", "date": {"equals": day}}},
    )
    matches = query.get("results", []) if query_status == 200 and isinstance(query, dict) else []
    if query_status != 200 or len(matches) != 1:
        return {"status": "SKIPPED", "query_status": query_status, "exact_date_count": len(matches)}
    page_id = matches[0].get("id")
    metadata_status, metadata = request(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
    if metadata_status != 200 or page_title(metadata) != f"{day} · Daily Tech Intelligence Brief" or page_date(metadata) != day or (metadata.get("parent") or {}).get("database_id") != IDS["daily_database_id"]:
        return {"status": "FAILED", "reason": "notion_target_identity_failed", "metadata_status": metadata_status}
    register_page(page_id, parent_database_id=IDS["daily_database_id"])
    derived = derive_metadata(signals, markdown)
    props = _properties_for_metadata(metadata, derived, stage="DRAFT", status="Incomplete Research", review=True)
    reason = "; ".join(str(item) for item in gate.get("reasons") or [])[:1800] or "publication_gate_blocked"
    if "Quality Note" in metadata.get("properties", {}):
        props["Quality Note"] = {"rich_text": [{"type": "text", "text": {"content": "Publication blocked: " + reason}}]}
    write_status, _ = request(f"https://api.notion.com/v1/pages/{page_id}", "PATCH", headers, {"properties": props})
    if write_status not in (200, 201):
        return {"status": "FAILED", "reason": "draft_metadata_write_failed", "write_status": write_status}
    progress_path = notion_write_progress_path(day)
    try:
        split = _write_split_brief_pages(
            day,
            page_id,
            headers,
            markdown,
            signals,
            stage="DRAFT",
            brief_status="Incomplete Research",
            review_needed=True,
            progress_path=progress_path,
        )
    except Exception as exc:
        return {
            "status": "FAILED",
            "reason": "draft_body_write_failed",
            "body_status": "ERROR",
            "body_chunks": 0,
            "detail": str(exc),
        }
    read_status, readback = request(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
    valid = (
        read_status == 200
        and select_prop(readback, "Stage") == "DRAFT"
        and select_prop(readback, "Brief Status") == "Incomplete Research"
        and readback.get("properties", {}).get("Review Needed", {}).get("checkbox") is True
    )
    if not valid:
        return {"status": "FAILED", "reason": "draft_metadata_readback_failed", "read_status": read_status}
    canonical_reader_path = ROOT / "data" / "evidence" / day / f"{day}.md"
    canonical_reader_path.write_text(markdown, encoding="utf-8")
    return {
        "status": "DRAFT_INCOMPLETE_RESEARCH",
        "page_id": page_id,
        "metadata_status": read_status,
        "stage": select_prop(readback, "Stage"),
        "brief_status": select_prop(readback, "Brief Status"),
        "review_needed": readback.get("properties", {}).get("Review Needed", {}).get("checkbox"),
        "body_status": split["index_body_status"],
        "body_write_chunks": split["index_write"].get("appended_blocks"),
        "native_layout": {"section_count": split["section_count"], "issue_count": split["section_issue_count"]},
        "native_write_mode": "split_sections",
        "body_story_count": split["section_issue_count"],
        "body_chars": len(split["index_body"]),
        "body_sha256": hashlib.sha256(split["index_body"].encode("utf-8")).hexdigest(),
        "local_reader_sha256": hashlib.sha256(canonical_reader_path.read_bytes()).hexdigest(),
        "exact_content_match": False,
        "split": split,
    }


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
    """Replace then append a large body without exceeding Notion/WAF limits."""
    initial = _markdown_chunks(markdown, max_chars=12000)
    first = initial[0]
    remainder = _markdown_chunks(markdown[len(first):], max_chars=80000) if len(first) < len(markdown) else []
    chunks = [first] + remainder
    endpoint = f"https://api.notion.com/v1/pages/{page_id}/markdown"
    transient = {429, 500, 502, 503, 504, "ERROR"}

    def retry_patch(payload: dict, marker: str, attempts: int = 6) -> object:
        last = None
        for attempt in range(attempts):
            status, _ = request(endpoint, "PATCH", headers, payload)
            last = status
            if status in (200, 201, 202):
                return status
            if status in transient:
                if status != 429:
                    read_status, current = request(endpoint, headers=headers)
                    current_body = current.get("markdown") or "" if isinstance(current, dict) else ""
                    if read_status == 200 and marker and current_body.startswith(marker):
                        return 200
                if attempt + 1 < attempts:
                    time.sleep(min(30.0, 3.0 * (2 ** attempt)))
                continue
            return status
        return last

    replace_payload = {"type": "replace_content", "replace_content": {"new_str": chunks[0]}}
    replace_marker = chunks[0].lstrip("\n")[:1000]
    status = retry_patch(replace_payload, replace_marker)
    if status not in (200, 201, 202):
        return status, 0
    for index, chunk in enumerate(chunks[1:], start=1):
        append_payload = {
            "type": "insert_content",
            "insert_content": {
                "content": "\n\n" + chunk.lstrip("\n"),
                "position": {"type": "end"},
            },
            "allow_async": False,
        }
        marker = chunk.lstrip("\n")[:1000]
        status = retry_patch(append_payload, marker)
        if status not in (200, 201, 202):
            return status, index
    return 200, len(chunks)


def normalize_notion_markdown(markdown: str) -> str:
    """Undo Notion markdown read-back escaping for issue checkboxes.

    The markdown endpoint currently returns literal backslashes before square
    brackets in headings.  They are transport escaping, not reader content;
    normalize only the stable checkbox syntax so validation and the local
    canonical artifact compare the same representation.
    """
    # Native Notion Toggle Headings are serialized by the Markdown endpoint
    # as ``## Topic {toggle="true"}``.  The marker is block metadata, not
    # reader-facing text; strip it before contract/quality validation while
    # native block read-back verifies the actual heading type separately.
    normalized = re.sub(r"^(#{1,6}\s+.+?)\s+\{toggle=\"true\"\}\s*$", r"\1", markdown, flags=re.M)
    normalized = re.sub(r"^(#### \d{2}-\d{3,}) \\\[([ x])\\\]( .*)$", r"\1 [\2]\3", normalized, flags=re.M)
    return re.sub(r"^(-\s+)\\\[([ x])\\\](\s+\d{2}-\d{3,}\s+.*)$", r"\1[\2]\3", normalized, flags=re.M)


def read_canonical_body(page_id: str, headers: dict[str, str], signals: list[dict], expected_count: int, day: str, attempts: int = 20) -> tuple[str, object, object]:
    """Poll Notion until its canonical body is complete and passes every reader gate."""
    last_status = None
    last_body = ""
    last_contract = None
    last_quality = None
    for attempt in range(attempts):
        last_status, payload = request(f"https://api.notion.com/v1/pages/{page_id}/markdown", headers=headers)
        last_body = payload.get("markdown", "") if isinstance(payload, dict) else ""
        last_body = normalize_notion_markdown(last_body)
        if last_status == 200 and last_body:
            last_contract = validate_reader_markdown(last_body, require_todo_checkboxes=True)
            last_quality = audit_reader(last_body, signals, prior_major=load_prior_major(day))
            if last_contract.ok and last_contract.story_count == expected_count and last_quality.passed:
                return last_body, last_contract, last_quality
        if attempt + 1 < attempts:
            time.sleep(1.0)
    detail = {
        "status": last_status,
        "stories": getattr(last_contract, "story_count", None),
        "expected": expected_count,
        "quality": quality_manifest(last_quality) if last_quality is not None else None,
        "chars": len(last_body),
    }
    raise SystemExit("notion_canonical_body_failed:" + json.dumps(detail, ensure_ascii=False))


def _apply_notion_split(day: str, markdown: str, signals: list[dict], expected_count: int) -> dict:
    expected_ids = {str(signal.get("feedback_id") or "").strip() for signal in signals}
    rewrite_gate = inspect_reader_rewrite_artifact(
        reader_rewrite_path(day),
        expected_ids,
        evidence_urls_by_issue(signals),
    )
    if not rewrite_gate.get("external_write_allowed"):
        raise SystemExit("semantic_rewrite_gate_failed:" + json.dumps(rewrite_gate, ensure_ascii=False))
    values = load_env()
    token = values.get("NOTION_API_KEY")
    if not token:
        raise SystemExit("NOTION_API_KEY_missing")
    headers = {"Authorization": "Bearer " + token, "Notion-Version": "2026-03-11", "Content-Type": "application/json"}
    query_status, query = request(
        f"https://api.notion.com/v1/data_sources/{IDS['daily_data_source_id']}/query",
        "POST",
        headers,
        {"filter": {"property": "Date", "date": {"equals": day}}},
    )
    matches = query.get("results", []) if query_status == 200 and isinstance(query, dict) else []
    if query_status != 200:
        raise SystemExit(f"exact_date_query_failed:{query_status}")
    if len(matches) > 1:
        raise SystemExit(f"exact_date_duplicate:{len(matches)}")
    if len(matches) == 1:
        page_id = matches[0]["id"]
        metadata_status, metadata = request(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
        if metadata_status != 200 or page_title(metadata) != f"{day} · Daily Tech Intelligence Brief" or page_date(metadata) != day:
            raise SystemExit("notion_target_identity_failed")
        if (metadata.get("parent") or {}).get("database_id") != IDS["daily_database_id"]:
            raise SystemExit("notion_parent_identity_failed")
        register_page(page_id, parent_database_id=IDS["daily_database_id"])
    else:
        create_status, created = request(
            "https://api.notion.com/v1/pages",
            "POST",
            headers,
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
        metadata_status, metadata = request(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
        if metadata_status != 200 or page_title(metadata) != f"{day} · Daily Tech Intelligence Brief" or page_date(metadata) != day:
            raise SystemExit("notion_created_identity_failed")
        if (metadata.get("parent") or {}).get("database_id") != IDS["daily_database_id"]:
            raise SystemExit("notion_created_parent_identity_failed")
        register_page(page_id, parent_database_id=IDS["daily_database_id"])

    derived = derive_metadata(signals, markdown)
    draft_props = _properties_for_metadata(metadata, derived, stage="DRAFT", status="Draft", review=True)
    draft_status, _ = request(f"https://api.notion.com/v1/pages/{page_id}", "PATCH", headers, {"properties": draft_props})
    if draft_status not in (200, 201):
        raise SystemExit(f"notion_draft_metadata_write_failed:{draft_status}")

    split = _write_split_brief_pages(
        day,
        page_id,
        headers,
        markdown,
        signals,
        stage="DRAFT",
        brief_status="Draft",
        review_needed=True,
        progress_path=notion_write_progress_path(day),
    )
    if split["section_issue_count"] != expected_count:
        raise SystemExit(f"split_issue_count_failed:{split['section_issue_count']}:{expected_count}")

    publication_query_status, publication_query = request(
        f"https://api.notion.com/v1/data_sources/{IDS['daily_data_source_id']}/query",
        "POST",
        headers,
        {"filter": {"property": "Date", "date": {"equals": day}}},
    )
    publication_matches = publication_query.get("results", []) if publication_query_status == 200 and isinstance(publication_query, dict) else []
    if publication_query_status != 200:
        raise SystemExit(f"notion_prepublication_exact_date_readback_failed:{publication_query_status}")
    if len(publication_matches) != 1 or str(publication_matches[0].get("id") or "") != page_id:
        raise SystemExit(f"notion_prepublication_exact_date_identity_failed:{len(publication_matches)}")

    final_props = _properties_for_metadata(metadata, derived, stage="FINAL", status="Published", review=False)
    final_status, _ = request(f"https://api.notion.com/v1/pages/{page_id}", "PATCH", headers, {"properties": final_props})
    if final_status not in (200, 201):
        raise SystemExit(f"notion_final_metadata_write_failed:{final_status}")
    section_status = _set_split_section_status(
        day,
        headers,
        page_id,
        split["section_results"],
        stage="FINAL",
        brief_status="Published",
        review_needed=False,
    )
    if any(item.get("status") != "Published" for item in section_status):
        raise SystemExit("split_section_final_status_readback_failed")

    fms, final_meta = request(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
    if fms != 200 or select_prop(final_meta, "Stage") != "FINAL" or select_prop(final_meta, "Brief Status") != "Published" or final_meta.get("properties", {}).get("Review Needed", {}).get("checkbox") is not False:
        raise SystemExit("notion_final_metadata_readback_failed")
    final_native_status, final_native_counts = notion_top_level_block_counts(page_id, headers)
    canonical_reader_path = ROOT / "data" / "evidence" / day / f"{day}.md"
    canonical_reader_path.write_text(markdown, encoding="utf-8")
    quality = audit_reader(markdown, signals, prior_major=load_prior_major(day))
    return {
        "target_date": day,
        "exact_date_count": len(publication_matches),
        "pre_write_exact_date_count": len(matches),
        "post_write_exact_date_count": len(publication_matches),
        "page_id": page_id,
        "metadata_status": fms,
        "markdown_status": split["index_body_status"],
        "title": page_title(final_meta),
        "date": page_date(final_meta),
        "stage": select_prop(final_meta, "Stage"),
        "brief_status": select_prop(final_meta, "Brief Status"),
        "review_needed": final_meta.get("properties", {}).get("Review Needed", {}).get("checkbox"),
        "markdown_chars": len(split["index_body"]),
        "markdown_sha256": hashlib.sha256(split["index_body"].encode("utf-8")).hexdigest(),
        "body_write_mode": "split_sections",
        "native_write_mode": "split_sections",
        "native_block_status": final_native_status,
        "native_block_counts": final_native_counts,
        "raw_renderer_content_equal": False,
        "canonical_readback_valid": split["section_issue_count"] == expected_count and bool(split["section_results"]),
        "reader_quality": quality_manifest(quality),
        "final_body_story_count": split["section_issue_count"],
        "body_metadata": derived,
        "local_reader_sha256": hashlib.sha256(canonical_reader_path.read_bytes()).hexdigest(),
        "split": split,
        "section_status": section_status,
    }


def apply_notion(day: str, markdown: str, signals: list[dict], expected_count: int) -> dict:
    # The split archive is now the canonical external representation.  The
    # legacy monolithic implementation remains below temporarily as a guarded
    # comparison path while the split migration is exercised in production.
    return _apply_notion_split(day, markdown, signals, expected_count)


def _apply_notion_legacy_monolithic(day: str, markdown: str, signals: list[dict], expected_count: int) -> dict:
    expected_ids = {str(signal.get("feedback_id") or "").strip() for signal in signals}
    rewrite_gate = inspect_reader_rewrite_artifact(
        reader_rewrite_path(day),
        expected_ids,
        evidence_urls_by_issue(signals),
    )
    if not rewrite_gate.get("external_write_allowed"):
        raise SystemExit("semantic_rewrite_gate_failed:" + json.dumps(rewrite_gate, ensure_ascii=False))
    values = load_env()
    token = values.get("NOTION_API_KEY")
    if not token:
        raise SystemExit("NOTION_API_KEY_missing")
    headers = {"Authorization": "Bearer " + token, "Notion-Version": "2026-03-11", "Content-Type": "application/json"}
    query_status, query = request(f"https://api.notion.com/v1/data_sources/{IDS['daily_data_source_id']}/query", "POST", headers, {"filter": {"property": "Date", "date": {"equals": day}}})
    matches = query.get("results", []) if isinstance(query, dict) else []
    if query_status != 200:
        raise SystemExit(f"exact_date_query_failed:{query_status}")
    if len(matches) > 1:
        raise SystemExit(f"exact_date_duplicate:{len(matches)}")
    page_id = None
    metadata = {}
    if len(matches) == 1:
        page_id = matches[0]["id"]
        ms, metadata = request(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
        if ms != 200 or page_title(metadata) != f"{day} · Daily Tech Intelligence Brief" or page_date(metadata) != day:
            raise SystemExit("notion_target_identity_failed")
        if (metadata.get("parent") or {}).get("database_id") != IDS["daily_database_id"]:
            raise SystemExit("notion_parent_identity_failed")
    else:
        create_status, created = request(
            "https://api.notion.com/v1/pages",
            "POST",
            headers,
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
        ms, metadata = request(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
        if ms != 200 or page_title(metadata) != f"{day} · Daily Tech Intelligence Brief" or page_date(metadata) != day:
            raise SystemExit("notion_created_identity_failed")
    derived = derive_metadata(signals, markdown)
    draft_props = _properties_for_metadata(metadata, derived, stage="DRAFT", status="Draft", review=True)
    draft_status, _ = request(f"https://api.notion.com/v1/pages/{page_id}", "PATCH", headers, {"properties": draft_props})
    if draft_status not in (200, 201):
        raise SystemExit(f"notion_draft_metadata_write_failed:{draft_status}")
    progress_path = notion_write_progress_path(day)
    body_write_mode = "blocks"
    try:
        block_result = replace_page_blocks(request, page_id, headers, markdown, progress_path=progress_path)
        body_status = 200
        body_chunks = int(block_result.get("appended_blocks") or 0)
    except Exception as exc:
        raise SystemExit(f"notion_body_write_failed:ERROR:block_write_failed:{exc}") from exc
    try:
        native_readback, _ = ensure_native_layout(page_id, headers, markdown, expected_count, progress_path=progress_path)
        native_mode = "blocks"
    except SystemExit:
        raise
    body, contract, quality = read_canonical_body(page_id, headers, signals, expected_count, day)
    canonical_reader_path = ROOT / "data" / "evidence" / day / f"{day}.md"
    canonical_reader_path.write_text(body, encoding="utf-8")
    final_props = _properties_for_metadata(metadata, derive_metadata(signals, body), stage="FINAL", status="Published", review=False)
    final_status, _ = request(f"https://api.notion.com/v1/pages/{page_id}", "PATCH", headers, {"properties": final_props})
    if final_status not in (200, 201):
        raise SystemExit(f"notion_final_metadata_write_failed:{final_status}")
    try:
        final_body, final_contract, final_quality = read_canonical_body(page_id, headers, signals, expected_count, day)
    except SystemExit:
        rollback_status, rollback_meta = request(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
        if rollback_status == 200:
            rollback_props = _properties_for_metadata(rollback_meta, derive_metadata(signals, body), stage="DRAFT", status="Draft", review=True)
            request(f"https://api.notion.com/v1/pages/{page_id}", "PATCH", headers, {"properties": rollback_props})
        raise
    canonical_reader_path.write_text(final_body, encoding="utf-8")
    fms = 0
    final_meta: dict = {}
    for attempt in range(10):
        fms, final_meta = request(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
        if fms == 200 and select_prop(final_meta, "Stage") == "FINAL" and select_prop(final_meta, "Brief Status") == "Published" and final_meta.get("properties", {}).get("Review Needed", {}).get("checkbox") is False:
            break
        if attempt + 1 < 10:
            time.sleep(1.0)
    if fms != 200 or select_prop(final_meta, "Stage") != "FINAL" or select_prop(final_meta, "Brief Status") != "Published" or final_meta.get("properties", {}).get("Review Needed", {}).get("checkbox") is not False:
        raise SystemExit("notion_final_metadata_readback_failed")
    final_native_status, final_native_counts = notion_top_level_block_counts(page_id, headers)
    expected_topic_heading_count = sum(
        block.get("type") == "heading_2"
        and (block.get("heading_2") or {}).get("is_toggleable") is True
        for block in markdown_blocks(markdown)
    )
    if final_native_status != 200 or final_native_counts.get("heading_2_toggle", 0) != expected_topic_heading_count:
        raise SystemExit(
            "notion_final_native_layout_readback_failed:"
            f"todo={final_native_counts.get('to_do', 0)}:"
            f"topic_toggle_heading={final_native_counts.get('heading_2_toggle', 0)}:"
            f"expected_topic_sections={expected_topic_heading_count}"
        )
    return {
        "target_date": day,
        "exact_date_count": len(matches),
        "page_id": page_id,
        "metadata_status": fms,
        "markdown_status": 200,
        "title": page_title(final_meta),
        "date": page_date(final_meta),
        "stage": select_prop(final_meta, "Stage"),
        "brief_status": select_prop(final_meta, "Brief Status"),
        "review_needed": final_meta.get("properties", {}).get("Review Needed", {}).get("checkbox"),
        "markdown_chars": len(final_body),
        "markdown_sha256": hashlib.sha256(final_body.encode("utf-8")).hexdigest(),
        "markdown_write_chunks": body_chunks,
        "body_write_mode": body_write_mode,
        "native_write_mode": native_mode,
        "native_block_status": final_native_status,
        "native_block_counts": final_native_counts,
        "raw_renderer_content_equal": body == markdown,
        "canonical_readback_valid": final_contract.ok and final_contract.story_count == expected_count,
        "reader_quality": quality_manifest(final_quality),
        "final_body_story_count": final_contract.story_count,
        "body_metadata": derive_metadata(signals, final_body),
    }


def main() -> int:
    cli = args()
    if cli.dry_run == cli.apply:
        raise SystemExit("pass exactly one of --dry-run or --apply")
    rows, included_rows, dismissed_rows, signals, markdown, manifest = prepare(cli.date)
    accounting = len(included_rows) + len(dismissed_rows) == len(rows)
    contract = validate_reader_markdown(markdown, require_todo_checkboxes=True)
    publication_gate = manifest.get("publication_gate") or {}
    expected_rewrite_ids = {str(signal.get("feedback_id") or "").strip() for signal in signals}
    rewrite_gate = inspect_reader_rewrite_artifact(
        reader_rewrite_path(cli.date),
        expected_rewrite_ids,
        evidence_urls_by_issue(signals),
    )
    summary = {
        "target_date": cli.date,
        "retained_issue_count": len(rows),
        "reader_issue_count": len(included_rows),
        "explicitly_dismissed_count": len(dismissed_rows),
        "story_count": contract.story_count,
        "markdown_chars": len(markdown),
        "accounting_passed": accounting,
        "reader_contract_ok": contract.ok,
        "contract_errors": contract.errors,
        "dynamic_subtopics": manifest.get("dynamic_subtopics", {}),
        "source_enrichment_states": manifest.get("source_enrichment_states", {}),
        "major_recall": manifest.get("major_story_recall_gate", {}),
        "publication_gate": publication_gate,
        "reader_rewrite_gate": rewrite_gate,
    }
    print(json.dumps(summary, ensure_ascii=False))
    if cli.dry_run:
        return 0 if accounting and contract.ok and contract.story_count == len(included_rows) and publication_gate.get("status") == "PASS" and rewrite_gate.get("external_write_allowed") else 1

    evidence = ROOT / "data" / "evidence" / cli.date
    disposition_path = evidence / f"{cli.date}.disposition.jsonl"
    manifest_path = evidence / f"{cli.date}.manifest.json"
    reader_path = evidence / f"{cli.date}.md"
    audit_path = evidence / f"{cli.date}.audit.md"
    readback_path = evidence / f"{cli.date}.notion-readback.json"
    by_id = {str(row.get("event_id") or row.get("inventory_id")): row for row in included_rows}
    storage_rows: list[dict] = []
    for signal in signals:
        storage_rows.append(_storage_row(signal, by_id[str(signal["inventory_id"])]))
    storage_rows.extend(dismissed_rows)
    disposition_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in storage_rows) + "\n", encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reader_path.write_text(markdown, encoding="utf-8")
    previous_audit = audit_path.read_text(encoding="utf-8") if audit_path.exists() else ""
    gate_status = str(publication_gate.get("status") or "BLOCKED")
    enrichment_status = enrichment_status_from_states(manifest.get("source_enrichment_states") or {})
    recall_status = str((manifest.get("major_story_recall_gate") or {}).get("status") or "UNAVAILABLE")
    audit_path.write_text(previous_audit.rstrip() + "\n\nclassification_version=mechanism-first-v3-event-aware\nwriting_version=event-distiller-v2-native-toggle-heading\nquality_gate_version=reader-quality-v3-native-toggle-heading-layout\nfeedback_contract_version=stable-issue-id-checkbox-v2\npreference_ledger=recency-weighted-checked-only-v1\nsource_enrichment=" + enrichment_status + "\ndynamic_subtopics=PASS\nmajor_story_recall_gate=" + recall_status + "\npublication_gate=" + gate_status + "\npublication_gate_reasons=" + "; ".join(str(item) for item in publication_gate.get("reasons") or []) + "\nbody_metadata_recomputed=PASS\n", encoding="utf-8")
    dashboard_view = {"status": "SKIPPED", "reason": "publication_not_complete"}
    overview = {"status": "SKIPPED", "reason": "publication_not_complete"}
    notion_token = load_env().get("NOTION_API_KEY")
    if not rewrite_gate.get("external_write_allowed"):
        readback = {
            "target_date": cli.date,
            "status": "blocked_no_semantic_rewrite",
            "publication_gate": publication_gate,
            "reader_rewrite_gate": rewrite_gate,
            "overview": overview,
            "dashboard_view": dashboard_view,
            "notion": {"status": "SKIPPED", "reason": "semantic_rewrite_gate_blocked"},
        }
        readback_path.write_text(json.dumps(readback, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "blocked_no_semantic_rewrite", "readback": readback}, ensure_ascii=False))
        return 1
    if gate_status != "PASS":
        draft_readback = keep_notion_draft(cli.date, markdown, signals, publication_gate)
        readback = {
            "target_date": cli.date,
            "status": "blocked_draft",
            "publication_gate": publication_gate,
            "reader_rewrite_gate": rewrite_gate,
            "overview": overview,
            "dashboard_view": dashboard_view,
            "notion": draft_readback,
        }
        readback_path.write_text(json.dumps(readback, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "draft_incomplete_research", "readback": readback}, ensure_ascii=False))
        return 0 if draft_readback.get("status") == "DRAFT_INCOMPLETE_RESEARCH" else 1
    if not notion_token:
        readback = {
            "target_date": cli.date,
            "status": "blocked_missing_notion_api_key",
            "publication_gate": publication_gate,
            "reader_rewrite_gate": rewrite_gate,
            "overview": overview,
            "dashboard_view": dashboard_view,
            "notion": {"status": "SKIPPED", "reason": "NOTION_API_KEY_missing"},
        }
        readback_path.write_text(json.dumps(readback, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "blocked_missing_notion_api_key", "readback": readback}, ensure_ascii=False))
        return 1

    notion_headers = {
        "Authorization": "Bearer " + notion_token,
        "Notion-Version": "2026-03-11",
        "Content-Type": "application/json",
    }
    readback = apply_notion(cli.date, markdown, signals, len(included_rows))
    split = readback.get("split") or {}
    section_links = list(split.get("section_links") or [])
    index_page_id = str(split.get("index_page_id") or readback.get("page_id") or "")
    if not index_page_id or not section_links and signals:
        raise SystemExit("notion_split_navigation_missing_before_dashboard_update")
    try:
        overview = ensure_overview_page(
            request,
            notion_headers,
            cli.date,
            signals,
            section_links,
            status_label=str(readback.get("brief_status") or "Published"),
            index_page_id=index_page_id,
            local_dir=evidence,
        )
        dashboard_view = update_dashboard_today_view(
            notion_headers,
            cli.date,
            section_links,
            index_page_id=index_page_id,
            overview_page_id=str(overview["page_id"]),
            local_dir=evidence,
        )
    except Exception as exc:
        readback["overview"] = overview
        readback["dashboard_view"] = {"status": "FAILED", "reason": str(exc)}
        readback_path.write_text(json.dumps(readback, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise SystemExit("dashboard_overview_orchestration_failed:" + str(exc)) from exc

    readback["overview"] = overview
    readback["dashboard_view"] = dashboard_view
    readback_path.write_text(json.dumps(readback, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not (
        readback["exact_date_count"] == 1
        and readback["stage"] == "FINAL"
        and readback["brief_status"] == "Published"
        and readback["review_needed"] is False
        and readback["canonical_readback_valid"]
        and overview.get("status") == "PASS"
        and dashboard_view.get("status") == "PASS"
    ):
        raise SystemExit("notion_final_dashboard_overview_readback_failed")
    print(json.dumps({"status": "applied", "readback": readback}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
