# -*- coding: utf-8 -*-
"""Anthropic API 호출. 스트리밍 + adaptive thinking + 프롬프트 캐싱."""
import anthropic

from . import config, prompts

# 인자 없는 생성자 — 자격증명 자동 해석:
# ANTHROPIC_API_KEY → ANTHROPIC_AUTH_TOKEN → `ant auth login` OAuth 프로필(팀 플랜).
# 팀 클로드로 돌릴 때는 API 키를 아예 설정하지 않는다 (빈 문자열도 프로필을 가려버림).
_client = anthropic.Anthropic()


def healthcheck() -> str:
    """기동 시 자격증명 검증. 토큰 카운트는 과금 없는 엔드포인트."""
    try:
        _client.messages.count_tokens(
            model=config.MODEL,
            messages=[{"role": "user", "content": "ping"}],
        )
        return "ok"
    except anthropic.AuthenticationError:
        raise SystemExit(
            "Anthropic 자격증명이 없거나 만료됨.\n"
            "팀 클로드로 돌리는 경우: `ant auth login` 후 재실행 "
            "(ANTHROPIC_API_KEY는 설정하지 말 것 — 빈 값도 프로필보다 우선됨).\n"
            "토큰 갱신이 계속 실패하면 `ant auth login`을 다시 실행."
        )


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
