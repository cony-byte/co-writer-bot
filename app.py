#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""co-writer-bot — 숏폼 드라마 보조 작가 슬랙 에이전트.

사용법 (슬랙):
  @co-writer 기획안: 재벌 남주 x 계약결혼, 오피스        → 기획안
  @co-writer 3화 절단점을 정체 폭로 직전으로 바꿔줘       → 수정본
  @co-writer [작품X] 24화 대본 써줘                      → 대본 + 시트에 저장(24화_대본)
  @co-writer [작품X] 인물: 연우는 1~38화 활동…            → 시트에 설정 저장(생성 안 함)
  @co-writer [작품X] 현재 24화                           → 진행상태 갱신
  @co-writer 요즘 트렌드                                 → 트렌드서치
  새로고침                                               → 시트 바이블 캐시 무효화
  DM으로도 동일하게 동작 (멘션 불필요)

바이블(구글 시트): [작품명]으로 작품 지정 시, 봇이 시트에서 그 작품 바이블을 읽어
PART D(시점 일관성) 규칙을 적용하고, 생성 결과를 시트에 되저장한다. 시트 미설정이면 바이블 없이 생성.

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
# 트렌드 질문 트리거 — 매칭되면 생성 대신 트렌드서치로 라우팅
TREND_RE = re.compile(r"트렌드|요즘|유행|인기|잘\s*나가|잘나가|순위|랭킹|톱\s*클립|톱클립")

# 바이블(구글 시트) 연동
WORK_RE = re.compile(r"\[([^\]]+)\]")          # [작품명] — 작품 지정
EPISODE_RE = re.compile(r"(\d+)\s*화")          # N화 — 대상 회차
CUR_RE = re.compile(r"현재\s*(\d+)\s*화")       # 현재 N화 — 진행상태 갱신
# 설정 명시 입력: "로그라인: …" 등 → 생성 없이 시트에 저장
SET_RE = re.compile(r"^\s*(로그라인|타겟정서|타겟|인물|줄거리|회차표)\s*[:：]\s*(.+)", re.S)
SET_KIND = {"로그라인": "로그라인", "타겟": "타겟정서", "타겟정서": "타겟정서",
            "인물": "인물", "줄거리": "줄거리", "회차표": "회차표"}


def _result_kind(query: str, target: int | None) -> str | None:
    """생성 결과를 시트 어느 구분(kind)에 저장할지. 없으면 저장 안 함(수정 등)."""
    if "대본" in query and target is not None:
        return f"{target}화_대본"
    if "개요" in query and target is not None:
        return f"{target}화_개요"
    if "기획안" in query:
        return "기획안"
    return None


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


def _sheet_write(channel: str, thread_ts: str, sheet, work: str, kind: str, content: str) -> None:
    """설정 명시 입력 → 시트 upsert + 확인 메시지."""
    try:
        sheet.upsert(work, kind, content)
        sheet.invalidate(work)
        msg = f"✅ 저장했어요 — *{work}* / {kind}"
    except Exception:
        log.exception("sheet write failed")
        msg = "⚠️ 시트 저장에 실패했어요. 잠시 후 다시 시도해 주세요."
    app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=msg)


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

    sheet = reference.sheet()  # 구글 시트 바이블 (미설정이면 None)

    # 시트 바이블 새로고침 (캐시 즉시 무효화)
    if query in ("새로고침", "refresh"):
        if sheet:
            sheet.invalidate()
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="시트 바이블 캐시를 비웠어요. 다음 요청부터 최신으로 읽어옵니다.",
        )
        return

    # 작품 지정([작품명])·대상 회차(N화) 파싱
    wm = WORK_RE.search(query)
    work = wm.group(1).strip() if wm else None
    body = WORK_RE.sub("", query).strip() if wm else query
    ep = EPISODE_RE.search(body)
    target = int(ep.group(1)) if ep else None

    # 설정 명시 입력 (생성 없이 시트에 저장) — 작품 지정 + 시트 필요
    if sheet and work:
        cur = CUR_RE.search(body)
        sm = SET_RE.match(body)
        if sm:
            kind, content = SET_KIND[sm.group(1)], sm.group(2).strip()
            _sheet_write(channel, thread_ts, sheet, work, kind, content)
            return
        if cur and len(body) <= 12:  # "현재 24화" 정도만 (긴 문장은 생성 요청)
            _sheet_write(channel, thread_ts, sheet, work, "현재화", cur.group(1))
            return

    # 트렌드 질문이면 생성 대신 트렌드서치 (v4 DB 성과 집계)
    if TREND_RE.search(query):
        trend = reference.load_trend()
        if trend is None:
            app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text="트렌드 DB가 아직 없어요. `sync_reference.py`로 데이터 반영 후 다시 물어봐 주세요.",
            )
            return
        try:
            _post_chunks(channel, thread_ts, trend.answer(query))
        except Exception:
            log.exception("trend search failed")
            app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text="트렌드 집계 중 오류가 났어요.",
            )
        return

    messages = _thread_messages(channel, thread_ts)
    if not messages:
        return

    # 작품 바이블 로드 (작품 지정 + 시트 설정 시) — PART D 실패방지 참조
    bible = None
    if sheet and work:
        try:
            bible = sheet.get(work)
        except Exception:
            log.exception("sheet bible load failed")  # 못 읽어도 생성은 계속

    try:
        answer = generator.generate(messages, body or query, bible=bible, target_episode=target)
    except Exception:
        log.exception("generation failed")
        answer = "생성 중 오류가 났어요. 잠시 후 다시 시도해 주세요."

    # 생성 결과를 시트에 저장 (작품 지정 + 저장 대상 kind일 때)
    kind = _result_kind(body, target) if (sheet and work) else None
    if kind:
        try:
            sheet.upsert(work, kind, answer)
            sheet.invalidate(work)  # 다음 참조 시 최신 반영
            answer += f"\n\n_📄 시트 저장: {work} / {kind}_"
        except Exception:
            log.exception("sheet upsert failed")
            answer += "\n\n_⚠️ 시트 저장 실패 (생성물은 위에 있어요)_"

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
