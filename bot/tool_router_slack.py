# -*- coding: utf-8 -*-
"""Slack execution adapter for decisions produced by :mod:`bot.tool_router`."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from . import tool_registry
from .pending_manager import PendingManager
from .shared.slack_io import app, log, _reply
from .tool_router import Decision


@dataclass
class ExecutionContext:
    channel: str
    thread_ts: str
    event: dict
    context: dict | None = None


_PENDING = PendingManager(ttl_seconds=900)
_PENDING_KIND = "tool_call_confirmation"


def _confirmation_text(spec: tool_registry.ToolSpec, args: dict) -> str:
    scope = []
    if args.get("work"):
        scope.append(f"<{args['work']}>")
    if args.get("episode") is not None:
        scope.append(f"{args['episode']}화")
    if args.get("scene") is not None:
        scope.append(f"씬{args['scene']}")
    if args.get("cuts"):
        scope.append("컷 " + ", ".join(str(n) for n in args["cuts"]))
    if args.get("attachment_id"):
        scope.append("첨부 이미지 참조")
    target = " ".join(scope)
    instruction = str(args.get("instruction") or "").strip()
    detail = f"\n요청: {instruction[:500]}" if instruction else ""
    return f"{target + '에서 ' if target else ''}*{spec.description}*{detail}\n실행할까요?"


def _post_confirmation(ctx: ExecutionContext, calls: list[dict]) -> None:
    pending_id = uuid.uuid4().hex
    _PENDING.create(ctx.thread_ts, _PENDING_KIND,
                    {"calls": calls, "event": ctx.event,
                     "channel": ctx.channel, "thread_ts": ctx.thread_ts,
                     "context": ctx.context},
                    request_id=pending_id)
    lines = []
    for index, item in enumerate(calls, 1):
        spec = tool_registry.get(item["tool"])
        detail = _confirmation_text(spec, item["arguments"]).removesuffix("\n실행할까요?")
        lines.append(f"{index}. {detail}" if len(calls) > 1 else detail)
    text = "\n".join(lines) + "\n실행할까요?"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": "confirm_tool_call", "style": "primary",
             "text": {"type": "plain_text", "text": "실행"}, "value": pending_id},
            {"type": "button", "action_id": "cancel_tool_call",
             "text": {"type": "plain_text", "text": "취소"}, "value": pending_id},
            {"type": "button", "action_id": "edit_tool_call",
             "text": {"type": "plain_text", "text": "수정"}, "value": pending_id},
        ]},
    ]
    app.client.chat_postMessage(channel=ctx.channel, thread_ts=ctx.thread_ts,
                                text=text, blocks=blocks)


def execute(channel: str, thread_ts: str, event: dict, decision: Decision) -> bool:
    if decision.type in ("answer", "clarification"):
        text = (decision.text or "").strip()
        _reply(channel, thread_ts, text or "조금 더 구체적으로 알려주세요.")
        return True
    if decision.type not in ("tool_call", "tool_calls"):
        return False
    model_context = (decision.raw or {}).get("context") if isinstance(decision.raw, dict) else None
    ctx = ExecutionContext(channel, thread_ts, event, model_context)
    calls = decision.calls or ([{"tool": decision.tool, "arguments": decision.arguments or {}}]
                               if decision.type == "tool_call" else [])
    if not calls:
        _reply(channel, thread_ts, "실행할 작업을 찾지 못했어요.")
        return True
    for item in calls:
        spec = tool_registry.get(item.get("tool") or "")
        if spec is None:
            _reply(channel, thread_ts, "지원하지 않는 작업이라 실행하지 않았어요.")
            return True
        item["arguments"] = tool_registry.hydrate_arguments(
            spec, item.get("arguments") or {}, model_context or {}
        )
        error = tool_registry.validate_call(spec, item["arguments"], ctx)
        if error:
            _reply(channel, thread_ts, error)
            return True
    # Every natural-language mutation, including formerly low-risk calls, is confirmed
    # by an exact Slack button payload. Natural-language approval is never sufficient.
    _post_confirmation(ctx, calls)
    return True


def _action_context(body: dict) -> tuple[str, str, str, str]:
    action = (body.get("actions") or [{}])[0]
    channel = body.get("channel", {}).get("id") or body.get("container", {}).get("channel_id")
    message = body.get("message") or {}
    thread_ts = message.get("thread_ts") or message.get("ts")
    return channel, thread_ts, str(action.get("value") or ""), str(message.get("ts") or "")


@app.action("confirm_tool_call")
def confirm_tool_call(ack, body):
    ack()
    channel, thread_ts, pending_id, message_ts = _action_context(body)
    current = _PENDING.peek(thread_ts, _PENDING_KIND)
    if current is None or current.request_id != pending_id:
        _reply(channel, thread_ts, "이 실행 요청은 만료됐거나 이미 처리됐어요.")
        return
    record = _PENDING.consume(thread_ts, _PENDING_KIND, request_id=pending_id)
    if record is None:
        _reply(channel, thread_ts, "이 실행 요청은 만료됐거나 이미 처리됐어요.")
        return
    try:
        payload = record.payload
        ctx = ExecutionContext(channel, thread_ts, payload.get("event") or {},
                               payload.get("context") or {})
        calls = payload.get("calls") or []
        if not calls:
            raise ValueError("실행 계획이 비어 있습니다")
        resolved = []
        for item in calls:
            spec = tool_registry.get(item.get("tool", ""))
            if spec is None:
                raise ValueError("허용 목록에서 사라진 tool입니다")
            args = tool_registry.hydrate_arguments(
                spec, item.get("arguments") or {}, payload.get("context") or {}
            )
            error = tool_registry.validate_call(spec, args, ctx)
            if error:
                raise ValueError(error)
            resolved.append((spec, args))
        app.client.chat_update(channel=channel, ts=message_ts,
                               text="✅ 실행을 확인했어요.", blocks=[])
        for spec, args in resolved:
            spec.executor(args, ctx)
        _PENDING.complete(thread_ts, _PENDING_KIND)
    except Exception as exc:
        _PENDING.fail(thread_ts, _PENDING_KIND, str(exc))
        log.exception("confirmed tool 실행 실패")
        _reply(channel, thread_ts, f"실행하지 못했어요: {exc}")


@app.action("cancel_tool_call")
def cancel_tool_call(ack, body):
    ack()
    channel, thread_ts, pending_id, message_ts = _action_context(body)
    record = _PENDING.peek(thread_ts, _PENDING_KIND)
    if record is None or record.request_id != pending_id:
        _reply(channel, thread_ts, "이 실행 요청은 만료됐거나 이미 처리됐어요.")
        return
    _PENDING.expire(thread_ts, _PENDING_KIND)
    app.client.chat_update(channel=channel, ts=message_ts, text="취소했어요.", blocks=[])


@app.action("edit_tool_call")
def edit_tool_call(ack, body):
    ack()
    channel, thread_ts, pending_id, message_ts = _action_context(body)
    record = _PENDING.peek(thread_ts, _PENDING_KIND)
    if record is None or record.request_id != pending_id:
        _reply(channel, thread_ts, "이 실행 요청은 만료됐거나 이미 처리됐어요.")
        return
    _PENDING.expire(thread_ts, _PENDING_KIND)
    app.client.chat_update(
        channel=channel, ts=message_ts,
        text="수정할 내용을 이 스레드에 구체적으로 적어주세요.", blocks=[])
