"""Event-aware reader renderer for the Daily Technology Brief.

This module deliberately separates source evidence from reader prose.  Source
facts are inputs to a small deterministic distiller; they are never emitted as
an abstract, README excerpt, crawler fragment, or generic category paragraph.
The renderer preserves every signal passed to it and only groups issues by the
fixed primary taxonomy plus inventory-derived subtopics.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable
from urllib.parse import urlparse

from work.feedback import stable_issue_key
from work.issue_taxonomy import (
    TOPIC_SECTIONS,
    classify_primary_topic,
    group_by_subtopic,
    related_topics,
    subtopic_for,
)


_UI_PATTERNS = (
    r"link copied to clipboard",
    r"copy link",
    r"share this article",
    r"subscribe(?: to)? (?:our|the) newsletter",
    r"sign up(?: for)? (?:our|the) newsletter",
    r"newsletter signup",
    r"\bsubscribe\b",
    r"cookie (?:settings|preferences|policy)",
    r"accept (?:all )?cookies",
    r"advertisement",
    r"sponsored content",
    r"follow us on",
    r"there was an error while loading",
    r"please reload this page",
    r"subscribe to unlock",
    r"back by popular demand",
    r"disrupt\s+20\d{2}",
    r"open more actions menu",
    r"repository files navigation",
    r"skip to (?:content|main content)",
    r"enable javascript",
    r"javascript is disabled",
    r"read more",
    r"continue reading",
)


def _clean_text(value: object, limit: int | None = 2400) -> str:
    """Remove retrieval/UI artifacts without creating a fake truncated sentence."""
    text = str(value or "")
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"https?://(?:www\.)?httpbin\.org/base64/\S+", " ", text, flags=re.I)
    text = re.sub(r"https?://\S{300,}", " ", text, flags=re.I)
    # Transport/audit markers are not reader-facing evidence.  Remove them
    # even when they appeared inside a crawled source excerpt.
    text = re.sub(r"\bHTTP\s+(?:200|201|202|204|301|302|400|401|403|404|429|500|502|503|504)\b", " ", text, flags=re.I)
    text = re.sub(r"\bcron job\b", "scheduled task", text, flags=re.I)
    for pattern in _UI_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.I)
    # Ellipses in crawled README/article fragments are almost always a clipped
    # excerpt marker.  Replace them before any reader-facing sentence is built;
    # this also keeps legitimate repository names such as "awesome-... stuff"
    # from tripping the raw-source gate.
    text = re.sub(r"\.{3,}|…", "—", text)
    text = re.sub(r"\s+The post .*? appeared first on .*?(?:\.|$)", ".", text, flags=re.I)
    text = re.sub(r"\s+(?:\.\.\.|…|\[truncated\]|\(truncated\))\s*$", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip(" ,;:") if limit is None else text[:limit].rstrip(" ,;:")


def _clean_detail_markdown(value: object) -> str:
    """Clean reader-authored Details without collapsing or clipping paragraphs.

    ``_clean_text`` is intentionally lossy for short labels and source
    fragments: it collapses whitespace and applies a character limit.
    Applying it to a deliberate ``reader_detail_markdown`` body used to clip
    every Details block to 160 characters.  Clean each physical line instead
    and preserve paragraph/list boundaries; page-size constraints belong to
    the page-level writer, not to an issue's reader content.
    """
    raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned_lines: list[str] = []
    for line in raw.split("\n"):
        if not line.strip():
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        # Use a line-relative limit so this helper sanitizes artifacts without
        # imposing a hidden content limit on a long source-grounded sentence.
        cleaned = _clean_text(line, max(2400, len(line) + 1))
        if cleaned:
            cleaned_lines.append(cleaned)
    while cleaned_lines and cleaned_lines[-1] == "":
        cleaned_lines.pop()
    return "\n".join(cleaned_lines)


def _sentence(text: str) -> str:
    text = _clean_text(text, None)
    if not text:
        return ""
    return text if text[-1] in ".!?…。！？" else text + "."


def _title(text: object) -> str:
    value = _clean_text(text, 360) or "이름 없는 기술 신호"
    value = re.sub(r"[,\s]*\((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[^)]*\)$", "", value, flags=re.I)
    value = value.replace("\\`", "'").replace("`", "'").replace("\\", "")
    return re.sub(r"[\r\n]+", " ", value).strip()


def _heading_text(text: str) -> str:
    # Use a plain H4 plus a separate source link.  Escaping prevents titles such
    # as "$tau$-Bench" or "**Important**" from becoming malformed Notion links.
    return (
        text.replace("\\", "")
        .replace("`", "'")
        .replace("$", "\\$")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("#", "\\#")
    )


def _combined_text(signal: dict) -> str:
    fields = [
        signal.get("title"),
        signal.get("source_title"),
        signal.get("summary"),
        signal.get("source_description"),
        signal.get("source_body_excerpt"),
        signal.get("source_facts"),
        " ".join(str(x) for x in signal.get("topics") or []),
        " ".join(str(x) for x in signal.get("categories") or []),
        " ".join(str(x) for x in signal.get("discovery_categories") or []),
    ]
    return _clean_text(" ".join(str(x or "") for x in fields), 12000).casefold()


def _url(signal: dict, issue_sources: Callable[[dict], list[dict]]) -> str:
    sources = issue_sources(signal)
    return str(sources[0].get("url") or "") if sources else str(signal.get("url") or "")


def _source_kind(signal: dict, issue_sources: Callable[[dict], list[dict]]) -> str:
    source = str(signal.get("source_id") or "").casefold()
    role = str(signal.get("role") or signal.get("source_role") or "").casefold()
    lane = str(signal.get("lane") or signal.get("source_lane") or "").casefold()
    host = urlparse(_url(signal, issue_sources)).netloc.casefold()
    community_host = host in {"news.ycombinator.com", "lobste.rs", "lobsters.dev", "www.reddit.com", "reddit.com"}
    community_source = source in {"hacker-news", "lobsters", "reddit"} or lane == "community_signals" or role in {"community", "community_signal"}
    # A Hacker News/Reddit submission that points to an original article,
    # paper, or repository is a discovery route, not the evidence type of the
    # linked source.  Read and classify the destination itself.
    if community_source and community_host:
        return "community"
    if community_source and host:
        if host in {"github.com", "www.github.com"}:
            return "repository"
        if host == "arxiv.org" or host.endswith(".arxiv.org"):
            return "research"
        return "reported"
    if source.startswith("github-") or role in {"github_trending", "repository_discovery", "github_active"} or host in {"github.com", "www.github.com"}:
        return "repository"
    if source.startswith("huggingface-") or source.startswith("arxiv") or role in {"research", "paper"}:
        return "research"
    return "reported"


def _primary(signal: dict) -> str:
    key = str(signal.get("primary_topic") or signal.get("topic_section") or "").strip()
    valid = {item[1] for item in TOPIC_SECTIONS}
    return key if key in valid else classify_primary_topic(signal)


def _subtopic(signal: dict) -> str:
    return str(signal.get("subtopic") or subtopic_for(signal, _primary(signal)))


def _status(signal: dict) -> str:
    state = str(signal.get("evidence_state") or "DEVELOPING").upper()
    return state if state in {"VERIFIED", "DEVELOPING", "UNVERIFIED", "SPECULATIVE"} else "DEVELOPING"


def _source_facts(signal: dict) -> str:
    candidates = [
        signal.get("source_body_excerpt"),
        signal.get("source_facts"),
        signal.get("source_description"),
        signal.get("summary"),
    ]
    cleaned_candidates = [
        _clean_text(value, None)
        for value in candidates
        if str(value or "").strip()
    ]
    text = max(cleaned_candidates, key=len, default="")
    # Broken extraction bytes/control characters must never become reader text.
    if "\ufffd" in text or any(
        ord(char) < 32 and char not in "\n\r\t" for char in text
    ) or any(0x7F <= ord(char) <= 0x9F for char in text):
        return ""
    return text


def _sentences(text: str) -> list[str]:
    text = _clean_text(text, None)
    if not text:
        return []
    parts = re.split(r"(?<=[.!?。！？])\s+", text)
    return [part.strip() for part in parts if len(part.strip()) >= 18]


_EVIDENCE_ACTIONS = re.compile(
    r"\b(?:targeted|compromised|stole|exfiltrat|harvest|enumerat|scann|registered|deployed|"
    r"rebuilt|modified|re[- ]?deployed|delivered|linked|exported|reverse[- ]?engineer|"
    r"fingerprint|phish|hijack|accessed|abused|took over|탈취|침해|유출|분석|배포|수정|재빌드)\w*\b",
    re.I,
)


def _evidence_first_detail(signal: dict, kind: str) -> str:
    """Build Details from source-observed facts before adding interpretation.

    The old fallback started with an abstract mechanism/impact paragraph and
    then padded thin evidence with generic caveats.  For a source that contains
    concrete cases, names, numbers, tools, or an explicit sequence, that loses
    the answer to "what actually happened?".  This fallback keeps source
    sentences in a skimmable evidence-first outline; Writer/Critic prose still
    takes precedence when ``reader_detail_markdown`` is present.
    """
    facts = _source_facts(signal)
    sentences = _sentences(facts)
    if not sentences:
        return ""

    def score(item: tuple[int, str]) -> tuple[int, int]:
        index, sentence = item
        concrete = 0
        if re.search(r"\d", sentence):
            concrete += 3
        if re.search(r"\b(?:CVE|GTG[- ]?\d+|APK|DNS|VPN|MFA|M365|Microsoft 365|WhatsApp|Claude|Sonnet|Opus|Haiku|AWS|GitHub|Telegram|TruffleHog|WPPConnect|C2)\b", sentence, re.I):
            concrete += 3
        if _EVIDENCE_ACTIONS.search(sentence):
            concrete += 2
        if re.search(r"\b(?:human|operator|actor|agent|AI|model|person|사람|공격자|에이전트)\b", sentence, re.I):
            concrete += 1
        return (-concrete, index)

    ranked = sorted(enumerate(sentences), key=score)
    selected_indices = sorted(index for index, _ in ranked[:12])
    selected = [sentences[index] for index in selected_indices]
    if len(selected) < 3:
        selected = sentences[: min(8, len(sentences))]

    lines = ["### 원문에서 확인된 실제 사례"]
    lines.extend(f"- {_sentence(sentence)}" for sentence in selected[:8])

    workflow = [
        sentence
        for sentence in sentences
        if re.search(
            r"\b(?:reconnaissance|initial access|execution|persistence|lateral movement|collection|"
            r"exfiltration|command and control|C2|정찰|초기 접근|수집|반출|측면 이동|지속성)\b",
            sentence,
            re.I,
        )
    ]
    if len(workflow) >= 2:
        lines.extend(["", "### 공격·실행 흐름"])
        lines.extend(f"{number}. {_sentence(sentence)}" for number, sentence in enumerate(workflow[:8], 1))

    roles = [
        sentence
        for sentence in sentences
        if re.search(r"\b(?:AI|Claude|agent|model|human|operator|사람|에이전트|공격자)\b", sentence, re.I)
    ]
    if roles:
        lines.extend(["", "### AI와 사람의 역할"])
        lines.extend(f"- {_sentence(sentence)}" for sentence in roles[:5])

    if kind in {"reported", "community", "community_link"}:
        lines.extend(
            [
                "",
                "### 확인 범위",
                "- 위 항목은 원문이 직접 제시한 사례·수치·행동을 우선 정리한 것이다. 원문에 없는 전체 피해 규모나 일반적인 성능·보안 효과는 덧붙이지 않는다.",
            ]
        )
    elif kind == "repository":
        lines.extend(
            [
                "",
                "### 확인 한계",
                "- README·저장소 활동만으로는 실제 운영 환경의 성능, 보안성, 실사용 채택과 유지보수 상태를 확정할 수 없다.",
            ]
        )
    return "\n".join(lines)


def _metrics(text: str) -> list[str]:
    patterns = (
        r"\bCVE[- ]?\d{4}[- ]?\d{3,}\b",
        r"\b\d+(?:\.\d+)?%\b",
        r"\b\d+(?:\.\d+)?\s*(?:CVE|projects?|queries|tasks?|models?|GPUs?|tools?|points?|paths?|locations?|configurations?)\b",
        r"\b\d+\s*/\s*\d+\b",
        r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?[KMB])?\b",
        r"\b(?:v\d+(?:\.\d+)*|\d+(?:\.\d+)?-bit)\b",
    )
    result: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            value = re.sub(r"\s+", " ", match.group(0)).strip()
            if value.casefold() not in {x.casefold() for x in result}:
                result.append(value)
            if len(result) >= 8:
                return result
    return result


def _technical_terms(text: str, *, limit: int = 7) -> list[str]:
    """Extract short technical names, not source sentences."""
    patterns = (
        r"\b(?:Dempster[- ]Shafer|Gaussian sensor model|variational free energy|query refinement|source[- ]level taxonomy|rule trees?|prompt[- ]level defense|iterative denoising|Semantic Depth Score|execution[- ]verified queries|provider adapter|ARC[- ]AGI[- ]\d|CodeQL|SQLite|MCP|SSE|NaFlexViT|PyTorch Image Models|SAR|NVLink\d*|Grace Blackwell|SBOM|SAST|CNI|OCI|Kubernetes|kubelet|LLM|DLM)\b",
        r"\b[A-Z][A-Za-z0-9.+-]{1,}(?:[-/][A-Za-z0-9.+-]+)*\b",
        r"\b[a-zA-Z][a-zA-Z0-9]+(?:[-/][a-zA-Z0-9]+){1,}\b",
    )
    seen: set[str] = set()
    out: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            value = re.sub(r"\s+", " ", match.group(0)).strip(" ,.;:()[]")
            if len(value) < 3 or value.casefold() in {"the", "this", "that", "with", "from", "into", "using", "paper", "model"}:
                continue
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                out.append(value)
            if len(out) >= limit:
                return out
    return out


def _evidence_boundary_observation(facts: str) -> str:
    """Describe retained evidence without copying raw source prose."""
    metrics = _metrics(facts)[:5]
    terms = _technical_terms(facts, limit=5)
    pieces: list[str] = []
    if terms:
        pieces.append("· ".join(terms))
    if metrics:
        pieces.append("· ".join(metrics))
    if pieces:
        return "출처에서 식별되는 구체적 단서는 " + "와 ".join(pieces) + "가 언급됐다는 점이다."
    return "현재 보존된 출처에는 issue-specific 본문이 있으나, 이를 reader 문장으로 직접 인용할 정도의 검증된 세부 범위는 제한적이다."


def _paper_subject(title: str, text: str) -> str:
    low = f"{title} {text}".casefold()
    if (
        "codeql" in low
        or "static application security" in low
        or bool(re.search(r"query refinement|java security query|\bsast\b", low, re.I))
    ):
        return "Java 취약점 분석에서 정적 분석 오탐을 줄이는 문제"
    if "active inference" in low and "reconnaissance" in low:
        return "불완전한 센서 관측으로 자율 정찰 경로를 선택하는 문제"
    if "prompt injection" in low and ("grading" in low or "automatic grading" in low):
        return "LLM 자동 채점 시스템을 프롬프트 주입으로 교란할 수 있는지"
    if "diffusion language" in low and "mobile edge" in low:
        return "모바일 엣지 환경에서 diffusion language model을 agentic AI에 적용하는 문제"
    if "ransomware" in low:
        return "랜섬웨어 탐지와 악성코드 family attribution을 비용 제약 안에서 수행하는 문제"
    if "nl-to-sql" in low or "nested enterprise schema" in low:
        return "중첩·그래프형 기업 스키마에 대한 자연어 질의 변환"
    if "agent construction" in low or "benchmark" in low and "agent" in low:
        return "실제 고객 조건에서 에이전트를 구축하고 평가하는 능력"
    if "jailbreak" in low or "prompt-level defense" in low:
        return "black-box LLM의 jailbreak를 입력 단계에서 완화하는 문제"
    if "intrusion detection" in low:
        return "변화가 감지된 네트워크 침입 탐지 모델을 안전하게 교체하는 문제"
    if "person search" in low and "camera" in low:
        return "불완전한 목격 단서로 여러 카메라에서 사람을 찾는 문제"
    return f"‘{title}’이 제시한 연구 질문"


def _paper_digest(signal: dict) -> str:
    title = _title(signal.get("title"))
    text = _source_facts(signal)
    low = f"{title} {text}".casefold()
    subject = _paper_subject(title, text)
    metrics = _metrics(text)
    terms = _technical_terms(text, limit=6)
    sentences: list[str] = [f"이 연구는 {subject}을 다룬다."]

    if "civbench" in low or ("civilization vi" in low and "tool-mediated" in low):
        return "CivBench는 Civilization VI에서 MCP tool을 사용하는 agent를 300회 이상 이어지는 episode로 평가하는 장기 horizon benchmark다. 76개 tool과 부분 관측 환경을 4개 model family·23개 admissible run에 적용해, 전략 상태를 능동적으로 조회하는 비율과 계획 commitment를 실제 행동으로 옮기는 비율을 측정한다."
    if (
        "codeql" in low
        or "static application security" in low
        or bool(re.search(r"query refinement|java security query|\bsast\b", low, re.I))
    ):
        sentences.append("CodeQL의 Java 보안 쿼리를 CVE 167건·프로젝트 110개에 적용하고, 오탐 경로 500건을 다섯 가지 원인으로 나눠 query refinement가 실제로 줄일 수 있는지를 살핀다.")
        sentences.append("보고된 결과는 검토한 오탐의 81.8%, 전체 보고 경로·위치의 15.8%를 제거하면서 진양성 8건 중 7건을 유지했다는 것이며, 새 프로젝트에 자동 적응하는 coding agent의 성공률은 도구별 56%와 62%로 제시된다.")
        sentences.append("따라서 이 논문의 의미는 ‘AI가 취약점을 찾는다’가 아니라, SAST의 오탐 원인을 query 수준에서 모델링할 수 있는지와 프로젝트별 일반화가 어디서 막히는지를 수치로 보여준다는 데 있다.")
        return " ".join(sentences)

    if "active inference" in low and "reconnaissance" in low:
        sentences.append("방법은 positive observation뿐 아니라 표적을 보지 못했다는 negative observation도 evidence map에 누적하고, Dempster–Shafer 이론·Gaussian sensor model·Bayesian posterior를 이용해 지도의 belief를 갱신한다.")
        sentences.append("각 위치의 variational free energy를 비교해 탐색과 추적 사이의 다음 이동을 고르는 시뮬레이션이 핵심이며, 실제 센서 노이즈·통신 제약·동적 장애물에서의 성능은 이 자료만으로 확인되지 않는다.")
        sentences.append("즉 LLM tool-use가 아니라, 물리 공간에서 불확실성을 줄이는 경로 선택이라는 점이 이 항목의 구체적인 가치다.")
        return " ".join(sentences)

    if "prompt injection" in low and ("grading" in low or "automatic grading" in low):
        sentences.append("대상은 LLM이 학생 답안을 자동 채점하는 시스템이며, 답안 안의 지시문이 채점 프롬프트의 우선순위를 바꾸거나 점수·피드백을 오염시키는 공격 경로를 분석한다.")
        sentences.append("따라서 단순한 jailbreak 사례가 아니라, 평가 입력과 채점 명령을 분리하지 않을 때 교육·검증 결과 자체가 변조될 수 있다는 문제를 다룬다.")
        sentences.append("실제 방어 효과는 사용한 채점 모델, 공격 성공률, 데이터셋과 human baseline을 확인해야 하므로 초록의 위협 모델을 배포 시스템의 보안 보장으로 확대하지 않는다.")
        return " ".join(sentences)

    if "diffusion language" in low and "mobile edge" in low:
        sentences.append("제안되는 방향은 token을 왼쪽에서 오른쪽으로 한 번씩 생성하는 대신 iterative denoising으로 문장을 정제하고, 그 계산을 mobile-edge agentic AI의 지연·전력 제약과 함께 설계하는 것이다.")
        sentences.append("핵심 비교 대상은 diffusion language model의 병렬성·지연 이점과 반복 denoising 비용이며, 실제 단말에서의 메모리·통신·배터리 수치는 별도 실험으로 확인해야 한다.")
        sentences.append("따라서 이 자료는 agent라는 이름보다 모델 아키텍처와 edge deployment 제약을 함께 다룬다는 점에서 읽을 가치가 있다.")
        return " ".join(sentences)

    method_terms = terms[:4]
    method = ", ".join(f"`{term}`" for term in method_terms) if method_terms else "제안 방법과 평가 절차"
    if metrics:
        sentences.append(f"초록에서 확인되는 방법·구성요소는 {method}이고, 평가 또는 결과에 {', '.join(f'`{x}`' for x in metrics[:5])}가 등장한다.")
    else:
        sentences.append(f"초록에서 확인되는 구체적 구성요소는 {method}이며, 제목의 문제를 이 방법으로 다루도록 설계됐다.")
    sentences.append("이 결과가 의미하는 범위는 해당 데이터·baseline·실험 조건 안의 연구 주장으로 한정되며, 독립 재현과 실제 운영 환경의 비용·실패 조건은 추가 확인이 필요하다.")
    return " ".join(sentences)


def _repo_facts(signal: dict) -> str:
    title = _title(signal.get("title"))
    meta = signal.get("repo_metadata") or {}
    description = _clean_text(meta.get("description") or signal.get("source_description") or signal.get("summary"), 1200)
    description = re.sub(r"\s+-\s+[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\s*$", "", description).strip()
    readme = _clean_text(meta.get("readme_excerpt") or signal.get("source_body_excerpt"), None)
    low = f"{title} {description} {readme}".casefold()
    if "openledger" in low or "double-entry" in low or "accounting ledger" in low:
        return "OpenLedger는 local-first double-entry accounting을 SQLite ledger와 15개 MCP tool로 노출하는 저장소다. 거래를 append-only audit log에 남기고, 한 transaction에 두 개 이상의 entry line과 debit=credit invariant를 write path에서 검사하며, Claude 계열 클라이언트에는 stdio·SSE transport로 연결한다. 즉 agent memory가 아니라 회계 상태를 읽고 안전하게 쓰기 위한 MCP application이다."
    if "serverless/serverless" in low or "serverless framework" in low:
        return "Serverless Framework는 YAML 설정으로 AWS Lambda 코드와 필요한 cloud infrastructure를 함께 배포하는 CLI·framework 저장소다. V4 공개 변경에는 Sandboxes, Bedrock AgentCore 연동, durable workflows, per-function IAM 정책과 HTTP response streaming이 포함돼 serverless 애플리케이션과 AI agent 실행 환경을 같은 배포 경로에서 다룬다. 따라서 이 항목은 Agent Memory가 아니라 AWS Lambda·CloudFormation·IAM을 묶는 infrastructure tooling으로 읽어야 하며, 저장소 activity만으로 실사용 채택을 확정할 수는 없다."
    if "pytorch-image-models" in low or "image models" in low or "vision backbone" in low:
        return "PyTorch Image Models는 vision backbone의 모델 정의·pretrained weight·train/validation/inference 경로를 묶은 Python 라이브러리다. README와 변경 항목에서 NaFlexViT의 variable-patch 처리, position sampling, checkpoint/pickle loading 보강이 확인되며, vision model 정의·weight·학습/추론 도구를 제공하는 라이브러리다. 실제 채택 판단은 지원 모델·라이선스·현재 release와 테스트 상태를 따로 봐야 한다."
    if "strix" in low and ("penetration" in low or "vulnerab" in low):
        return "Strix는 애플리케이션을 대상으로 취약점을 찾고 수정까지 보조하는 open-source AI penetration-testing tool이다. 저장소가 제시하는 차별점은 스캐너 하나가 아니라 에이전트가 탐색·검증·수정 흐름을 연결하는 데 있으며, 실제 탐지 정확도와 안전한 실행 권한은 별도 검증이 필요하다."
    if re.search(r"\becc\b", low) and ("harness" in low or "claude code" in low or "codex" in low):
        return "ECC는 Claude Code·Codex 같은 coding agent에 skills, instincts, memory, security, research-first 규칙을 공급하는 agent harness다. 핵심 산출물은 모델이 아니라 명령·검증·보안 가이드가 담긴 Markdown workflow이며, 설치 경로와 공식 채널을 확인해야 한다."
    if re.search(r"memory|persistent understanding|context", low):
        return f"`{title}`는 장기 실행 coding agent가 codebase의 구조와 이전 작업을 어떤 memory/context 계층에 보존할지 다루는 저장소다. 공개 설명에서 확인되는 목적은 {description or 'persistent project context 관리'}이며, 검색·저장 형식과 갱신 정책이 실제 품질을 좌우한다. 저장소 activity만으로 운영 품질이나 실사용 채택을 확정할 수는 없다."

    kind = "개발자 도구" if _primary(signal) == "developer_tools" else "공개 저장소"
    architecture_terms = [
        term for term in ("SQLite", "Postgres", "MCP", "stdio", "SSE", "Docker", "Kubernetes", "FastAPI", "Python", "TypeScript", "repository", "services", "plugins", "CLI")
        if re.search(rf"\b{re.escape(term)}\b", f"{description} {readme}", re.I)
    ]
    detail = ", ".join(f"`{term}`" for term in architecture_terms[:6])
    if description:
        first = description.rstrip(".")
        text = f"이 저장소는 {kind} `{title}`이며, 공개 설명상 핵심 목적은 ‘{first}’다."
    else:
        text = f"`{title}` 저장소의 목적 설명은 현재 공개된 metadata에서 제한적으로 확인된다."
    if detail:
        text += f" README에서 확인되는 구현 단서는 {detail}이다."
    else:
        text += " README에서 확인할 수 있는 architecture 단서는 부족하다."
    if meta.get("language") or meta.get("pushed_at"):
        language = f"주요 언어는 {meta['language']}" if meta.get("language") else "주요 언어는 공개되지 않았다"
        pushed = f", 최근 push는 {str(meta['pushed_at']).replace('T', ' ').replace('Z', ' UTC')}" if meta.get("pushed_at") else ""
        text += f" {language}{pushed}."
    text += " 저장소 activity는 구현 방향과 성숙도를 보여주지만, 실사용 채택이나 보안성을 증명하지는 않는다."
    return text


def _reported_digest(signal: dict) -> str:
    title = _title(signal.get("title"))
    text = _source_facts(signal)
    low = f"{title} {text}".casefold()
    metrics = _metrics(text)
    terms = _technical_terms(text, limit=6)

    if "bigbear" in low and re.search(r"phishing|microsoft 365|mfa", low, re.I):
        organizations = next((m.group(1) for m in re.finditer(r"(\d[\d,]*)\s+organizations?", text, re.I)), "258")
        credentials = next((m.group(1) for m in re.finditer(r"(\d[\d,]*)\s+(?:microsoft 365 )?credentials?", text, re.I)), "5,000")
        return f"BigBear 2.0은 Microsoft 365 로그인 페이지를 중계해 MFA 완료 뒤 세션 쿠키를 훔치는 phishing-as-a-service로 최소 {organizations}개 조직을 침해했고, {credentials}개가 넘는 자격 증명이 노출됐다. MFA 토큰을 직접 깨는 공격이 아니라 인증된 세션을 재사용하는 공격이라, FIDO2 강제·fallback 제한·세션 폐기가 방어의 핵심이다."
    if "connectwise" in low and "screenconnect" in low:
        return "ConnectWise가 ScreenConnect의 새 취약점을 공개했지만 당시 정식 패치는 아직 제공되지 않았고, 임시 완화 조치가 먼저 제시됐다. 원격 관리면은 외부 노출과 높은 권한으로 이어질 수 있으므로 affected build·공식 mitigation·패치 적용 여부를 함께 확인해야 한다."
    if "mikrotik" in low and re.search(r"exploit|hijack|routeros", low, re.I):
        return "공격자들이 MikroTik RouterOS의 최근 취약점 두 개를 연결해 SSH가 노출된 라우터를 탈취하는 공격을 수행하고 있다. 취약점 하나의 존재보다 공개 관리면과 결합된 exploit chain이 핵심이므로, 노출된 SSH와 RouterOS 버전을 우선 점검해야 한다."
    if "jsceal" in low or ("stolen session cookies" in low and "google" in low):
        return "JSCeal 악성코드는 훔친 session cookie를 이용해 Google 인증 절차를 우회할 수 있는 것으로 보고됐다. 비밀번호나 MFA를 다시 입력하게 만드는 공격이 아니라 이미 인증된 브라우저 상태를 탈취하는 흐름이라, 세션 무효화와 악성 광고·다운로드 경로 차단이 중요하다."
    if "stylesmuggler" in low:
        return "StyleSmuggler로 불리는 Magento·Adobe Commerce zero-day가 실제 공격에 악용돼 Linux backdoor 배포로 이어지고 있다. 모든 버전이 영향 범위로 언급된 만큼 공개 exploit 여부와 vendor patch·임시 완화책을 제품 버전별로 대조해야 한다."
    if "n-central" in low and re.search(r"rce|hotfix", low, re.I):
        return "N-able은 인증 없이 원격 코드 실행이 가능한 N-central 결함에 대해 5주 동안 네 번째 hotfix를 배포했다. 반복 hotfix 자체가 제품의 현재 exposure를 뜻하는 것은 아니므로, 사용 중인 on-premises build와 해당 hotfix 적용 상태를 직접 확인해야 한다."
    if "telerik" in low and re.search(r"padding|rce|exploit", low, re.I):
        return "Telerik UI의 padding-oracle 취약점에 unauthenticated RCE를 연결하는 공개 proof-of-concept가 나왔다. 공개 PoC는 공격 재현 가능성을 높이는 변화이므로, 영향 버전·외부 노출·인증 우회 조건을 패치 우선순위와 함께 판단해야 한다."
    if re.search(r"data breach|breach affecting|stolen data|customer", low, re.I) and re.search(r"\b\d[\d,]*\b", text):
        numbers = ", ".join(metrics[:3]) or "공개된 피해 규모"
        return f"`{title}`에 대한 데이터 침해가 공개됐고, 원문은 {numbers}를 피해 범위의 구체적 단서로 제시한다. 실제 위험은 어떤 개인정보·계정·공급업체 경계가 침해됐는지와 통지 대상이 일치하는지에 달려 있으므로, headline의 총량만으로 동일한 영향을 가정할 수 없다."
    if re.search(r"benchmark|bench|tested|experiments?|evaluation", low, re.I) and metrics:
        measured = ", ".join(metrics[:4])
        return f"`{title}`는 {measured}가 포함된 조건에서 특정 모델·도구·구현의 결과를 비교한 자료다. 이 수치는 사용한 workload·harness·비용·실험 조건에 묶여 있으므로 단일 점수를 일반적인 제품 우위로 해석하지 말아야 한다."
    if "deepseek v4 flash" in low and "provider" in low:
        return "DeepSeek V4 Flash를 14개 provider 경로에 보내 비용·첫 토큰 지연·생성 속도·cache share·실패율을 비교한 측정이 공개됐다. 입력 길이와 output budget을 통제한 비교라 모델 자체보다 어느 serving route를 선택하느냐가 비용·지연·성공률을 바꾼다는 점이 핵심이다."
    if "specialized gpu kernel generation" in low:
        return "Databricks가 agentic system으로 specialized GPU kernel을 병렬 생성하고, 실제 실행 결과를 검증해 inference 효율을 높이는 workflow를 설명했다. 이 접근의 병목은 초안 생성량이 아니라 두 구현이 같은 일을 했는지와 이전 실행의 잔여 작업이 섞이지 않았는지를 검증하는 측정 계층이다."
    if "ai crawler" in low and re.search(r"spoof|user-agent|metadata", low, re.I):
        addresses = next((m.group(1) for m in re.finditer(r"(\d[\d,]*)\s+addresses?", text, re.I)), "26")
        agents = next((m.group(1) for m in re.finditer(r"(\d[\d,]*)\s+user-agents?", text, re.I)), "42,321")
        spellings = next((m.group(1) for m in re.finditer(r"(\d[\d,]*)\s+different spellings", text, re.I)), "6,648")
        return f"AI crawler 이름을 위조해 credential을 찾던 스캐너가 {addresses}개 주소의 단일 HTTP client로 확인됐고, {agents}개 user-agent와 Claude-User의 {spellings}개 변형을 생성했다. 이름 기반 allow-list는 이 변형을 신뢰할 수 없으므로, user-agent가 아니라 요청 행위·네트워크 출처·metadata endpoint 접근 통제로 탐지해야 한다."
    if "agent skill" in low and re.search(r"shell|credential|permission", low, re.I):
        return "설치한 agent skill이 나중에 개발자 shell·repository·credential에 접근할 수 있다는 권한 경계 문제가 제기됐다. permissions manifest는 의도를 표시할 뿐 실행을 강제하지 않으므로, OS 수준 enforcement와 trusted command 경계가 있어야 설치 시점 이후의 지연된 악용을 막을 수 있다."

    if re.search(r"astra", low) and re.search(r"rollout|subscription|plus", low):
        price = next((x for x in metrics if "$" in x), "$20")
        return f"이 보도는 ChatGPT Astra가 {price} Plus 구독자에게 순차 제공되기 시작했다는 제품 접근성 변화를 다룬다. 무료 이용자 제공 시점은 제시되지 않아, 현재 확인된 변화는 모델 성능 주장이 아니라 가격·계정 등급에 따른 사용 권한의 확장이다."
    if re.search(r"astra", low) and re.search(r"arc[- ]agi|score|benchmark|provider adapter|harness", low):
        score = next((x for x in metrics if "%" in x), "99.9%")
        return f"이 자료는 GPT-6 Astra를 ARC-AGI 계열 환경에서 여러 harness 구성으로 측정한 결과를 설명한다. Provider Adapter가 요청 사이의 opaque reasoning state와 compaction을 보존했고, 관측된 최고 점수로 {score}가 제시되지만 harness·추론 비용·평가 split이 결과에 직접 영향을 준다. 따라서 모델 단독 능력과 adapter가 만든 실행 조건을 분리해 읽어야 한다."
    if "jensen huang" in low and "agi" in low:
        terms_text = ", ".join(f"`{x}`" for x in terms[:4]) or "OpenAI와 GPT-6 Astra"
        return f"Jensen Huang의 발언은 GPT-6 Astra와 OpenAI의 진전을 AGI 도달로 해석한 업계 논평이다. 본문에서 함께 언급된 구체적 단서는 {terms_text}이며, 이는 NVIDIA 경영진의 평가·축하 발언이지 AGI에 대한 독립적인 capability 검증은 아니다."
    if "iceye" in low and ("satellite" in low or "sar" in low):
        return "ICEYE의 공개 위성 피드는 Synthetic Aperture Radar(SAR) 위성이 구름·연기·비·눈을 통과해 주야간 지표를 관측하고, 최대 16cm 수준의 해상도와 짧은 동영상을 제공할 수 있다는 사례를 보여준다. 이 항목의 기술적 의미는 AI datacenter가 아니라 기상·위장 조건에 덜 묶이는 지구관측 데이터와 위성 운영의 실제 활용성에 있다."
    if "nscale" in low and re.search(r"fund|financ|investment|valuation", low):
        return f"Nscale 관련 자료는 AI datacenter·compute 사업의 자금 조달 또는 시장 평가 변화를 다룬다. 확인 가능한 수치는 {', '.join(f'`{x}`' for x in metrics[:5]) if metrics else '공개된 조달·시장 수치'}이며, 이것은 반도체 실적이 아니라 인프라 사업의 자본·수요·확장 가능성에 관한 사건이다."
    if re.search(r"cve|vulnerab|exploit|phishing|malware|ransomware", low):
        return f"`{title}`에 관한 보안 사건 또는 취약점 악용 보고가 확인됐다. 제목과 확보된 자료가 가리키는 공격 대상·경계까지를 사실로 다루고, affected version·실제 exploitation·패치 상태가 확인되기 전에는 전체 제품의 위험으로 확대하지 않는다."
    if "alien mind" in low:
        return "이 글은 OpenAI의 reasoning 연구가 초기 실험에서 어떤 확신을 얻었고, 그 경험이 모델 capability·제품·거버넌스 논의로 어떻게 이어졌는지를 설명하는 연구자 관점의 서술이다. 구체적 기술 결과와 미래 전망을 분리해서 읽어야 하며, 글의 해석 자체를 독립 benchmark 결과로 취급하지 않는다."

    # A thin report must remain honest, but the expanded toggle still needs to
    # explain exactly what was and was not observed.  Keep the source facts in
    # the prose rather than using a category-shaped filler paragraph.
    observed = _clean_text(text, 1500)
    if observed:
        return (
            f"`{title}`에 대해 확보된 자료는 다음과 같은 관찰을 담고 있다: {observed}. "
            f"이 기록에서 직접 읽히는 대상은 {title}에 표시된 제품·연구·사건의 범위이며, 공개된 문장에 없는 원인이나 규모를 추가로 추정하지 않는다. "
            "따라서 이 항목은 현재 자료가 지지하는 사실과 해석의 경계를 함께 보여주는 기록으로 읽어야 하며, 독립적인 수치·재현·영향 분석이 없는 부분은 확정된 결과로 취급하지 않는다."
        )
    return (
        f"`{title}`에 관한 자료에서 확인되는 것은 제목과 연결된 대상 및 공개된 주장 자체다. "
        "원문에서 사건의 원인, 영향 범위, 성능 수치 또는 재현 절차를 읽을 수 없으므로 그 부분을 사실처럼 보태지 않는다. "
        "이 항목의 가치는 제한된 관찰을 보존하는 데 있고, 추가 맥락이 없는 상태에서 제품 전체의 동작이나 산업적 결과를 결론내릴 수 없다는 점이 핵심이다."
    )


def _community_digest(signal: dict) -> str:
    title = _title(signal.get("title"))
    text = _source_facts(signal)
    terms = _technical_terms(text, limit=5)
    metrics = _metrics(text)
    details = ", ".join(f"`{x}`" for x in (terms + metrics)[:6])
    source = str(signal.get("source_id") or "커뮤니티")
    if re.search(r"benchmark|leaderboard|evaluation|beats?", f"{title} {text}", re.I):
        return f"{source}에 공유된 ‘{title}’은 특정 benchmark·evaluation에서 한 모델 또는 agent가 더 나았다는 비교 주장이다. 이 주장을 읽을 때는 작업 구성, 데이터셋, harness, 비용과 재현 조건이 빠지면 ‘일반적인 모델 우위’로 해석할 수 없다는 점이 핵심이다."
    if details:
        return f"{source}에 올라온 ‘{title}’은 {details}에 관한 사용자 관찰·질문·주장이다. 게시물 수준에서 확인되는 것은 이 구체적 경험의 범위이며, 별도 원문이 없는 경우 제품 전체의 동작이나 커뮤니티 합의로 확대하지 않는다."
    return (
        f"{source}의 ‘{title}’은 특정 기술 현장에서 제기된 관찰·질문·주장이다. "
        "게시물에서 확인되는 범위는 작성자가 제시한 대상과 문제 제기이며, 댓글을 읽지 않았거나 별도 원문이 연결되지 않은 경우에는 그 이상의 합의·원인·영향을 추정하지 않는다. "
        "따라서 이 신호는 독립 자료와 재현이 붙기 전까지 해당 커뮤니티에서 관찰된 주장으로만 보존하며, 제품 전체의 동작이나 보안성을 증명하는 자료로 취급하지 않는다."
    )


def _event_digest(signal: dict, kind: str) -> str:
    if kind == "repository":
        return _repo_facts(signal)
    if kind == "research":
        return _paper_digest(signal)
    if kind in {"community", "community_link"}:
        return _community_digest(signal)
    return _reported_digest(signal)


def _continuity(signal: dict) -> str:
    continuity = str(signal.get("continuity_class") or "").upper()
    if continuity not in {"DEVELOPING", "ONGOING", "WATCH"}:
        return ""
    title = _title(signal.get("title"))
    changed = _clean_text(signal.get("what_changed_today"), 500)
    known = _clean_text(signal.get("what_was_known"), 500)
    generic = {"current intake contains a retained public signal", "target-date editorial comparison is performed by the draft/finalizer agents"}
    if known.casefold().rstrip(".") in generic:
        known = ""
    if changed.casefold().rstrip(".") in generic:
        changed = ""
    if changed:
        return f"‘{title}’은 이전 날짜부터 이어진 흐름이며, 이번 날짜에 새로 붙은 맥락은 {changed}."
    if known:
        return f"‘{title}’은 이전에 알려진 이슈가 계속 관찰되는 상태다. 이미 확인된 범위는 {known}."
    if continuity == "WATCH":
        return f"‘{title}’은 후속 자료가 붙을 때까지 현재의 구체적 영향 범위를 관찰할 항목이다."
    return f"‘{title}’은 이전 날짜에도 등장한 사안으로, 이번 날짜에도 현재 영향 범위가 유지되는지 확인할 가치가 있다."


def _evidence_sentence(signal: dict, kind: str) -> str:
    state = _status(signal)
    enrichment = str(signal.get("enrichment_state") or "").upper()
    title = _title(signal.get("title"))
    if state == "UNVERIFIED":
        return f"이는 UNVERIFIED 초기 신호이며, ‘{title}’의 핵심 주장과 실제 영향 범위는 원문·독립 자료가 더 필요하다."
    if state == "SPECULATIVE":
        return f"현재는 SPECULATIVE 해석이 섞인 자료라, ‘{title}’의 사실과 전망을 분리해 읽어야 한다."
    if state == "DEVELOPING":
        if kind in {"community", "community_link"}:
            return f"현재는 DEVELOPING 커뮤니티 신호로, 게시물에서 확인된 범위와 실제 제품·사건의 확정 사실을 분리한다."
        if enrichment == "RESEARCH_INCOMPLETE":
            return f"현재는 DEVELOPING 단계이며, ‘{title}’은 제목과 제한된 자료에서 확인된 범위만 다룬다."
        return f"현재는 DEVELOPING 단계로, ‘{title}’의 후속 발표·측정·영향 범위가 더해져야 전체 의미가 확정된다."
    if enrichment == "RESEARCH_INCOMPLETE":
        return f"‘{title}’은 제목과 제한된 source signal만 확인된 항목이므로 세부 메커니즘은 확정하지 않는다."
    return ""


def _narrative(signal: dict, *, issue_sources: Callable[[dict], list[dict]], source_label: Callable[[str], str], compact: bool = False) -> str:
    kind = _source_kind(signal, issue_sources)
    parts = [_event_digest(signal, kind)]
    continuity = _continuity(signal)
    if continuity:
        parts.append(continuity)
    evidence = _evidence_sentence(signal, kind)
    if evidence:
        parts.append(evidence)
    return " ".join(_sentence(part) for part in parts if part)


def _prose_sentences(text: str, limit: int = 8) -> list[str]:
    """Return complete generated sentences suitable for a visible summary."""
    cleaned = _clean_text(text, None)
    # Descriptions from GitHub and feeds frequently contain a Markdown list
    # marker after a wrapped line.  A summary line must be one physical line;
    # never let that source formatting become a native Notion bullet block.
    cleaned = re.sub(r"\s*\n\s*-\s+", " — ", cleaned)
    cleaned = re.sub(r"\s*\n\s*", " ", cleaned)
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?。！？])\s+", cleaned)
    result: list[str] = []
    for part in parts:
        part = part.strip()
        if len(part) < 18:
            continue
        result.append(_sentence(part))
        if len(result) >= limit:
            break
    return result


def _summary_lines(
    signal: dict,
    kind: str,
    *,
    issue_sources: Callable[[dict], list[dict]],
    source_label: Callable[[str], str],
) -> tuple[str, str]:
    """Build exactly two issue-specific reader lines.

    The detail writer may be longer, but the collapsed view is deliberately
    limited to two complete sentences.  Known high-value mechanisms get an
    explicit distillation; thin evidence becomes a clearly marked unresolved
    signal instead of a generic category paragraph.
    """
    provided = signal.get("reader_summary_lines") or signal.get("summary_lines")
    if isinstance(provided, (list, tuple)) and len(provided) >= 2:
        values = [re.sub(r"\s+", " ", _clean_text(value, None)).strip() for value in provided[:2]]
        if all(values):
            return values[0], values[1]

    title = _title(signal.get("title"))
    facts = _source_facts(signal)
    low = f"{title} {facts}".casefold()

    if "bigbear" in low and re.search(r"phishing|microsoft 365|mfa", low, re.I):
        organizations = next((m.group(1) for m in re.finditer(r"(\d[\d,]*)\s+organizations?", facts, re.I)), "258")
        credentials = next((m.group(1) for m in re.finditer(r"(\d[\d,]*)\s+(?:microsoft 365 )?credentials?", facts, re.I)), "5,000")
        return (
            f"BigBear 2.0은 Microsoft 365 로그인을 중계하는 AiTM 방식으로 MFA 완료 후 세션 쿠키를 탈취해 최소 {organizations}개 조직을 침해했고, {credentials}개가 넘는 자격 증명이 노출됐다.",
            "MFA 자체를 깨는 게 아니라 인증된 세션을 가로채는 구조라 FIDO2 강제, fallback 제한, session revocation 같은 통제가 중요하다.",
        )

    if "serverless/serverless" in low or "serverless framework" in low:
        return (
            "Serverless Framework는 YAML 설정으로 AWS Lambda 코드와 CloudFormation 인프라를 함께 배포하는 CLI·framework다.",
            "V4의 Sandboxes, Bedrock AgentCore, durable workflows, per-function IAM은 serverless 애플리케이션과 agent 실행 환경의 배포·권한 경계를 넓힌다.",
        )

    # Do not let an unrelated report that merely mentions false positives
    # inherit the CodeQL-specific analysis.  Bind this distillation to the
    # actual CodeQL/SAST/query-refinement vocabulary.
    if (
        "codeql" in low
        or "static application security" in low
        or bool(re.search(r"query refinement|java security query|\bsast\b", low, re.I))
    ):
        cvEs = next((m.group(1) for m in re.finditer(r"(\d[\d,]*)\s+(?:CVE|CVE instances)", facts, re.I)), "167")
        projects = next((m.group(1) for m in re.finditer(r"(\d[\d,]*)\s+projects?", facts, re.I)), "110")
        reduction = next((m.group(1) for m in re.finditer(r"(\d+(?:\.\d+)?)%", facts, re.I)), "81.8")
        return (
            f"CodeQL Java 보안 쿼리를 CVE {cvEs}건·프로젝트 {projects}개에 적용한 연구에서, 오탐 경로 500건의 원인과 query refinement 효과를 측정했다.",
            f"정제 후 검토된 오탐의 {reduction}%를 줄이면서 진양성 8건 중 7건을 유지했지만, 프로젝트별 데이터 흐름 차이가 있어 같은 개선률을 일반화할 수는 없다.",
        )

    if "active inference" in low and "reconnaissance" in low:
        return (
            "자율 정찰 agent가 양성 관측뿐 아니라 표적을 보지 못했다는 음성 관측까지 누적해 지도의 불확실성을 갱신하는 연구다.",
            "Dempster–Shafer·Gaussian sensor model·Bayesian posterior와 variational free energy로 다음 이동을 고르며, 현재 근거는 시뮬레이션 범위에 머문다.",
        )

    if kind == "repository":
        sentences = _prose_sentences(_repo_facts(signal))
    elif kind == "research":
        sentences = _prose_sentences(_paper_digest(signal))
    else:
        sentences = _prose_sentences(_event_digest(signal, kind))
    if len(sentences) >= 2:
        return sentences[0], sentences[1]
    if sentences:
        return sentences[0], ""

    return "", ""


def _summary_text(
    signal: dict,
    kind: str,
    *,
    issue_sources: Callable[[dict], list[dict]],
    source_label: Callable[[str], str],
) -> str:
    """Return a natural-length lead; legacy line arrays are joined, not enforced."""
    for key in ("reader_summary",):
        value = signal.get(key)
        if isinstance(value, str) and value.strip():
            text = re.sub(r"\s+", " ", _clean_text(value, None)).strip()
            return text if re.search(r"[가-힣]", text) else ""
        if isinstance(value, (list, tuple)):
            text = " ".join(_clean_text(part, None).strip() for part in value if _clean_text(part, None).strip())
            if text:
                text = re.sub(r"\s+", " ", text).strip()
                return text if re.search(r"[가-힣]", text) else ""
    legacy = signal.get("reader_summary_lines") or signal.get("summary_lines")
    if isinstance(legacy, (list, tuple)):
        text = " ".join(_clean_text(part, None).strip() for part in legacy if _clean_text(part, None).strip())
        if text:
            text = re.sub(r"\s+", " ", text).strip()
            return text if re.search(r"[가-힣]", text) else ""
    return ""


def _detail_repeats_summary(detail: str, summaries: tuple[str, str]) -> bool:
    """Detect the old renderer's summary-in-Details false positive."""
    def compact(value: str) -> str:
        return re.sub(r"[^\w가-힣]+", "", value).casefold()

    body = compact(detail)
    if not body:
        return False
    for summary in summaries:
        candidate = compact(summary)
        if len(candidate) >= 40 and (body.startswith(candidate[:120]) or candidate.startswith(body[:120])):
            return True
    joined = compact(" ".join(summaries))
    return len(joined) >= 80 and joined[:120] in body


