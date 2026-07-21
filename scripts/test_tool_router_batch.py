#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch-evaluate native tool decisions without Slack or handler execution.

Accepted inputs:
  JSON   {"cases": [{"id", "text", "ctx", "expected"}, ...]} or a plain list
  JSONL  one case object per line
  CSV    id,text,ctx,expected (ctx/expected are JSON strings)

Example:
  python3 scripts/test_tool_router_batch.py utterances.json --workers 5
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


BASE_CONTEXT = {
    "tracked_work": None,
    "tracked_episode": None,
    "sb_stage": 0,
    "registered_works": {},
    "registered_elements": {},
    "last_bot_output_head": None,
    "attached_image_count": 0,
    "attached_file_names": [],
    "answer_sources": {},
    "recent_messages": [],
    "attachments": [],
}


# Compatibility for the 2026-07-21 corpus, whose expectations were written for the
# removed intent taxonomy.  This is test-input translation only; production never sees
# or returns these names.
LEGACY_INTENT_TARGETS = {
    "answer_question": ("answer", None),
    "work_status": ("answer", None),
    "episode_status": ("answer", None),
    "smalltalk": ("answer", None),
    "script_generate": ("tool_call", "generate_script"),
    "script_revise": ("tool_call", "revise_script"),
    "plan_edit": ("tool_call", "edit_plan"),
    "feedback": ("tool_call", "review_script"),
    "scene_design": ("tool_call", "generate_scene_design"),
    "detail_conti": ("tool_call", "generate_detail_conti"),
    "conti_rewrite": ("tool_call", "rewrite_conti"),
    "storyboard_image": ("tool_call", "generate_storyboard_grid"),
    "stillcut": ("tool_call", "generate_stillcuts"),
    "video": ("tool_call", "generate_video"),
    "element_register": ("tool_call", "register_reference_image"),
    "element_edit": ("tool_call", "replace_reference_image"),
    "element_generate": ("tool_call", "generate_reference_image"),
    "cancel_job": ("tool_call", "cancel_current_job"),
    "resume_interrupted": ("tool_call", "resume_interrupted_job"),
    "sync": ("tool_call", "sync_notion"),
    # Natural-language approval/rejection no longer consumes pending execution state.
    "confirm_previous": ("clarification", None),
    "reject_previous": ("clarification", None),
}

_BUTTON_ONLY_ACK_RE = re.compile(
    r"\s*(?:응|웅|네|넵|예|그래|좋아|오케이|오키|ㅇㅋ|ㅇㅇ|ok|okay|yes|"
    r"해줘|그렇게\s*해줘|응\s*그렇게\s*해줘|네\s*그렇게\s*해줘|그걸로|이걸로|"
    r"계속|계속해|이어서\s*해)\s*[.!~]*\s*", re.I,
)


def _normalize_expected(raw: dict) -> dict:
    """Translate old intent expectations and preserve detailed slot assertions."""
    expected = dict(raw or {})
    normalized = {
        key: value for key, value in expected.items()
        if key in {"type", "type_oneof", "tool", "tool_oneof", "forbid_tool",
                   "arguments", "validation_ok", "requires_confirmation"}
    }
    if expected.get("needs_clarification"):
        normalized["type"] = "clarification"
    elif expected.get("intent"):
        target = LEGACY_INTENT_TARGETS.get(expected["intent"])
        if target:
            normalized["type"], tool = target
            if tool:
                normalized["tool"] = tool
            if expected["intent"] == "element_register":
                normalized.pop("tool", None)
                normalized["tool_oneof"] = [
                    "register_reference_image", "register_reference_images"
                ]
            elif expected["intent"] == "element_generate":
                normalized.pop("tool", None)
                normalized["tool_oneof"] = [
                    "generate_reference_image", "generate_reference_images"
                ]
            elif expected["intent"] == "element_edit":
                normalized.pop("tool", None)
                normalized["tool_oneof"] = [
                    "replace_reference_image", "generate_reference_image"
                ]
            elif expected["intent"] == "confirm_previous":
                # Button-only policy forbids execution, but a work-name answer to a
                # clarification may safely be acknowledged as answer or clarification.
                normalized.pop("type", None)
                normalized["type_oneof"] = ["answer", "clarification"]
        else:
            normalized["unsupported_legacy_intent"] = expected["intent"]
    if expected.get("intent_oneof"):
        normalized["decision_oneof"] = [
            {"type": target[0], "tool": target[1]}
            for name in expected["intent_oneof"]
            if (target := LEGACY_INTENT_TARGETS.get(name))
        ]
    if expected.get("forbid_intent"):
        target = LEGACY_INTENT_TARGETS.get(expected["forbid_intent"])
        if target:
            normalized.setdefault("forbid_decisions", []).append(
                {"type": target[0], "tool": target[1]}
            )

    arguments = dict(normalized.get("arguments") or {})
    for key in ("episode", "episodes", "scene", "cuts", "work"):
        if key in expected:
            arguments[key] = expected[key]
    if "work_canon" in expected:
        arguments["work"] = expected["work_canon"]
    if arguments:
        normalized["arguments"] = arguments
    for key in ("forbid_episode", "instruction_contains", "instruction_forbid",
                "elements_len", "elements_min", "forbid_element_names", "steps_min"):
        if key in expected:
            normalized[key] = expected[key]
    normalized["legacy"] = expected
    return normalized


