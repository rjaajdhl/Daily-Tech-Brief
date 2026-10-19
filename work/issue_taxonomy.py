"""Primary-topic and dynamic-subtopic taxonomy for Daily Tech Intelligence.

The numbered top-level schema is stable.  Subtopics are derived from the
issue's actual technical mechanism and source text, not from the source host
or a single broad intake label.  This module is intentionally network-free so
both collection and presentation paths can use the same classification.
"""
from __future__ import annotations

import re
from collections import OrderedDict


TOPIC_SECTIONS: tuple[tuple[str, str], ...] = (
    ("01. AI·에이전트·프런티어 모델", "ai"),
    ("02. 사이버보안·오펜시브 시큐리티", "security"),
    ("03. 로컬·오픈 모델", "local_models"),
    ("04. 인프라·클라우드·컴퓨트", "infrastructure"),
    ("05. 반도체·AI 인프라", "semiconductors"),
    ("06. 개발자 도구·오픈소스·GitHub", "developer_tools"),
    ("07. 연구·논문·벤치마크", "research"),
    ("08. 로봇·응용 기술·오픈 디스커버리", "outside_bubble"),
    ("09. 커뮤니티 신호·초기 경보", "community"),
    ("10. 기술 기업·비즈니스·거버넌스", "business"),
)

TOPIC_KEYS = {key for _, key in TOPIC_SECTIONS}

# Terms are deliberately grouped by mechanism rather than publisher.  The
# first matching branch in classify_primary_topic encodes precedence where a
# story could otherwise be mistaken for a generic AI or research item.
_SEMICONDUCTOR = re.compile(
    r"\b(?:broadcom|marvell|tsmc|sk hynix|sk하이닉스|samsung|micron|nvidia|amd|intel|hbm|asic|xpu|gpu|accelerator|custom silicon|custom chip|chiplet|advanced packaging|interconnect|silicon|semiconductor|ai server|datacenter power)\b",
    re.I,
)
_INFRASTRUCTURE = re.compile(
    r"\b(?:exchange online|microsoft 365|outlook outage|azure|aws|amazon eks|sagemaker hyperpod|hyperpod|kubernetes|kubelet|rootless|container|docker|podman|cloud outage|cloud service|cluster|control plane|node component|datacenter operations|cloud infrastructure|storage|network fabric)\b",
    re.I,
)
_SECURITY = re.compile(
    r"\b(?:cve[- ]?\d|vulnerability|vulnerabilities|zero.?day|actively exploited|known exploited|exploit(?:ed|ation)?|rce|remote code execution|code execution|malware|ransomware|phishing|credential[- ]?theft|supply chain attack|security advisory|cybersecurity|intrusion|security flaw|sandbox escape|privilege escalation|attack path|breach)\b",
    re.I,
)
_LOCAL_MODEL = re.compile(
    r"\b(?:local model|local llm|open model|open.?weight|open.?source model|qwen|llama|deepseek|mistral|gemma|glm|kimi|ollama|gguf|gptq|awq|quantiz(?:ation|ed)?|fine.?tun(?:e|ing)|model card)\b",
    re.I,
)
_RESEARCH = re.compile(
    r"\b(?:arxiv|paper|preprint|peer.?review|benchmark|evaluation|dataset|baseline|ablation|research study|technical report)\b",
    re.I,
)
_AGENT = re.compile(
    r"\b(?:agent|agents|agentic|frontier model|openai|anthropic|claude|gemini|chatgpt|copilot|grok|astra|mcp|tool use|tool.?calling|orchestrat|harness|sandbox|memory|context window|coding agent)\b",
    re.I,
)
_DEVELOPER = re.compile(
    r"\b(?:github|repository|repo|open.?source|developer tool|coding|cli|sdk|library|plugin|skills?|harness|ide|vscode|pull request|workflow automation|document parser)\b",
    re.I,
)
_BUSINESS = re.compile(
    r"\b(?:ipo|valuation|funding|fundraise|investment|acquisition|acquire|merger|partnership|earnings|revenue|forecast|governance|regulation|regulatory|public market|antitrust|policy|lawsuit)\b",
    re.I,
)
_OUTSIDE = re.compile(
    r"\b(?:robot\w*|robotaxi|drone|spacecraft|satellite|nasa|mars|quantum|biotech|biology|energy|battery|materials?|manufactur\w*|instrumentation|hci|physical ai)\b",
    re.I,
)