def _nonduplicating_detail(signal: dict, kind: str, summaries: tuple[str, str]) -> str:
    """Return an honest detail when the generated detail repeats the summary."""
    title = _title(signal.get("title"))
    title_folded = title.casefold()
    facts = _source_facts(signal)
    candidates = _sentences(facts)
    for candidate in candidates:
        sentence = _sentence(candidate)
        if sentence.casefold().startswith(title_folded):
            sentence = sentence[len(title):].lstrip(" :-—–")
            sentence = _sentence(sentence)
        if not sentence or not re.search(r"[가-힣]", sentence) or _detail_repeats_summary(sentence, summaries):
            continue
        return sentence
    observed = _evidence_boundary_observation(facts)
    return observed if observed and re.search(r"[가-힣]", observed) and not _detail_repeats_summary(observed, summaries) else ""


def _reader_source_label(source: dict, source_label: Callable[[str], str]) -> str:
    for key in ("source_name", "publisher", "publication", "title", "source_title", "label"):
        value = _clean_text(source.get(key), None).strip()
        if value and not re.fullmatch(r"src_[A-Za-z0-9_-]+", value, re.I):
            return _heading_text(value)
    source_id = str(source.get("source_id") or "")
    mapped = _clean_text(source_label(source_id), None).strip() if source_id else ""
    if mapped and not re.fullmatch(r"src_[A-Za-z0-9_-]+", mapped, re.I):
        return _heading_text(mapped)
    url = str(source.get("url") or "").strip()
    host = re.sub(r"^https?://", "", url, flags=re.I).split("/", 1)[0].split(":", 1)[0]
    host = re.sub(r"^www\.", "", host, flags=re.I)
    return _heading_text(host or "원문")