def _load_env(path: Path | None) -> None:
    """Minimal .env reader; existing process variables always win."""
    if path is None or not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        try:
            # Match shell-style .env semantics: an unquoted trailing ``# comment`` is
            # not part of the value, while a quoted # remains literal.
            parsed = shlex.split(value, comments=True, posix=True)
            value = parsed[0] if len(parsed) == 1 else value
        except ValueError:
            value = value.strip("'\"")
        os.environ[key] = value


def _json_cell(value: str | None, default):
    if value is None or not value.strip():
        return default
    return json.loads(value)


def _load_cases(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                 if line.strip()]
    elif suffix == ".csv":
        cases = []
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for index, row in enumerate(csv.DictReader(handle), 1):
                expected = _json_cell(row.get("expected"), {})
                if row.get("expected_type"):
                    expected["type"] = row["expected_type"]
                if row.get("expected_tool"):
                    expected["tool"] = row["expected_tool"]
                cases.append({
                    "id": row.get("id") or str(index),
                    "text": row.get("text") or row.get("utterance") or "",
                    "ctx": _json_cell(row.get("ctx") or row.get("context"), {}),
                    "expected": expected,
                })
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        cases = data.get("cases", []) if isinstance(data, dict) else data
    if not isinstance(cases, list):
        raise ValueError("입력은 cases 배열 또는 JSON 배열이어야 합니다")

    normalized = []
    seen = set()
    for index, raw in enumerate(cases, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"case {index}가 객체가 아닙니다")
        case_id = str(raw.get("id") or index)
        text = str(raw.get("text") or raw.get("utterance") or raw.get("query") or "").strip()
        if not text:
            raise ValueError(f"case {case_id}: text가 비어 있습니다")
        if case_id in seen:
            raise ValueError(f"중복 case id: {case_id}")
        seen.add(case_id)
        ctx = dict(raw.get("ctx") or raw.get("context") or {})
        attachments = list(raw.get("attachments") or ctx.get("attachments") or [])
        if not attachments and int(ctx.get("attached_image_count") or 0) > 0:
            attachments = [
                {"id": f"F_TEST_{n + 1}", "name": f"image_{n + 1}.png", "mimetype": "image/png"}
                for n in range(int(ctx["attached_image_count"]))
            ]
        ctx = {**BASE_CONTEXT, **ctx, "attachments": attachments}
        ctx["attached_image_count"] = sum(
            1 for item in attachments
            if str(item.get("mimetype") or "").startswith("image/")
        )
        ctx["attached_file_names"] = [str(item.get("name") or "") for item in attachments]
        expected = _normalize_expected(dict(raw.get("expected") or {}))
        if _BUTTON_ONLY_ACK_RE.fullmatch(text):
            expected["type"] = "clarification"
            expected.pop("tool", None)
            expected.pop("tool_oneof", None)
            expected.pop("decision_oneof", None)
        if (raw.get("expected") or {}).get("intent") == "resume_interrupted" \
                and not ctx.get("interrupted_job"):
            expected["type"] = "clarification"
            expected.pop("tool", None)
        normalized.append({
            "index": index - 1,
            "id": case_id,
            "text": text,
            "context": ctx,
            "expected": expected,
        })
    return normalized


