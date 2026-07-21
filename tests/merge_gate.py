# -*- coding: utf-8 -*-
"""merge_gate.py — tool_router/프롬프트 변경의 머지 게이트 (자기성장형 평가 파이프라인 4/5).

tool_router.py / tool_router_prompt / tool_registry / 라우터 관련 프롬프트를 바꿨으면
머지 전에 이걸 CI처럼 필수로 돌린다. 세 관문을 순서대로 실행하고, 하나라도 실패(회귀
포함)하면 non-zero로 끝난다:

  1) 손으로 고정한 회귀 코퍼스 (tests/tool_router_corpus.json)
  2) (--with-migrated) 이관 시드 코퍼스 (tests/tool_router_corpus_migrated.json)
  3) (--skip-replay 아니면) 실제 결정 로그 리플레이 — 실패 라벨 케이스 기준 회귀 0 확인
     (logs/router_decisions.jsonl이 비어 있으면 0건으로 통과)

정책(CLAUDE.md에도 명시): **회귀가 0이 아닌 변경은 diff 리포트 첨부 없이 머지 금지.**

실행(라이브 LLM 호출 — OPENROUTER_API_KEY 필요, 비용 발생):
    set -a && source .env && set +a
    python3 -m tests.merge_gate                 # 손 코퍼스 + 리플레이
    python3 -m tests.merge_gate --with-migrated # + 이관 시드까지
    python3 -m tests.merge_gate --skip-replay   # 로그 리플레이 생략(코퍼스만)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATED = _ROOT / "tests" / "tool_router_corpus_migrated.json"


def _run(label: str, cmd: list[str]) -> bool:
    print(f"\n{'='*70}\n▶ {label}\n  $ {' '.join(cmd)}\n{'='*70}", flush=True)
    rc = subprocess.run(cmd, cwd=str(_ROOT)).returncode
    ok = rc == 0
    print(f"  → {label}: {'PASS' if ok else f'FAIL (exit {rc})'}", flush=True)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3, help="리플레이 대상 로그 기간(일)")
    ap.add_argument("--with-migrated", action="store_true",
                    help="이관 시드 코퍼스도 게이트에 포함(검수 완료 후 사용)")
    ap.add_argument("--skip-replay", action="store_true", help="실제 로그 리플레이 생략")
    args = ap.parse_args()

    py = [sys.executable, "-m"]
    results: list[tuple[str, bool]] = []

    results.append(("손 코퍼스", _run(
        "손으로 고정한 회귀 코퍼스",
        py + ["tests.test_tool_router", "--live", "--retries", "2"])))

    if args.with_migrated:
        if _MIGRATED.exists():
            results.append(("이관 코퍼스", _run(
                "이관 시드 코퍼스",
                py + ["tests.test_tool_router", "--live", "--retries", "2",
                      "--corpus", str(_MIGRATED)])))
        else:
            print(f"\n⚠️ 이관 코퍼스 없음({_MIGRATED}) — 건너뜀")

    if not args.skip_replay:
        results.append(("로그 리플레이", _run(
            "실제 결정 로그 리플레이(실패 라벨 기준 회귀 0 확인)",
            py + ["tests.replay", "--days", str(args.days), "--only-labeled", "--yes"])))

    print(f"\n{'='*70}\n머지 게이트 결과\n{'='*70}")
    for label, ok in results:
        print(f"  {'✅' if ok else '❌'} {label}")
    all_ok = all(ok for _, ok in results)
    if all_ok:
        print("\n✅ 머지 게이트 통과 — 회귀 없음.")
        return 0
    print("\n❌ 머지 게이트 실패 — 회귀/오류 있음. 위 diff를 확인하고, 의도된 변경이면")
    print("   변경 diff 리포트를 첨부해야만 머지할 수 있어요(CLAUDE.md 정책).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
