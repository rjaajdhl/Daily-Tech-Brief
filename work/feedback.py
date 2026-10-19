"""Persistent reader-feedback IDs and preference learning.

Checkboxes are opt-in positive signals. An unchecked issue is absent from the
observation set and is never treated as a negative vote.
"""
from __future__ import annotations
import os

import datetime as dt
import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import urlparse, urlunparse

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
FEEDBACK_DIR = ROOT / "data" / "feedback"
ISSUE_LEDGER_PATH = FEEDBACK_DIR / "issue_ids.json"
PREFERENCE_LEDGER_PATH = FEEDBACK_DIR / "preference_ledger.json"
LEGACY_ID_RE = re.compile(r"^####\s+(\d{2}-\d{3,})\s+\\?\[([ xX])\\?\]\s+(.+?)\s*$", re.M)
TODO_ID_RE = re.compile(r"^-\s+\\?\[([ xX])\\?\]\s+(\d{2}-\d{3,})\s+(.+?)\s*$", re.M)


def _load(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path: Path, value: dict) -> None:
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _norm(value: object) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"\[([ x])\]", " ", text)
    text = re.sub(r"^\d{2}-\d{3,}\s+", "", text)
    text = re.sub(r"[^a-z0-9가-힣]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _canonical_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw.startswith(("http://", "https://")):
        return ""
    parsed = urlparse(raw)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.casefold(), parsed.netloc.casefold(), path, "", "", ""))


def _urls(signal: dict) -> list[str]:
    values = [signal.get("url"), signal.get("source_url")]
    for source in signal.get("supporting_sources") or signal.get("all_supporting_sources") or []:
        if isinstance(source, dict):
            values.append(source.get("url"))
    return list(dict.fromkeys(url for url in (_canonical_url(x) for x in values) if url))


def stable_issue_key(signal: dict) -> str:
    """Use source identity first; event IDs are the fallback for title-only items."""
    urls = _urls(signal)
    if urls:
        return "url:" + urls[0]
    event_id = str(signal.get("event_id") or signal.get("inventory_id") or "").strip()
    if event_id:
        return "event:" + event_id
    title = _norm(signal.get("canonical_title") or signal.get("title") or signal.get("source_title"))
    source = _norm(signal.get("source_id"))
    return "title:" + hashlib.sha256((title + "|" + source).encode("utf-8")).hexdigest()[:24]


KNOWN_TERMS = (
    "MCP", "CodeQL", "Kubernetes", "Kubelet", "NVLink", "HBM", "CUDA", "PyTorch",
    "V8", "Chrome", "Chromium", "GitHub", "Hugging Face", "SQLite", "NL2SQL",
    "Active Inference", "prompt injection", "RAG", "Docker", "KVM", "CVE", "ARC-AGI",
    "Blackwell", "Grace", "TPU", "GPU", "LLM", "agent", "memory", "diffusion",
)
KNOWN_ENTITIES = (
    "OpenAI", "Anthropic", "Google", "DeepMind", "Meta", "Microsoft", "AWS", "Amazon",
    "NVIDIA", "AMD", "Intel", "Broadcom", "TSMC", "Hugging Face", "xAI", "Mistral",
    "Kubernetes", "GitHub", "CISA", "Cloudflare", "ICEYE", "Nscale", "OpenLedger",
)


def _terms(text: str, values: tuple[str, ...], limit: int = 8) -> list[str]:
    found: list[str] = []
    for value in values:
        if re.search(re.escape(value), text, flags=re.I) and value.casefold() not in {x.casefold() for x in found}:
            found.append(value)
    for token in re.findall(r"\b[A-Z][A-Za-z0-9+./_-]{2,}\b", text):
        if token.casefold() not in {x.casefold() for x in found} and token.casefold() not in {"The", "This", "What", "Current", "Readme"}:
            found.append(token)
    return found[:limit]


def feature_values(signal: dict) -> dict[str, list[str]]:
    title = str(signal.get("title") or signal.get("canonical_title") or signal.get("source_title") or "")
    text = " ".join(
        str(signal.get(key) or "")
        for key in ("title", "canonical_title", "source_title", "source_description", "source_facts", "subtopic")
    )
    primary = str(signal.get("primary_topic") or signal.get("topic_section") or "").strip() or "unknown"
    subtopic = str(signal.get("subtopic") or "").strip() or "unknown"
    source_id = str(signal.get("source_id") or "").casefold()
    urls = " ".join(_urls(signal)).casefold()
    if "arxiv" in source_id or "arxiv.org" in urls or "paper" in source_id or primary == "research":
        issue_type = "research"
    elif "github" in source_id or "github.com" in urls:
        issue_type = "repository"
    elif "reddit" in source_id or "hacker" in source_id or primary == "community":
        issue_type = "community_signal"
    elif primary == "security" or re.search(r"\bCVE[- ]?\d|exploit|vulnerability|ransomware|prompt injection", text, re.I):
        issue_type = "security_event"
    elif re.search(r"funding|financ|revenue|valuation|earnings|acquisition", text, re.I):
        issue_type = "business_change"
    else:
        issue_type = "product_or_platform_change"
    return {
        "category": [primary],
        "subtopic": [subtopic],
        "entities": _terms(text, KNOWN_ENTITIES, 8),
        "technologies": _terms(text, KNOWN_TERMS, 10),
        "issue_type": [issue_type],
        "research_domain": [subtopic] if primary == "research" else [],
        "security_class": [subtopic] if primary == "security" else [],
        "infrastructure_layer": [subtopic] if primary in {"infrastructure", "semiconductors"} else [],
        "title_hint": [title[:180]] if title else [],
    }