def _check_expected(expected: dict, result: dict) -> list[str]:
    errors = []
    actual_type, actual_tool = result.get("type"), result.get("tool")
    compatible_compound = (actual_type == "tool_calls" and expected.get("type") == "tool_call"
                           and expected.get("steps_min", 0) > 1)
    if "type" in expected and actual_type != expected["type"] and not compatible_compound:
        errors.append(f"type {actual_type!r} != {expected['type']!r}")
    if "type_oneof" in expected and actual_type not in expected["type_oneof"]:
        errors.append(f"type {actual_type!r} not in {expected['type_oneof']!r}")
    if "tool" in expected and actual_tool != expected["tool"]:
        legacy_intent = (expected.get("legacy") or {}).get("intent")
        logo_equivalent = actual_tool == "replace_logo" and legacy_intent in {
            "element_register", "element_edit"
        }
        if not logo_equivalent:
            errors.append(f"tool {actual_tool!r} != {expected['tool']!r}")
    if "tool_oneof" in expected and actual_tool not in expected["tool_oneof"]:
        legacy_intents = set((expected.get("legacy") or {}).get("intent_oneof") or [])
        legacy_intent = (expected.get("legacy") or {}).get("intent")
        if not (actual_tool == "replace_logo" and (
                legacy_intent in {"element_register", "element_edit"}
                or legacy_intents.intersection({"element_register", "element_edit"}))):
            errors.append(f"tool {actual_tool!r} not in {expected['tool_oneof']!r}")
    if "forbid_tool" in expected and actual_tool == expected["forbid_tool"]:
        errors.append(f"tool must not be {actual_tool!r}")
    if expected.get("unsupported_legacy_intent"):
        errors.append(f"unsupported legacy intent: {expected['unsupported_legacy_intent']}")
    if expected.get("decision_oneof"):
        if not any(
            (actual_type == item["type"] or
             (actual_type == "tool_calls" and item["type"] == "tool_call"))
            and (item.get("tool") is None or actual_tool == item["tool"])
            for item in expected["decision_oneof"]
        ):
            legacy_intents = set((expected.get("legacy") or {}).get("intent_oneof") or [])
            if not (actual_tool == "replace_logo" and legacy_intents.intersection(
                    {"element_register", "element_edit"})):
                errors.append(f"decision {actual_type}/{actual_tool} not in {expected['decision_oneof']!r}")
    for item in expected.get("forbid_decisions", []):
        if actual_type == item["type"] and (item.get("tool") is None or actual_tool == item["tool"]):
            errors.append(f"forbidden decision: {actual_type}/{actual_tool}")
    # Slot assertions apply only to executable calls. Answers and clarification have no
    # execution arguments; old corpora sometimes carried work_canon/episode merely as
    # answer context, and treating those as missing tool slots creates false failures.
    if actual_type in ("tool_call", "tool_calls"):
        arguments = result.get("arguments") or {}
        for key, value in expected.get("arguments", {}).items():
            if arguments.get(key) != value:
                errors.append(f"argument {key}={arguments.get(key)!r} != {value!r}")
        if "forbid_episode" in expected and arguments.get("episode") == expected["forbid_episode"]:
            errors.append(f"episode must not be {expected['forbid_episode']!r}")
        instruction = str(arguments.get("instruction") or "")
        if expected.get("instruction_contains") not in (None, ""):
            needle = expected["instruction_contains"]
            if needle not in instruction:
                errors.append(f"instruction must contain {needle!r}")
        if expected.get("instruction_forbid") and expected["instruction_forbid"] in instruction:
            errors.append(f"instruction must not contain {expected['instruction_forbid']!r}")
        elements = arguments.get("elements") or ([] if not arguments.get("name") else [arguments])
        if "elements_len" in expected and len(elements) != expected["elements_len"]:
            errors.append(f"elements len {len(elements)} != {expected['elements_len']}")
        if "elements_min" in expected and len(elements) < expected["elements_min"]:
            errors.append(f"elements len {len(elements)} < {expected['elements_min']}")
        names = [str(item.get("name") or "") for item in elements if isinstance(item, dict)]
        for forbidden in expected.get("forbid_element_names", []):
            if forbidden in names:
                errors.append(f"element name must not be {forbidden!r}")
    if expected.get("steps_min", 0) > 1:
        count = len(result.get("calls") or [])
        if count < expected["steps_min"]:
            errors.append(f"compound request requires {expected['steps_min']} steps; got {count}")
    if expected.get("validation_ok") is True and result.get("validation_error"):
        errors.append(f"validation failed: {result['validation_error']}")
    if expected.get("requires_confirmation") is not None:
        if result.get("requires_confirmation") is not expected["requires_confirmation"]:
            errors.append(
                f"requires_confirmation {result.get('requires_confirmation')!r} "
                f"!= {expected['requires_confirmation']!r}"
            )
    return errors


