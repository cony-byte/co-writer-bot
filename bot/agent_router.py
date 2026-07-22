# -*- coding: utf-8 -*-
"""구독 경로 전면 에이전트화 라우터 (Claude Agent SDK / 팀 로그인, 키 불필요).

OpenRouter 단발 분류기(tool_router.decide → 함수 하나 선택)를 대체해, 진짜 멀티턴
에이전트 루프를 돈다. 모델이 스스로 조회 도구로 상태를 확인하고 필요한 만큼 실행 도구를
여러 번 호출해 요청을 끝까지 수행한다.

- 도구 소스는 기존 tool_registry를 그대로 재사용한다(도구를 다시 쓰지 않는다). 각 registry
  ToolSpec을 SDK MCP 도구(@tool)로 감싸고, 도구 본문은 tool_router_slack과 동일한 규율로
  hydrate → validate → executor(args, ctx)를 호출한다. 사용자에게 보이는 실제 출력(진행 카드,
  이미지 업로드, 결과 게시)은 executor가 직접 Slack에 올린다 — 모델에는 짧은 상태 문자열만
  돌려준다.
- 시스템 프롬프트는 tool_router의 고정 규칙(_system_prompt_static)을 그대로 이어받고, 앞에
  멀티스텝 에이전트 지시 프리앰블을, 뒤에 스레드 상태 컨텍스트를 붙인다.
- 프롬프트 캐싱/cache_control은 여기서 절대 쓰지 않는다(OpenRouter 전용 레버 — 구독 경로는
  Claude Code가 내부적으로 캐싱한다).
- 어떤 예외도 dispatch를 죽이지 않는다: 로그 남기고 False 반환 → dispatch의 안전 정지로 폴백.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from . import config, tool_registry

log = logging.getLogger("agent-router")

_MCP_SERVER_NAME = "cowriter"


def _preamble() -> str:
    return (
        "너는 도구를 스스로 여러 번 호출해 사용자의 요청을 끝까지 수행하는 에이전트다. "
        "필요하면 조회 도구로 상태를 먼저 확인하고(예: 등록 현황, 제작 진행상황), 그 결과로 "
        "실제 작업 도구를 필요한 횟수만큼 호출해라. 라벨 하나만 고르는 게 아니다. "
        "요청을 다 처리했으면 도구 호출을 멈추고 짧게 마무리한다. "
        "아래 규칙은 그대로 지킨다(특히 상태/존재 질문은 근거 자료로만 답하고 임의 생성 금지, "
        "명시적 부정 제약을 조용히 폴백하지 않기)."
    )


def _build_context(channel: str, thread_ts: str, query_text: str, event: dict) -> dict:
    """tool_router.decide와 동일한 컨텍스트 수집/보강 재사용."""
    from . import nl_router, tool_router
    from . import dispatch_storyboard as sb

    context = nl_router._build_context(channel, thread_ts, event, query_text=query_text)
    context["interrupted_job"] = sb.interrupted_state.get(thread_ts)
    context["attachments"] = [
        {"id": f.get("id"), "name": f.get("name"), "mimetype": f.get("mimetype")}
        for f in (event.get("files") or [])
    ]
    context = tool_router._resolved_context(context)
    context["_user_query"] = query_text
    return context


def _system_prompt(context: dict) -> str:
    from . import tool_router
    return (
        f"{_preamble()}\n\n"
        f"{tool_router._system_prompt_static()}\n\n"
        f"현재 스레드 상태와 근거:\n{json.dumps(context, ensure_ascii=False, indent=1)}"
    )


def _make_tool(spec: tool_registry.ToolSpec, ctx, context: dict, executed: list[str]):
    """registry ToolSpec 하나를 SDK MCP 도구로 감싼다.

    executor는 blocking Slack/네트워크 I/O를 직접 하므로 이벤트 루프를 막지 않도록
    asyncio.to_thread로 돌린다. 모델에는 짧은 상태 텍스트만 반환한다.
    """
    from claude_agent_sdk import tool

    @tool(spec.name, spec.description, spec.parameters)
    async def _impl(args: dict) -> dict:
        def _work() -> str:
            hydrated = tool_registry.hydrate_arguments(spec, args or {}, context)
            error = tool_registry.validate_call(spec, hydrated, ctx)
            if error:
                return error  # 검증 실패 메시지를 그대로 모델에 돌려줘 다음 판단에 쓰게 함
            spec.executor(hydrated, ctx)
            executed.append(spec.name)
            return f"done: {spec.name}"

        try:
            text = await asyncio.to_thread(_work)
        except Exception as exc:  # 개별 도구 실패는 루프를 죽이지 않는다
            log.exception("agent tool 실행 실패: %s", spec.name)
            return {"content": [{"type": "text", "text": f"error: {spec.name}: {exc}"}],
                    "is_error": True}
        return {"content": [{"type": "text", "text": text}]}

    return _impl


def _record(rec, executed: list[str], final_text: str, elapsed_ms: int) -> None:
    """결정 로그(router_log.DecisionRecord)에 agent 경로 실행 내역을 채운다 — 자기성장형 평가
    파이프라인이 현재 라이브(agent) 백엔드도 리플레이·라벨링·지표에 쓸 수 있게(★2026-07-22).
    로깅이 라우팅을 절대 안 깨도록 전부 방어."""
    if rec is None:
        return
    try:
        rec.executed_handler = ",".join(executed) or ("answer" if final_text else None)
        rec.route = {
            "intent": "agent",
            "tools": list(executed),
            "tool": (executed[0] if executed else None),
            "slots": {},
            "backend": (config.AGENT_ROUTER_MODEL or "agent"),
            "latency_ms": elapsed_ms,
        }
    except Exception:
        log.exception("agent 결정 로그 기록 실패(무시)")


def run(channel: str, thread_ts: str, query_text: str, event: dict, rec=None) -> bool:
    """dispatch가 호출하는 진입점. 처리했으면(무언가 게시했으면) True, 아니면 False.
    rec: router_log 결정 레코드(있으면 실행 도구·지연을 채운다 — 평가 파이프라인용)."""
    _t0 = time.monotonic()
    try:
        from claude_agent_sdk import (
            AssistantMessage, ClaudeAgentOptions, TextBlock,
            create_sdk_mcp_server, query,
        )
        from .tool_router_slack import ExecutionContext

        context = _build_context(channel, thread_ts, query_text, event)
        ctx = ExecutionContext(channel, thread_ts, event, context)
        executed: list[str] = []

        specs = tool_registry.all_specs()
        tools = [_make_tool(spec, ctx, context, executed) for spec in specs]
        server = create_sdk_mcp_server(name=_MCP_SERVER_NAME, tools=tools)
        allowed_tools = [f"mcp__{_MCP_SERVER_NAME}__{spec.name}" for spec in specs]
        log.info("agent_router allowed_tools=%s", allowed_tools)

        options = ClaudeAgentOptions(
            system_prompt=_system_prompt(context),
            model=config.AGENT_ROUTER_MODEL or None,
            max_turns=config.AGENT_ROUTER_MAX_TURNS,
            mcp_servers={_MCP_SERVER_NAME: server},
            allowed_tools=allowed_tools,
            permission_mode="bypassPermissions",
        )

        async def _run() -> list[str]:
            texts: list[str] = []
            async for message in query(prompt=query_text, options=options):
                if isinstance(message, AssistantMessage):
                    texts += [b.text for b in message.content if isinstance(b, TextBlock)]
            return texts

        async def _guarded() -> list[str]:
            return await asyncio.wait_for(_run(), timeout=config.AGENT_ROUTER_TIMEOUT)

        texts = asyncio.run(_guarded())

        # 도구가 사용자에게 보이는 출력을 직접 냈으므로, 아무 도구도 안 돌았을 때만
        # (= 순수 질문/대화 응답) 에이전트의 최종 텍스트를 스레드에 올린다.
        final_text = tool_registry.sanitize_user_text("".join(texts).strip())
        _elapsed_ms = int((time.monotonic() - _t0) * 1000)
        _record(rec, executed, final_text, _elapsed_ms)
        if not executed and final_text:
            from .shared.slack_io import _reply
            _reply(channel, thread_ts, final_text)
            return True
        if executed:
            return True
        # 도구도 텍스트도 없음 → 처리 못 함, dispatch 안전 정지로.
        log.info("agent_router: 도구/텍스트 모두 없음 → 미처리")
        return False
    except Exception:
        log.exception("agent_router 실패 → 안전 정지(False)")
        return False
