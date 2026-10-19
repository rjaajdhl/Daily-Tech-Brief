"""Pre-flight for the v3 pipeline: the pipeline provisions its own runtime instead of relying on someone having started it.

Required (failure => exit 2 BEFORE anything is written):
  - Ollama bge-m3 embedding returns a 1024-d vector (Ollama is started here when it is down)
  - Codex OAuth LLM call succeeds
  - Notion API authenticates (read-only GET)
Optional (failure => run continues, recorded as degraded): SearXNG, Crawl4AI.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(Path(__file__).parent))

OLLAMA_BASE = os.environ.get("V3_OLLAMA_BASE", "http://localhost:11434")
OLLAMA_BIND = os.environ.get("V3_OLLAMA_BIND", "0.0.0.0:11434")
OLLAMA_LOG = ROOT / "data" / "v3" / "ollama-serve.log"
OLLAMA_CANDIDATES = ("/usr/local/bin/ollama", "/opt/homebrew/bin/ollama", "/Applications/Ollama.app/Contents/Resources/ollama")


class PreflightError(RuntimeError):
    def __init__(self, check: str, detail: str):
        super().__init__(f"{check}: {detail}")
        self.check, self.detail = check, detail


def _get(url: str, timeout: float = 5.0, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout)


def ollama_up(base: str = OLLAMA_BASE) -> bool:
    try:
        with _get(base + "/api/tags", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def find_ollama() -> str | None:
    return shutil.which("ollama") or next((p for p in OLLAMA_CANDIDATES if os.path.exists(p)), None)


def start_ollama() -> None:
    exe = find_ollama()
    if not exe:
        raise PreflightError("ollama", "executable not found")
    OLLAMA_LOG.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, OLLAMA_HOST=OLLAMA_BIND)
    log = open(OLLAMA_LOG, "ab")
    # detached so the server outlives this process (the cron run that started it)
    subprocess.Popen([exe, "serve"], env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)


def ensure_ollama(*, autostart: bool = True, wait: float = 90.0, base: str = OLLAMA_BASE) -> dict:
    started = False
    if not ollama_up(base):
        if not autostart:
            raise PreflightError("ollama", f"not reachable at {base} and autostart disabled")
        start_ollama()
        started = True
        t0 = time.time()
        while time.time() - t0 < wait:
            if ollama_up(base):
                break
            time.sleep(1.0)
        else:
            raise PreflightError("ollama", f"did not come up within {wait:.0f}s")
    body = json.dumps({"model": "bge-m3", "input": ["preflight"]}).encode()
    try:
        req = urllib.request.Request(base + "/api/embed", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            vec = json.load(r)["embeddings"][0]
    except Exception as e:
        raise PreflightError("ollama", f"bge-m3 embedding failed: {e}") from e
    if len(vec) != 1024:
        raise PreflightError("ollama", f"bge-m3 returned {len(vec)}-d vector")
    return {"ok": True, "started_by_preflight": started, "dim": len(vec)}


def check_llm() -> dict:
    import llm
    try:
        out = llm.call("Reply with exactly: OK", "ping", retries=2, timeout=120)
    except Exception as e:
        raise PreflightError("llm", f"Codex OAuth call failed: {e}") from e
    if "OK" not in out:
        raise PreflightError("llm", f"unexpected reply: {out[:80]!r}")
    return {"ok": True, "model": llm.MODEL, "effort": llm.EFFORT}


def _env() -> dict:
    vals = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def check_notion() -> dict:
    tok = _env().get("NOTION_API_KEY")
    if not tok:
        raise PreflightError("notion", "NOTION_API_KEY missing")
    try:
        with _get("https://api.notion.com/v1/users/me", timeout=20,
                  headers={"Authorization": "Bearer " + tok, "Notion-Version": "2026-03-11"}) as r:
            ok = r.status == 200
    except Exception as e:
        raise PreflightError("notion", f"auth failed: {e}") from e
    return {"ok": ok}


def check_optional() -> dict:
    env, out = _env(), {}
    sx = env.get("SEARXNG_URL", "").rstrip("/")
    try:
        q = urllib.parse.urlencode({"q": "test", "format": "json"})
        with _get(sx + "/search?" + q, timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as r:
            out["searxng"] = {"ok": r.status == 200 and bool(json.load(r).get("results"))}
    except Exception as e:
        out["searxng"] = {"ok": False, "error": str(e)[:120]}
    cu = env.get("CRAWL4AI_URL", "").rstrip("/")
    try:
        with _get(cu + "/health", timeout=15) as r:
            out["crawl4ai"] = {"ok": r.status == 200}
    except Exception as e:
        out["crawl4ai"] = {"ok": False, "error": str(e)[:120]}
    return out


def run(*, autostart: bool | None = None, llm_check: bool = True, notion_check: bool = True, optional: bool = True) -> dict:
    if autostart is None:
        autostart = os.environ.get("V3_PREFLIGHT_AUTOSTART", "1") != "0"
    t0 = time.time()
    rep: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "required": {}, "optional": {}, "degraded": []}
    rep["required"]["ollama"] = ensure_ollama(autostart=autostart)
    if llm_check:
        rep["required"]["llm"] = check_llm()
    if notion_check:
        rep["required"]["notion"] = check_notion()
    if optional:
        rep["optional"] = check_optional()
        rep["degraded"] = [k for k, v in rep["optional"].items() if not v.get("ok")]
    rep["seconds"] = round(time.time() - t0, 1)
    return rep


if __name__ == "__main__":
    try:
        print(json.dumps(run(), ensure_ascii=False, indent=1))
    except PreflightError as e:
        print(f"PREFLIGHT FAILED {e}", file=sys.stderr)
        sys.exit(2)