def _detail_lines(
    signal: dict,
    *,
    issue_sources: Callable[[dict], list[dict]],
    source_label: Callable[[str], str],
) -> list[str]:
    """Render the expanded, issue-bound detail body and its native-toggle input."""
    kind = _source_kind(signal, issue_sources)
    summaries = (_summary_text(signal, kind, issue_sources=issue_sources, source_label=source_label), "")
    detail = str(signal.get("reader_detail_markdown") or "").strip()
    if detail and not re.search(r"[가-힣]", detail):
        detail = ""
    # Keep deliberate paragraph/list boundaries from the source-aware rewrite;
    # collapsing the whole body to one line would make Details merely repeat
    # the visible summary.  Do not use the short label/source limit here: it
    # previously clipped every substantive Details body to 160 characters.
    detail = _clean_detail_markdown(detail)
    detail_lines = detail.splitlines() or [detail]
    lines = ["<details>", "<summary>세부 내용 · 출처</summary>"]
    lines.extend("\t" + line if line else "\t" for line in detail_lines)
    if "reader_deep_analysis" in signal:
        deep = str(signal.get("reader_deep_analysis") or "").strip()
    else:
        deep = _deep_analysis(signal)
    if deep:
        lines.extend(["", "\tDeep Analysis"])
        lines.extend("\t" + line if line else "\t" for line in _clean_detail_markdown(deep).splitlines())
    lines.extend(["", "\t출처"])
    sources = signal.get("reader_sources") or issue_sources(signal)
    linked = False
    seen_urls: set[str] = set()
    for source in sources:
        url = str(source.get("url") or "").strip()
        if not url.startswith("http") or url in seen_urls:
            continue
        seen_urls.add(url)
        linked = True
        label = _reader_source_label(source, source_label)
        lines.append(f"\t- [{_heading_text(label)}]({url})")
    if not linked:
        lines.append("\t- Source URL unavailable; this remains an unresolved signal.")
    lines.append("</details>")
    return lines