def _evaluate(case: dict, *, model: str | None, timeout: int,
              retries: int) -> dict:
    from bot import tool_registry, tool_router

    started = time.monotonic()
    last_error = None
    for attempt in range(retries + 1):
        try:
            decision = tool_router.decide_from_context(
                case["text"], case["context"], model=model, timeout=timeout,
            )
            result = {
                "index": case["index"],
                "id": case["id"],
                "text": case["text"],
                "type": decision.type,
                "tool": decision.tool,
                "arguments": decision.arguments,
                "calls": decision.calls or [],
                "answer": decision.text if decision.type == "answer" else None,
                "clarification": decision.text if decision.type == "clarification" else None,
                "validation_error": None,
                "risk": None,
                "requires_confirmation": False,
                "attempts": attempt + 1,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            if decision.type in ("tool_call", "tool_calls"):
                calls = decision.calls or [{
                    "tool": decision.tool, "arguments": decision.arguments or {}
                }]
                context = tool_router._resolved_context(case["context"])
                event = {"files": list(context.get("attachments") or [])}
                ctx = SimpleNamespace(event=event, context=context)
                risks, validation_errors = [], []
                for item in calls:
                    spec = tool_registry.get(item.get("tool") or "")
                    if spec is None:
                        validation_errors.append(f"unknown tool: {item.get('tool')}")
                        continue
                    item["arguments"] = tool_registry.hydrate_arguments(
                        spec, item.get("arguments") or {}, context
                    )
                    error = tool_registry.validate_call(spec, item["arguments"], ctx)
                    if error:
                        validation_errors.append(f"{spec.name}: {error}")
                    risks.append(spec.risk)
                result["calls"] = calls
                if calls:
                    result["tool"] = calls[0].get("tool")
                    result["arguments"] = calls[0].get("arguments")
                result["validation_error"] = "; ".join(validation_errors) or None
                result["risk"] = (tool_registry.HIGH if tool_registry.HIGH in risks
                                  else tool_registry.LOW)
                result["requires_confirmation"] = True
            expectation_errors = _check_expected(case["expected"], result)
            if result["validation_error"]:
                expectation_errors.append(f"schema/domain: {result['validation_error']}")
            result["expected"] = case["expected"]
            result["errors"] = expectation_errors
            result["passed"] = not expectation_errors
            return result
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(min(2 ** attempt, 4))
    return {
        "index": case["index"], "id": case["id"], "text": case["text"],
        "type": None, "tool": None, "arguments": None,
        "provider_error": last_error, "attempts": retries + 1,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "expected": case["expected"], "errors": [last_error], "passed": False,
    }


def _write_report(path: Path, cases: list[dict], results: list[dict], model: str | None) -> None:
    ordered = sorted(results, key=lambda item: item["index"])
    passed = sum(bool(item.get("passed")) for item in ordered)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_count": len(cases),
        "model": model or os.environ.get("COWRITER_ROUTER_MODEL")
                 or os.environ.get("OPENROUTER_LLM_MODEL") or "project-default",
        "summary": {"passed": passed, "failed": len(ordered) - passed,
                    "completed": len(ordered)},
        "results": ordered,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Slack 없는 native tool-router 100문장 배치 테스트")
    parser.add_argument("input", type=Path, help="JSON, JSONL 또는 CSV 케이스 파일")
    parser.add_argument("--output", type=Path, help="결과 JSON 경로")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--model")
    parser.add_argument("--id", action="append", dest="ids", help="특정 case id만 실행(반복 가능)")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true", help="입력만 검증하고 API를 호출하지 않음")
    args = parser.parse_args()

    _load_env(args.env_file)
    cases = _load_cases(args.input)
    if args.ids:
        wanted = set(args.ids)
        cases = [case for case in cases if case["id"] in wanted]
    if args.limit is not None:
        cases = cases[:max(0, args.limit)]
    if not cases:
        parser.error("실행할 케이스가 없습니다")
    if len(cases) > 1000:
        parser.error("한 번에 최대 1000케이스까지 허용합니다")

    output = args.output or args.input.with_name(args.input.stem + ".results.json")
    print(f"입력 검증 완료: {len(cases)}건 | Slack 호출/handler 실행 없음")
    if args.dry_run:
        print("dry-run 완료 — API를 호출하지 않았습니다.")
        return 0
    if not os.environ.get("OPENROUTER_API_KEY"):
        parser.error("OPENROUTER_API_KEY가 필요합니다 (--env-file 또는 환경변수)")

    workers = max(1, min(args.workers, 20))
    results = []
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_evaluate, case, model=args.model, timeout=args.timeout,
                        retries=max(0, args.retries)): case
            for case in cases
        }
        for future in as_completed(futures):
            result = future.result()
            with lock:
                results.append(result)
                _write_report(output, cases, results, args.model)
                mark = "✅" if result.get("passed") else "❌"
                print(f"[{len(results):>3}/{len(cases)}] {mark} {result['id']}: "
                      f"{result.get('type')}/{result.get('tool') or '-'}")

    _write_report(output, cases, results, args.model)
    failed = [item for item in results if not item.get("passed")]
    print(f"완료: {len(results) - len(failed)}/{len(results)} 통과 | 결과: {output}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
