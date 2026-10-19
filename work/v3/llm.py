"""Codex OAuth client for the v3 pipeline (gpt-6-luna, reasoning max).

Uses Hermes's own Codex token resolution (auto-refresh), no API key.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import httpx

PROFILE = os.environ.get("TI_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HERMES_HOME", PROFILE)
sys.path.insert(0, os.environ.get("HERMES_AGENT_DIR", os.path.expanduser("~/.hermes/hermes-agent")))

MODEL = os.environ.get("V3_MODEL", "gpt-6-luna")
_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# medium: user decision after the extract comparison (same quality band as max, ~58% fewer output tokens, ~half the time).
# V3_EFFORT overrides for experiments; an unknown value falls back to medium instead of sending an invalid request.
EFFORT = os.environ.get("V3_EFFORT", "medium") if os.environ.get("V3_EFFORT", "medium") in _EFFORTS else "medium"
BASE = "https://chatgpt.com/backend-api/codex"

_lock = threading.Lock()
_cred: dict[str, Any] = {}
USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "errors": 0}


def _credentials(force: bool = False) -> dict[str, Any]:
    with _lock:
        if force or not _cred:
            from hermes_cli.auth import resolve_codex_runtime_credentials

            c = resolve_codex_runtime_credentials(force_refresh=force)
            _cred.clear()
            _cred.update(c)
        return dict(_cred)


def _headers() -> dict[str, str]:
    c = _credentials()
    token = c.get("api_key") or c.get("access_token")
    h = {
        "Authorization": f"Bearer {token}",
        "OpenAI-Beta": "responses=experimental",
        "originator": "codex_cli_rs",
        "Content-Type": "application/json",
    }
    acct = c.get("account_id") or c.get("chatgpt_account_id")
    if not acct:
        try:
            a = json.load(open(os.path.expanduser("~/.codex/auth.json")))
            acct = a.get("tokens", {}).get("account_id")
        except Exception:
            acct = None
    if acct:
        h["chatgpt-account-id"] = acct
    return h


class QuotaExceeded(ValueError):
    """Non-transient OAuth quota exhaustion, with the complete provider error."""

    def __init__(self, details: dict):
        self.details = dict(details)
        reset = self.details.get("resets_at")
        if reset is not None:
            try:
                self.details["reset_timestamp"] = dt.datetime.fromtimestamp(float(reset), dt.timezone.utc).isoformat()
            except (ValueError, TypeError, OverflowError):
                pass
        super().__init__("usage_limit_reached: " + json.dumps(self.details, ensure_ascii=False))


def _raise_quota(payload) -> None:
    if isinstance(payload, dict):
        if payload.get("type") == "usage_limit_reached" or payload.get("code") == "usage_limit_reached":
            raise QuotaExceeded(payload)
        for value in payload.values():
            _raise_quota(value)
    elif isinstance(payload, list):
        for value in payload:
            _raise_quota(value)


def call(system: str, user: str, *, effort: str | None = None, retries: int = 6, timeout: float = 600) -> str:
    body = {
        "model": MODEL,
        "instructions": system,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": user}]}],
        "reasoning": {"effort": effort or EFFORT},
        "stream": True,
        "store": False,
    }
    delay = 5.0
    for attempt in range(retries):
        try:
            text_parts: list[str] = []
            usage = None
            with httpx.stream("POST", f"{BASE}/responses", headers=_headers(), json=body, timeout=timeout) as r:
                if r.status_code == 401:
                    _credentials(force=True)
                    raise RuntimeError("401")
                if r.status_code != 200:
                    r.read()
                    try:
                        payload = json.loads(r.text)
                    except json.JSONDecodeError:
                        payload = {}
                    _raise_quota(payload)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                if r.status_code != 200:
                    raise ValueError(f"HTTP {r.status_code}: {r.text[:400]}")
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    ev = json.loads(data)
                    t = ev.get("type")
                    if t == "response.output_text.delta":
                        text_parts.append(ev.get("delta", ""))
                    elif t == "response.completed":
                        usage = (ev.get("response") or {}).get("usage")
                    elif t in ("response.failed", "error"):
                        _raise_quota(ev)
                        raise RuntimeError(f"stream error: {json.dumps(ev)}")
            with _lock:
                USAGE["calls"] += 1
                if usage:
                    USAGE["input_tokens"] += usage.get("input_tokens", 0)
                    USAGE["output_tokens"] += usage.get("output_tokens", 0)
                    USAGE["reasoning_tokens"] += (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
            return "".join(text_parts)
        except ValueError:
            with _lock:
                USAGE["errors"] += 1
            raise
        except Exception as e:  # transient
            if attempt == retries - 1:
                with _lock:
                    USAGE["errors"] += 1
                raise
            time.sleep(delay)
            delay = min(delay * 2, 120)
    raise RuntimeError("unreachable")


def parse_json(text: str) -> Any:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        if start < 0:
            raise
        end = max(text.rfind("}"), text.rfind("]"))
        return json.loads(text[start : end + 1])


def call_json(system: str, user: str, **kw) -> Any:
    last = None
    for _ in range(3):
        out = call(system, user, **kw)
        try:
            return parse_json(out)
        except Exception as e:
            last = e
            user = user + "\n\nReturn ONLY valid JSON."
    raise ValueError(f"JSON parse failed: {last}")


def parallel(items: list, fn: Callable, workers: int = 12, label: str = "") -> list:
    results: list = [None] * len(items)
    t0 = time.time()
    done = 0
    ex = ThreadPoolExecutor(max_workers=workers)
    quota_failed = False
    try:
        futs = {ex.submit(fn, it): i for i, it in enumerate(items)}
        for f in as_completed(futs):
            i = futs[f]
            try:
                results[i] = f.result()
            except QuotaExceeded:
                quota_failed = True
                for pending in futs:
                    pending.cancel()
                raise
            except Exception as e:
                results[i] = {"_error": str(e)}
            done += 1
            if done % 25 == 0 or done == len(items):
                print(f"[{label}] {done}/{len(items)} {time.time()-t0:.0f}s usage={USAGE}", flush=True)
    finally:
        ex.shutdown(wait=not quota_failed, cancel_futures=quota_failed)
    return results
