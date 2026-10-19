"""Reader-facing Daily Brief contract helpers.

This module is deliberately network-free.  The cron agents and the Notion
read-back utility use it to keep internal research telemetry out of the
reader-facing Markdown while preserving that telemetry in audit artifacts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# These belong in local audit artifacts, not in the reader-facing brief.
INTERNAL_MARKERS = (
    "SearXNG",
    "Crawl4AI",
    "search endpoint",
    "search.jsonl",
    "crawl.jsonl",
    "manifest.json",
    "Evidence Pack",
    "source registry",
    "disposition ledger",
    "coverage gate",
    "transport success",
    "transport failure",
    "HTTP status",
    "HTTP 200",
    "search_required",
    "manual_followup_required",
    "FAIL — SEARCH",
    "FAIL — EVIDENCE",
    "primary source가 부족",
    "원문 검수가 완료되지 않았다",
    "다음 실행에서 확인",
    "다음 실행에서 조사",
    "internal ledger",
    "cron/job",
    "cron job",
    "verified_count",
    "**Stage:**",
    "**Brief Status:**",
    "**Review Needed:**",
    "Stage=",
    "Brief Status=",
)

# These labels were useful to the internal ledger but make the reader-facing
# brief repetitive when emitted once per issue.  The writer must turn their
# meaning into natural prose or omit it when no reader action is required.
READER_TEMPLATE_MARKERS = (
    "**왜 중요한가:**",
    "**Evidence:**",
    "**Continuity:**",
    "**Priority:**",
    "**Sources:**",
    "**현재 판단:**",
    "**지금 할 일:**",
    "**다음에 확인할 것:**",
    "**Watch trigger:**",
)

# These exact phrases are acceptable in an internal Quality Note but not in
# the main reader-facing narrative.
PIPELINE_FAILURE_PHRASES = (
    "후보는 있었지만 검증 부족",
    "검색은 수행됨",
    "원문 검수 필요",
    "0 verified stories",
    "0 verified events",
)

# The old renderer used these two sentences as a universal placeholder.  They
# are not an acceptable explanation even when they appear without bold labels.
GENERIC_READER_BOILERPLATE = (
    "공개 자료에서 기술적으로 의미 있는 변화 또는 신호가 관찰됐다.",
    "이 이슈의 기술적 영향·현재 relevance·후속 변화를 판단할 근거가 있다.",
)

STATUS_LABELS = {
    "VERIFIED",
    "DEVELOPING",
    "UNVERIFIED",
    "SPECULATIVE",
    "DISMISSED",
}
CONTINUITY_LABELS = {"NEW", "DEVELOPING", "ONGOING", "WATCH"}


@dataclass
class ContractResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    story_count: int = 0
    content_sections: list[str] = field(default_factory=list)
    internal_leaks: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _section_blocks(markdown: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^[ \t]*## (?!#)([^\n]+)\n", markdown, re.M))
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        blocks.append((match.group(1).strip(), markdown[match.end() : end].strip()))
    return blocks


def internal_leaks(markdown: str) -> list[str]:
    lowered = markdown.lower()
    # Source URLs are required reader-facing provenance.  Project names that
    # happen to occur in a URL path must not be treated as prose telemetry;
    # keep link labels and surrounding narrative visible to the leak scanner.
    visible_lowered = re.sub(r"https?://[^ )]+", " ", lowered)
    found: list[str] = []
    if re.search(r"\bsrc_[a-f0-9]{8,}\b", visible_lowered, re.I):
        found.append("raw_source_id")
    for marker in INTERNAL_MARKERS + PIPELINE_FAILURE_PHRASES:
        if marker == "manifest.json":
            if re.search(r"(?:data/evidence|evidence pack|target[_ -]?date).{0,80}manifest[.]json", lowered):
                found.append(marker)
            continue
        if marker == "Crawl4AI":
            if re.search(
                r"crawl4ai.{0,80}(?:browser|engine|브라우저|엔진|crawler|collector|endpoint|manifest|crawl[.]jsonl|http status|telemetry|coverage|internal|failure)"
                r"|(?:browser|engine|브라우저|엔진|crawler|collector|endpoint|manifest|crawl[.]jsonl|http status|telemetry|coverage|internal|failure).{0,80}crawl4ai",
                visible_lowered,
            ):
                found.append(marker)
            continue
        if marker.lower() in visible_lowered:
            found.append(marker)
    return found


def reader_template_leaks(markdown: str) -> list[str]:
    found: list[str] = []
    for marker in READER_TEMPLATE_MARKERS:
        if marker in markdown:
            found.append(marker)
    return found


STRUCTURAL_SUBHEADINGS = {
    "daily glossary",
    "sources",
    "research notes",
    "my feedback",
    "what you should know today",
    "꼭 알아둘 소식",
    "알아두면 좋은 소식",
    "레이더",
    "confirmed / developing",
    "early radar / unverified",
    "주요 변화",
    "초기 신호",
    "핵심 이슈",
    "standard issue",
    "deep analysis",
}


def _issue_line_parts(line: str) -> tuple[str, str, str] | None:
    """Parse both the legacy text heading and the real task-list form."""
    legacy = re.fullmatch(r"####\s+(\d{2}-\d{3,})\s+\\?\[([ xX])\\?\]\s+(.+?)\s*", line)
    if legacy:
        return legacy.group(1), legacy.group(2), legacy.group(3).strip()
    task = re.fullmatch(r"-\s+\\?\[([ xX])\\?\]\s+(\d{2}-\d{3,})(?:\s+·)?\s+(.+?)\s*", line)
    if task:
        return task.group(2), task.group(1), task.group(3).strip()
    return None


def _issue_line_title(line: str) -> str | None:
    parts = _issue_line_parts(line.strip())
    if not parts:
        return None
    feedback_id, state, title = parts
    return f"{feedback_id} [{state.lower()}] {title}"


def _issue_blocks(markdown: str) -> list[tuple[str, str]]:
    """Return issue blocks for task-list issues and legacy headings."""
    lines = list(re.finditer(r"^.*(?:\n|$)", markdown, re.M))
    markers: list[tuple[int, int, str]] = []
    for line in lines:
        raw = line.group(0).rstrip("\n")
        title = _issue_line_title(raw)
        if not title and raw.startswith("#### ") and raw[5:].strip().casefold() != "deep analysis":
            title = raw[5:].strip()
        if title:
            markers.append((line.start(), line.end(), title))
    blocks: list[tuple[str, str]] = []
    for index, (_start, body_start, title) in enumerate(markers):
        end = markers[index + 1][0] if index + 1 < len(markers) else len(markdown)
        section = re.search(r"^#{1,3} \S.*$", markdown[body_start:end], re.M)
        if section:
            end = body_start + section.start()
        blocks.append((title, markdown[body_start:end].strip()))
    return blocks


def _story_count(markdown: str) -> int:
    return len(_issue_blocks(markdown))


def _feedback_contract(markdown: str) -> list[str]:
    errors: list[str] = []
    parsed: list[tuple[str, str, str]] = []
    for line in markdown.splitlines():
        parts = _issue_line_parts(line)
        if parts:
            parsed.append(parts)
    ids: list[str] = []
    for feedback_id, state, _title in parsed:
        ids.append(feedback_id)
        if state.casefold() not in {" ", "x"}:
            errors.append("feedback_checkbox_format:" + feedback_id)
    if len(ids) != len(set(ids)):
        errors.append("feedback_id_duplicate")
    return errors


def _new_layout_contract(markdown: str) -> list[str]:
    """Validate the compact issue/two-line/native-toggle Markdown shape."""
    errors: list[str] = []
    for index, (title, body) in enumerate(_issue_blocks(markdown), start=1):
        lines = body.splitlines()
        detail_index = next((i for i, line in enumerate(lines) if line.strip().casefold() == "<details>"), None)
        if detail_index is None:
            errors.append(f"missing_native_toggle:{index}")
            continue
        visible = [line.strip() for line in lines[:detail_index] if line.strip()]
        if not visible:
            errors.append(f"missing_reader_lead:{title}")
        issue_title = re.sub(r"^\d{2}-\d{3,}\s+\[[ xX]\]\s+", "", title)
        if re.match(r"^(?:NEW|DEVELOPING|ONGOING|WATCH|ACTION|CONFIRMED|EARLY RADAR|RESEARCH RADAR|COMMUNITY SIGNAL)\s*[—–:-]", issue_title, re.I):
            errors.append(f"workflow_status_prefix_visible:{title}")
        if not any(line.strip().casefold() == "</details>" for line in lines[detail_index + 1 :]):
            errors.append(f"toggle_not_closed:{title}")
        if not any(re.fullmatch(r"<summary>\s*(?:▶\s*Details|세부 내용\s*[·・]\s*출처)\s*</summary>", line.strip(), re.I) for line in lines[detail_index :]):
            errors.append(f"missing_details_summary:{title}")
        detail_text = "\n".join(lines[detail_index:])
        if not re.search(r"(?im)^\s*(?:\*\*)?(?:Sources|출처)(?:\*\*)?\s*:?[ \t]*(?:$|\[)", detail_text):
            errors.append(f"missing_issue_sources:{title}")
    return errors


def validate_reader_markdown(
    markdown: str,
    *,
    require_substantive: bool = True,
    require_todo_checkboxes: bool = False,
) -> ContractResult:
    result = ContractResult()
    if not markdown.strip():
        result.errors.append("empty_markdown")
        return result

    if not re.search(r"^# .+\d{4}-\d{2}-\d{2}", markdown, re.M):
        result.errors.append("missing_dated_title")
    if not re.search(r"^## (?:00\. )?(?:오늘의 한눈에 보기|오늘 꼭 볼 이슈|What You Should Know Today)", markdown, re.M | re.I):
        result.errors.append("missing_top_issue_section")
    has_global_sources = bool(re.search(r"^## (?:Sources|출처)", markdown, re.M | re.I))
    has_issue_sources = bool(re.search(r"(?im)^\s*(?:\*\*)?(?:Sources|출처)(?:\*\*)?\s*:?[ \t]*(?:$|\[)", markdown))
    if not has_global_sources and not has_issue_sources:
        result.errors.append("missing_sources_section")
    feedback_count = len(re.findall(r"(?m)^#{1,2}\s+(?:My Feedback / Follow-up|피드백 / 후속 요청|내 피드백 / 후속 요청)\s*$", markdown))
    if feedback_count != 1:
        result.errors.append("feedback_section_count_not_one")

    blocks = _section_blocks(markdown)
    result.content_sections = [name for name, body in blocks if body and not re.fullmatch(r"(?:Sources|출처|Daily Glossary|Research Notes|일일 용어)", name, re.I)]
    result.story_count = _story_count(markdown)
    result.errors.extend(_feedback_contract(markdown))
    todo_count = len(re.findall(r"^[ \t]*-\s+\[[ xX]\]\s+\d{2}-\d{3,}\s+", markdown, re.M))
    if require_todo_checkboxes and todo_count != result.story_count:
        result.errors.append(f"feedback_requires_markdown_todo:todo={todo_count}:stories={result.story_count}")
    if require_todo_checkboxes:
        result.errors.extend(_new_layout_contract(markdown))
    result.internal_leaks = internal_leaks(markdown)
    if result.internal_leaks:
        result.errors.append("reader_internal_leak:" + ",".join(result.internal_leaks))
    generic_leaks = [marker for marker in GENERIC_READER_BOILERPLATE if marker in markdown]
    if generic_leaks:
        result.errors.append("generic_reader_boilerplate:" + ",".join(generic_leaks))
    template_leaks = reader_template_leaks(markdown)
    if template_leaks:
        result.errors.append("reader_repetitive_template:" + ",".join(template_leaks))

    # Dynamic subtopics are mandatory wherever a numbered topic contains
    # issues; the old two-bucket headings are explicitly rejected.
    if "### 주요 변화" in markdown or "### 초기 신호" in markdown:
        result.errors.append("legacy_fixed_subtopic_bucket")
    for name, body in blocks:
        if re.match(r"^\d{2}\.\s", name) and body:
            if (re.search(r"^[ \t]*#### ", body, re.M) or re.search(r"^[ \t]*-\s+\[[ xX]\]\s+\d{2}-\d{3,}\s+", body, re.M)) and not re.search(r"^[ \t]*### .+", body, re.M):
                result.errors.append("missing_dynamic_subtopic:" + name)
    issue_bodies = [body for _title, body in _issue_blocks(markdown)]
    if any("원문 본문을 확인하지 못" in body and len(body) > 800 for body in issue_bodies):
        result.errors.append("title_only_item_overwritten_with_long_prose")
    if require_substantive:
        if result.story_count == 0:
            result.errors.append("no_substantive_story")
        if any(not body.strip() for _title, body in _issue_blocks(markdown)):
            result.errors.append("empty_substantive_story")
        if "후보" in markdown and "검증" in markdown and result.story_count == 0:
            result.errors.append("candidate_only_brief")

    # A reader-facing issue must expose its evidence state when it uses one of
    # the controlled labels.  This check is intentionally permissive for
    # prose that says "verified" in a source explanation.
    status_mentions = set(re.findall(r"\b(VERIFIED|DEVELOPING|UNVERIFIED|SPECULATIVE|DISMISSED)\b", markdown))
    unknown = status_mentions - STATUS_LABELS
    if unknown:
        result.errors.append("unknown_evidence_status:" + ",".join(sorted(unknown)))

    if "UNVERIFIED" in markdown and not re.search(r"Early Radar|Community Radar|커뮤니티|아직 검증 중", markdown, re.I):
        result.errors.append("unverified_not_in_radar_context")

    return result