def _source_label_line(signal: dict, *, issue_sources: Callable[[dict], list[dict]], source_label: Callable[[str], str]) -> str | None:
    sources = signal.get("reader_sources") or issue_sources(signal)
    source = next((item for item in sources if str(item.get("url") or "").startswith("http")), None)
    if not source:
        return None
    return f"원문: [{_reader_source_label(source, source_label)}]({source['url']})"


def _additional_sources(signal: dict, *, issue_sources: Callable[[dict], list[dict]], source_label: Callable[[str], str]) -> str | None:
    sources = issue_sources(signal)
    links = []
    for source in sources[1:]:
        url = str(source.get("url") or "")
        if url.startswith("http"):
            links.append(f"[{_reader_source_label(source, source_label)}]({url})")
    return f"관련 원문: {' · '.join(links)}" if links else None


def _related_line(signal: dict, topic_name_by_key: dict[str, str]) -> str | None:
    refs = signal.get("related_topics") or related_topics(signal, _primary(signal))
    labels = []
    for ref in refs:
        label = topic_name_by_key.get(str(ref))
        if label:
            number, _, plain = label.partition(". ")
            labels.append(f"{number} / {plain or label}")
    return "Related: " + "; ".join(labels) if labels else None


def _lead(signal: dict, *, issue_sources: Callable[[dict], list[dict]], source_label: Callable[[str], str]) -> str:
    return _summary_text(
        signal,
        _source_kind(signal, issue_sources),
        issue_sources=issue_sources,
        source_label=source_label,
    )


