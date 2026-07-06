# -*- coding: utf-8 -*-
"""Anthropic API 호출. 스트리밍 + adaptive thinking + 프롬프트 캐싱."""
import anthropic

from . import config, prompts

_client = anthropic.Anthropic()


def generate(thread_messages: list[dict], query: str) -> str:
    """thread_messages: [{"role": "user"|"assistant", "content": str}, ...] (시간순).
    query: 유사 사례 선별에 쓸 이번 요청 텍스트."""
    with _client.messages.stream(
        model=config.MODEL,
        max_tokens=config.MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=prompts.system_blocks(query),
        messages=thread_messages,
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        return "요청을 처리할 수 없었어요. 표현을 바꿔 다시 시도해 주세요."

    text = "".join(b.text for b in message.content if b.type == "text")
    if message.stop_reason == "max_tokens":
        text += "\n\n_(출력 한도로 잘렸어요 — \"이어서\"라고 답장하면 계속 씁니다)_"
    return text or "(빈 응답)"
