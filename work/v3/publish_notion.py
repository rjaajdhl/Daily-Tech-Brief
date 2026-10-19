"""Publish the v3 articles for one date to the existing Notion archive (Index + Section pages + Overview + Dashboard).

Reuses the production Notion machinery (scope guard, exact-date idempotency, resumable native block writer,
recursive read-back).  Only the content source changes: v3 `articles.jsonl` instead of the legacy template.

Usage: python work/v3/publish_notion.py --date 2026-09-27 [--dry-run]
Fail-closed: the Index/Sections stay DRAFT until every section passes native read-back.
"""
from __future__ import annotations
import os

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "work" / "v3"))

import llm  # noqa: E402
import pipeline as p  # noqa: E402
import followup_section as FS  # noqa: E402
from work import daily_finalizer as F  # noqa: E402
from work.daily_brief_sections import resolve_section_page  # noqa: E402
from work.issue_taxonomy import TOPIC_SECTIONS as _TOPIC_SECTIONS  # noqa: E402
from work.notion_block_writer import markdown_blocks, _block_plain as plain  # noqa: E402

from work.scope_guard import register_page  # noqa: E402

# Section 11 collects drafts that still failed the source check after every rewrite. It exists only in the v3 publisher.
REVIEW_FAILED_KEY = "review_failed"
# English display names are local to v3. Shared taxonomy keys and legacy renderers remain unchanged.
_ENGLISH_TOPICS = ("AI, Agents & Frontier Models", "Cybersecurity & Offensive Security", "Local & Open Models",
                   "Infrastructure, Cloud & Compute", "Semiconductors & AI Infrastructure", "Developer Tools, Open Source & GitHub",
                   "Research, Papers & Benchmarks", "Robotics, Applied Technology & Open Discovery",
                   "Community Signals & Early Radar", "Technology Business & Governance")
TOPIC_SECTIONS = (*( (f"{i:02}. {label}", key) for i, (label, (_, key)) in
                    enumerate(zip(_ENGLISH_TOPICS, _TOPIC_SECTIONS), 1)), ("11. Review Failed", REVIEW_FAILED_KEY))
SECTION_LABELS = {name.split(".", 1)[0]: name.split(". ", 1)[1] for name, _ in TOPIC_SECTIONS}
TAG = re.compile(r"\s*\[S\d+\](?:\[S\d+\])*")
TAGID = re.compile(r"\[S(\d+)\]")
SEC_CODE_TO_KEY = {name.split(".", 1)[0]: key for name, key in TOPIC_SECTIONS}
KEY_TO_NAME = {key: name for name, key in TOPIC_SECTIONS}
ID_REGISTRY = ROOT / "data" / "v3" / "feedback_ids.json"


def clean(text: str) -> str:
    return TAG.sub("", text or "").strip()


def build_brief_index_markdown(day: str, full_markdown: str, section_links: list[dict], *, data_day: str | None = None,
                               followup_url: str | None = None) -> str:
    """English Index for new v3 briefs; no mutation or translation of legacy pages."""
    match = re.search(r"(?ms)^## 00\. (?:At a glance|오늘의 한눈에 보기)\n(.*?)(?=^<details>\s*$|^## Sources\s*$|\Z)", full_markdown)
    summary = match.group(1).strip() if match else "Open the topic sections below for the complete brief."
    lines = [f"# Daily Tech Brief — {day}",
             f"Published: {day} · Evidence date: {data_day or day} (America/Los_Angeles)", "",
             "## 00. At a glance", summary, "", "## Topic Sections",
             "Open a section for its complete articles, technical details and sources."]
    for item in section_links:
        name = str(item.get("name") or item.get("key") or "Section")
        url = str(item.get("url") or "").strip()
        count = int(item.get("issue_count") or 0)
        label = f"[{name}]({url})" if url.startswith("http") else name
        lines.append(f"- {label} — {count} issues")
    if followup_url and followup_url.startswith("http"):
        lines.append(f"- [{FS.NAME}]({followup_url}) — answers to your follow-up requests")
    # This placeholder is already recognized by the feedback collector; never treat assistant instructions as user feedback.
    lines.extend(["", "## My Feedback / Follow-up", "Leave feedback here.", ""])
    return "\n".join(lines)


def load_rows(date: str) -> list[dict]:
    selections = p.jl_read(p.V3 / date / "selection.jsonl")
    sel = {r["event_id"]: r for r in selections if r.get("selected") is True}
    if len(sel) != sum(r.get("selected") is True for r in selections):
        print("DUPLICATE_SELECTION_ROWS_COLLAPSED", flush=True)  # the dict above already keeps one row per event
    if not sel:
        print("EMPTY_SELECTION: publishing a 0-article brief", flush=True)
        return []
    articles = p.jl_read(p.V3 / date / "articles.jsonl")
    ev = {e["event_id"]: e for e in p.load_events()}
    rows, errors = [], []
    for eid, selection in sel.items():
        candidates = [r for r in articles if r.get("event_id") == eid]
        if len(candidates) != 1 or eid not in ev:
            errors.append(f"{eid}:article_count={len(candidates)}:current_event={eid in ev}")
            continue
        r = dict(candidates[0])
        if "_error" in r or "_error" in selection or "score error" in str(selection.get("reason", "")):
            errors.append(f"{eid}:error")
        elif r.get("action") == "skip" and str(r.get("reason") or r.get("skip_reason") or "").strip():
            continue  # Explicit, reasoned skips are resolved, not publishable.
        elif r.get("action") != "publish":
            errors.append(f"{eid}:unresolved_action")
        else:
            r["_score"] = selection.get("total") or 0
            r["_event"] = ev[eid]
            rows.append(r)
    # One unresolved article never stops the day: it is reported here and left out (content_ready normally quarantines it first).
    for err in errors:
        print("EXCLUDED_AT_LOAD", err, flush=True)
    if not rows:
        print("NO_PUBLISHABLE_ARTICLES: publishing a 0-article brief", flush=True)
    return rows


def assign_ids(rows: list[dict]) -> None:
    registry_path = p.V3 / "feedback_ids.json"
    reg = json.loads(registry_path.read_text()) if registry_path.exists() else {}
    used = Counter()
    for v in reg.values():
        used[v.split("-")[0]] = max(used[v.split("-")[0]], int(v.split("-")[1]))
    for r in sorted(rows, key=lambda r: (r.get("section") or "10", -r["_score"], r["event_id"])):
        code = r.get("section") if r.get("section") in SEC_CODE_TO_KEY else "10"
        r["_code"] = code
        if r["event_id"] not in reg:
            used[code] += 1
            reg[r["event_id"]] = f"{code}-{used[code]:03d}"
        r["feedback_id"] = reg[r["event_id"]]
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(reg, ensure_ascii=False, indent=1, sort_keys=True))


def review_notes(r: dict) -> list[str]:
    """One plain sentence per leftover problem: the reviewer's first sentence names the problem, the rest is an edit instruction."""
    out = []
    for reason in r.get("review_failed_reasons") or []:
        text = re.sub(r"\s+", " ", str(reason)).strip()
        m = re.search(r"(?<=[다요음])\.(?=\s|$)", text)
        first = text[:m.start() + 1] if m else text
        out.append(first[:300])
    return out


def issue_lines(r: dict) -> list[str]:
    """One checkbox owns the whole issue: lead, details, glossary and sources are all its indented children."""
    cited = sorted({int(t) for t in TAGID.findall((r.get("lead") or "") + (r.get("body") or ""))})
    srcs = [s for s in r["sources"] if int(s["tag"][1:]) in cited] or r["sources"][:1]
    lines = [f"- [ ] {r['feedback_id']} · {clean(r['headline'])}"]
    notes = review_notes(r) if r.get("review_failed") else []
    if notes:
        orig = r.get("section_original")
        where = f"Original section {orig} {SECTION_LABELS.get(orig, '')}".strip() if orig else "Original section unknown"
        lines.append(f"\tReview Failed · {where} · {len(notes)} unresolved source-check issues (listed at the end of Details)")
    lines += ["\t" + clean(r["lead"]), "\t", "\t<details>", "\t<summary>▶ Details</summary>"]
    for para in (r.get("body") or "").split("\n\n"):
        para = clean(para)
        if para:
            for ln in para.split("\n"):
                if ln.strip():
                    lines.append("\t\t" + ln.strip())
            lines.append("\t\t")
    gl = r.get("glossary") or []
    if gl:
        lines.append("\t\tGlossary")
        lines += [f"\t\t- {g['term']}: {g['definition']}" for g in gl]
        lines.append("\t\t")
    lines.append("\t\tSources")
    seen = set()
    for s in srcs:
        if s["url"] in seen:
            continue
        seen.add(s["url"])
        host = s.get("domain") or re.sub(r"^https?://(www\.)?([^/]+).*$", r"\2", s["url"])  # a source without `domain` still renders
        title = re.sub(r"\s+", " ", (s.get("title") or host).replace("[", "(").replace("]", ")")).strip()[:120]
        lines.append(f"\t\t- [{host} · {title}]({s['url']})")
    if notes:
        lines += ["\t\t", "\t\tRemaining review issues"] + [f"\t\t- {n}" for n in notes]
    lines += ["\t</details>", ""]
    return lines