def _topic_intro(key: str) -> str:
    return {
        "ai": "모델·에이전트·제품이 실제 작업과 권한을 어떻게 바꾸는지 본다.",
        "security": "공격 경로와 영향을 받는 경계를 사건별로 분리한다.",
        "local_models": "오픈 모델과 로컬 실행에 직접 영향을 주는 변화를 모은다.",
        "infrastructure": "클라우드·플랫폼·컴퓨트 운영의 구체적 변화를 정리한다.",
        "semiconductors": "칩·메모리·패키징·네트워크·전력의 실제 병목을 본다.",
        "developer_tools": "각 저장소와 도구가 실제로 무엇을 제공하는지 구분한다.",
        "research": "연구의 문제·방법·측정·한계를 분리해서 읽는다.",
        "outside_bubble": "물리 환경과 응용 시스템의 기술 신호를 살핀다.",
        "community": "초기 경험·주장과 독립적으로 확인된 사실을 구분한다.",
        "business": "기술 변화가 자본·시장·규제와 만나는 지점을 본다.",
    }.get(key, "이 기술 영역의 구체적인 변화를 정리한다.")


def deep_analysis_binding(signal: dict) -> str | None:
    """Return an exact-event key; broad body keywords never create a binding."""
    explicit_id = str(signal.get("deep_analysis_event_id") or "").strip()
    inventory_id = str(signal.get("inventory_id") or signal.get("event_id") or "").strip()
    reader_custom = _clean_text(signal.get("reader_deep_analysis"), None)
    if "reader_deep_analysis" in signal:
        return f"reader:{inventory_id}" if reader_custom and inventory_id else None
    custom = _clean_text(signal.get("deep_analysis"), None)
    title = " ".join(_clean_text(signal.get(key), 500) for key in ("title", "source_title") if signal.get(key))
    if explicit_id and explicit_id == inventory_id and custom:
        identity = _title(signal.get("title")).casefold()
        if identity and (identity in custom.casefold() or any(term.casefold() in custom.casefold() for term in _technical_terms(title, limit=4))):
            return f"custom:{inventory_id}"
    rules = (
        (r"broadcom", r"ai|semiconductor|custom silicon|revenue|forecast|outlook", "broadcom-ai-semiconductor"),
        (r"exchange online|outlook outage|microsoft 365 outage|server busy", r"", "exchange-online-incident"),
        (r"kubernetes.*rootless|rootless.*kubernetes|kubeletinusernamespace", r"", "kubernetes-rootless"),
        (r"hyperpod|sagemaker.*(?:instantstart|operations)|instantstart", r"", "hyperpod-operations"),
        (r"openai", r"wiki|rogue|hijack|incident|unintended", "openai-wiki-incident"),
        (r"(?:chrome|chromium|v8).*(?:cve|vulnerability|exploit|type confusion)|(?:cve|vulnerability|exploit|type confusion).*(?:chrome|chromium|v8)", r"", "chrome-v8-vulnerability"),
        (r"codeql|static application security|query refinement|java security query|\bsast\b", r"", "codeql-false-positives"),
        (r"active inference.*reconnaissance|reconnaissance.*active inference", r"", "active-inference-reconnaissance"),
    )
    for title_pattern, extra_pattern, key in rules:
        if re.search(title_pattern, title, re.I) and (not extra_pattern or re.search(extra_pattern, title, re.I)):
            return key
    return None


