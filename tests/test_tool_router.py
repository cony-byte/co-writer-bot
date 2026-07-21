# -*- coding: utf-8 -*-

"""test_tool_router.py — tool_router 회귀 평가 하네스.

★2026-07-21: nl_router가 tool_router(native function calling)로 교체되면서 예전
58케이스 코퍼스(test_nl_router.py)가 안전망 역할을 잃었다 — 실사용 오라우팅이
날 때마다 개별 수정만 반복하고, 수정이 다른 케이스를 깨뜨려도 알 수 없었다.
이 하네스는 실사용에서 실제로 오라우팅이 났던 발화들을 tool_router 기준으로
고정한다. tool_router.decide_from_context()는 Slack 의존이 없어서(모듈 임포트
포함) SLACK_BOT_TOKEN 없이도 돌지만, OPENROUTER_API_KEY는 필요하다(.env source).

사용법:
    python3 -m tests.test_tool_router --live            # 전체
    python3 -m tests.test_tool_router --live --id <id>  # 한 케이스
    python3 -m tests.test_tool_router --live --retries 2  # 실패 케이스 자동 재시도

expected 키:
    type / type_oneof     — answer | clarification | tool_call | tool_calls
    tool / tool_oneof     — 첫 번째 실행 호출의 tool 이름
    forbid_tool           — 첫 번째 호출이 이 tool이면 실패
    tools_include         — 전체 호출 목록에 이 tool들이 모두 포함돼야 함
    forbid_tools_anywhere — 전체 호출 목록 어디에도 있으면 안 되는 tool들
    calls_min             — 실행 호출 개수 하한
    args                  — 첫 번째 호출 arguments 검사:
                             work/episode/scene/kind 정확일치, cuts 리스트 일치,
                             attachment_id_required(true → 비어있지 않아야),
                             instruction_contains(부분 문자열)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CORPUS = Path(__file__).parent / "tool_router_corpus.json"

BASE_CTX = {
    "tracked_work": None,
    "tracked_episode": None,
    "sb_stage": 0,
    "registered_works": {},
    "registered_elements": {},
    "last_bot_output_head": None,
    "attached_image_count": 0,
    "attached_file_names": [],
    "recent_messages": [],
    "attachments": [],
    "interrupted_job": None,
}


def _calls(decision) -> list[dict]:
    if decision.calls:
        return decision.calls
    if decision.type == "tool_call" and decision.tool:
        return [{"tool": decision.tool, "arguments": decision.arguments or {}}]
    return []


def _check(expected: dict, decision) -> list[str]:
    errs: list[str] = []
    if "type" in expected and decision.type != expected["type"]:
        errs.append(f"type {decision.type!r} != {expected['type']!r}")
    if "type_oneof" in expected and decision.type not in expected["type_oneof"]:
        errs.append(f"type {decision.type!r} not in {expected['type_oneof']}")

    calls = _calls(decision)
    tools = [c["tool"] for c in calls]
    first = calls[0] if calls else {"tool": None, "arguments": {}}

    if "tool" in expected and first["tool"] != expected["tool"]:
        errs.append(f"tool {first['tool']!r} != {expected['tool']!r}")
    if "tool_oneof" in expected and first["tool"] not in expected["tool_oneof"]:
        errs.append(f"tool {first['tool']!r} not in {expected['tool_oneof']}")
    if "forbid_tool" in expected and first["tool"] == expected["forbid_tool"]:
        errs.append(f"tool must not be {expected['forbid_tool']!r}")
    for t in expected.get("tools_include", []):
        if t not in tools:
            errs.append(f"calls must include {t!r} (got {tools})")
    for t in expected.get("forbid_tools_anywhere", []):
        if t in tools:
            errs.append(f"calls must not include {t!r} (got {tools})")
    if "calls_min" in expected and len(calls) < expected["calls_min"]:
        errs.append(f"calls len {len(calls)} < {expected['calls_min']}")

    args_exp = expected.get("args") or {}
    args = first.get("arguments") or {}
    for key in ("work", "episode", "scene", "kind", "use_notion_storyboard_ref"):
        if key in args_exp and args.get(key) != args_exp[key]:
            errs.append(f"args.{key} {args.get(key)!r} != {args_exp[key]!r}")
    if "cuts" in args_exp and (args.get("cuts") or None) != args_exp["cuts"]:
        errs.append(f"args.cuts {args.get('cuts')!r} != {args_exp['cuts']!r}")
    if args_exp.get("attachment_id_required") and not str(args.get("attachment_id") or "").strip():
        errs.append("args.attachment_id must be non-empty")
    if (
        "instruction_contains" in args_exp
        and args_exp["instruction_contains"] not in (args.get("instruction") or "")
    ):
        errs.append(f"args.instruction must contain {args_exp['instruction_contains']!r}")
    return errs


def _run_case(case: dict):
    from bot import tool_router

    ctx = {**BASE_CTX, **(case.get("ctx") or {})}
    return tool_router.decide_from_context(case["text"], ctx)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="실제 LLM 백엔드로 평가")
    parser.add_argument("--id", help="특정 케이스만")
    parser.add_argument("--retries", type=int, default=1,
                        help="실패 케이스 재시도 횟수(백엔드 일시 오류 대비, 기본 1)")
    args = parser.parse_args()

    if not args.live:
        print("오프라인 모드 없음 — --live 로 실행하세요 (라우터는 LLM 호출이 본체입니다).")
        return 2

    cases = json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]
    if args.id:
        cases = [case for case in cases if case["id"] == args.id]
        if not cases:
            print(f"해당 id 케이스 없음: {args.id}")
            return 2

    passed, failed = 0, []
    for case in cases:
        errs, decision = None, None
        for attempt in range(max(1, args.retries)):
            try:
                decision = _run_case(case)
            except Exception as exc:
                errs = [f"backend error: {exc}"]
                continue
            errs = _check(case["expected"], decision)
            if not errs:
                break
        if errs:
            raw = None
            if decision is not None:
                raw = {
                    "type": decision.type,
                    "tool": decision.tool,
                    "calls": _calls(decision),
                    "text": (decision.text or "")[:150],
                }
            failed.append((case["id"], errs, raw))
        else:
            calls = _calls(decision)
            label = decision.type if not calls else ", ".join(c["tool"] for c in calls)
            passed += 1
            print(f"  ✅ {case['id']}: {label}")

    print(f"\n{passed}/{len(cases)} 통과")
    for case_id, errs, raw in failed:
        print(f"  ❌ {case_id}")
        for err in errs:
            print(f"     - {err}")
        if raw:
            print(f"     raw: {json.dumps(raw, ensure_ascii=False)[:300]}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
