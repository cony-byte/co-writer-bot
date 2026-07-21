# -*- coding: utf-8 -*-

"""test_nl_router.py — 라우터 회귀 평가 하네스.

사용법:
python -m tests.test_nl_router --live
python -m tests.test_nl_router --live --id target-not-episode
COWRITER_ROUTER_BACKEND=api python -m tests.test_nl_router --live
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CORPUS = Path(__file__).parent / "nl_router_corpus.json"


def _mock_slack(ctx: dict):
    """slack_io 의존을 끊고 ctx의 recent_messages를 스레드 이력으로 주입."""
    from bot.shared import slack_io

    msgs = []
    for message in ctx.get("recent_messages", []):
        role = "assistant" if message.get("role") == "봇" else "user"
        msgs.append({"role": role, "content": message.get("text", "")})
    slack_io._thread_messages = lambda ch, ts: msgs
    slack_io._reply = lambda ch, ts, text, **kw: None


def _check(expected: dict, route) -> list[str]:
    errs = []
    if "intent" in expected and route.intent != expected["intent"]:
        errs.append(f"intent {route.intent!r} != {expected['intent']!r}")
    if "intent_oneof" in expected and route.intent not in expected["intent_oneof"]:
        errs.append(f"intent {route.intent!r} not in {expected['intent_oneof']}")
    if "forbid_intent" in expected and route.intent == expected["forbid_intent"]:
        errs.append(f"intent must not be {expected['forbid_intent']!r}")
    if "episode" in expected and route.episode != expected["episode"]:
        errs.append(f"episode {route.episode!r} != {expected['episode']!r}")
    if "forbid_episode" in expected and route.episode == expected["forbid_episode"]:
        errs.append(f"episode must not be {expected['forbid_episode']}")
    if "scene" in expected and route.scene != expected["scene"]:
        errs.append(f"scene {route.scene!r} != {expected['scene']!r}")
    if "cuts" in expected and route.cuts != expected["cuts"]:
        errs.append(f"cuts {route.cuts!r} != {expected['cuts']!r}")
    if "work" in expected and route.work != expected["work"]:
        errs.append(f"work {route.work!r} != {expected['work']!r}")
    if "question_type" in expected and route.question_type != expected["question_type"]:
        errs.append(
            f"question_type {route.question_type!r} != {expected['question_type']!r}"
        )
    if "elements_len" in expected:
        count = len(route.elements or [])
        if count != expected["elements_len"]:
            errs.append(f"elements len {count} != {expected['elements_len']}")
    if "elements_min" in expected:
        count = len(route.elements or [])
        if count < expected["elements_min"]:
            errs.append(f"elements len {count} < {expected['elements_min']}")
    if "element_names" in expected:
        names = [element.get("name") for element in (route.elements or [])]
        if names != expected["element_names"]:
            errs.append(f"element names {names!r} != {expected['element_names']!r}")
    if "element_image_indices" in expected:
        indices = [element.get("image_index") for element in (route.elements or [])]
        if indices != expected["element_image_indices"]:
            errs.append(f"element image indices {indices!r} != {expected['element_image_indices']!r}")
    for bad in expected.get("forbid_element_names", []):
        names = [element.get("name", "") for element in (route.elements or [])]
        if bad in names:
            errs.append(f"element name {bad!r} must not appear (got {names})")
    if "episodes" in expected and (route.episodes or None) != expected["episodes"]:
        errs.append(f"episodes {route.episodes!r} != {expected['episodes']!r}")
    if "steps_min" in expected:
        count = len(route.steps or [])
        if count < expected["steps_min"]:
            errs.append(f"steps len {count} < {expected['steps_min']}")
    if (
        "instruction_contains" in expected
        and expected["instruction_contains"] not in (route.instruction or "")
    ):
        errs.append(
            f"instruction must contain {expected['instruction_contains']!r}"
        )
    if "element_character" in expected:
        got = (route.elements or [{}])[0].get("character") if route.elements else None
        if got != expected["element_character"]:
            errs.append(f"element character {got!r} != {expected['element_character']!r}")
    if "element_part" in expected:
        got = (route.elements or [{}])[0].get("part") if route.elements else None
        if got != expected["element_part"]:
            errs.append(f"element part {got!r} != {expected['element_part']!r}")
    if "display_label" in expected and route.display_label != expected["display_label"]:
        errs.append(f"display_label {route.display_label!r} != {expected['display_label']!r}")
    if (
        "display_label_not_contains" in expected
        and expected["display_label_not_contains"] in (route.display_label or "")
    ):
        errs.append(
            f"display_label must not contain {expected['display_label_not_contains']!r}"
        )
    if expected.get("needs_clarification") and not route.needs_clarification:
        errs.append("needs_clarification expected")
    if (
        "instruction_forbid" in expected
        and expected["instruction_forbid"] in (route.instruction or "")
    ):
        errs.append(f"instruction must not contain {expected['instruction_forbid']!r}")
    return errs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="실제 LLM 백엔드로 평가")
    parser.add_argument("--id", help="특정 케이스만")
    args = parser.parse_args()

    if not args.live:
        print("오프라인 모드 없음 — --live 로 실행하세요 (라우터는 LLM 호출이 본체입니다).")
        return 2

    cases = json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]
    if args.id:
        cases = [case for case in cases if case["id"] == args.id]

    from bot import nl_router

    base_ctx = {
        "tracked_work": None,
        "tracked_episode": None,
        "sb_stage": 0,
        "registered_works": {},
        "registered_elements": {},
        "last_bot_output_head": None,
        "attached_image_count": 0,
        "attached_file_names": [],
        "recent_messages": [],
    }

    passed, failed = 0, []
    for case in cases:
        ctx = {**base_ctx, **case.get("ctx", {})}
        _mock_slack(ctx)
        nl_router._build_context = lambda ch, ts, ev, _ctx=ctx: _ctx
        event = {
            "text": case["text"],
            "files": [
                {"mimetype": "image/png", "name": f"img{i}.png"}
                for i in range(ctx["attached_image_count"])
            ],
        }
        route = nl_router.route("C_TEST", "T_TEST", case["text"], event)
        if route is None:
            failed.append((case["id"], ["router returned None (backend fail)"]))
            continue
        errs = _check(case["expected"], route)
        if errs:
            failed.append((case["id"], errs, route.raw))
        else:
            passed += 1
            print(f"  ✅ {case['id']}: {route.intent}")

    print(f"\n{passed}/{len(cases)} 통과")
    for item in failed:
        case_id, errs = item[0], item[1]
        print(f"  ❌ {case_id}")
        for err in errs:
            print(f"     - {err}")
        if len(item) > 2:
            print(f"     raw: {json.dumps(item[2], ensure_ascii=False)[:300]}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
