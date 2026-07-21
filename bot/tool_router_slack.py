# -*- coding: utf-8 -*-
"""Slack execution adapter for decisions produced by :mod:`bot.tool_router`."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from . import tool_registry
from .pending_manager import PendingManager, WAITING
from .shared.slack_io import app, log, _reply
from .tool_router import Decision


@dataclass
class ExecutionContext:
    channel: str
    thread_ts: str
    event: dict
    context: dict | None = None


_RUNNING = PendingManager(ttl_seconds=3600)
_RUNNING_KIND = "running_tool_call"


def _progress_text(spec: tool_registry.ToolSpec, args: dict) -> str:
    scope = []
    if args.get("work"):
        scope.append(str(args["work"]))
    if args.get("episode") is not None:
        scope.append(f"{args['episode']}화")
    if args.get("scene") is not None:
        scope.append(f"씬{args['scene']}")
    if args.get("cuts"):
        scope.append("컷 " + ", ".join(str(n) for n in args["cuts"]))
    if args.get("attachment_id"):
        scope.append("첨부 이미지 사용")
    target = " · ".join(scope)
    instruction = str(args.get("instruction") or "").strip()
    lines = []
    if target:
        lines.append(f"*{target}*")
    lines.append(f"진행할 작업: *{spec.user_label or '요청한 작업 진행하기'}*")
    if instruction:
        safe_instruction = tool_registry.sanitize_user_text(instruction[:500]).replace("\n", "\n> ")
        lines.extend(["", "*반영할 내용*", f"> {safe_instruction}"])
    return "\n".join(lines)


def _run_immediately(ctx: ExecutionContext, calls: list[dict]) -> None:
    run_id = uuid.uuid4().hex
    _RUNNING.create(
        ctx.thread_ts, _RUNNING_KIND,
        {"calls": calls, "event": ctx.event, "channel": ctx.channel,
         "thread_ts": ctx.thread_ts, "context": ctx.context},
        request_id=run_id,
    )
    summaries = []
    for index, item in enumerate(calls, 1):
        spec = tool_registry.get(item["tool"])
        detail = _progress_text(spec, item["arguments"])
        summaries.append(f"*작업 {index}*\n{detail}" if len(calls) > 1 else detail)
    intro = (f"요청한 작업 {len(calls)}개를 순서대로 시작했어요."
             if len(calls) > 1 else "요청한 작업을 시작했어요.")
    text = f"{intro}\n\n" + "\n\n".join(summaries)
    text += "\n\n필요하면 아래 버튼으로 중단할 수 있어요."
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": "stop_running_tool_call",
             "style": "danger", "text": {"type": "plain_text", "text": "중단"},
             "value": run_id},
        ]},
    ]
    try:
        response = app.client.chat_postMessage(
            channel=ctx.channel, thread_ts=ctx.thread_ts, text=text, blocks=blocks,
        )
        message_ts = str((response or {}).get("ts") or "")
        for item in calls:
            spec = tool_registry.get(item["tool"])
            spec.executor(item["arguments"], ctx)
        record = _RUNNING.peek(ctx.thread_ts, _RUNNING_KIND)
        if record and record.request_id == run_id and record.status == WAITING:
            _RUNNING.consume(ctx.thread_ts, _RUNNING_KIND, request_id=run_id)
            _RUNNING.complete(ctx.thread_ts, _RUNNING_KIND)
            if message_ts:
                app.client.chat_update(
                    channel=ctx.channel, ts=message_ts,
                    text="✅ 요청한 작업을 마쳤어요.", blocks=[],
                )
    except Exception as exc:
        _RUNNING.fail(ctx.thread_ts, _RUNNING_KIND, str(exc))
        log.exception("immediate tool 실행 실패")
        _reply(ctx.channel, ctx.thread_ts,
               "작업을 진행하는 중 문제가 생겼어요. 같은 요청을 다시 보내주시거나 관리자에게 알려주세요.")


def execute(channel: str, thread_ts: str, event: dict, decision: Decision) -> bool:
    if decision.type in ("answer", "clarification"):
        text = tool_registry.sanitize_user_text((decision.text or "").strip())
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
    # Validation is still mandatory, but valid natural-language actions start
    # immediately. The exact run id on the stop button is the only runtime control.
    _run_immediately(ctx, calls)
    return True


def _action_context(body: dict) -> tuple[str, str, str, str]:
    action = (body.get("actions") or [{}])[0]
    channel = body.get("channel", {}).get("id") or body.get("container", {}).get("channel_id")
    message = body.get("message") or {}
    thread_ts = message.get("thread_ts") or message.get("ts")
    return channel, thread_ts, str(action.get("value") or ""), str(message.get("ts") or "")


@app.action("stop_running_tool_call")
def stop_running_tool_call(ack, body):
    ack()
    channel, thread_ts, run_id, message_ts = _action_context(body)
    current = _RUNNING.peek(thread_ts, _RUNNING_KIND)
    if current is None or current.request_id != run_id or current.status != WAITING:
        _reply(channel, thread_ts, "이 작업은 이미 끝났거나 중단할 수 없는 상태예요.")
        return
    record = _RUNNING.consume(thread_ts, _RUNNING_KIND, request_id=run_id)
    if record is None:
        _reply(channel, thread_ts, "이 작업은 이미 끝났거나 중단할 수 없는 상태예요.")
        return
    try:
        payload = record.payload
        cancel_spec = tool_registry.get("cancel_current_job")
        if cancel_spec is None:
            raise RuntimeError("cancel tool missing")
        ctx = ExecutionContext(channel, thread_ts, payload.get("event") or {},
                               payload.get("context") or {})
        cancel_spec.executor({}, ctx)
        _RUNNING.complete(thread_ts, _RUNNING_KIND)
        app.client.chat_update(
            channel=channel, ts=message_ts,
            text="🛑 중단을 요청했어요. 현재 처리 중인 단계가 정리되면 멈춰요.", blocks=[],
        )
    except Exception as exc:
        _RUNNING.fail(thread_ts, _RUNNING_KIND, str(exc))
        log.exception("running tool 중단 실패")
        _reply(channel, thread_ts,
               "중단 요청을 처리하지 못했어요. 잠시 후 다시 시도해 주세요.")
