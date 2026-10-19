"""Concept extraction (LLM) and synonym normalization (embedding shortlist + LLM judge).

Both take injectable callables so tests never touch the network.  Defaults use the same Codex-OAuth Luna transport and the
same Ollama bge-m3 embedder as the 02:30 pipeline.  The embedder only NARROWS candidates; the LLM decides "same concept".
"""
from __future__ import annotations

import json
import sys
from typing import Callable

from . import store

EXTRACT_SYS = """You extract the durable TECHNICAL CONCEPTS a reader is interested in, from one technology-news article they bookmarked.
Return JSON: {"concepts": ["...", ...]}  (2 to 4 items, most important first).
Rules:
- Prefer reusable technical concepts, mechanisms or problem classes (e.g. "Agent Security", "Credential Exposure", "Coding Agents", "Mixture of Experts", "HBM Supply") over one-off facts.
- Do NOT return bare company, person, product or model names (no "OpenAI", "DeepSeek", "Hugging Face") unless the concept itself is that entity's technology category.
- Each concept is a short noun phrase, English, Title Case, at most 4 words.
- Specific beats generic: "Agent Harness Security" is better than "Security"; never return umbrella words like "AI", "Technology", "Software".
Return ONLY the JSON."""

JUDGE_SYS = """Decide whether a NEW interest label names the same concept as one of the EXISTING interest labels (a synonym, abbreviation,
reordering, or trivially narrower/wider wording that a reader would treat as one topic).
Return JSON: {"same_as": "<exact existing label>" or null}.
Same: "AI Agent Security" ~ "Agent Security"; "LLM Agents Safety" ~ "Agent Safety". Different: "Agent Security" vs "Agent Memory"; "Credential Exposure" vs "Credential Stuffing".
When unsure, answer null (a duplicate is cheaper than a wrong merge). Return ONLY the JSON."""


def _llm_json(system: str, user: str):
    here = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "v3")
    if here not in sys.path:
        sys.path.insert(0, here)
    import llm  # noqa: WPS433 - lazy: pulls in Codex credentials
    return llm.call_json(system, user, effort="low")


def extract_concepts(title: str, text: str, *, llm_json: Callable = _llm_json) -> list[str]:
    out = llm_json(EXTRACT_SYS, f"TITLE: {title}\n\nARTICLE:\n{text[:5000]}")
    items = out.get("concepts") if isinstance(out, dict) else out
    seen, clean = set(), []
    for c in items or []:
        label = " ".join(str(c).split())[:60]
        if label and store.norm(label) not in seen and len(label.split()) <= 6:
            seen.add(store.norm(label))
            clean.append(label)
    return clean[:4]


def _embed_shortlist(candidate: str, labels: list[str], k: int = 5, floor: float = 0.55) -> list[str]:
    here = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "v3")
    if here not in sys.path:
        sys.path.insert(0, here)
    import pipeline  # noqa: WPS433
    vecs = pipeline.embed([candidate] + labels)
    sims = vecs[1:] @ vecs[0]
    order = sims.argsort()[::-1][:k]
    return [labels[i] for i in order if sims[i] >= floor]


def _llm_judge(candidate: str, shortlist: list[str]) -> str | None:
    out = _llm_json(JUDGE_SYS, json.dumps({"new": candidate, "existing": shortlist}, ensure_ascii=False))
    same = out.get("same_as") if isinstance(out, dict) else None
    return same if same in shortlist else None


def resolve_concept(state: dict, candidate: str, *, shortlist_fn: Callable = _embed_shortlist, judge_fn: Callable = _llm_judge) -> tuple[str | None, str]:
    """Return (existing concept_id or None, how).  None means the label is genuinely new."""
    hit = store.find_concept(state, candidate)
    if hit:
        return hit, "exact"
    labels = [c["label"] for c in state["concepts"].values()]
    if not labels:
        return None, "empty"
    short = shortlist_fn(candidate, labels)
    if not short:
        return None, "no_candidates"
    same = judge_fn(candidate, short)
    if same:
        cid = store.find_concept(state, same)
        if cid:
            c = state["concepts"][cid]
            if store.norm(candidate) not in {store.norm(a) for a in c.get("aliases") or []}:
                c.setdefault("aliases", []).append(candidate)
            return cid, "judge"
    return None, "judge_different"