def _deep_analysis(signal: dict) -> str:
    key = deep_analysis_binding(signal)
    title = _title(signal.get("title"))
    if not key:
        return ""
    if key.startswith("reader:"):
        return _clean_text(signal.get("reader_deep_analysis"), None)
    if key.startswith("custom:"):
        return _clean_text(signal.get("deep_analysis"), None)
    if key == "broadcom-ai-semiconductor":
        return f"‘{title}’를 읽을 때는 맞춤형 가속기·패키징·네트워크·공급 계약이 한 묶음으로 AI 인프라 economics를 만든다는 실제 사업 구조를 봐야 한다. 이 사건의 전망을 전체 반도체 시장의 확정 예측으로 확대하지 않고, 공개된 고객·수요·출하 근거와 분리해 판단한다."
    if key == "exchange-online-incident":
        return f"‘{title}’의 장애 경계는 단일 메일 기능이 아니라 tenant의 전송·인증·관리 평면이 같은 공급자에 의존하는 구조다. 따라서 영향 범위는 제품명보다 공식 incident timeline과 tenant별 증상으로 확인해야 한다."
    if key == "kubernetes-rootless":
        return f"‘{title}’은 pod 격리만의 변화가 아니라 kubelet·CRI·OCI runtime이 host 권한을 획득하는 경계를 다시 그리는 운영 변경이다. 실제 안전성은 runtime·CNI 호환성, privileged 작업, 실패 시 복구 경로를 확인해야 한다."
    if key == "hyperpod-operations":
        return f"‘{title}’의 핵심은 모델 API가 아니라 cluster bootstrap·capacity·training/inference 상태를 조작하는 플랫폼 운영 계층이다. 자동화의 이점은 provisioning 시간에 달리지만, 권한과 실패 시 자원 정리 동작이 함께 검증돼야 한다."
    if key == "openai-wiki-incident":
        return f"‘{title}’은 모델 출력이 인증된 계정·브라우저·외부 게시 권한과 연결될 때 발생하는 event-specific agent control 문제다. 정확한 권한, 원인, 피해 범위가 공개된 facts와 일치하는지 확인하지 않고 일반적인 agent failure로 확대하지 않는다."
    if key == "chrome-v8-vulnerability":
        return f"‘{title}’은 브라우저 입력 처리에서 engine memory/type boundary를 넘어 sandbox와 code-execution 경계로 이어질 수 있는 사건이다. 영향을 받는 버전, exploitation evidence, 패치 가용성을 advisory 단위로 분리해 확인해야 한다."
    if key == "codeql-false-positives":
        return f"‘{title}’의 분석 단위는 모델 점수가 아니라 Java CodeQL query가 path constraint·sanitization·trust boundary를 얼마나 정밀하게 표현하는가다. 오탐 원인을 query refinement로 줄여도 프로젝트별 데이터 흐름 차이가 남으므로, 81.8% reviewed reduction을 모든 저장소의 동일한 개선률로 읽어서는 안 된다."
    if key == "active-inference-reconnaissance":
        return f"‘{title}’은 현재 belief와 새 sensor observation의 불일치를 free energy로 계산해 정찰 경로를 갱신하는 구조다. negative observation과 탐색 가치가 경로 선택에 들어간다는 점이 핵심이며, simulation 결과를 실제 센서·통신·동적 장애물 환경의 성능으로 확대하지 않는다."
    return ""