def _compact(value: object, limit: int = 4000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def mechanism_text(signal: dict) -> str:
    """Return evidence-bearing text with actual content weighted first."""
    fields = [
        signal.get("title"),
        signal.get("source_title"),
        signal.get("summary"),
        signal.get("source_description"),
        signal.get("source_body_excerpt"),
        signal.get("source_facts"),
        signal.get("deep_analysis"),
        " ".join(str(x) for x in signal.get("topics") or []),
        " ".join(str(x) for x in signal.get("categories") or []),
        " ".join(str(x) for x in signal.get("discovery_categories") or []),
    ]
    return _compact(" ".join(_compact(x) for x in fields)).casefold()


def classify_primary_topic(signal: dict) -> str:
    """Classify by what changed, with one primary category per issue."""
    text = mechanism_text(signal)
    source = str(signal.get("source_id") or "").casefold()
    role = str(signal.get("role") or "").casefold()
    lane = str(signal.get("lane") or "").casefold()
    source_url = _compact(signal.get("url") or signal.get("source_url"), 1000).casefold()
    title = _compact(signal.get("title"), 500).casefold()
    title_context = " ".join(
        _compact(signal.get(key), 500).casefold()
        for key in ("title", "source_title")
        if signal.get(key)
    )

    # Title-first subject guards.  A paper or repository may mention agents,
    # models, or compute in its abstract/README while its actual subject is
    # static analysis, autonomous systems, an accounting tool, or a vision
    # library.  The title is the strongest low-cost event identity available at
    # this stage, so these guards must run before broad body keyword rules.
    if re.search(
        r"\b(?:codeql|sast|static application security|static analysis|false positives?|query refinements?|vulnerability detection|intrusion detection|phishing detection|malware detection|ransomware detection)\b",
        title_context,
        re.I,
    ):
        return "security"
    # Identity attacks stay in Cybersecurity even when the report names a
    # cloud platform (for example Microsoft 365).  The affected platform is
    # not the same thing as the changed mechanism.
    if re.search(
        r"\b(?:phishing|phishing-as-a-service|phishing service|mfa bypass|bypass(?:ed|ing)? mfa|stolen session cookie|session cookie|credential(?:s)? theft|account takeover|identity attack)\b",
        title_context,
        re.I,
    ):
        return "security"
    if "isc.sans.edu" in source_url and "stormcast" in title_context:
        return "security"
    if re.search(
        r"\b(?:ai crawler|user-agent|metadata endpoint|cloud metadata|agent skill|shell|developer credentials|runtime permission)\b",
        title_context,
        re.I,
    ) and re.search(r"\b(?:spoof\w*|credential\w*|shell|permission\w*|security|attack\w*|access)\b", title_context, re.I):
        return "security"
    if re.search(
        r"\b(?:active inference|autonomous reconnaissance|reconnaissance mission|route planning|person search|multi-camera)\b",
        title_context,
        re.I,
    ) and re.search(r"\b(?:autonomous|robot|mission|camera|reconnaissance|physical|sensor)\b", title_context, re.I):
        return "outside_bubble"
    if re.search(
        r"\b(?:pytorch[- ]image[- ]models|image models?|vision backbone|computer vision library|timm)\b",
        title_context,
        re.I,
    ) and (source.startswith("github-") or "github" in title_context):
        return "developer_tools"
    if re.search(r"\b(?:openledger|open-ledger|double-entry accounting|accounting ledger|mcp-native)\b", title_context, re.I):
        return "developer_tools"
    if re.search(r"\bjensen huang\b", title_context, re.I) and re.search(r"\b(?:agi|arrived|congratulat)", title_context, re.I):
        return "ai"

    # Title-level semantic overrides prevent unrelated source-body words from
    # hijacking the primary lane.  These are mechanism guards, not a story list:
    # a browser CVE is security, a satellite feed is applied technology, and a
    # financing announcement is business even when the article mentions AI
    # servers, agents, or datacenter compute.
    if re.search(r"\b(?:chrome|chromium|v8|firefox|edge|safari)\b", title_context, re.I) and re.search(
        r"\b(?:cve[- ]?\d|vulnerabilit|zero.?day|exploit|actively exploited|known exploited|rce|code execution)\b",
        title_context,
        re.I,
    ):
        return "security"
    if re.search(r"\b(?:ransomware|malware|intrusion|vulnerability|exploit|cve[- ]?\d)\b", title_context, re.I) and (
        source == "arxiv" or _SECURITY.search(title_context)
    ):
        return "security"
    if re.search(r"\bopenai\b", title_context, re.I) and re.search(
        r"\b(?:wiki|rogue|hijack|incident|unintended|external site|agent attack)\b",
        title_context,
        re.I,
    ):
        return "ai"
    if re.search(r"\b(?:gpt[- ]?6|astra)\b", title_context, re.I) and re.search(
        r"\b(?:rollout|release|model|availability|launch|update|frontier|score|scored|benchmark|results|defeats|leaderboard)\b",
        title_context,
        re.I,
    ):
        return "ai"
    if re.search(r"\b(?:llamafactory|llama factory)\b", title_context, re.I):
        return "developer_tools"
    if re.search(r"\bserverless/serverless\b|\bserverless framework\b", title_context, re.I) and re.search(
        r"\b(?:aws|lambda|cloud|serverless)\b", title_context, re.I
    ):
        return "infrastructure"
    if re.search(r"\b(?:iceye|satellite|spacecraft|earth observation|orbit(?:al)?|rocket|aerospace|space launch)\b", title_context, re.I):
        return "outside_bubble"
    if re.search(r"\bnscale\b", title_context, re.I) and re.search(
        r"\b(?:funding|financing|fundraise|investment|valuation|series [a-z])\b",
        title_context,
        re.I,
    ):
        return "business"

    # Cloud/platform changes take the infrastructure lane even when their
    # source explains the security problems they mitigate (notably rootless
    # Kubernetes node components).
    if _INFRASTRUCTURE.search(text) and re.search(
        r"\b(?:kubernetes|kubelet|rootless|hyperpod|sagemaker|exchange online|microsoft 365|cloud outage|service outage|service degradation|cloud incident)\b",
        title + " " + text,
        re.I,
    ) and not re.search(r"\b(?:cve[- ]?\d|zero.?day|actively exploited|known exploited|rce|credential[- ]?theft|phishing|mfa bypass|session cookie|account takeover)\b", title, re.I):
        return "infrastructure"

    # Security advisories and concrete attack paths outrank generic AI words.
    if _SECURITY.search(text):
        # An agent incident about unintended external actions is an AI safety
        # story unless it contains a conventional CVE/exploit/compromise claim.
        if _AGENT.search(text) and not re.search(
            r"\b(?:cve[- ]?\d|rce|remote code execution|known exploited|actively exploited|malware|credential theft|breach)\b",
            text,
            re.I,
        ):
            return "ai"
        return "security"

    # Cloud/platform operations must not be mistaken for AI, robotics, or
    # research merely because the platform is used for ML workloads.
    if _INFRASTRUCTURE.search(text):
        return "infrastructure"

    # Local/open model release, quantization, or deployment choices are
    # distinct from a semiconductor story even when the source mentions GPU
    # memory as a runtime constraint.
    if _LOCAL_MODEL.search(text):
        return "local_models"

    # A paper's primary change is its method/result, not the hardware noun it
    # contains. Industry releases and earnings fall through to Semiconductors.
    if (source.startswith("arxiv") or source.startswith("huggingface-daily-papers") or role in {"research", "paper"} or "arxiv.org" in source_url) and _RESEARCH.search(text):
        return "research"

    # Chip, memory, packaging, interconnect, and AI-infrastructure company
    # stories belong here even when the source is an earnings article.
    if _SEMICONDUCTOR.search(text):
        if "hugging face" in text and re.search(r"\b(?:acquire|acquisition|buy|deal)\b", text):
            return "business"
        return "semiconductors"

    # A local/open model release or deployment choice is distinct from a
    # generic AI product or a repository that merely happens to use an LLM.
    if _LOCAL_MODEL.search(text):
        return "local_models"

    # A paper/benchmark remains research only when the primary change is the
    # study itself. AWS HyperPod or a GitHub tool mentioning evaluation does
    # not get promoted to Research.
    if _RESEARCH.search(title) and not (_AGENT.search(title) or _INFRASTRUCTURE.search(title)):
        return "research"

    if _OUTSIDE.search(text):
        return "outside_bubble"

    if _BUSINESS.search(text):
        return "business"

    # Repository/tooling classification follows the repo's purpose, not only
    # the fact that it is hosted on GitHub.  Agent harnesses, memory layers,
    # MCP tools and coding agents are developer infrastructure here.
    if source.startswith("github-") or role in {"repository_discovery", "github_trending", "github_active"} or _DEVELOPER.search(title):
        return "developer_tools"

    if lane == "community_signals" or source in {"hacker-news", "lobsters", "the-hacker-news", "reddit"}:
        # Community-hosted links with a concrete mechanism were handled above;
        # the remainder is intentionally kept in the community section.
        return "community"

    if _AGENT.search(text):
        return "ai"

    return "ai"


def related_topics(signal: dict, primary: str | None = None) -> list[str]:
    """Return optional cross-reference labels without duplicating the story."""
    primary = primary or classify_primary_topic(signal)
    text = mechanism_text(signal)
    related: list[str] = []
    if primary != "ai" and _AGENT.search(text):
        related.append("ai")
    if primary != "security" and _SECURITY.search(text):
        related.append("security")
    if primary != "infrastructure" and _INFRASTRUCTURE.search(text):
        related.append("infrastructure")
    if primary != "semiconductors" and _SEMICONDUCTOR.search(text):
        related.append("semiconductors")
    if primary != "developer_tools" and _DEVELOPER.search(text):
        related.append("developer_tools")
    if primary != "research" and _RESEARCH.search(text):
        related.append("research")
    return [item for item in OrderedDict.fromkeys(related) if item != primary]


def _has(text: str, pattern: str) -> bool:
    return bool(re.search(pattern, text, re.I))


def subtopic_for(signal: dict, primary: str | None = None) -> str:
    """Generate a readable subtopic from the issue inventory."""
    primary = primary or classify_primary_topic(signal)
    text = mechanism_text(signal)
    source = str(signal.get("source_id") or "").casefold()
    title = _compact(signal.get("title"), 500).casefold()

    if primary == "ai":
        title = _compact(signal.get("title"), 500).casefold()
        if _has(title, r"gpt[- ]?6|astra|rollout|model release|frontier model"):
            return "Frontier Models & Product Behavior"
        if _has(title, r"wiki|rogue|hijack|agent incident|unintended"):
            return "Agent Safety / Misalignment"
        if _has(title, r"sandbox|hijack|rogue|misalign|external site|public wiki|unintended|escape|approval|permission|safety"):
            return "Agent Safety / Misalignment"
        if _has(text, r"memory|context|long.?running|persistent state|mem0|context7"):
            return "Agent Memory & Context"
        if _has(text, r"coding agent|code agent|copilot|opencode|claude code|codex|software engineer"):
            return "Coding Agents & Agent Runtimes"
        if _has(text, r"mcp|tool.?calling|plugin|orchestrat|workflow|harness|swarm"):
            return "Agent Tooling & Runtime"
        if _has(text, r"ipo|valuation|funding|investment|governance|regulation|revenue|forecast"):
            return "AI Business / Governance"
        if _has(text, r"benchmark|evaluation|model release|model update|frontier|context window|pricing|limit"):
            return "Frontier Models & Product Behavior"
        return "AI Ecosystem & Product Changes"

    if primary == "security":
        title = _compact(signal.get("title"), 500).casefold()
        if _has(title, r"phishing|mfa|session cookie|credential|account takeover|identity"):
            return "Identity / Phishing / Account Takeover"
        if _has(title, r"ai crawler|metadata endpoint|cloud metadata|user-agent|agent skill|shell|runtime permission"):
            return "AI / Agent Security"
        if _has(title, r"chrome|chromium|v8|firefox|edge|safari"):
            return "Browser & Client Security"
        if _has(title, r"codeql|sast|static analysis|false positive|query refinement|vulnerability detection|intrusion detection|phishing detection|malware detection"):
            return "Static Analysis & Vulnerability Research"
        if _has(title, r"ransomware|malware|intrusion|vulnerability|cve[- ]?\d|exploit"):
            return "Active Exploitation & Vulnerabilities"
        if _has(title, r"agent|prompt injection|tool.?use|model|sandbox"):
            return "AI / Agent Security"
        if _has(text, r"cve|zero.?day|actively exploited|known exploited|exploit|rce|code execution|intrusion|credential[- ]?theft|vulnerability|flaw"):
            return "Active Exploitation & Vulnerabilities"
        if _has(text, r"cloud|identity|credential|oauth|exchange online|azure|microsoft 365"):
            return "Cloud & Identity Security"
        if _has(text, r"github|repository|package|dependency|supply chain|developer"):
            return "Developer / Supply Chain Security"
        if _has(text, r"chrome|browser|v8|webkit|firefox|edge"):
            return "Browser & Client Security"
        return "Security Research & Defensive Response"

    if primary == "local_models":
        if _has(text, r"qwen|llama|deepseek|mistral|gemma|glm|kimi|model release|model card"):
            return "Open Models & Releases"
        if _has(text, r"local|ollama|gguf|gptq|awq|quantiz|self.?host"):
            return "Local Inference & Quantization"
        if _has(text, r"benchmark|evaluation|score|tested|leaderboard"):
            return "Model Evaluations"
        return "Open Model Ecosystem"

    if primary == "infrastructure":
        if _has(title, r"serverless/serverless|serverless framework|lambda"):
            return "Cloud / Serverless / AWS Lambda"
        if _has(text, r"outage|downtime|service degradation|server busy|email delay|incident"):
            return "Cloud Incidents & Reliability"
        if _has(text, r"hyperpod|sagemaker|control plane|cluster bootstrap|training|inference"):
            return "AI Platform Operations"
        if _has(text, r"kubernetes|kubelet|rootless|container|docker|podman|eks"):
            return "Kubernetes & Containers"
        if _has(text, r"datacenter|data center|compute|capacity|power|storage|network fabric"):
            return "Compute / Datacenter Operations"
        return "Infrastructure Tooling"

    if primary == "semiconductors":
        if _has(text, r"broadcom|marvell|custom silicon|custom chip|asic|xpu|accelerator"):
            return "Custom Silicon & AI Accelerators"
        if _has(text, r"hbm|memory|dram|nand|packaging|chiplet"):
            return "HBM / Memory / Packaging"
        if _has(text, r"interconnect|networking|switch|ethernet|infiniband|optical"):
            return "Networking & Interconnect"
        if _has(text, r"power|gigawatt|datacenter|data center|capacity"):
            return "Datacenter Power & Capacity"
        if _has(text, r"earnings|revenue|forecast|outlook|customer|partnership"):
            return "Semiconductor Strategy & Earnings"
        return "Other Compute Hardware"

    if primary == "developer_tools":
        if _has(title, r"llamafactory|llama factory"):
            return "Open Model Training & Fine-tuning"
        if _has(title, r"pytorch[- ]image[- ]models|image models?|vision backbone|timm"):
            return "ML / Vision Libraries"
        if _has(title, r"openledger|open-ledger|double-entry accounting|accounting ledger|mcp-native"):
            return "MCP / Tool Integrations"
        if _has(text, r"skill|harness|agent framework|agentic|workflow"):
            return "Agent Skills / Harnesses"
        if _has(text, r"coding|ide|vscode|copilot|code agent|pull request"):
            return "Coding Agents & IDEs"
        if _has(text, r"mcp|plugin|tool server|context7"):
            return "MCP / Tool Integrations"
        if _has(text, r"document|docling|scrap|parser|data extraction"):
            return "Document / Data Tooling"
        if source.startswith("github-") or "github" in title:
            return "Repository Inventory"
        return "Open Source Infrastructure"

    if primary == "research":
        if _has(text, r"agent|multi.?agent|coding agent"):
            return "Agents & Evaluation"
        if _has(text, r"model|training|language|vision|llm"):
            return "Models & Training"
        if _has(text, r"security|attack|crypt|privacy"):
            return "Security Research"
        if _has(text, r"chip|hardware|system|network|memory"):
            return "Systems / Hardware Research"
        if _has(text, r"benchmark|dataset|baseline|metric|evaluation"):
            return "Benchmarks & Measurement"
        return "Other Research"

    if primary == "outside_bubble":
        if _has(title, r"iceye|satellite|spacecraft|earth observation|orbit(?:al)?|rocket|aerospace|space launch|nasa|mars"):
            return "Space & Aerospace"
        if _has(title, r"active inference|autonomous reconnaissance|reconnaissance mission|route planning|person search|multi-camera"):
            return "Autonomous Systems & Robotics"
        if _has(text, r"robot|robotaxi|drone|physical ai"):
            return "Robotics & Physical AI"
        if _has(text, r"space|nasa|satellite|spacecraft|mars"):
            return "Space & Aerospace"
        if _has(text, r"quantum|biotech|biology|energy|battery|material"):
            return "Quantum / Biotech / Materials"
        if _has(text, r"network|manufactur|instrument|hci"):
            return "Networking / Manufacturing / HCI"
        return "Other Applied Technology"

    if primary == "community":
        if _has(text, r"agent|model|openai|anthropic|llm|mcp"):
            return "Agent & Model Discussion"
        if _has(text, r"security|exploit|vulnerability|attack|breach"):
            return "Security Community Signals"
        if _has(text, r"outage|cloud|kubernetes|infrastructure"):
            return "Infrastructure Incidents"
        if _has(text, r"github|repository|developer|coding|open source"):
            return "Developer Ecosystem"
        return "Other Community Signals"

    if primary == "business":
        if _has(title, r"nscale") and _has(title, r"funding|financing|fundraise|investment|valuation"):
            return "Funding / Valuation / Markets"
        if _has(text, r"openai|anthropic|google|meta|ai|agent|model"):
            return "AI Corporate Strategy"
        if _has(text, r"broadcom|marvell|nvidia|amd|intel|chip|semiconductor"):
            return "Semiconductor Business"
        if _has(text, r"ipo|valuation|funding|fundraise|investment|revenue|earnings|forecast"):
            return "Funding / Valuation / Markets"
        if _has(text, r"policy|regulation|regulatory|governance|antitrust|law"):
            return "Policy / Regulation / Governance"
        if _has(text, r"acquisition|acquire|merger|partnership|deal"):
            return "M&A / Partnerships"
        return "Other Business Signals"

    return "Other Signals"


def classification_confidence(signal: dict, primary: str | None = None) -> str:
    """Return an internal confidence gate for the semantic placement.

    HIGH means a title/mechanism guard or a strong source-specific lane made the
    decision.  MEDIUM means the title and evidence agree but no narrow guard was
    needed.  LOW is reserved for title-only or otherwise weakly evidenced
    fallback placement; callers should trigger another source read before
    publishing such an item.
    """
    primary = primary or classify_primary_topic(signal)
    title = _compact(signal.get("title") or signal.get("source_title"), 600)
    evidence = mechanism_text(signal)
    source = str(signal.get("source_id") or "").casefold()
    if re.search(
        r"\b(?:codeql|sast|static analysis|false positives?|active inference|reconnaissance|pytorch[- ]image[- ]models|openledger|mcp-native|iceye|satellite|nscale|gpt[- ]?6|astra|chrome|chromium|v8|cve[- ]?\d)\b",
        title,
        re.I,
    ):
        return "HIGH"
    if title and len(evidence) >= 120 and (source.startswith(("arxiv", "github-")) or signal.get("enrichment_state") == "FULL_SOURCE"):
        return "MEDIUM"
    if title and evidence:
        return "MEDIUM"
    return "LOW"


def group_by_subtopic(signals: list[dict], primary: str) -> OrderedDict[str, list[dict]]:
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for signal in signals:
        label = str(signal.get("subtopic") or "").strip() or subtopic_for(signal, primary)
        groups.setdefault(label, []).append(signal)
    return groups
