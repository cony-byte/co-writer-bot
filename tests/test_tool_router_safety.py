# -*- coding: utf-8 -*-
"""Offline deterministic tests for confirmation and execution safety.

No Slack or LLM request is made.  A tiny fake slack_io module is installed before
tool_router import so button actions can be exercised as ordinary functions.
"""
from __future__ import annotations

import json
import sys
import types


class _Client:
    def __init__(self):
        self.posts = []
        self.updates = []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ts": "M1"}

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {}


class _App:
    def __init__(self):
        self.client = _Client()
        self.actions = {}

    def action(self, action_id):
        def decorator(fn):
            self.actions[action_id] = fn
            return fn
        return decorator


_replies = []
_fake_app = _App()
_fake_slack = types.ModuleType("bot.shared.slack_io")
_fake_slack.app = _fake_app
_fake_slack.log = types.SimpleNamespace(exception=lambda *_a, **_k: None)
_fake_slack._reply = lambda channel, thread_ts, text: _replies.append(
    {"channel": channel, "thread_ts": thread_ts, "text": text}
)
sys.modules["bot.shared.slack_io"] = _fake_slack

from bot import tool_registry  # noqa: E402
from bot.pending_manager import PendingManager  # noqa: E402
from bot import tool_router  # noqa: E402
from bot import tool_router_slack as tool_runtime  # noqa: E402


def _reset():
    _fake_app.client.posts.clear()
    _fake_app.client.updates.clear()
    _replies.clear()
    tool_runtime._PENDING = PendingManager(ttl_seconds=900)


def _body(pending_id: str) -> dict:
    return {
        "actions": [{"value": pending_id}],
        "channel": {"id": "C1"},
        "message": {"ts": "M1", "thread_ts": "T1"},
    }


def _decision(name: str, args=None):
    return tool_router.Decision(
        type="tool_call", tool=name, arguments=args or {},
        raw={"context": {"registered_works": {}}},
    )


def _install_test_tool(name: str, risk: str, calls: list):
    spec = tool_registry.ToolSpec(
        name=name,
        description="테스트 작업을 수행한다.",
        parameters={"type": "object", "properties": {}, "required": [],
                    "additionalProperties": False},
        risk=risk,
        executor=lambda args, ctx: calls.append((args, ctx.thread_ts)),
    )
    tool_registry.TOOLS[name] = spec
    return spec


def test_parser_accepts_native_calls_and_ordered_compound_plan():
    answer = tool_router._parse_message({"tool_calls": [{"function": {
        "name": "respond_with_answer", "arguments": json.dumps({"text": "답"})}}]})
    assert answer.type == "answer" and answer.text == "답"

    call = tool_router._parse_message({"tool_calls": [{"function": {
        "name": "cancel_current_job", "arguments": "{}"}}]})
    assert call.type == "tool_call" and call.tool == "cancel_current_job"

    compound = tool_router._parse_message({"tool_calls": [
        {"function": {"name": "cancel_current_job", "arguments": "{}"}},
        {"function": {"name": "resume_interrupted_job", "arguments": "{}"}},
    ]})
    assert compound.type == "tool_calls"
    assert [item["tool"] for item in compound.calls] == [
        "cancel_current_job", "resume_interrupted_job"
    ]

    for bad in ({}, {"tool_calls": [
        {"function": {"name": "respond_with_answer", "arguments": '{"text":"답"}'}},
        {"function": {"name": "cancel_current_job", "arguments": "{}"}},
    ]}):
        try:
            tool_router._parse_message(bad)
            raise AssertionError("invalid tool-call count was accepted")
        except ValueError:
            pass

    try:
        tool_router._parse_message({"tool_calls": [{"function": {
            "name": "respond_with_answer",
            "arguments": json.dumps({"text": "답", "unexpected": True}),
        }}]})
        raise AssertionError("extra answer arguments were accepted")
    except ValueError:
        pass


def test_unknown_tool_never_executes():
    _reset()
    assert tool_runtime.execute("C1", "T1", {}, _decision("not_allowed"))
    assert "지원하지 않는" in _replies[-1]["text"]


def test_invalid_high_risk_call_never_gets_confirmation_card():
    _reset()
    decision = _decision("reset_episode_outputs", {"work": "작품"})
    tool_runtime.execute("C1", "T1", {}, decision)
    assert not _fake_app.client.posts
    assert _replies and "episode" in _replies[-1]["text"]


