# -*- coding: utf-8 -*-
"""LLM 단발 호출 (씬설계·콘티·샷분해용). 백엔드 2종:
- agent (기본): Claude Agent SDK — 이 머신 Claude Code 로그인 재사용(키 불필요, `claude` CLI 필요)
- api: Anthropic SDK — ANTHROPIC_API_KEY 필요

MERGE NOTE (2026-07-16, Phase 3 final assembly): this is storyboard-bot's OWN
bot/generator.py, verbatim, kept under the `sb_` prefix because co-writer-bot's own
bot/generator.py already occupies that name in this merged bot/ package and has
INCOMPATIBLE content (no job_key/cancel()/cancel_prefix()/CANCEL_MSG support, which
storyboard's "멈춰"-mid-generation cancel feature depends on). dispatch_storyboard.py
imports this as `from bot import sb_generator as generator` so its many `generator.*`
call sites don't need touching. See HANDOFF_봇병합.md §3-5 risk #3 (the two LLM
backends must never be collapsed into one module) -- this module-naming collision was
one level deeper than HANDOFF had already flagged (it called out generator.py itself,
but not that bot/prompts.py and bot/video_guide.py have the exact same shared-name/
different-body problem -- see bot/sb_prompts.py and bot/sb_video_guide.py).
"""
import asyncio
import shutil
import threading

from . import config

TIMEOUT_MSG = "⏱️ 응답이 너무 오래 걸려 중단했어요. 잠시 후 다시 시도해 주세요 (입력이 길면 줄여보세요)."
CANCEL_MSG = "🛑 중단했어요."

# job_key(보통 thread_ts) → (loop, task). "멈춰"로 특정 스레드의 진행 중인 생성만 취소하기 위함.
_ACTIVE_LOCK = threading.Lock()
_ACTIVE: dict[str, tuple] = {}


def cancel(job_key: str) -> bool:
    """job_key로 등록된, 지금 돌고 있는 생성 호출을 취소. 반환: 실제로 취소 신호를 보냈는지."""
    with _ACTIVE_LOCK:
        entry = _ACTIVE.get(job_key)
    if not entry:
        return False
    loop, task = entry
    loop.call_soon_threadsafe(task.cancel)
    return True


def cancel_prefix(thread_ts: str) -> bool:
    """★2026-07-15: 자동주행 리뷰 지적 — 씬이 여러 개면 2단계(상세 콘티)가 씬마다
    job_key=f"{thread_ts}:씬{num}"로 따로 등록되는데(app.py의 병렬 콘티 생성 분기),
    cancel(thread_ts)는 정확히 그 키 하나만 찾아서 "멈춰"라고 해도 진행 중인 병렬 호출들이
    그대로 끝까지 돌았다. thread_ts 자신이거나 "thread_ts:"로 시작하는 모든 job_key를
    찾아 전부 취소한다. 반환: 하나라도 취소 신호를 보냈는지."""
    with _ACTIVE_LOCK:
        targets = [(k, v) for k, v in _ACTIVE.items()
                  if k == thread_ts or k.startswith(f"{thread_ts}:")]
    for _, (loop, task) in targets:
        loop.call_soon_threadsafe(task.cancel)
    return bool(targets)


async def _agent_generate(system_text: str, prompt: str, timeout: int | None = None,
                          model: str | None = None) -> str:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query
    options = ClaudeAgentOptions(
        system_prompt=system_text,
        model=model or config.AGENT_MODEL or None,
        max_turns=1,
        allowed_tools=[],
    )
    async def _run() -> str:
        out = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                out += [b.text for b in message.content if isinstance(b, TextBlock)]
        return "".join(out).strip()
    return await asyncio.wait_for(_run(), timeout=timeout or config.AGENT_TIMEOUT)


def complete(system_text: str, user_text: str, timeout: int | None = None, job_key: str | None = None,
             model: str | None = None) -> str:
    """(system, user) → text. 스토리보드 씬설계/콘티/샷분해용 단발 호출.
    job_key(보통 thread_ts)를 주면 cancel(job_key)로 이 호출만 중간에 끊을 수 있다.
    model: 이 호출만 다른 모델로(예: 합본/CapCut 편집 지시는 Sonnet 5). 안 주면 백엔드 기본."""
    if config.BACKEND == "api":
        import anthropic
        client = anthropic.Anthropic()
        with client.messages.stream(
            model=model or config.MODEL, max_tokens=config.MAX_TOKENS,
            system=system_text, messages=[{"role": "user", "content": user_text}],
        ) as stream:
            message = stream.get_final_message()
        return "".join(b.text for b in message.content if b.type == "text")
    if job_key is None:
        try:
            return asyncio.run(_agent_generate(system_text, user_text, timeout=timeout, model=model))
        except (asyncio.TimeoutError, TimeoutError):
            return TIMEOUT_MSG
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        task = loop.create_task(_agent_generate(system_text, user_text, timeout=timeout, model=model))
        with _ACTIVE_LOCK:
            _ACTIVE[job_key] = (loop, task)
        try:
            return loop.run_until_complete(task)
        except asyncio.CancelledError:
            return CANCEL_MSG
        except (asyncio.TimeoutError, TimeoutError):
            return TIMEOUT_MSG
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.pop(job_key, None)
        asyncio.set_event_loop(None)
        loop.close()


def healthcheck() -> None:
    """기동 시 자격증명/환경 fail-fast."""
    if config.BACKEND == "api":
        import anthropic
        try:
            anthropic.Anthropic().messages.count_tokens(
                model=config.MODEL, messages=[{"role": "user", "content": "ping"}])
        except anthropic.AuthenticationError:
            raise SystemExit("Anthropic 자격증명 없음/만료 — ANTHROPIC_API_KEY 설정하거나 SB_BACKEND=agent.")
    else:
        if not shutil.which("claude"):
            raise SystemExit("`claude` CLI 없음 — agent 백엔드는 Claude Code 설치·로그인 필요. 또는 SB_BACKEND=api.")
