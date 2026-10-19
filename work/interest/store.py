"""Persistent state + scoring for Learned Interests (positive-only; unchecked is never negative).

State file: data/interest/state.json (separate from the legacy data/feedback/preference_ledger.json).

  articles[ "<day>|<feedback_id>" ] = {day, fid, title, concepts:[concept_id], status:"extracted"|"pending", text}
  concepts[ concept_id ]           = {label, aliases:[...], block_id, active:bool, source:"checkbox"|"request", created}
  pages[ page_id ]                 = {day, key, edited, checked:[fid]}     # read cache: only pages whose last_edited_time moved are re-read
  checked_now                      = [[day, fid], ...]                     # what is checked in Notion right now (recomputed every run)

Scores are never accumulated incrementally.  They are recomputed from ``checked_now`` every run, so a duplicate
(day, fid) cannot count twice and un-checking removes the signal automatically.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import unicodedata
from pathlib import Path

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[2])
STATE_PATH = ROOT / "data" / "interest" / "state.json"
HALF_LIFE_DAYS = 21.0
REQUEST_MARK = "(요청)"


def empty_state() -> dict:
    return {"version": 1, "articles": {}, "concepts": {}, "pages": {}, "checked_now": [], "processed_feedback": {}, "page_ids": {}, "updated_at": None}


def load_state(path: Path = STATE_PATH) -> dict:
    if not Path(path).exists():
        return empty_state()
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    for k, v in empty_state().items():
        state.setdefault(k, v)
    return state


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def norm(text: str) -> str:
    """Case/spacing/punctuation-insensitive key.  Hangul is kept (NFKC)."""
    t = unicodedata.normalize("NFKC", str(text or "")).casefold()
    t = re.sub(r"[^\w가-힣]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def article_key(day: str, fid: str) -> str:
    return f"{day}|{fid}"


def decay(age_days: int) -> float:
    return math.exp(-math.log(2) * max(0, age_days) / HALF_LIFE_DAYS)


def compute_scores(state: dict, today: str) -> dict[str, float]:
    """concept_id -> score, from the CURRENT checked set only.  A (day, fid) pair is a set member, so it counts once."""
    t = dt.date.fromisoformat(today)
    scores: dict[str, float] = {}
    for day, fid in {(d, f) for d, f in state.get("checked_now", [])}:
        art = state["articles"].get(article_key(day, fid))
        if not art:
            continue
        try:
            age = (t - dt.date.fromisoformat(day)).days
        except ValueError:
            continue
        w = decay(age)
        for cid in set(art.get("concepts") or []):
            if cid in state["concepts"]:
                scores[cid] = scores.get(cid, 0.0) + w
    return scores


def block_text(label: str, score: float | None, *, request: bool = False) -> str:
    base = f"{label} {REQUEST_MARK}" if request else label
    return base if score is None else f"{base} · {score:.1f}"


_TEXT_RE = re.compile(r"^(?P<label>.*?)(?: (?P<mark>\(요청\)))?(?: · (?P<score>\d+(?:\.\d+)?))?$")


def parse_block_text(text: str) -> tuple[str, bool, float | None]:
    m = _TEXT_RE.match(str(text or "").strip())
    if not m:
        return str(text).strip(), False, None
    return m["label"].strip(), bool(m["mark"]), float(m["score"]) if m["score"] else None


def find_concept(state: dict, label: str) -> str | None:
    """Exact (normalized) match on a label or alias."""
    key = norm(label)
    for cid, c in state["concepts"].items():
        if key == norm(c["label"]) or key in {norm(a) for a in c.get("aliases") or []}:
            return cid
    return None


def new_concept_id(state: dict) -> str:
    n = 1 + max([int(c[1:]) for c in state["concepts"] if re.fullmatch(r"c\d+", c)] or [0])
    return f"c{n:04d}"
