"""Hard quality gates for the reader-facing Daily Brief.

The gates are intentionally conservative about content loss and source leakage,
while allowing a large high-recall inventory.  They operate on the final
Markdown and the event signals that produced it, so a passing transport/write
cannot hide a bad reader artifact.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from work.issue_taxonomy import classification_confidence, classify_primary_topic
from work.reader_brief_contract import _issue_blocks, _issue_line_title, _new_layout_contract
from work.reader_narrative import _clean_text, _detail_repeats_summary, _title, deep_analysis_binding


RAW_SOURCE_PATTERNS = (
    r"\.\.\.",
    r"…",
    r"\[truncated\]",
    r"\(truncated\)",
    r"there was an error while loading",
    r"please reload this page",
    r"link copied to clipboard",
    r"copy link",
    r"\bsubscribe(?:\s+to)?(?:\s+(?:unlock|our|the))?\b",
    r"read more",
    r"back by popular demand",
    r"disrupt\s+20\d{2}",
    r"newsletter signup",
    r"cookie (?:settings|preferences|policy)",
    r"repository files navigation",
    r"open more actions menu",
)

@dataclass
class QualityResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_source_leaks: list[str] = field(default_factory=list)
    generic_failures: list[str] = field(default_factory=list)
    misclassified: list[str] = field(default_factory=list)
    analysis_binding_failures: list[str] = field(default_factory=list)
    repeated_analysis: list[str] = field(default_factory=list)
    continuity_loss: list[str] = field(default_factory=list)
    rendered_issue_count: int = 0
    rendered_issue_titles: list[str] = field(default_factory=list)
    passed: bool = False


def _norm(value: object) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = text.replace("\\_", "_").replace("\\*", "*").replace("\\[", "[").replace("\\]", "]")
    text = re.sub(r"^\d{2}-\d{3,}\s+\[[ x]\]\s+", "", text)
    return re.sub(r"[^a-z0-9가-힣]+", "", text)


def _sentences(body: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.!?。！？])\s+", body) if len(x.strip()) >= 45]


def _distinctive_tokens(title: str) -> set[str]:
    tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9+.-]{2,}|[가-힣]{3,}", title.casefold()))
    return {x for x in tokens if x not in {"the", "and", "for", "with", "from", "this", "that", "agent", "model"}}


def _major_id(signal: dict) -> str:
    return str(signal.get("inventory_id") or signal.get("event_id") or signal.get("title") or "")


def _quality_body(block_body: str) -> str:
    return re.sub(r"^원문:.*?$", "", block_body, flags=re.M)


def _check_raw_source(markdown: str, result: QualityResult) -> None:
    for pattern in RAW_SOURCE_PATTERNS:
        if re.search(pattern, markdown, re.I):
            result.raw_source_leaks.append(pattern)
    if result.raw_source_leaks:
        result.errors.append("FAIL — RAW_SOURCE_LEAK")
        result.errors.append("FAIL — SOURCE_CLEANING")


def _check_generic_and_reuse(blocks: list[tuple[str, str]], result: QualityResult) -> None:
    paragraph_counter: Counter[str] = Counter()
    paragraph_titles: dict[str, set[str]] = {}
    for title, body in blocks:
        clean_body = _quality_body(body)
        # Reuse is a Deep Analysis failure, not the presence of the same
        # evidence-boundary caveat in many ordinary issue narratives.  Compare
        # only the expanded analysis region here; the binding checker below
        # validates every issue that actually owns one.
        match = re.search(
            r"(?ims)^\s*(?:#{4,5}\s+)?Deep Analysis\s*$\n(.+?)(?=^\s*Sources\s*$|^\s*</details>\s*$|\Z)",
            clean_body,
        )
        analysis_text = match.group(1) if match else ""
        for sentence in _sentences(analysis_text):
            normalized = re.sub(r"[^a-z0-9가-힣]+", "", sentence.casefold())
            if len(normalized) < 70:
                continue
            paragraph_counter[normalized] += 1
            paragraph_titles.setdefault(normalized, set()).add(_norm(title))
    for normalized, count in paragraph_counter.items():
        if count >= 2 and len(paragraph_titles.get(normalized, set())) >= 2:
            result.repeated_analysis.append(normalized[:120])
    if result.generic_failures:
        result.errors.append("FAIL — READER_CONTENT_INTEGRITY")
    if result.repeated_analysis:
        result.errors.append("FAIL — REUSED_ANALYSIS")


def _check_classification(signals: list[dict], result: QualityResult) -> None:
    for signal in signals:
        expected = classify_primary_topic(signal)
        actual = str(signal.get("primary_topic") or signal.get("topic_section") or "")
        if expected != actual:
            result.misclassified.append(_major_id(signal))
        if classification_confidence(signal, expected) == "LOW":
            result.warnings.append("LOW_CLASSIFICATION_CONFIDENCE:" + _major_id(signal))
    if result.misclassified:
        result.errors.append("FAIL — MISCLASSIFIED_EVENT")


def _issue_region(markdown: str, title: str) -> str:
    target = _norm(title)
    lines = markdown.splitlines()
    start = None
    for index, line in enumerate(lines):
        heading = _issue_line_title(line)
        if not heading:
            continue
        if _norm(heading) == target or target in _norm(heading) or _norm(heading) in target:
            start = index
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^#{1,3} ", lines[index]):
            end = index
            break
        if _issue_line_title(lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def _check_analysis_binding(markdown: str, signals: list[dict], result: QualityResult) -> None:
    analysis_titles: dict[str, set[str]] = {}
    for signal in signals:
        key = deep_analysis_binding(signal)
        if not key:
            continue
        title = _title(signal.get("title"))
        body = _issue_region(markdown, title)
        match = re.search(
            r"(?ims)^\s*(?:#{4,5}\s+)?Deep Analysis\s*$\n(.+?)(?=^\s*Sources\s*$|^\s*</details>\s*$|\Z)",
            body,
        )
        if not match:
            result.analysis_binding_failures.append(_major_id(signal) + ":missing")
            continue
        analysis = _clean_text(match.group(1), 4000)
        tokens = _distinctive_tokens(title)
        if not key.startswith("reader:") and tokens and not tokens.intersection(_distinctive_tokens(analysis)):
            result.analysis_binding_failures.append(_major_id(signal) + ":unbound")
        analysis_key = re.sub(r"\s+", " ", analysis).casefold()
        analysis_titles.setdefault(analysis_key, set()).add(_norm(title))
    result.repeated_analysis.extend(text[:120] for text, titles in analysis_titles.items() if len(titles) > 1)
    if result.analysis_binding_failures:
        result.errors.append("FAIL — EVENT_ANALYSIS_BINDING")
    if result.repeated_analysis and "FAIL — REUSED_ANALYSIS" not in result.errors:
        result.errors.append("FAIL — REUSED_ANALYSIS")


def _check_layout(markdown: str, result: QualityResult) -> None:
    layout_errors = _new_layout_contract(markdown)
    if layout_errors:
        result.errors.extend("FAIL — READER_LAYOUT:" + item for item in layout_errors)


def _check_detail_depth(
    blocks: list[tuple[str, str]],
    result: QualityResult,
    signals: Iterable[dict] | None = None,
) -> None:
    """Check Details presence and summary duplication without prose quotas."""
    for title, body in blocks:
        match = re.search(r"(?is)<summary>(?:▶\s*Details|세부 내용\s*[·・]\s*출처)</summary>(.*?)(?:^\s*(?:\*\*)?(?:Sources|출처)(?:\*\*)?\s*:?[^\n]*(?:\n|$)|^\s*</details>)", body, re.M)
        if not match:
            result.generic_failures.append(title + ":DETAIL_MISSING")
            continue
        detail = re.sub(r"\[[^]]+\]\([^)]*\)", "", match.group(1))
        detail = re.sub(r"\s+", " ", detail).strip()
        if not detail:
            result.generic_failures.append(title + ":DETAIL_MISSING")
        visible = body.split("<details>", 1)[0].splitlines()
        summaries = [
            line.strip()
            for line in visible
            if line.strip() and not line.strip().startswith(("<", "- [")) and line.strip() != "Sources"
        ]
        if summaries and _detail_repeats_summary(detail, tuple(summaries)):
            result.generic_failures.append(title + ":DETAIL_SUMMARY_DUPLICATE")

    if result.generic_failures and "FAIL — READER_CONTENT_INTEGRITY" not in result.errors:
        result.errors.append("FAIL — READER_CONTENT_INTEGRITY")


def _check_content(signals: list[dict], blocks: list[tuple[str, str]], result: QualityResult) -> None:
    result.rendered_issue_count = len(blocks)
    result.rendered_issue_titles = [title for title, _ in blocks]
    if len(blocks) != len(signals):
        result.errors.append(f"FAIL — CONTENT_LOSS:rendered={len(blocks)}:signals={len(signals)}")
        return
    rendered = {_norm(title) for title, _ in blocks}
    missing = [_major_id(signal) for signal in signals if _norm(_title(signal.get("title"))) not in rendered]
    if missing:
        result.errors.append("FAIL — CONTENT_LOSS:" + ",".join(missing[:10]))


def _check_summary(markdown: str, signals: list[dict], result: QualityResult) -> None:
    match = re.search(r"^## 00\. 오늘의 한눈에 보기\n(.+?)(?=^## 01\. |\Z)", markdown, re.M | re.S)
    if not match or len(signals) < 5:
        return
    signal_by_title = {_norm(_title(signal.get("title"))): signal for signal in signals}
    topics: set[str] = set()
    for line in match.group(1).splitlines():
        if not line.startswith("- **"):
            continue
        title = line[4:].split("**", 1)[0]
        signal = signal_by_title.get(_norm(title))
        if signal:
            topics.add(str(signal.get("primary_topic") or signal.get("topic_section") or ""))
    if len(topics) < min(3, len({str(s.get('primary_topic') or s.get('topic_section') or '') for s in signals})):
        result.errors.append("FAIL — SUMMARY_SOURCE_VOLUME_BIAS")


def _check_continuity(signals: list[dict], prior_major: Iterable[dict] | None, result: QualityResult) -> None:
    current_urls = set()
    for signal in signals:
        urls = [signal.get("url")]
        urls.extend(item.get("url") for item in (signal.get("supporting_sources") or []) if isinstance(item, dict))
        current_urls.update(str(url).rstrip("/").casefold() for url in urls if url)
    current_titles = {_norm(signal.get("title")) for signal in signals}
    for prior in prior_major or []:
        title = _title(prior.get("title") or prior.get("canonical_title"))
        prior_urls = [prior.get("url"), prior.get("source_url")]
        prior_urls.extend(prior.get("source_urls") or [])
        urls = {str(url).rstrip("/").casefold() for url in prior_urls if url}
        if (_norm(title) not in current_titles) and not (urls & current_urls):
            result.continuity_loss.append(title)
    if result.continuity_loss:
        result.errors.append("FAIL — CONTINUITY_LOSS")


def audit_reader(markdown: str, signals: list[dict], *, prior_major: Iterable[dict] | None = None) -> QualityResult:
    result = QualityResult()
    blocks = _issue_blocks(markdown)
    _check_raw_source(markdown, result)
    _check_generic_and_reuse(blocks, result)
    _check_classification(signals, result)
    _check_analysis_binding(markdown, signals, result)
    _check_layout(markdown, result)
    _check_detail_depth(blocks, result, signals)
    _check_content(signals, blocks, result)
    _check_summary(markdown, signals, result)
    _check_continuity(signals, prior_major, result)
    result.passed = not result.errors
    return result


def quality_manifest(result: QualityResult) -> dict:
    return {
        "passed": result.passed,
        "errors": result.errors,
        "warnings": result.warnings,
        "raw_source_leaks": result.raw_source_leaks,
        "reader_content_integrity_failures": result.generic_failures,
        "misclassified_events": result.misclassified,
        "event_analysis_binding_failures": result.analysis_binding_failures,
        "reused_analysis_blocks": result.repeated_analysis,
        "continuity_loss": result.continuity_loss,
        "rendered_issue_count": result.rendered_issue_count,
    }
