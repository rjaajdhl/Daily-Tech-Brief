#!/usr/bin/env python3
"""Prepare the complete daily technology-intelligence evidence intake.

Primary evidence (official, research, advisories, specialist and independent
reporting, repositories) is emitted before bounded community navigation. A
failure in one collector is surfaced as a coverage gap so the cron agent can
recover with direct web search instead of silently treating it as no news.
"""

from __future__ import annotations
import os

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

PROFILE_ROOT = Path(os.environ.get("TI_ROOT") or Path(__file__).resolve().parents[1])
SCRIPT_ROOT = PROFILE_ROOT / "scripts"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="PDT/PST calendar date (YYYY-MM-DD)")
    return parser.parse_args()


def target_day(value: str | None) -> dt.date:
    if value:
        return dt.date.fromisoformat(value)
    return dt.datetime.now(LOCAL_TZ).date() - dt.timedelta(days=1)


def run_script(name: str, day: dt.date, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_ROOT / name), "--date", day.isoformat()],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def emit_result(label: str, result: subprocess.CompletedProcess[str]) -> None:
    print(f"\n===== {label} =====")
    if result.returncode == 0:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        return
    diagnostic = (result.stderr or result.stdout or "no diagnostic")[-3000:]
    print(f"커버리지 실패: 수집기가 종료 코드 {result.returncode}로 끝났습니다.")
    print(diagnostic)
    print(
        "이 범주는 표적 웹 검색과 원문 직접 조회로 복구해야 합니다. "
        "실패를 중요 사건이 없었다는 증거로 해석하지 마세요."
    )