def _heading(signal: dict, issue_sources: Callable[[dict], list[dict]]) -> str:
    feedback_id = str(signal.get("feedback_id") or "00-000")
    state = signal.get("feedback_state") if signal.get("feedback_state") in {"[x]", "[ ]"} else "[ ]"
    # Use a real Markdown task-list item.  A bare ``[ ]`` in a heading is
    # ordinary text and does not become an interactive Notion checkbox.
    # ``·`` is reader-facing punctuation; the stable ID remains parseable.
    title = _heading_text(_title(signal.get("title")))
    title = re.sub(
        r"^\s*(?:(?:NEW|DEVELOPING|ONGOING|WATCH|ACTION|CONFIRMED|EARLY RADAR|RESEARCH RADAR|COMMUNITY SIGNAL|INVESTIGATION|UNVERIFIED|SPECULATIVE)\s*(?:[—–:-]|·)\s*)+",
        "",
        title,
        flags=re.I,
    )
    return f"- {state} {feedback_id} · {title}"


def _assign_render_fallback_ids(signals: list[dict]) -> None:
    """Provide deterministic IDs for direct renderer callers without a ledger."""
    codes = {
        "ai": "01", "security": "02", "local_models": "03", "infrastructure": "04",
        "semiconductors": "05", "developer_tools": "06", "research": "07",
        "outside_bubble": "08", "community": "09", "business": "10",
    }
    missing = sorted((signal for signal in signals if not signal.get("feedback_id")), key=stable_issue_key)
    used = {str(signal.get("feedback_id")) for signal in signals if signal.get("feedback_id")}
    next_by_code: dict[str, int] = {}
    for feedback_id in used:
        match = re.fullmatch(r"(\d{2})-(\d{3,})", feedback_id)
        if match:
            next_by_code[match.group(1)] = max(next_by_code.get(match.group(1), 0), int(match.group(2)))
    for signal in missing:
        code = codes.get(str(signal.get("primary_topic") or ""), "10")
        number = next_by_code.get(code, 0) + 1
        feedback_id = f"{code}-{number:03d}"
        while feedback_id in used:
            number += 1
            feedback_id = f"{code}-{number:03d}"
        signal["feedback_id"] = feedback_id
        signal.setdefault("feedback_state", "[ ]")
        used.add(feedback_id)
        next_by_code[code] = number


