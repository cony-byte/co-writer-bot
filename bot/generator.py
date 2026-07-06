# -*- coding: utf-8 -*-
"""생성 백엔드 2종 (COWRITER_BACKEND로 선택):

- agent (기본): Claude Agent SDK — 이 머신의 Claude Code 로그인(팀 구독)을 그대로 사용.
  API 키·ant 로그인 전부 불필요. `claude` CLI가 설치·로그인돼 있으면 끝.
- api: Anthropic SDK 직접 호출 — ANTHROPIC_API_KEY 또는 `ant auth login` 프로필 필요.
"""
import asyncio
import shutil

from . import config, prompts

REFUSAL_MSG = "요청을 처리할 수 없었어요. 표현을 바꿔 다시 시도해 주세요."
TRUNCATED_MSG = "\n\n_(출력 한도로 잘렸어요 — \"이어서\"라고 답장하면 계속 씁니다)_"


# ── agent 백엔드 (Claude Code 구독 재사용) ──────────────────────────────

def _flatten_thread(thread_messages: list[dict]) -> str:
    """스레드 히스토리를 단일 프롬프트로 변환 (마지막 user 메시지가 이번 요청)."""
    lines = []
    for m in thread_messages[:-1]:
        who = "작가" if m["role"] == "user" else "너(보조 작가)"
        lines.append(f"[{who}]\n{m['content']}")
    history = "\n\n".join(lines)
    current = thread_messages[-1]["content"]
    if history:
        return f"지금까지의 대화:\n\n{history}\n\n[작가의 새 요청]\n{current}"
    return current


async def _agent_generate(system_text: str, prompt: str) -> str:
    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, TextBlock, query,
    )

    options = ClaudeAgentOptions(
        system_prompt=system_text,
        model=config.AGENT_MODEL or None,   # None = Claude Code 기본 모델
        max_turns=1,
        allowed_tools=[],                   # 순수 텍스트 생성 — 도구 사용 없음
    )
    out: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            out += [b.text for b in message.content if isinstance(b, TextBlock)]
    return "".join(out).strip()


# ── api 백엔드 (Anthropic SDK) ─────────────────────────────────────────

def _api_generate(thread_messages: list[dict], system_blocks: list[dict]) -> str:
    import anthropic
    client = anthropic.Anthropic()
    with client.messages.stream(
        model=config.MODEL,
        max_tokens=config.MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=system_blocks,
        messages=thread_messages,
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        return REFUSAL_MSG
    text = "".join(b.text for b in message.content if b.type == "text")
    if message.stop_reason == "max_tokens":
        text += TRUNCATED_MSG
    return text or "(빈 응답)"


# ── 공통 진입점 ─────────────────────────────────────────────────────────

def generate(thread_messages: list[dict], query_text: str,
             bible: dict | None = None, target_episode: int | None = None) -> str:
    blocks = prompts.system_blocks(query_text, bible=bible, target_episode=target_episode)
    if config.BACKEND == "api":
        return _api_generate(thread_messages, blocks)
    system_text = "\n\n".join(b["text"] for b in blocks)
    text = asyncio.run(_agent_generate(system_text, _flatten_thread(thread_messages)))
    return text or "(빈 응답)"


def healthcheck() -> None:
    """기동 시 자격증명/환경 fail-fast."""
    if config.BACKEND == "api":
        import anthropic
        try:
            anthropic.Anthropic().messages.count_tokens(
                model=config.MODEL, messages=[{"role": "user", "content": "ping"}]
            )
        except anthropic.AuthenticationError:
            raise SystemExit(
                "Anthropic 자격증명이 없거나 만료됨. ANTHROPIC_API_KEY를 설정하거나 "
                "`ant auth login` 후 재실행 (빈 API 키는 프로필을 가리므로 unset)."
            )
    else:
        if not shutil.which("claude"):
            raise SystemExit(
                "`claude` CLI를 찾을 수 없음. agent 백엔드는 Claude Code 설치·로그인이 필요함.\n"
                "설치 후 `claude` 한 번 실행해 팀 계정으로 로그인하거나, "
                "COWRITER_BACKEND=api 로 전환."
            )