def section_markdown(day: str, name: str, key: str, rows: list[dict], *, data_day: str | None = None) -> str:
    """Flat heading; one checkbox owns the lead and nested Details."""
    lines = [f"# {name}", f"Published: {day} · Evidence date: {data_day or day} · {len(rows)} issues", ""]
    if not rows:
        lines += ["No Review Failed articles." if key == REVIEW_FAILED_KEY else "No articles passed the publication checks.", ""]
    for r in sorted(rows, key=lambda r: (-r["_score"], r["feedback_id"])):
        lines += issue_lines(r)
    return "\n".join(lines)


SUMMARY_SYS = """Write the opening overview of a Daily Tech Brief in natural professional English.
The input contains the section, headline and lead of each published article. The reader follows AI, agents, cybersecurity and infrastructure.
Use only facts present in those articles. Let concrete developments carry the summary; avoid generic lessons, filler and mechanical source-by-source retelling.
- conclusion: identify important developments with concrete subjects and events.
- changes: list changes actually supported by the articles. Do not add items to meet a count or length target.
- why: state the concrete significance relevant to this reader, without inventing implications or weakening source claims.
Keep the publication/display date distinct from the source evidence date. Older evidence is not a new announcement merely because the brief is published today.
Return JSON only: {"conclusion":"...","changes":["..."],"why":"..."}"""


SUMMARY_REVIEW_SYS = """Independently review the English overview of a technical brief against the supplied article headlines and leads.
Identify unsupported claims, incorrect scope or attribution, and confusion between source evidence dates and the publication date.
Assess unnatural wording, generic lessons and repetition in context, not by fixed sentence, item, character, word-count or language-ratio rules.
Use only the supplied facts. Do not demand unsupported additions or rewrite the summary yourself.
Return JSON only: {"ok":true|false,"issues":["specific issue and supporting evidence"]}."""


def summary_numbers_supported(text: str, listing: str, extra_values=()) -> bool:
    values = p._source_values(listing) | {v for _, v in p._claim_values(listing)} | set(extra_values)
    return all(p._value_supported(v, values) for _, v in p._claim_values(text))