def _feature_keys(features: dict[str, list[str]]) -> list[str]:
    keys: list[str] = []
    for group, values in features.items():
        if group == "title_hint":
            continue
        for value in values:
            normalized = _norm(value)
            if normalized:
                keys.append(f"{group}:{normalized}")
    return keys


def _section_code(signal: dict) -> str:
    primary = str(signal.get("primary_topic") or signal.get("topic_section") or "").strip()
    return {
        "ai": "01", "security": "02", "local_models": "03", "infrastructure": "04",
        "semiconductors": "05", "developer_tools": "06", "research": "07",
        "outside_bubble": "08", "community": "09", "business": "10",
    }.get(primary, "10")


def assign_feedback_ids(day: str, signals: list[dict], checked_states: dict[str, str] | None = None) -> dict:
    """Assign immutable IDs; order changes never renumber existing issues."""
    ledger = _load(ISSUE_LEDGER_PATH, {"version": 1, "issues": {}, "dates": {}})
    issues = ledger.setdefault("issues", {})
    dates = ledger.setdefault("dates", {})
    used = {str(entry.get("feedback_id")) for entry in issues.values() if entry.get("feedback_id")}
    next_by_code: dict[str, int] = {}
    for feedback_id in used:
        match = re.match(r"^(\d{2})-(\d{3,})$", feedback_id)
        if match:
            next_by_code[match.group(1)] = max(next_by_code.get(match.group(1), 0), int(match.group(2)))
    date_map: dict[str, str] = {}
    checked_states = checked_states or {}
    for signal in signals:
        key = stable_issue_key(signal)
        if key in date_map:
            event_id = str(signal.get("event_id") or signal.get("inventory_id") or "").strip()
            key = f"{key}|event:{event_id}" if event_id else f"{key}|duplicate"
            suffix = 2
            while key in date_map:
                key = f"{key.rsplit('|duplicate', 1)[0]}|duplicate{suffix}"
                suffix += 1
        entry = issues.get(key) or {}
        feedback_id = str(entry.get("feedback_id") or "")
        if not re.fullmatch(r"\d{2}-\d{3,}", feedback_id):
            code = _section_code(signal)
            number = next_by_code.get(code, 0) + 1
            feedback_id = f"{code}-{number:03d}"
            while feedback_id in used:
                number += 1
                feedback_id = f"{code}-{number:03d}"
            next_by_code[code] = number
            used.add(feedback_id)
        features = feature_values(signal)
        signal["feedback_features"] = features
        entry.update({
            "feedback_id": feedback_id,
            "stable_issue_key": key,
            "first_seen_date": entry.get("first_seen_date") or day,
            "last_seen_date": day,
            "title": str(signal.get("title") or signal.get("canonical_title") or ""),
            "section_code": entry.get("section_code") or feedback_id[:2],
            "features": features,
        })
        issues[key] = entry
        date_map[key] = feedback_id
        signal["feedback_id"] = feedback_id
        signal["feedback_state"] = "[x]" if checked_states.get(feedback_id) == "[x]" else "[ ]"
    dates[day] = date_map
    ledger["version"] = 1
    _save(ISSUE_LEDGER_PATH, ledger)
    return ledger


def extract_feedback(markdown: str) -> list[dict]:
    matches: list[tuple[int, str, str, str]] = []
    for pattern, task_order in ((LEGACY_ID_RE, False), (TODO_ID_RE, True)):
        for match in pattern.finditer(markdown or ""):
            state, feedback_id, title = match.groups() if task_order else (match.group(2), match.group(1), match.group(3))
            matches.append((match.start(), feedback_id, state, title.strip()))
    return [
        {"feedback_id": feedback_id, "checked": state.casefold() == "x", "title": title}
        for _position, feedback_id, state, title in sorted(matches, key=lambda row: row[0])
    ]


def feedback_states(markdown: str) -> dict[str, str]:
    return {row["feedback_id"]: "[x]" for row in extract_feedback(markdown) if row["checked"]}