def test_low_risk_also_requires_exact_button():
    _reset()
    calls = []
    _install_test_tool("__test_low", tool_registry.LOW, calls)
    tool_runtime.execute("C1", "T1", {}, _decision("__test_low"))
    assert calls == []
    pending_id = _fake_app.client.posts[-1]["blocks"][1]["elements"][0]["value"]
    tool_runtime.confirm_tool_call(lambda: None, _body(pending_id))
    assert len(calls) == 1


def test_high_risk_waits_for_exact_button_and_executes_once():
    _reset()
    calls = []
    _install_test_tool("__test_high", tool_registry.HIGH, calls)
    tool_runtime.execute("C1", "T1", {}, _decision("__test_high"))
    assert calls == []
    post = _fake_app.client.posts[-1]
    buttons = post["blocks"][1]["elements"]
    assert [button["action_id"] for button in buttons] == [
        "confirm_tool_call", "cancel_tool_call", "edit_tool_call"
    ]
    pending_id = buttons[0]["value"]

    acked = []
    tool_runtime.confirm_tool_call(lambda: acked.append(True), _body("wrong-id"))
    assert calls == []
    current = tool_runtime._PENDING.peek("T1", tool_runtime._PENDING_KIND)
    assert current and current.status == "waiting"

    tool_runtime.confirm_tool_call(lambda: acked.append(True), _body(pending_id))
    assert len(calls) == 1
    tool_runtime.confirm_tool_call(lambda: acked.append(True), _body(pending_id))
    assert len(calls) == 1
    assert len(acked) == 3


def test_compound_plan_is_fully_validated_then_executes_in_order():
    _reset()
    calls = []
    _install_test_tool("__step_one", tool_registry.LOW, calls)
    _install_test_tool("__step_two", tool_registry.HIGH, calls)
    decision = tool_router.Decision(type="tool_calls", calls=[
        {"tool": "__step_one", "arguments": {}},
        {"tool": "__step_two", "arguments": {}},
    ], raw={"context": {}})
    tool_runtime.execute("C1", "T1", {}, decision)
    assert calls == []
    pending_id = _fake_app.client.posts[-1]["blocks"][1]["elements"][0]["value"]
    tool_runtime.confirm_tool_call(lambda: None, _body(pending_id))
    assert [thread for _args, thread in calls] == ["T1", "T1"]


def test_cancel_and_edit_buttons_make_call_unexecutable():
    for action in (tool_runtime.cancel_tool_call, tool_runtime.edit_tool_call):
        _reset()
        calls = []
        _install_test_tool("__test_abort", tool_registry.HIGH, calls)
        tool_runtime.execute("C1", "T1", {}, _decision("__test_abort"))
        pending_id = _fake_app.client.posts[-1]["blocks"][1]["elements"][0]["value"]
        action(lambda: None, _body(pending_id))
        tool_runtime.confirm_tool_call(lambda: None, _body(pending_id))
        assert calls == []


def test_expired_pending_cannot_be_consumed():
    now = [100.0]
    manager = PendingManager(ttl_seconds=10, now_fn=lambda: now[0])
    manager.create("T", "kind", {}, request_id="P")
    now[0] = 111.0
    assert manager.consume("T", "kind", request_id="P") is None
    assert manager.peek("T", "kind").status == "expired"


def test_reference_adapter_passes_only_selected_attachment():
    captured = {}
    fake_sb = types.ModuleType("bot.dispatch_storyboard")
    fake_sb._REF_TYPE_KW = {"인물": "person"}

    def typed_ref(channel, thread_ts, event, **kwargs):
        captured["files"] = event.get("files")
        return True

    fake_sb._do_typed_ref = typed_ref
    sys.modules["bot.dispatch_storyboard"] = fake_sb
    ctx = types.SimpleNamespace(
        channel="C", thread_ts="T",
        event={"files": [{"id": "F1"}, {"id": "F2"}]},
    )
    tool_registry._reference("register_reference_image", {
        "work": "작품", "kind": "인물", "name": "연우", "attachment_id": "F2",
    }, ctx)
    assert captured["files"] == [{"id": "F2"}]


def test_short_natural_ack_is_blocked_before_llm():
    original = tool_router.oi.tool_chat
    tool_router.oi.tool_chat = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("LLM must not be called")
    )
    try:
        for text in ("응", "네", "그래", "응 그렇게 해줘", "계속"):
            decision = tool_router.decide_from_context(text, {})
            assert decision.type == "clarification"
            assert decision.raw["blocked_short_ack"] is True
    finally:
        tool_router.oi.tool_chat = original


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} tool-router safety tests passed")