def summary_block(day: str, rows: list[dict], n_sections: int, *, cache_dir: Path | None = None, data_day: str | None = None) -> str:
    data_day = data_day or day
    content = [{k: r.get(k) for k in ("event_id", "headline", "lead", "body", "glossary", "sources", "_code", "_score", "quality_ok", "validation")}
               for r in sorted(rows, key=lambda r: r["event_id"])]
    source = json.dumps({"display_day": day, "evidence_day": data_day, "sections": n_sections, "rows": content,
                         "system": SUMMARY_SYS, "review_system": SUMMARY_REVIEW_SYS}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(source.encode()).hexdigest()
    cache = Path(cache_dir or (p.V3 / "notion_summary_cache")) / (digest + ".json")
    if cache.exists():
        entry = json.loads(cache.read_text())
        summary = entry.get("summary")
        if (entry.get("input_sha256") != digest or not isinstance(summary, str) or entry.get("summary_sha256") != hashlib.sha256(summary.encode()).hexdigest()
                or (entry.get("quality") or {}).get("ok") is not True):
            raise RuntimeError("summary_cache_integrity_failed")
        return summary  # Exact bytes; never re-render or ask Luna to rewrite cached prose.
    listing = "\n".join(f"[{r['_code']}] {clean(r['headline'])} — {clean(r['lead'])}" for r in sorted(rows, key=lambda r: (r["_code"], -r["_score"])))
    base_prompt = f"DISPLAY_DATE: {day}\nSOURCE_EVIDENCE_DATE: {data_day}\nDo not confuse the publication/display date with the evidence date.\n\n{listing}"
    draft_path = cache.parent / "drafts" / (digest + ".json")
    saved = json.loads(draft_path.read_text()) if draft_path.exists() else {}
    feedback = saved.get("issues", []) if saved.get("input_sha256") == digest else []
    out = saved.get("out") if saved.get("input_sha256") == digest else None
    feedback_path = p.V3 / data_day / "summary_feedback.json"
    if not feedback and feedback_path.exists():
        feedback = json.loads(feedback_path.read_text()).get("issues", [])
    for attempt in range(5):  # a reviewer can fixate on one phrase; keep the same editorial bar but allow more repair rounds
        prompt = base_prompt
        if feedback:
            prompt += "\n\nPREVIOUS DRAFT:\n" + json.dumps(out, ensure_ascii=False) + "\nFIX THESE SOURCE/EDITORIAL ISSUES (do not invent facts):\n- " + "\n- ".join(feedback)
        try:
            out = llm.call_json(SUMMARY_SYS, prompt, timeout=600)
        except Exception as exc:
            prefix = "summary_quality_failed:editorial_review:" + json.dumps(feedback, ensure_ascii=False) if feedback else "summary_generation_failed:"
            raise RuntimeError(prefix + str(exc)) from exc
        feedback = []
        if not isinstance(out, dict) or "_error" in out:
            raise RuntimeError("summary_generation_failed")
        fields = [out.get("conclusion"), out.get("why")]
        changes = out.get("changes")
        if not isinstance(changes, list) or not changes:
            feedback.append("changes must be a nonempty list of developments supported by the articles.")
            changes = []
        fields += changes
        if any(not isinstance(v, str) or not v.strip() or TAGID.search(v) for v in fields):
            feedback.append("Return nonempty text fields and remove internal source tags.")
        if not feedback and not summary_numbers_supported(" ".join(fields), listing, (len(rows), n_sections)):
            feedback.append("Remove numeric claims absent from the article headlines/leads, or use the supported value with its original meaning and unit.")
        if not feedback:
            try:
                review = llm.call_json(SUMMARY_REVIEW_SYS, f"DISPLAY_DATE: {day}\nSOURCE_EVIDENCE_DATE: {data_day}\nARTICLES:\n{listing}\n\nSUMMARY:\n" + json.dumps(out, ensure_ascii=False), timeout=600)
            except Exception as exc:
                raise RuntimeError("summary_generation_failed:review_unavailable:" + str(exc)) from exc
            if not isinstance(review, dict) or "_error" in review or not isinstance(review.get("issues"), list):
                raise RuntimeError("summary_generation_failed:review_unavailable")
            feedback = [str(x) for x in review["issues"]]
            if review.get("ok") is not True and not feedback:
                feedback = ["The summary did not pass source-based review. Use only supported facts."]
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(json.dumps({"input_sha256": digest, "out": out, "issues": feedback, "approved": not feedback}, ensure_ascii=False, indent=2))
        if not feedback:
            break
    if feedback:
        raise RuntimeError("summary_quality_failed:editorial_review:" + json.dumps(feedback, ensure_ascii=False))
    lines = [f"Today's brief: {out['conclusion'].strip()}", "", "Key developments:"]
    lines += [f"- {c.strip()}" for c in changes]
    lines += ["", f"Why it matters: {out['why'].strip()}", "", f"Coverage: {len(rows)} published articles across {n_sections} sections. Evidence date: {data_day}; publication date: {day}. Open the section pages below for details and sources."]
    summary = "\n".join(lines)
    cache.parent.mkdir(parents=True, exist_ok=True)
    entry = {"input_sha256": digest, "summary_sha256": hashlib.sha256(summary.encode()).hexdigest(), "summary": summary,
             "quality": {"ok": True, "checks": ["nonempty_fields", "source_tags_removed", "supported_numbers", "changes_shape"], "editorial_review": review}}
    temporary = cache.with_suffix(".tmp")
    temporary.write_text(json.dumps(entry, ensure_ascii=False, indent=2))
    temporary.replace(cache)
    return summary


def read_native_tree(request, headers: dict, parent_id: str, *, _ancestors=()) -> list[dict]:
    """Read every child page at every depth; malformed/failed pagination is fatal."""
    if parent_id in _ancestors or len(_ancestors) > 100:
        raise RuntimeError("children_cycle_or_depth_limit")
    result, cursor, seen = [], None, set()
    for _ in range(1000):
        url = f"https://api.notion.com/v1/blocks/{parent_id}/children?page_size=100"
        if cursor:
            from urllib.parse import quote
            url += "&start_cursor=" + quote(str(cursor), safe="")
        st, body = request(url, "GET", headers)
        if st != 200 or not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise RuntimeError(f"children_read_failed:{parent_id}:{st}")
        for block in body["results"]:
            import copy
            block = copy.deepcopy(block)
            kind = block.get("type")
            if block.get("has_children"):
                if not block.get("id"):
                    raise RuntimeError("children_id_missing")
                block[kind]["children"] = read_native_tree(request, headers, block["id"], _ancestors=(*_ancestors, parent_id))
            result.append(block)
        if not body.get("has_more"):
            return result
        cursor = body.get("next_cursor")
        if not cursor or cursor in seen:
            raise RuntimeError("children_pagination_invalid")
        seen.add(cursor)
    raise RuntimeError("children_pagination_limit")


def walk_tree(tree):
    for block in tree:
        yield block
        yield from walk_tree((block.get(block.get("type")) or {}).get("children") or [])


def checked_ids_from_tree(tree) -> set[str]:
    return {m.group(1) for b in walk_tree(tree)
            if b.get("type") == "to_do" and b["to_do"].get("checked")
            and (m := re.match(r"(\d{2}-\d{3,})\b", plain(b)))}


def _canon_url(u):
    """Notion rewrites a bare-host URL (https://x.im) to https://x.im/ on save; treat both as the same link."""
    from urllib.parse import urlsplit, urlunsplit
    try:
        s = urlsplit(str(u))
    except ValueError:
        return u
    if s.scheme in {"http", "https"} and s.netloc and not s.path:
        return urlunsplit((s.scheme, s.netloc, "/", s.query, s.fragment))
    return u


def tree_signature(tree):
    """Canonical structure, text, checked state AND links, ignoring API metadata."""
    out = []
    for b in tree:
        kind = b.get("type")
        payload = b.get(kind) or {}
        links = [t.get("href") or ((t.get("text") or {}).get("link") or {}).get("url")
                 for t in payload.get("rich_text") or []]
        out.append((kind, plain(b), tuple(_canon_url(u) for u in links if u),
                    payload.get("checked", False), payload.get("is_toggleable", False),
                    tree_signature(payload.get("children") or [])))
    return tuple(out)


def verify_native_section(actual: list[dict], expected: list[dict] | None = None) -> dict:
    """Strict flat sections: direct to_do -> lead paragraphs + one Details subtree.

    Only preamble paragraphs before the first checkbox may be top-level. A stray
    sibling lead/body is NOT mistaken for a harmless nested issue paragraph.
    Expected payload comparison detects missing prose, URLs, order and checks.
    """
    errors, ids, details, links, saw_issue = [], [], 0, 0, False
    for b in actual:
        kind, payload = b.get("type"), b.get(b.get("type")) or {}
        if kind != "to_do":
            if saw_issue or kind not in {"heading_1", "heading_2", "paragraph"} or payload.get("is_toggleable") or payload.get("children"):
                errors.append(f"top_level_stray:{kind}:{plain(b)[:60]}")
            continue
        saw_issue = True
        m = re.match(r"(\d{2}-\d{3,})\b", plain(b))
        if not m:
            errors.append("issue_id_missing")
        else:
            ids.append(m.group(1))
        kids = payload.get("children") or []
        toggles = [c for c in kids if c.get("type") == "toggle"]
        leads = [c for c in kids if c.get("type") == "paragraph" and plain(c).strip()]
        if not leads or len(toggles) != 1 or not kids or kids[-1].get("type") != "toggle" or any(c.get("type") != "paragraph" for c in kids[:-1]):
            errors.append(f"issue_children_layout:{plain(b)[:20]}")
        if toggles:
            toggle = toggles[0]
            if plain(toggle) not in {"세부 내용", "▶ Details", "Details", "세부 내용 · 출처"}:
                errors.append("details_title_invalid")
            descendants = list(walk_tree(toggle["toggle"].get("children") or []))
            if not descendants or any(c.get("type") in {"to_do", "toggle"} or (c.get(c.get("type")) or {}).get("is_toggleable") for c in descendants):
                errors.append("details_empty_or_nested_issue")
            source_links = [t.get("href") or ((t.get("text") or {}).get("link") or {}).get("url")
                            for c in descendants for t in (c.get(c.get("type")) or {}).get("rich_text") or []]
            nlinks = sum(bool(u) for u in source_links)
            if not nlinks:
                errors.append("issue_sources_missing")
            links += nlinks
            details += 1
    if len(ids) != len(set(ids)):
        errors.append("duplicate_issue_ids")
    if expected is not None and tree_signature(actual) != tree_signature(expected):
        errors.append("exact_native_tree_mismatch")
    return {"status": "FAIL" if errors else "PASS", "errors": errors, "issue_ids": ids,
            "todo": len(ids), "details_toggle": details, "source_links": links,
            "top_level_blocks": len(actual), "layout": "flat_nested_details"}


def live_readback(request, headers: dict, page_id: str, *, markdown: str | None = None, expected_tree=None) -> dict:
    """Read-only helper for the parent: verifies full native content, not Markdown indentation."""
    expected = markdown_blocks(markdown) if markdown is not None else expected_tree
    return verify_native_section(read_native_tree(request, headers, page_id), expected)


def read_checked_ids(day: str, headers: dict) -> set[str]:
    out: set[str] = set()
    for pg in list_section_pages(F.request, headers, day):
        out.update(checked_ids_from_tree(read_native_tree(F.request, headers, pg["id"])))
    return out


def mark_checked(md: str, checked: set[str]) -> str:
    return re.sub(r"^- \[ \] (\d{2}-\d{3,}) ", lambda m: ("- [x] " if m.group(1) in checked else "- [ ] ") + m.group(1) + " ", md, flags=re.M)


def lines_ko(text: str) -> str:
    return text.strip()


def build_overview_ko(rows: list[dict], summary: str, index_url: str, *, data_day: str | None = None):
    """All ten sections, real counts and authored leads; Open is a native link, not a button."""
    def build(day, signals, section_links, *, status, index_page_id, dashboard_page_id):
        by_key = {l["key"]: l for l in section_links}
        if set(by_key) != set(KEY_TO_NAME) or len(section_links) != len(TOPIC_SECTIONS):
            raise RuntimeError("overview_section_inventory_failed")
        good = [r for r in rows if not r.get("review_failed")]
        failed_n = len(rows) - len(good)
        count_line = f"{len(good)} published articles" + (f" · {failed_n} Review Failed articles" if failed_n else "") + f" · {len(section_links)} sections"
        lines = [f"# Tech Brief Overview — {day}", "", f"Published: {day} · Evidence date: {data_day or day}",
                 count_line, "", "## At a glance", summary.strip(), "", "## Browse by section"]
        counts = Counter(SEC_CODE_TO_KEY[r["_code"]] for r in rows)
        for name, key in TOPIC_SECTIONS:
            link = by_key[key]
            if link["issue_count"] != counts.get(key, 0) or not link["url"].startswith("https://"):
                raise RuntimeError("overview_actual_rows_mismatch:" + key)
            count = counts.get(key, 0)
            suffix = (" · none" if key == REVIEW_FAILED_KEY else " · no articles passed publication checks") if not count else ""
            lines.append(f"- {name} · {count} issues · [Open]({link['url']}){suffix}")
        lines += ["", "## Recommended reading"]
        for r in sorted(good, key=lambda r: (-r["_score"], r["feedback_id"]))[:8]:
            link = by_key[SEC_CODE_TO_KEY[r["_code"]]]["url"]
            lines.append(f"- {clean(r['headline'])} — {clean(r['lead'])} [Open]({link})")
        lines += ["", "## Complete inventory", f"[Open the complete daily brief]({index_url})"]
        return "\n".join(lines) + "\n"
    return build


def row_render_problems(row: dict) -> list[str]:
    """Render ONE article alone through the section markdown/native-block QA; the reasons it would break a section."""
    r = dict(row)
    code = r.get("section") if r.get("section") in SEC_CODE_TO_KEY else "10"
    r["_code"] = code
    r.setdefault("feedback_id", f"{code}-001")
    r.setdefault("_score", 0)
    key = SEC_CODE_TO_KEY[code]
    qa = verify_native_section(markdown_blocks(section_markdown("2000-01-01", KEY_TO_NAME[key], key, [r])))
    return [] if qa["status"] == "PASS" else [str(e) for e in qa["errors"]]


def outage_summary(day: str, data_day: str, outage: dict) -> str:
    """Summary for a day the pipeline could not run: states the failed stage and reason plainly, invents no news."""
    stage = outage.get("stage") or "unknown"
    reason = re.sub(r"\s+", " ", str(outage.get("reason") or "")).strip()[:400]
    lines = [f"Today's brief: The {day} run stopped at `{stage}` before a complete brief could be published.", "",
             "Run status:", "- 0 published articles in this notice", f"- Failed stage: {stage}", f"- Reason: {reason or 'not recorded'}"]
    if outage.get("resets_at"):
        lines.append(f"- Codex usage limit resets at: {outage['resets_at']}")
    lines += ["", f"Next step: {outage.get('next') or 'The brief remains unavailable until an automatic retry or manual rerun succeeds.'}", "",
              f"Coverage: 0 published articles in this notice. Evidence date: {data_day}; publication date: {day}. This is an outage notice, not a completed brief or a copy of yesterday's articles."]
    return "\n".join(lines)


def fallback_summary(day: str, data_day: str, rows: list[dict], n_sections: int, reason: str) -> str:
    """Summary without an LLM: only counts and headlines that already exist in the published articles."""
    good = [r for r in rows if not r.get("review_failed")]
    if not rows:
        return "\n".join(["Today's brief: No articles passed the publication checks.", "",
                          "Published articles:", "- 0 published articles", "",
                          "Status: No publishable article remained in today's material. This notice reports the empty inventory; it does not claim that nothing happened.", "",
                          f"Coverage: 0 published articles across {n_sections} sections. Evidence date: {data_day}; publication date: {day}."])
    lines = ["Today's brief: Automatic summary generation failed. Published article headlines are listed instead.", "", "Published headlines:"]
    lines += [f"- {clean(r['headline'])}" for r in sorted(good, key=lambda r: (-r["_score"], r["feedback_id"]))[:8]]
    lines += ["", "Status: The overview could not be generated. Open each section for the article leads, technical details and sources.", "",
              f"Coverage: {len(rows)} published articles across {n_sections} sections. Evidence date: {data_day}; publication date: {day}."]
    return "\n".join(lines)


def exclude_defective(rows: list[dict]) -> list[dict]:
    """Remove (in place) every article that cannot be rendered/identified; one bad article never stops the day.
    Returns [{"event_id", "problems"}] for the removed ones."""
    import row_check
    excluded, kept = [], []
    for r in rows:
        problems = row_check.row_defects(r)
        if r.get("_code") not in SEC_CODE_TO_KEY or not re.fullmatch(r"\d{2}-\d{3,}", r.get("feedback_id", "")):
            problems.append("Invalid feedback ID or section code")
        if not problems:
            problems = row_check.render_defects(r, row_render_problems)
        if problems:
            excluded.append({"event_id": r.get("event_id"), "problems": problems})
            print("EXCLUDED_AT_PUBLISH", r.get("event_id"), "; ".join(problems)[:200], flush=True)
        else:
            kept.append(r)
    rows[:] = kept
    return excluded


def prepare_publication(day: str, data_day: str, rows: list[dict], *, summary_override: str | None = None) -> dict:
    """All content generation/QA, including Luna, finishes BEFORE any Notion mutation."""
    from datetime import date
    date.fromisoformat(day)
    date.fromisoformat(data_day)
    if len({r["event_id"] for r in rows}) != len(rows):
        seen_ids, unique = set(), []
        for r in rows:  # a duplicate row is collapsed, never fatal
            if r["event_id"] not in seen_ids:
                seen_ids.add(r["event_id"])
                unique.append(r)
        rows[:] = unique
    excluded = exclude_defective(rows)
    by_code = {}
    for r in rows:
        by_code.setdefault(r["_code"], []).append(r)
    specs = [{"name": name, "key": key, "order": int(name.split(".", 1)[0]), "rows": by_code.get(name.split(".", 1)[0], [])}
             for name, key in TOPIC_SECTIONS]
    section_md = {s["key"]: section_markdown(day, s["name"], s["key"], s["rows"], data_day=data_day) for s in specs}
    for key, md in section_md.items():
        qa = verify_native_section(markdown_blocks(md))
        if qa["status"] != "PASS":
            raise RuntimeError("section_content_qa_failed:" + key + ":" + json.dumps(qa, ensure_ascii=False))
    summary_fallback = None
    try:
        if summary_override is not None:
            summary = summary_override
        elif not rows:
            raise RuntimeError("no_articles_to_summarize")
        else:
            summary = summary_block(day, [r for r in rows if not r.get("review_failed")], len(specs), data_day=data_day)
    except llm.QuotaExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed summary must not cost the whole day
        summary_fallback = f"{type(exc).__name__}: {str(exc)[:200]}"
        summary = fallback_summary(day, data_day, rows, len(specs), summary_fallback)
        if rows:
            print("SUMMARY_FALLBACK", summary_fallback, flush=True)
    # Validate navigation-independent content with explicit non-publishable placeholders.
    links = [{"name": s["name"], "key": s["key"], "issue_count": len(s["rows"]), "url": "https://pending.invalid/" + s["key"]} for s in specs]
    overview = build_overview_ko(rows, summary, "https://pending.invalid/index", data_day=data_day)(day, [], links, status="Draft", index_page_id="pending", dashboard_page_id="pending")
    index = build_brief_index_markdown(day, f"## 00. At a glance\n{summary}\n", links, data_day=data_day)
    return {"specs": specs, "section_md": section_md, "summary": summary, "overview_preflight": overview, "index_preflight": index,
            "excluded": excluded, "summary_fallback": summary_fallback}


def to_signals(rows: list[dict]) -> list[dict]:
    sig = []
    for r in rows:
        e = r["_event"]
        prim = any(s.get("kind") == "primary" for s in r["sources"])
        sig.append({
            "feedback_id": r["feedback_id"], "inventory_id": r["event_id"], "event_id": r["event_id"],
            "title": clean(r["headline"]), "primary_topic": SEC_CODE_TO_KEY[r["_code"]], "topic_section": SEC_CODE_TO_KEY[r["_code"]],
            "evidence_state": "VERIFIED" if prim else "DEVELOPING",
            "continuity_class": "DEVELOPING" if len(e.get("dates", [])) > 1 else "NEW",
            "supporting_sources": [{"url": s["url"]} for s in r["sources"]],
            "url": r["sources"][0]["url"] if r["sources"] else "",
            "source_id": "", "role": "",
            "materiality": float(r["_score"]),
        })
    return sig


def find_or_create_index(day: str, headers: dict) -> tuple[str, dict, int]:
    matches = query_all(F.request, headers, F.IDS["daily_data_source_id"], filter={"property": "Date", "date": {"equals": day}})
    if len(matches) > 1:
        raise SystemExit(f"exact_date_duplicate:{len(matches)}")
    title = f"{day} · Daily Tech Intelligence Brief"
    if matches:
        page_id = matches[0]["id"]
        st, meta = F.request(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
        if st != 200 or F.page_title(meta) != title or F.page_date(meta) != day or (meta.get("parent") or {}).get("database_id") != F.IDS["daily_database_id"]:
            raise SystemExit("notion_target_identity_failed")
        register_page(page_id, parent_database_id=F.IDS["daily_database_id"])
        return page_id, meta, len(matches)
    st, created = F.request("https://api.notion.com/v1/pages", "POST", headers, {
        "parent": {"database_id": F.IDS["daily_database_id"]},
        "properties": {"Name": {"title": [{"text": {"content": title}}]}, "Date": {"date": {"start": day}},
                       "Stage": {"select": {"name": "DRAFT"}}, "Brief Status": {"select": {"name": "Draft"}}, "Review Needed": {"checkbox": True}}})
    if st not in (200, 201) or not created.get("id"):
        raise SystemExit(f"notion_create_failed:{st}")
    page_id = created["id"]
    register_page(page_id, parent_database_id=F.IDS["daily_database_id"])
    st, meta = F.request(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
    return page_id, meta, 0


def backup_page(request, headers: dict, page_id: str, backup_dir: Path) -> Path:
    """Immutable native + Markdown snapshot. No caller may replace after a partial backup."""
    from uuid import uuid4
    tree = read_native_tree(request, headers, page_id)
    st, body = request(f"https://api.notion.com/v1/pages/{page_id}/markdown", "GET", headers)
    if st != 200 or not isinstance(body, dict) or not isinstance(body.get("markdown"), str) or body.get("truncated"):
        raise RuntimeError(f"backup_markdown_failed:{page_id}:{st}")
    out = Path(backup_dir) / (page_id + "-" + uuid4().hex)
    out.mkdir(parents=True, exist_ok=False)
    native = json.dumps(tree, ensure_ascii=False, indent=2)
    md = body["markdown"]
    (out / "page.native.json").write_text(native, encoding="utf-8")
    (out / "page.md").write_text(md, encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({"page_id": page_id, "native_sha256": hashlib.sha256(native.encode()).hexdigest(),
                                                "markdown_sha256": hashlib.sha256(md.encode()).hexdigest(),
                                                "checked_ids": sorted(checked_ids_from_tree(tree))}, indent=2))
    return out


def backup_existing(day: str, headers: dict, index_id: str) -> Path:
    from uuid import uuid4
    out = p.V3 / "notion_backups" / day / uuid4().hex
    pages = {"index": index_id, **{pg["id"]: pg["id"] for pg in list_section_pages(F.request, headers, day)}}
    for name in ("dashboard_page_id", "dashboard_overview_page_id"):
        if F.IDS.get(name):
            pages[name] = F.IDS[name]
    snapshots = {name: str(backup_page(F.request, headers, pid, out)) for name, pid in pages.items()}
    (out / "pages.json").write_text(json.dumps(snapshots, ensure_ascii=False, indent=2))
    return out


def native_payload(tree):
    """Remove read-only API fields without rewriting prose, rich text or source URLs."""
    import copy
    result = []
    supported = {"paragraph", "heading_1", "heading_2", "heading_3", "to_do", "toggle", "bulleted_list_item", "numbered_list_item", "quote"}
    for b in tree:
        kind = b.get("type")
        if kind not in supported:
            raise RuntimeError(f"repair_unsupported_block:{kind}")
        data = b.get(kind) or {}
        payload = {k: copy.deepcopy(v) for k, v in data.items() if k in {"rich_text", "color", "checked", "is_toggleable"}}
        payload["rich_text"] = [{k: copy.deepcopy(v) for k, v in t.items() if k in {"type", "text", "mention", "equation", "annotations"}}
                                for t in data.get("rich_text") or []]
        if data.get("children"):
            payload["children"] = native_payload(data["children"])
        result.append({"object": "block", "type": kind, kind: payload})
    return result


def nest_legacy_tree(tree) -> list[dict]:
    """Pure conversion of authored native blocks. No row re-authoring or LLM call.

    Flatten only recognized topic wrappers, move each issue's legacy sibling
    lead/Details beneath its checkbox, wrap already-flat bodies in Details.
    Existing rich text, source blocks, glossary and checked values are untouched.
    """
    blocks = native_payload(tree)
    flat = []
    topic_names = {name for name, _ in (*TOPIC_SECTIONS, *_TOPIC_SECTIONS)}
    for b in blocks:
        kind, data = b["type"], b[b["type"]]
        if (kind == "heading_2" and data.get("is_toggleable")) or (kind == "toggle" and plain(b) in topic_names):
            children = data.pop("children", [])
            if kind == "toggle":
                b = {"object": "block", "type": "heading_2", "heading_2": {"rich_text": data["rich_text"]}}
            else:
                data["is_toggleable"] = False
            flat.extend([b, *children])
        else:
            flat.append(b)
    result, owner = [], None
    for b in flat:
        if b["type"] == "to_do":
            owner = b
            owner["to_do"].setdefault("children", [])
            result.append(owner)
        elif owner is not None:
            owner["to_do"]["children"].append(b)
        else:
            result.append(b)
    for b in result:
        if b["type"] != "to_do":
            continue
        children = b["to_do"]["children"]
        if not any(c["type"] == "toggle" for c in children):
            if len(children) < 2 or children[0]["type"] != "paragraph":
                raise RuntimeError("repair_issue_missing_lead_or_details")
            b["to_do"]["children"] = [children[0], {"object": "block", "type": "toggle", "toggle": {
                "rich_text": [{"type": "text", "text": {"content": "세부 내용"}}], "children": children[1:]}}]
    qa = verify_native_section(result)
    if qa["status"] != "PASS":
        raise RuntimeError("repair_preflight_failed:" + json.dumps(qa, ensure_ascii=False))
    return result


def shallow_request(request):
    """Notion allows only two child levels per append. Populate deeper nodes separately."""
    def call(url, method="GET", headers=None, body=None):
        if method == "PATCH" and url.endswith("/children") and isinstance(body, dict):
            import copy
            body = copy.deepcopy(body)
            for b in body.get("children") or []:
                # Shells only: the resumable tree writer then fills each subtree.
                (b.get(b.get("type")) or {}).pop("children", None)
        return request(url, method, headers, body)
    return call


def replace_native_tree(request, headers: dict, page_id: str, tree: list[dict], progress_path: Path) -> dict:
    """Native-payload counterpart of the production resumable writer; no Markdown round trip."""
    from work import notion_block_writer as NB
    digest = hashlib.sha256(json.dumps(tree, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    prior = NB._load_progress(progress_path)
    current = NB._children(request, page_id, headers)
    same = prior.get("page_id") == page_id and prior.get("source_sha256") == digest and prior.get("phase") in {"append", "done"}
    if not same or not NB._is_prefix(current, tree):
        NB.archive_top_level_resumable(request, page_id, headers, progress_path, digest, len(tree))
        current = []
    try:
        NB._append_tree_resumable(shallow_request(request), page_id, headers, tree, current, progress_path, digest, len(tree), 25)
    except RuntimeError:
        # Do not erase a partially written repair on error. Parent can inspect
        # backup/checkpoint and explicitly restart if a nested prefix is corrupt.
        raise
    actual = read_native_tree(request, headers, page_id)
    if tree_signature(actual) != tree_signature(tree):
        raise RuntimeError("native_replacement_readback_failed")
    NB._save_progress(progress_path, {"phase": "done", "page_id": page_id, "source_sha256": digest, "expected_blocks": len(tree)})
    return {"status": "PASS", "page_id": page_id, "source_sha256": digest}


def repair_legacy_section(request, headers: dict, page_id: str, backup_dir: Path, *, day="2026-09-30", apply=False) -> dict:
    """Parent-callable 9/30 repair; default is read-only. apply=True never invokes Luna.

    Only exact-date existing Section pages are eligible. Prose is copied from
    the live native tree (not articles.jsonl). Full backup and QA precede writes.
    """
    st, meta = request(f"https://api.notion.com/v1/pages/{page_id}", "GET", headers)
    if st != 200 or F.page_date(meta) != day or (meta.get("parent") or {}).get("database_id") != F.IDS["daily_sections_database_id"]:
        raise RuntimeError("repair_target_identity_failed")
    tree = read_native_tree(request, headers, page_id)
    new = nest_legacy_tree(tree)
    result = {"page_id": page_id, "date": day, "applied": False, "expected_tree": new, "native": verify_native_section(new)}
    if not apply:
        return result
    backup = backup_page(request, headers, page_id, backup_dir)
    # Fail if edits occurred between planning and backup; never replace user changes.
    saved = json.loads((backup / "page.native.json").read_text())
    if tree_signature(saved) != tree_signature(tree):
        raise RuntimeError("repair_concurrent_change")
    register_page(page_id, parent_database_id=F.IDS["daily_sections_database_id"])
    replace_native_tree(request, headers, page_id, new, Path(backup_dir) / f"repair-{page_id}.progress.json")
    result.update(applied=True, backup=str(backup), native=live_readback(request, headers, page_id, expected_tree=new))
    if result["native"]["status"] != "PASS":
        raise RuntimeError("repair_readback_failed")
    return result


def write_section_page(request, headers: dict, *, markdown: str, progress_path=None, **spec) -> dict:
    """Publisher-local writer: production Markdown parser, strict new-layout QA.

    Do not call daily_brief_sections.write_section_page: its legacy validator
    rejects exactly the requested checkbox-owned Details layout.
    """
    expected = markdown_blocks(markdown)
    preflight = verify_native_section(expected)
    if preflight["status"] != "PASS" or preflight["todo"] != spec["issue_count"]:
        raise RuntimeError("section_preflight_failed:" + json.dumps(preflight, ensure_ascii=False))
    page_id, meta, created = resolve_section_page(request, headers, **spec)
    backup = backup_page(request, headers, page_id, p.V3 / "notion_backups" / spec["day"])
    checkpoint = Path(progress_path) if progress_path else p.V3 / spec["day"] / f"notion-progress-{spec['section_key']}.json"
    written = replace_native_tree(request, headers, page_id, expected, checkpoint)
    native = live_readback(request, headers, page_id, expected_tree=expected)
    if native["status"] != "PASS":
        raise RuntimeError("section_native_readback_failed:" + json.dumps(native, ensure_ascii=False))
    return {"page_id": page_id, "url": meta.get("url") or "https://www.notion.so/" + page_id.replace("-", ""),
            "issue_count": spec["issue_count"], "created": created, "native": native, "write": written, "backup": str(backup)}


def query_all(request, headers: dict, source_id: str, *, filter=None) -> list[dict]:
    rows, cursor, seen = [], None, set()
    for _ in range(1000):
        payload = {"page_size": 100}
        if filter is not None:
            payload["filter"] = filter
        if cursor:
            payload["start_cursor"] = cursor
        st, result = request(f"https://api.notion.com/v1/data_sources/{source_id}/query", "POST", headers, payload)
        if st != 200 or not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise RuntimeError(f"data_source_query_failed:{source_id}:{st}")
        rows += result["results"]
        if not result.get("has_more"):
            return rows
        cursor = result.get("next_cursor")
        if not cursor or cursor in seen:
            raise RuntimeError("data_source_pagination_invalid")
        seen.add(cursor)
    raise RuntimeError("data_source_pagination_limit")


def list_section_pages(request, headers: dict, day: str) -> list[dict]:
    return query_all(request, headers, F.IDS["daily_sections_data_source_id"], filter={"property": "Date", "date": {"equals": day}})


def retain_stale_sections(request, headers: dict, day: str, pages: list[dict], desired_counts: dict, backup_dir: Path) -> list[str]:
    """Keep stale body/history alive; retire current key so a real empty page can replace navigation."""
    retained = []
    for pg in pages:
        key = F.rich_text_prop(pg, "Section Key")
        if key.startswith("history:") or key == FS.KEY:      # section 12 is owned by the 11:00 job; never retire it as stale
            continue
        old_count = int((pg.get("properties", {}).get("Issue Count") or {}).get("number") or 0)
        if key in desired_counts and (desired_counts[key] or old_count == 0):
            continue
        if F.page_date(pg) != day or (pg.get("parent") or {}).get("database_id") != F.IDS["daily_sections_database_id"]:
            raise RuntimeError("stale_section_identity_failed")
        backup_page(request, headers, pg["id"], backup_dir)
        register_page(pg["id"], parent_database_id=F.IDS["daily_sections_database_id"])
        historical_key = f"history:{key}:{pg['id']}"
        note = f"NON_CURRENT: preserved history for {day}; excluded from current navigation. Original Section Key: {key}."
        props = {"Section Key": {"rich_text": [{"text": {"content": historical_key}}]},
                 "Stage": {"select": {"name": "DRAFT"}}, "Brief Status": {"select": {"name": "Draft"}},
                 "Review Needed": {"checkbox": False}, "Quality Note": {"rich_text": [{"text": {"content": note}}]}}
        st, _ = request(f"https://api.notion.com/v1/pages/{pg['id']}", "PATCH", headers, {"properties": props})
        st2, actual = request(f"https://api.notion.com/v1/pages/{pg['id']}", "GET", headers)
        if st != 200 or st2 != 200 or F.rich_text_prop(actual, "Section Key") != historical_key or F.select_prop(actual, "Brief Status") != "Draft" or actual.get("in_trash") or actual.get("archived"):
            raise RuntimeError("stale_history_readback_failed:" + pg["id"])
        retained.append(pg["id"])
    return retained


def remove_dashboard_sections_listing(request, headers: dict, backup_dir: Path, *, apply=True) -> dict:
    """Inspect API ownership before deleting ONLY a linked display shell block.

    child_database is ambiguous: it may be the real owning database, or a
    separate Dashboard shell sharing an external data source. Never DELETE
    /databases, the canonical database's block, or a shell that owns the source.
    Underlying history IDs must survive exact readback. apply=False is read-only.
    """
    from work.scope_guard import register_block
    ids = F.IDS
    canonical = ids["daily_sections_database_id"]
    source_id = ids["daily_sections_data_source_id"]
    dashboard = ids["dashboard_page_id"]
    def read(kind, identifier):
        st, body = request(f"https://api.notion.com/v1/{kind}/{identifier}", "GET", headers)
        if st != 200 or not isinstance(body, dict):
            raise RuntimeError(f"dashboard_listing_identity_failed:{kind}:{st}")
        return body
    owner = read("databases", canonical)
    source = read("data_sources", source_id)
    if owner.get("id") != canonical or owner.get("in_trash") or owner.get("archived") or (source.get("parent") or {}).get("database_id") != canonical:
        raise RuntimeError("dashboard_underlying_database_identity_failed")
    before_tree = read_native_tree(request, headers, dashboard)
    st, view = request(f"https://api.notion.com/v1/views/{ids['dashboard_sections_view_id']}", "GET", headers)
    stale_view_shell = None
    if st == 200 and view.get("data_source_id") == source_id:
        # A view can still answer 200 after its display shell was trashed. Treat that as
        # already removed, but only after proving no live listing remains on the Dashboard.
        view_shell_id = (view.get("parent") or {}).get("database_id")
        if view_shell_id and view_shell_id != canonical:
            vs = read("databases", view_shell_id)
            if vs.get("in_trash") or vs.get("archived"):
                stale_view_shell = view_shell_id
    if st == 404 or stale_view_shell:
        # Reruns after shell removal: still inspect each remaining display shell,
        # because a stale configured view ID is not proof that no listing exists.
        candidates = []
        for b in before_tree:
            if b.get("type") == "child_database":
                shell = read("databases", b["id"])
                if any(d.get("id") == source_id for d in shell.get("data_sources") or []):
                    candidates.append(b["id"])
        if candidates:
            raise RuntimeError("dashboard_unresolved_sections_listing:" + ",".join(candidates))
        return {"status": "PASS", "removed": False, "reason": "already_absent", "source_database_id": canonical}
    if st != 200 or view.get("data_source_id") != source_id:
        raise RuntimeError("dashboard_sections_view_identity_failed")
    shell_id = (view.get("parent") or {}).get("database_id")
    if not shell_id or shell_id == canonical or shell_id in {v for k, v in ids.items() if k.endswith("database_id")}:
        raise RuntimeError("dashboard_underlying_database_delete_forbidden")
    shell = read("databases", shell_id)
    if (shell.get("id") != shell_id or (shell.get("parent") or {}).get("page_id") != dashboard
            or {d.get("id") for d in shell.get("data_sources") or []} != {source_id}
            or not any(b.get("id") == shell_id and b.get("type") == "child_database" for b in before_tree)):
        raise RuntimeError("dashboard_linked_shell_identity_failed")
    history_before = sorted(pg["id"] for pg in query_all(request, headers, source_id))
    plan = {"source_database_id": canonical, "source_id": source_id, "display_shell_id": shell_id,
            "history_page_ids": history_before, "view": view, "shell": shell, "source": source}
    if not apply:
        return {"status": "PASS", "removed": False, "plan": plan}
    backup_dir.mkdir(parents=True, exist_ok=True)
    from uuid import uuid4
    (backup_dir / ("dashboard-listing-" + uuid4().hex + ".json")).write_text(json.dumps({**plan, "tree": before_tree}, ensure_ascii=False, indent=2))
    register_block(shell_id)
    st, _ = request(f"https://api.notion.com/v1/blocks/{shell_id}", "DELETE", headers)
    after_tree = read_native_tree(request, headers, dashboard)
    owner_after = read("databases", canonical)
    source_after = read("data_sources", source_id)
    history_after = sorted(pg["id"] for pg in query_all(request, headers, source_id))
    if (st != 200 or any(b.get("id") == shell_id for b in after_tree) or owner_after.get("in_trash") or owner_after.get("archived")
            or (source_after.get("parent") or {}).get("database_id") != canonical or history_before != history_after):
        raise RuntimeError("dashboard_listing_removal_readback_failed")
    return {"status": "PASS", "removed": True, "display_shell_id": shell_id,
            "source_database_id": canonical, "history_count": len(history_after)}


def write_overview_native(request, headers: dict, markdown: str, local_dir: Path) -> dict:
    """Strict native Overview replacement without config edits or lax nonempty QA."""
    from work import notion_dashboard as ND
    from work.scope_guard import register_page_under_page
    page_id = F.IDS.get("dashboard_overview_page_id") or ND._find_overview_page(request, headers)
    if not page_id:
        st, meta = request("https://api.notion.com/v1/pages", "POST", headers, {
            "parent": {"page_id": F.IDS["dashboard_page_id"]},
            "properties": {"title": [{"text": {"content": ND.OVERVIEW_TITLE}}]}})
        if st not in (200, 201) or not meta.get("id"):
            raise RuntimeError("overview_create_failed")
        page_id = meta["id"]
        # Intentionally no config persistence; future runs discover the exact child.
    st, meta = request(f"https://api.notion.com/v1/pages/{page_id}", "GET", headers)
    if st != 200 or ND._page_title(meta) != ND.OVERVIEW_TITLE or (meta.get("parent") or {}).get("page_id") != F.IDS["dashboard_page_id"]:
        raise RuntimeError("overview_identity_failed")
    register_page_under_page(page_id, F.IDS["dashboard_page_id"])
    backup_page(request, headers, page_id, local_dir / "backups")
    expected = markdown_blocks(markdown)
    replace_native_tree(request, headers, page_id, expected, local_dir / "notion-progress-overview.json")
    if tree_signature(read_native_tree(request, headers, page_id)) != tree_signature(expected):
        raise RuntimeError("overview_native_readback_failed")
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "overview.md").write_text(markdown, encoding="utf-8")
    return {"status": "PASS", "page_id": page_id, "url": meta.get("url") or ND.notion_url(page_id)}


def ensure_dashboard_overview_entry(request, headers: dict, overview_page_id: str) -> dict:
    """Keep existing Dashboard content/databases; expose one Overview navigation entry."""
    from work import notion_dashboard as ND
    from work import notion_block_writer as NB
    ND._verify_dashboard_page(request, headers)
    dashboard = F.IDS["dashboard_page_id"]
    def visible(tree):
        return any(b.get("type") == "child_page" and b.get("id") == overview_page_id for b in tree) or any(
            overview_page_id.replace("-", "") in str(t.get("href") or ((t.get("text") or {}).get("link") or {}).get("url") or "").replace("-", "")
            for b in tree for t in (b.get(b.get("type")) or {}).get("rich_text") or [])
    tree = read_native_tree(request, headers, dashboard)
    overview_tree = read_native_tree(request, headers, overview_page_id)
    date_match = re.search(r"\d{4}-\d{2}-\d{2}", " ".join(plain(b) for b in overview_tree if b.get("type") == "heading_1"))
    # One fixed entry. A date in the label made every day look "new" and stacked a line per day, all pointing at the same page.
    label = "Open today's Overview"
    overview_url = ND.notion_url(overview_page_id)
    def is_entry(b):
        if b.get("type") != "paragraph":
            return False
        text = plain(b)
        return text in {label, "오늘의 Overview 열기"} or bool(re.fullmatch(r"\d{4}-\d{2}-\d{2} · (?:오늘의 Overview 열기|Open today's Overview)", text))
    def points_at_overview(b):
        return any(overview_page_id.replace("-", "") in str(((t.get("text") or {}).get("link") or {}).get("url") or t.get("href") or "").replace("-", "")
                   for t in (b.get("paragraph") or {}).get("rich_text") or [])
    entries = [b for b in tree if is_entry(b) and points_at_overview(b)]
    fixed = [b for b in entries if plain(b) == label]
    removed = []
    if date_match:
        if not fixed:
            paragraph = {"rich_text": [{"type": "text", "text": {"content": label, "link": {"url": overview_url}}, "annotations": {"bold": True}}]}
            st, _ = request(f"https://api.notion.com/v1/blocks/{dashboard}/children", "PATCH", headers,
                            {"children": [{"object": "block", "type": "paragraph", "paragraph": paragraph}], "position": {"type": "start"}})
            after_add = read_native_tree(request, headers, dashboard)
            if st != 200 or not any(is_entry(b) and plain(b) == label and points_at_overview(b) for b in after_add):
                raise RuntimeError("dashboard_top_overview_readback_failed")
            tree = after_add
            fixed = [b for b in tree if is_entry(b) and plain(b) == label and points_at_overview(b)]
        # Retire the dated lines older runs left behind (only paragraphs that are exactly this entry and link to this Overview),
        # and any duplicate fixed lines beyond the first. Keep a backup before deleting.
        stale = [b for b in tree if is_entry(b) and points_at_overview(b) and plain(b) != label] + fixed[1:]
        if stale:
            from work.scope_guard import register_block
            backup = ROOT / "data" / "evidence" / "dashboard-entry-backups"
            backup.mkdir(parents=True, exist_ok=True)
            (backup / f"stale-entries-{date_match.group(0)}.json").write_text(json.dumps(stale, ensure_ascii=False, indent=2))
            for b in stale:
                register_block(b["id"])
                st, _ = request(f"https://api.notion.com/v1/blocks/{b['id']}", "DELETE", headers)
                if st != 200:
                    raise RuntimeError(f"dashboard_stale_entry_delete_failed:{b['id']}:{st}")
                removed.append(b["id"])
            tree = read_native_tree(request, headers, dashboard)
            left = [b for b in tree if is_entry(b) and points_at_overview(b)]
            if len(left) != 1 or plain(left[0]) != label:
                raise RuntimeError("dashboard_entry_cleanup_readback_failed")
    if not visible(tree):
        NB.append_blocks(request, dashboard, headers, markdown_blocks(f"- [Technology Daily Overview]({overview_url})"))
    actual = read_native_tree(request, headers, dashboard)
    if not visible(actual):
        raise RuntimeError("dashboard_overview_entry_readback_failed")
    return {"status": "PASS", "overview_page_id": overview_page_id, "stale_entries_removed": removed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="local artifact/source evidence date; never replaced by run-date")
    ap.add_argument("--dry-run", action="store_true", help="generate and QA all content, without Notion calls")
    ap.add_argument("--run-date", help="Notion display/Date property date, separate from source evidence date")
    ap.add_argument("--use-feedback-ledger", action="store_true", help="bind source identities to the operational immutable feedback ledger")
    ap.add_argument("--outage", metavar="JSON_FILE", help="publish a 0-article brief explaining why the day could not run (no pipeline artifacts needed)")
    args = ap.parse_args()
    data_day, day = args.date, args.run_date or args.date
    outage = json.loads(Path(args.outage).read_text()) if args.outage else None
    rows = [] if outage else load_rows(data_day)
    assign_ids(rows)
    exclude_defective(rows)  # before the feedback ledger plan, which reads headline/sources of every row
    feedback_plan = None
    if args.use_feedback_ledger:
        import feedback_bridge
        feedback_plan = feedback_bridge.plan(rows, day)
        registry = json.loads((p.V3 / "feedback_ids.json").read_text())
        registry.update({r["event_id"]: r["feedback_id"] for r in rows})
        (p.V3 / "feedback_ids.json").write_text(json.dumps(registry, ensure_ascii=False, indent=2))
    plan = prepare_publication(day, data_day, rows, summary_override=outage_summary(day, data_day, outage) if outage else None)
    specs, section_md = plan["specs"], plan["section_md"]
    if outage:
        for key in section_md:
            section_md[key] = section_md[key].replace("No articles passed the publication checks.", "This section is empty because the daily run could not complete. See the outage notice at the top of the brief.")
    print("publishable", len(rows), {s["key"]: len(s["rows"]) for s in specs})
    local = p.V3 / data_day
    md_dir = local / "notion_md"
    md_dir.mkdir(parents=True, exist_ok=True)
    for key, md in section_md.items():
        (md_dir / f"section-{key}.md").write_text(md, encoding="utf-8")
    (md_dir / "summary.md").write_text(plan["summary"], encoding="utf-8")
    if args.dry_run:
        (md_dir / "index.md").write_text(plan["index_preflight"], encoding="utf-8")
        (md_dir / "overview.md").write_text(plan["overview_preflight"], encoding="utf-8")
        return 0
    token = F.load_env().get("NOTION_API_KEY")
    if not token:
        raise RuntimeError("NOTION_API_KEY_missing")
    headers = {"Authorization": "Bearer " + token, "Notion-Version": "2026-03-11", "Content-Type": "application/json"}
    from work import notion_dashboard as ND
    # Ownership discovery is read-only. Refuse unsafe Dashboard mechanics before
    # creating/replacing anything externally.
    remove_dashboard_sections_listing(F.request, headers, local / "evidence", apply=False)
    index_id, meta, _ = find_or_create_index(day, headers)
    if outage and F.select_prop(meta, "Stage") == "FINAL" and F.select_prop(meta, "Brief Status") == "Published" \
            and not meta.get("properties", {}).get("Review Needed", {}).get("checkbox"):
        # A finished real brief already exists for this date: an outage notice must never replace it.
        print("OUTAGE_PAGE_SKIPPED: a published brief already exists for", day, flush=True)
        return 0
    results = []
    try:
        backup = backup_existing(day, headers, index_id)
        checked = read_checked_ids(day, headers)
        section_md = {key: mark_checked(md, checked) for key, md in section_md.items()}
        st, _ = F.request(f"https://api.notion.com/v1/pages/{index_id}", "PATCH", headers,
                          {"properties": F._properties_for_metadata(meta, {}, stage="DRAFT", status="Draft", review=True)})
        if st != 200:
            raise RuntimeError("index_draft_write_failed")
        retained = retain_stale_sections(F.request, headers, day, list_section_pages(F.request, headers, day),
                                        {s["key"]: len(s["rows"]) for s in specs}, backup)
        for spec in specs:
            key = spec["key"]
            result = write_section_page(F.request, headers, day=day, section_key=key, section_name=spec["name"], section_order=spec["order"],
                                        parent_brief_id=index_id, markdown=section_md[key], issue_count=len(spec["rows"]), stage="DRAFT",
                                        brief_status="Draft", coverage_status="PASS", review_needed=True, progress_path=local / f"notion-progress-{key}.json")
            if result["native"]["status"] != "PASS":
                raise RuntimeError("section_native_failed:" + key)
            result.update(section_key=key, section_name=spec["name"], section_order=spec["order"], coverage_status="PASS")
            results.append(result)
            (md_dir / f"section-{key}.md").write_text(section_md[key], encoding="utf-8")
            print("section", key, result["issue_count"], result["native"]["status"])
        current = [pg for pg in list_section_pages(F.request, headers, day) if not F.rich_text_prop(pg, "Section Key").startswith("history:")
                   and F.rich_text_prop(pg, "Section Key") != FS.KEY]
        got = {F.rich_text_prop(pg, "Section Key"): int((pg.get("properties", {}).get("Issue Count") or {}).get("number") or 0) for pg in current}
        if len(current) != len(specs) or got != {s["key"]: len(s["rows"]) for s in specs}:
            raise RuntimeError("section_inventory_readback_failed:" + json.dumps(got))
        actual_ids = [eid for result in results for eid in result["native"]["issue_ids"]]
        expected_ids = {r["feedback_id"] for r in rows}
        if len(actual_ids) != len(rows) or set(actual_ids) != expected_ids:
            raise RuntimeError("issue_id_readback_failed")
        links = [{"key": r["section_key"], "name": r["section_name"], "url": r["url"], "issue_count": r["issue_count"]} for r in results]
        summary = plan["summary"]
        # Section 12 exists from the first publish on (placeholder only); its content belongs to the 11:00 job.
        followup = FS.ensure_followup_section(F.request, headers, day=day, parent_brief_id=index_id)
        index_md = build_brief_index_markdown(day, f"## 00. At a glance\n{summary}\n", links, data_day=data_day, followup_url=followup["url"])
        index_tree = markdown_blocks(index_md)
        # The Index is rewritten wholesale; text the reader wrote under "My Feedback / Follow-up" must survive a same-day re-publish.
        index_tree, preserved_feedback = FS.with_preserved_feedback(index_tree, read_native_tree(F.request, headers, index_id), plain, native_payload)
        replace_native_tree(F.request, headers, index_id, index_tree, local / "notion-progress-index.json")
        if tree_signature(read_native_tree(F.request, headers, index_id)) != tree_signature(index_tree):
            raise RuntimeError("index_native_readback_failed")
        (md_dir / "index.md").write_text(index_md, encoding="utf-8")
        index_url = meta.get("url") or ND.notion_url(index_id)
        overview_md = build_overview_ko(rows, summary, index_url, data_day=data_day)(day, to_signals(rows), links, status="Published", index_page_id=index_id, dashboard_page_id=F.IDS["dashboard_page_id"])
        evidence = local / "evidence"
        overview = write_overview_native(F.request, headers, overview_md, evidence)
        dash = ensure_dashboard_overview_entry(F.request, headers, overview["page_id"])
        listing = remove_dashboard_sections_listing(F.request, headers, evidence)
        daily_view = ND.ensure_dashboard_daily_brief_view(F.request, headers, day, index_id)
        if any(r.get("status") != "PASS" for r in (overview, dash, listing, daily_view)):
            raise RuntimeError("navigation_readback_failed")
        # Independent full-tree readback before promotion; no content is authored
        # after this point. Missing nested prose/URLs/checks blocks FINAL.
        for result in results:
            native = live_readback(F.request, headers, result["page_id"], markdown=section_md[result["section_key"]])
            if native["status"] != "PASS":
                raise RuntimeError("final_section_native_failed:" + json.dumps(native, ensure_ascii=False))
            result["native"] = native
        feedback_result = None
        if feedback_plan is not None:
            feedback_result = feedback_bridge.commit(feedback_plan)
        nonempty = {s["key"] for s in specs if s["rows"]}
        labels = {"ai": "AI", "security": "Cybersecurity", "local_models": "Open Models", "infrastructure": "Infrastructure", "semiconductors": "Semiconductors",
                  "developer_tools": "Developer Tools", "research": "Research", "outside_bubble": "Applied Technology", "community": "Community", "business": "Business"}
        derived = {"Research Included": "research" in nonempty or any("arxiv.org" in s["url"] for r in rows for s in r["sources"]),
                   "Reddit Included": any("reddit.com" in s["url"] for r in rows for s in r["sources"]),
                   "Top Topics": [labels[s["key"]] for s in specs if s["rows"] and s["key"] in labels], "Major Story Count": 0, "Watchlist Changes": 0}
        statuses = F._set_split_section_status(day, headers, index_id, results, stage="FINAL", brief_status="Published", review_needed=bool(outage))
        if len(statuses) != len(specs) or any(r.get("status") != "Published" for r in statuses):
            raise RuntimeError("section_final_status_failed")
        followup = FS.ensure_followup_section(F.request, headers, day=day, parent_brief_id=index_id, stage="FINAL",
                                              brief_status="Published", review_needed=bool(outage))
        st, meta = F.request(f"https://api.notion.com/v1/pages/{index_id}", "GET", headers)
        if st != 200:
            raise RuntimeError("index_metadata_read_failed")
        st, _ = F.request(f"https://api.notion.com/v1/pages/{index_id}", "PATCH", headers,
                          {"properties": F._properties_for_metadata(meta, derived, stage="FINAL", status="Published", review=bool(outage))})
        if st != 200:
            raise RuntimeError("final_metadata_failed")
        exact = query_all(F.request, headers, F.IDS["daily_data_source_id"], filter={"property": "Date", "date": {"equals": day}})
        st, final_meta = F.request(f"https://api.notion.com/v1/pages/{index_id}", "GET", headers)
        if (st != 200 or [pg["id"] for pg in exact] != [index_id] or F.page_date(final_meta) != day
                or F.select_prop(final_meta, "Stage") != "FINAL" or F.select_prop(final_meta, "Brief Status") != "Published"
                or final_meta.get("properties", {}).get("Review Needed", {}).get("checkbox") is not bool(outage)):
            raise RuntimeError("final_metadata_readback_failed")
        final = {"date": day, "data_day": data_day, "data_root": str(p.V3), "index_page_id": index_id, "index_url": final_meta.get("url"),
                 "exact_date_count": len(exact), "stage": "FINAL", "brief_status": "Published", "review_needed": bool(outage), "outage": outage,
                 "sections": {s["key"]: len(s["rows"]) for s in specs}, "issues": len(rows), "issue_ids": sorted(expected_ids),
                 "section_urls": {l["key"]: l["url"] for l in links}, "native": {r["section_key"]: r["native"] for r in results},
                 "overview": overview, "dashboard": dash, "sections_listing": listing, "daily_view": daily_view,
                 "retained_history_page_ids": retained, "backup": str(backup), "feedback_ledger": feedback_result,
                 "followup_section": {**followup, "preserved_feedback_blocks": preserved_feedback}}
        (local / "notion_publish_readback.json").write_text(json.dumps(final, ensure_ascii=False, indent=2))
        print(json.dumps(final, ensure_ascii=False, indent=2))
        print("PUBLISH PASS")
        return 0
    except (Exception, SystemExit):
        # Non-atomic writes may be partial, but never label their Index final.
        # Preserve all backups/checkpoints; do not replace bodies on failure.
        st, _ = F.request(f"https://api.notion.com/v1/pages/{index_id}", "PATCH", headers,
                          {"properties": F._properties_for_metadata(meta, {}, stage="DRAFT", status="Draft", review=True)})
        if st != 200:
            print("FAIL_CLOSED_DRAFT_RESET_FAILED", st, file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