def _finalized_markdowns(target_date: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    evidence = ROOT / "data" / "evidence"
    if not evidence.exists():
        return result
    target = dt.date.fromisoformat(target_date)
    for path in sorted(evidence.glob("*/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md")):
        day = path.stem
        try:
            if dt.date.fromisoformat(day) >= target:
                continue
        except ValueError:
            continue
        readback = path.with_name(path.stem + ".notion-readback.json")
        is_final = False
        if readback.exists():
            try:
                meta = json.loads(readback.read_text(encoding="utf-8"))
                stage = str(meta.get("stage") or "").upper()
                status = str(meta.get("brief_status") or meta.get("status") or "").casefold()
                is_final = stage == "FINAL" and status in {"published", "final"}
            except json.JSONDecodeError:
                pass
        if is_final:
            result.append((day, path.read_text(encoding="utf-8", errors="replace")))
    return result


def update_preference_ledger(target_date: str, additional_markdowns: dict[str, str] | None = None) -> dict:
    """Ingest only checked IDs from prior finalized briefs and recompute scores."""
    pref = _load(PREFERENCE_LEDGER_PATH, {"version": 1, "observations": [], "checked_ids_by_date": {}, "feature_scores": {}, "feature_counts": {}})
    issue_ledger = _load(ISSUE_LEDGER_PATH, {"issues": {}})
    issues = issue_ledger.get("issues", {})
    observations = {(str(row.get("date")), str(row.get("feedback_id"))): row for row in pref.get("observations", []) if row.get("date") and row.get("feedback_id")}
    checked_by_date: dict[str, list[str]] = {}
    sources = {day: markdown for day, markdown in _finalized_markdowns(target_date)}
    for day, markdown in (additional_markdowns or {}).items():
        sources[str(day)] = markdown
    for day, markdown in sorted(sources.items()):
        checked = [row for row in extract_feedback(markdown) if row["checked"]]
        checked_by_date[day] = sorted({row["feedback_id"] for row in checked})
        by_id = {str(entry.get("feedback_id")): entry for entry in issues.values()}
        for row in checked:
            entry = by_id.get(row["feedback_id"], {})
            observations[(day, row["feedback_id"])] = {
                "date": day,
                "feedback_id": row["feedback_id"],
                "title": row["title"],
                "features": entry.get("features") or {"title_hint": [row["title"][:180]]},
            }
    target = dt.date.fromisoformat(target_date)
    feature_scores: dict[str, float] = {}
    feature_counts: dict[str, int] = {}
    for observation in observations.values():
        try:
            age = max(0, (target - dt.date.fromisoformat(str(observation["date"]))).days)
        except ValueError:
            continue
        weight = math.exp(-math.log(2) * age / 21.0)
        for feature in _feature_keys(observation.get("features") or {}):
            feature_scores[feature] = feature_scores.get(feature, 0.0) + weight
            feature_counts[feature] = feature_counts.get(feature, 0) + 1
    pref.update({
        "version": 1,
        "updated_for_date": target_date,
        "observations": sorted(observations.values(), key=lambda row: (str(row.get("date")), str(row.get("feedback_id")))),
        "checked_ids_by_date": checked_by_date,
        "feature_scores": {key: round(value, 6) for key, value in sorted(feature_scores.items())},
        "feature_counts": dict(sorted(feature_counts.items())),
        "recency_half_life_days": 21,
        "unchecked_is_not_negative": True,
        "minimum_repeated_signals": 2,
    })
    _save(PREFERENCE_LEDGER_PATH, pref)
    return pref


def preference_score(signal: dict, preference_ledger: dict | None = None) -> float:
    pref = preference_ledger or _load(PREFERENCE_LEDGER_PATH, {})
    scores = pref.get("feature_scores") or {}
    counts = pref.get("feature_counts") or {}
    keys = _feature_keys(feature_values(signal))
    if not keys:
        return 0.0
    eligible = [float(scores.get(key, 0.0)) for key in keys if int(counts.get(key, 0)) >= 2]
    if not eligible:
        return 0.0
    # Repeated signals are required before a material effect; cap the boost so
    # preference can reorder/deepen content but cannot suppress or include it.
    return round(min(1.0, sum(eligible) / max(1, len(eligible) * 2.0)), 6)


def apply_preferences(target_date: str, signals: list[dict], additional_markdowns: dict[str, str] | None = None) -> dict:
    pref = update_preference_ledger(target_date, additional_markdowns=additional_markdowns)
    for signal in signals:
        score = preference_score(signal, pref)
        signal["feedback_rank_score"] = score
        signal["feedback_deep_priority"] = score if score >= 0.25 else 0.0
        signal["feedback_enrichment_priority"] = score
    return pref


def enrichment_priority(row: dict, preference_ledger: dict | None = None) -> float:
    return preference_score(row, preference_ledger)