def main() -> int:
    args = parse_args()
    day = target_day(args.date)
    print("매일 기술 인텔리전스 — 아침 브리핑 증거 준비")
    print(f"대상 날짜: {day.isoformat()} ({LOCAL_TZ})")
    print(
        "근거 우선순위: 공식 발표·권고·원 저장소·모델 카드 -> 논문·기술 보고서 -> "
        "독립·전문 보도 -> Hacker News·Reddit 등 커뮤니티 신호."
    )
    print(
        "출처별 기사 quota는 없습니다. 커뮤니티의 게시물 수와 참여량이 브리핑의 비중을 결정하지 않습니다."
    )

    try:
        primary = run_script("multi_source_daily_intake.py", day, 360)
    except subprocess.TimeoutExpired as exc:
        primary = subprocess.CompletedProcess(exc.cmd, 124, exc.stdout or "", exc.stderr or "collector timed out")
    emit_result("다중 출처 원문·발견 수집", primary)

    try:
        reddit = run_script("reddit_daily_prepare.py", day, 720)
    except subprocess.TimeoutExpired as exc:
        reddit = subprocess.CompletedProcess(exc.cmd, 124, exc.stdout or "", exc.stderr or "collector timed out")
    emit_result("보조 Reddit 커뮤니티 수집", reddit)

    print("\n===== Coverage & Discovery Correction 확인 =====")
    print(
        "GitHub Trending은 GitHub Changelog와 별개의 실행 source입니다. "
        "multi_source_daily_intake.py가 github-trending-daily를 fetch하고, "
        "github_daily_discovery.py parser로 repository·stars today·언어·설명을 raw/archive와 ledger에 남깁니다."
    )
    print(
        "조건부 empty 결과 템플릿은 `GitHub Trending checked; no repository passed editorial threshold.`입니다. "
        "이 문구는 실제 Trending 접근 성공과 후보 0건이 동시에 확인된 경우에만 최종 본문에 사용하며, "
        "현재 prepare stdout의 이 설명 자체를 실제 결과로 해석하지 않습니다. 접근 실패·rate limit이면 checked가 아니라 unavailable/partial로 기록합니다."
    )
    print(
        "GitHub repository discovery는 created/pushed activity, rapid attention, releases, security tools, agent frameworks, "
        "research code와 paper-linked repositories를 Trending과 별도로 확인합니다."
    )
    print(
        "Local/Open Model model radar는 formal release가 없어도 Hugging Face model activity, model card, benchmark discussion, "
        "developer report, checkpoint, capability/licensing/adoption signal을 확인합니다. routine inference-engine optimization은 제외합니다."
    )
    print(
        "Community discovery는 Hacker News·Reddit·GitHub Issues/Discussions·Hugging Face Discussions·X·Lobsters·" 
        "security/model/developer communities를 여러 conversation으로 분리합니다. low confidence는 삭제 사유가 아니라 label 변경 사유입니다."
    )
    print(
        "Outside My Usual Bubble broad discovery는 fixed-category 수집 뒤 keyword-independent query로 robotics·space·biotech·" 
        "quantum·energy·hardware·materials·networking·instrumentation·manufacturing·HCI 등을 별도 확인합니다."
    )
    print(
        "허용 confidence tiers: Confirmed / Developing / Community Signal / Early Radar / Speculative. "
        "Speculative은 정말 유용할 때만 경고와 함께 포함하며, 불확실성을 이유로 정보를 자동 삭제하지 않습니다."
    )
    print(
        "선정 기준은 User Relevance only가 아니라 Importance × Novelty × User Relevance입니다. "
        "최종 story 수·Major 수·Deep Analysis 수에는 minimum/maximum quota가 없습니다."
    )

    print("\n===== 최종 초안 전 확인 =====")
    print(
        "1. 공식 feed가 없는 페이지, 통신·사업 보도, Project Zero/GTIG, vendor PSIRT, "
        "GitHub Trending/repository discovery, Hugging Face model radar, 반도체·Outside My Usual Bubble 검색 행을 직접 확인합니다."
    )
    print(
        "2. AI·에이전트·프런티어 범위는 모델/API 동작·제한, 에이전트 runtime·orchestration·도구·MCP·메모리, "
        "평가·안전·prompt injection·도구 오용, 공개·독립 구현을 여러 vendor 계열에서 확인합니다. "
        "AWS 한 건으로 전체 범위를 덮었다고 하지 않습니다. 일반 serving/inference 최적화는 정기 범위에서 제외합니다."
    )
    print(
        "3. 모든 근거 lane에서 사건·claim cluster와 후보/처분 장부를 만든 뒤 최종 작성 단계에 넘깁니다. "
        "고정 기사 수나 filler는 금지하며, full archive의 모든 deduplicated cluster를 included·duplicate·weak·held·failed 후보로 명시적으로 처분합니다. "
        "Reddit 등 collector의 bounded review queue/stdout은 탐색용 navigation일 뿐 topic-section inclusion cap이 아닙니다."
    )
    print(
        "4. 선택한 기사·권고·논문/모델 카드·저장소를 직접 엽니다. metadata·snippet·커뮤니티 요약만으로 "
        "기술 mechanism이나 숫자 주장을 만들지 않습니다."
    )
    print(
        "5. 이 단계에서는 최종 독자용 장문 분석을 반복하지 않습니다. material 후보에 what/why/evidence, "
        "불확실성·confidence label·날짜·다음 trigger와 필요한 Deep Dive 후보만 준비합니다. Deep Analysis는 2–4문장으로 끝낼 수 없을 때만 표시합니다. "
        "보안 threat model·상세 PoC는 AI Security 또는 별도 Deep Dive Queue로 보냅니다."
    )
    print(
        "6. 커뮤니티는 검증된 사실만 기다리지 않습니다. 기술적으로 흥미로운 대화는 `COMMUNITY SIGNAL — UNVERIFIED` "
        "또는 `Early Radar` 후보로 보존하고 플랫폼·대표 thread·확인된 부분·추측을 분리합니다. 참여도·점수·댓글 수는 포함 문턱이 아닙니다."
    )
    print(
        "7. 최종 브리핑은 기본 고대비 글자색을 사용하며, 장식용 카드·SVG·저대비 색상·페이지 끝 일괄 분석을 사용하지 않습니다. "
        "보안 항목이 쉬운 검증 때문에 과점유하지 않는지 비보안 material 후보와 함께 균형을 재검토합니다."
    )
    print(
        "8. 공식·연구·보안·보도·저장소·community·model radar·outside-bubble 커버리지가 부분적이면 gap을 backend evidence에 기록하고 직접 복구를 시도합니다. "
        "이 준비 단계는 최종 확정이 아니며, 05:55 최종화가 read-back 후 상태를 결정합니다."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