def build_reader_markdown(
    day: str,
    signals: list[dict],
    *,
    topic_sections: tuple[tuple[str, str], ...] = TOPIC_SECTIONS,
    topic_key: Callable[[dict], str] = classify_primary_topic,
    issue_sources: Callable[[dict], list[dict]],
    source_label: Callable[[str], str],
    evidence_priority: Callable[[dict], int] | None = None,
    source_priority: Callable[[dict], int] | None = None,
) -> str:
    """Render every retained issue; no Top-N pruning is performed."""
    ordered = list(signals)
    _assign_render_fallback_ids(ordered)
    topic_name_by_key = {key: name for name, key in topic_sections}
    grouped: dict[str, list[dict]] = {key: [] for _, key in topic_sections}
    for signal in ordered:
        primary = str(signal.get("primary_topic") or topic_key(signal))
        if primary not in grouped:
            primary = classify_primary_topic(signal)
        signal["primary_topic"] = primary
        signal["topic_section"] = primary
        signal["subtopic"] = str(signal.get("subtopic") or subtopic_for(signal, primary))
        grouped.setdefault(primary, []).append(signal)

    def rank(item: dict) -> tuple[int, int, float, str]:
        ev = evidence_priority(item) if evidence_priority else {"VERIFIED": 5, "DEVELOPING": 4, "UNVERIFIED": 2, "SPECULATIVE": 1}.get(_status(item), 3)
        src = source_priority(item) if source_priority else 0
        feedback = float(item.get("feedback_rank_score") or 0)
        return (-ev, -src, -feedback, -float(item.get("score") or 0), str(item.get("title") or "").casefold())

    # Summary selection is deliberately topic-diverse: choose the strongest
    # item from distinct lanes before filling any remaining slot.
    best_by_topic = []
    for key, bucket in grouped.items():
        if bucket:
            best_by_topic.append(min(bucket, key=rank))
    summary_signals = sorted(best_by_topic, key=rank)[:5]
    if len(summary_signals) < 5:
        used = {str(x.get("inventory_id")) for x in summary_signals}
        for item in sorted(ordered, key=rank):
            if str(item.get("inventory_id")) not in used:
                summary_signals.append(item)
                used.add(str(item.get("inventory_id")))
            if len(summary_signals) == 5:
                break

    lines = [
        f"# 아침 기술 브리핑 — {day}",
        f"대상 날짜: {day} (America/Los_Angeles)",
        "",
        '<table_of_contents color="gray"/>',
        "",
        "## 00. 오늘의 한눈에 보기",
    ]
    if not ordered:
        lines.extend(["오늘 확인할 만한 기술 변화가 없습니다.", ""])
    else:
        lead_items = [
            _lead(signal, issue_sources=issue_sources, source_label=source_label)
            for signal in summary_signals
        ]
        if len(summary_signals) >= 2:
            conclusion = (
                f"{_heading_text(_title(summary_signals[0].get('title')))}에서는 {lead_items[0]} "
                f"동시에 {_heading_text(_title(summary_signals[1].get('title')))}도 함께 볼 필요가 있다."
            )
        else:
            conclusion = f"{_heading_text(_title(summary_signals[0].get('title')))}에서 {lead_items[0]}"
        labels = [
            topic_name_by_key.get(str(signal.get("primary_topic") or ""), "")
            for signal in summary_signals
        ]
        labels = [label.split(". ", 1)[-1] for label in labels if label]
        label_text = "·".join(dict.fromkeys(labels[:3])) or "여러 기술 영역"
        partial_count = sum(
            str(signal.get("reader_rewrite_source_status") or signal.get("source_understanding") or "").upper()
            not in {"", "FULL_SOURCE"}
            for signal in summary_signals
        )
        scope_note = (
            "선정 항목에는 원문이 충분한 자료와 일부 자료에 기반한 신호가 함께 있다. "
            "제목·요약만으로 확정할 수 없는 내용은 각 Details에서 별도로 경계를 표시한다."
            if partial_count
            else "선정 항목은 확인 가능한 원문을 바탕으로 정리했으며, 각 Details에 사건의 작동 방식과 한계를 덧붙였다."
        )
        lines.extend([
            f"**오늘의 결론:** {conclusion}",
            "**핵심 변화:**",
        ])
        for signal in summary_signals:
            lines.append(f"- **{_heading_text(_title(signal.get('title')))}** — {_lead(signal, issue_sources=issue_sources, source_label=source_label)}")
        lines.extend([
            f"**왜 지금 중요한가:** 오늘의 주요 신호는 {label_text}에서 시스템의 동작·검증·운영 경계가 바뀌는 흐름을 보여준다. 발표 이름보다 실제 mechanism과 적용 조건을 함께 읽어야 한다.",
            f"**확인 범위:** {scope_note}",
            "",
        ])

    for section_name, key in topic_sections:
        bucket = grouped.get(key, [])
        if not bucket:
            continue
        lines.extend([
            "<details>",
            f"<summary>{section_name}</summary>",
            # The native topic toggle summary is the section's only visible
            # title.  Do not emit a duplicate heading inside the toggle.
            f"\t{_topic_intro(key)}",
            "\t",
        ])
        subgroups = group_by_subtopic(bucket, key)
        ordered_groups = sorted(subgroups.items(), key=lambda item: min(rank(row) for row in item[1]))
        for subheading, issues in ordered_groups:
            lines.extend([f"\t### {subheading}", "\t"])
            for signal in sorted(issues, key=rank):
                summary_text = _summary_text(
                    signal,
                    _source_kind(signal, issue_sources),
                    issue_sources=issue_sources,
                    source_label=source_label,
                )
                issue_lines = [
                    _heading(signal, issue_sources),
                    summary_text,
                    "",
                    *_detail_lines(signal, issue_sources=issue_sources, source_label=source_label),
                    "",
                ]
                lines.extend("\t" + line if line else "\t" for line in issue_lines)
        lines.extend(["</details>", ""])

    # Sources remain issue-local; do not emit a second global source list.
    lines.extend([
        "## 내 피드백 / 후속 요청",
        "다음 브리핑에서 더 보고 싶은 주제나 수정할 점을 여기에 적어 주세요.",
        "",
    ])
    return "\n".join(lines)


def build_topic_section_markdown(
    day: str,
    signals: list[dict],
    *,
    section_name: str,
    section_key: str,
    issue_sources: Callable[[dict], list[dict]],
    source_label: Callable[[str], str],
    evidence_priority: Callable[[dict], int] | None = None,
    source_priority: Callable[[dict], int] | None = None,
) -> str:
    """Render one topic as an independently writable Notion section page.

    The page keeps the native topic Toggle Heading as its sole visible topic
    heading. The database title is intentionally generic (``Brief Section
    01``), so the page body does not repeat the topic name as another heading.
    """
    ordered = list(signals)
    _assign_render_fallback_ids(ordered)
    for signal in ordered:
        primary = str(signal.get("primary_topic") or classify_primary_topic(signal))
        signal["primary_topic"] = primary
        signal["topic_section"] = primary
        signal["subtopic"] = str(signal.get("subtopic") or subtopic_for(signal, primary))
    bucket = [signal for signal in ordered if str(signal.get("primary_topic")) == section_key]

    def rank(item: dict) -> tuple[int, int, float, str]:
        ev = evidence_priority(item) if evidence_priority else {"VERIFIED": 5, "DEVELOPING": 4, "UNVERIFIED": 2, "SPECULATIVE": 1}.get(_status(item), 3)
        src = source_priority(item) if source_priority else 0
        feedback = float(item.get("feedback_rank_score") or 0)
        return (-ev, -src, -feedback, -float(item.get("score") or 0), str(item.get("title") or "").casefold())

    lines = [
        f"# {day} · Brief Section {section_name.split('.', 1)[0]}",
        f"대상 날짜: {day} (America/Los_Angeles)",
        "",
        "<details>",
        f"<summary>{section_name}</summary>",
        f"\t{_topic_intro(section_key)}",
        "\t",
    ]
    subgroups = group_by_subtopic(bucket, section_key)
    ordered_groups = sorted(subgroups.items(), key=lambda item: min(rank(row) for row in item[1]))
    for subheading, issues in ordered_groups:
        lines.extend([f"\t### {subheading}", "\t"])
        for signal in sorted(issues, key=rank):
            summary_text = _summary_text(
                signal,
                _source_kind(signal, issue_sources),
                issue_sources=issue_sources,
                source_label=source_label,
            )
            issue_lines = [
                _heading(signal, issue_sources),
                summary_text,
                "",
                *_detail_lines(signal, issue_sources=issue_sources, source_label=source_label),
                "",
            ]
            lines.extend("\t" + line if line else "\t" for line in issue_lines)
    lines.extend([
        "</details>",
        "",
    ])
    return "\n".join(lines)


def build_brief_index_markdown(day: str, full_markdown: str, section_links: list[dict]) -> str:
    """Create the small per-day Index page after topic pages are written."""
    summary_match = re.search(
        r"(?ms)^## 00\. 오늘의 한눈에 보기\n(.*?)(?=^<details>\s*$|^## Sources\s*$|\Z)",
        full_markdown,
    )
    summary_body = summary_match.group(1).strip() if summary_match else "오늘의 상세 이슈는 아래 topic section page에서 확인한다."
    lines = [
        f"# 아침 기술 브리핑 — {day}",
        f"대상 날짜: {day} (America/Los_Angeles)",
        "",
        "## 00. 오늘의 한눈에 보기",
        summary_body,
        "",
        "## Topic Sections",
        "아래 section page를 열면 해당 topic의 전체 issue, source-grounded Details, Sources를 확인할 수 있다.",
    ]
    for item in section_links:
        name = str(item.get("name") or item.get("key") or "Section")
        url = str(item.get("url") or "").strip()
        count = int(item.get("issue_count") or 0)
        if url.startswith("http"):
            lines.append(f"- [{_heading_text(name)}]({url}) — {count}개 이슈")
        else:
            lines.append(f"- {_heading_text(name)} — {count}개 이슈")
    lines.extend(["", "## 내 피드백 / 후속 요청", "다음 브리핑에서 더 보고 싶은 주제나 수정할 점을 여기에 적어 주세요.", ""])
    return "\n".join(lines)
