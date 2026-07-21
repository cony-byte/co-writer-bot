# -*- coding: utf-8 -*-
"""Offline tests for the Slack-free 100-case batch runner."""
from __future__ import annotations

import json
import os
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from bot import tool_registry, tool_router
from scripts import test_tool_router_batch as batch


def test_loads_exactly_one_hundred_json_cases():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "cases.json"
        path.write_text(json.dumps({"cases": [
            {"id": f"case-{n}", "text": f"문장 {n}"} for n in range(100)
        ]}, ensure_ascii=False), encoding="utf-8")
        cases = batch._load_cases(path)
    assert len(cases) == 100
    assert cases[0]["id"] == "case-0"
    assert cases[-1]["id"] == "case-99"


def test_evaluation_validates_but_never_executes_tool():
    executed = []
    name = "__batch_never_execute"
    tool_registry.TOOLS[name] = tool_registry.ToolSpec(
        name=name,
        description="batch safety test",
        parameters={"type": "object", "properties": {}, "required": [],
                    "additionalProperties": False},
        risk=tool_registry.HIGH,
        executor=lambda *_args: executed.append(True),
    )
    case = {
        "index": 0, "id": "safe", "text": "test",
        "context": dict(batch.BASE_CONTEXT),
        "expected": {"type": "tool_call", "tool": name,
                     "requires_confirmation": True},
    }
    decision = tool_router.Decision(type="tool_call", tool=name, arguments={})
    with patch("bot.tool_router.decide_from_context", return_value=decision):
        result = batch._evaluate(case, model=None, timeout=1, retries=0)
    assert result["passed"] is True
    assert result["requires_confirmation"] is True
    assert executed == []


def test_provider_failure_retries_then_succeeds():
    calls = []

    def decide(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("temporary")
        return tool_router.Decision(type="answer", text="ok")

    case = {
        "index": 0, "id": "retry", "text": "test",
        "context": dict(batch.BASE_CONTEXT), "expected": {"type": "answer"},
    }
    with patch("bot.tool_router.decide_from_context", side_effect=decide):
        result = batch._evaluate(case, model=None, timeout=1, retries=1)
    assert result["passed"] is True
    assert result["attempts"] == 2


def test_report_is_written_in_input_order():
    cases = [{"index": 0}, {"index": 1}]
    results = [
        {"index": 1, "passed": False},
        {"index": 0, "passed": True},
    ]
    with TemporaryDirectory() as directory:
        path = Path(directory) / "report.json"
        batch._write_report(path, cases, results, None)
        report = json.loads(path.read_text(encoding="utf-8"))
    assert [item["index"] for item in report["results"]] == [0, 1]
    assert report["summary"] == {"passed": 1, "failed": 1, "completed": 2}


def test_legacy_expectations_translate_to_real_tools():
    expected = batch._normalize_expected({
        "intent": "stillcut", "scene": 3, "cuts": [5, 13, 14],
        "forbid_episode": 3,
    })
    assert expected["type"] == "tool_call"
    assert expected["tool"] == "generate_stillcuts"
    assert expected["arguments"] == {"scene": 3, "cuts": [5, 13, 14]}
    assert expected["forbid_episode"] == 3


def test_natural_confirmation_legacy_case_becomes_clarification():
    expected = batch._normalize_expected({"intent": "confirm_previous"})
    assert expected["type_oneof"] == ["answer", "clarification"]


def test_env_loader_strips_unquoted_inline_comments():
    with TemporaryDirectory() as directory:
        path = Path(directory) / ".env"
        path.write_text(
            "BATCH_TEST_NUMBER=600   # seconds\n"
            "BATCH_TEST_HASH='value#inside'\n",
            encoding="utf-8",
        )
        os.environ.pop("BATCH_TEST_NUMBER", None)
        os.environ.pop("BATCH_TEST_HASH", None)
        batch._load_env(path)
        assert os.environ.pop("BATCH_TEST_NUMBER") == "600"
        assert os.environ.pop("BATCH_TEST_HASH") == "value#inside"


def test_answer_does_not_require_tool_arguments_from_legacy_context():
    expected = batch._normalize_expected({
        "intent": "answer_question", "work_canon": "정식 작품", "episode": 1,
    })
    errors = batch._check_expected(expected, {
        "type": "answer", "tool": None, "arguments": None,
        "requires_confirmation": False,
    })
    assert errors == []


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} batch CLI tests passed")
