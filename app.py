#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""co-writer-bot — 숏폼 드라마 보조 작가 슬랙 에이전트.

사용법 (슬랙):
  채널에서  @co-writer 기획안: 재벌 남주 x 계약결혼, 오피스   → 기획안
  스레드에서 @co-writer 3화 절단점을 정체 폭로 직전으로 바꿔줘  → 수정본
  스레드에서 @co-writer 대본: 1화                            → 대본
  DM으로도 동일하게 동작 (멘션 불필요)

실행:
  python3 app.py   (Socket Mode — 공개 URL 불필요)
"""
import logging
import re

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from bot import config, generator, reference

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("co-writer")

app = App(token=config.SLACK_BOT_TOKEN)
BOT_USER_ID = app.client.auth_test()["user_id"]

MENTION_RE = re.compile(rf"<@{BOT_USER_ID}>\s*")


def _clean(text: str) -> str:
    return MENTION_RE.sub("", text or "").strip()


def _thread_messages(channel: str, thread_ts: str) -> list[dict]:
    """스레드 전체를 슬랙에서 다시 읽어 모델 메시지로 변환 (무상태 — 재시작에 안전)."""
    resp = app.client.conversations_replies(
        channel=channel, ts=thread_ts, limit=config.THREAD_HISTORY_LIMIT
    )
    messages: list[dict] = []
    for m in resp.get("messages", []):
        text = _clean(m.get("text", ""))
        if not text:
            continue
        role = "assistant" if m.get("user") == BOT_USER_ID or m.get("bot_id") else "user"
        # 연속 같은 role은 API가 한 턴으로 합쳐주므로 그대로 쌓는다
        messages.append({"role": role, "content": text})
    # 첫 메시지는 user여야 함
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


def _post_chunks(channel: str, thread_ts: str, text: str) -> None:
    """슬랙 메시지 길이 제한(4000자) 대응 — 문단 경계로 분할 전송."""
    chunk, chunks = "", []
    for para in text.split("\n\n"):
        if len(chunk) + len(para) + 2 > 3800:
            chunks.append(chunk)
            chunk = para
        else:
            chunk = f"{chunk}\n\n{para}" if chunk else para
    if chunk:
        chunks.append(chunk)
    for c in chunks:
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=c)


def _handle(event: dict) -> None:
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    query = _clean(event.get("text", ""))

    if query in ("reload", "리로드"):
        reference.reload()
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="레퍼런스 DB·템플릿을 다시 불러왔어요.",
        )
        return

    messages = _thread_messages(channel, thread_ts)
    if not messages:
        return

    try:
        answer = generator.generate(messages, query)
    except Exception:
        log.exception("generation failed")
        answer = "생성 중 오류가 났어요. 잠시 후 다시 시도해 주세요."
    _post_chunks(channel, thread_ts, answer)


@app.event("app_mention")
def on_mention(event, ack):
    ack()
    _handle(event)


@app.event("message")
def on_message(event, ack):
    ack()
    # DM만 처리 (채널 일반 메시지는 멘션으로만 반응). 봇 자신·수정 이벤트 무시.
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id") or event.get("subtype"):
        return
    _handle(event)


if __name__ == "__main__":
    generator.healthcheck()  # Anthropic 자격증명 확인 (API 키 또는 ant auth login 프로필)
    log.info("co-writer-bot 시작 (model=%s, reference=%s)", config.MODEL, config.REFERENCE_DIR)
    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()
