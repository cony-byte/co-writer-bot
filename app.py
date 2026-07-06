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
# 입력 모드 명령: [입력]/[저장]/[바이블] → 이하 내용을 시트에 저장(생성 안 함)
INPUT_CMD_RE = re.compile(r"\[\s*(입력|저장|바이블|input)\s*\]")
# 변환 모드 명령: [변환]/[포맷] → 줄글 초안을 촬영대본 포맷으로 재편(창작 아님)
CONVERT_CMD_RE = re.compile(r"\[\s*(변환|포맷|대본변환)\s*\]")
# 입력 블록 안의 "항목: 내용" 줄
FIELD_RE = re.compile(r"^\s*([^:：\n]+?)\s*[:：]\s*(.+)$", re.S)
# 항목 키 별칭 → 시트 kind
FIELD_ALIAS = {
    "로그라인": "로그라인", "키워드": "로그라인",
    "타겟": "타겟정서", "타겟정서": "타겟정서", "핵심정서": "타겟정서", "타겟층": "타겟정서",
    "인물": "인물", "등장인물": "인물", "캐릭터": "인물",
    "줄거리": "줄거리", "시놉시스": "줄거리",
    "회차표": "회차표", "회차분배": "회차표",
    "현재": "현재화", "현재화": "현재화", "진행": "현재화", "진행상태": "현재화",
}


def _norm_field(key: str) -> str | None:
    """항목명 → 시트 kind. 'N화 대본'/'N화 개요'도 인식. 모르면 None."""
    k = key.strip().replace(" ", "")
    if k in FIELD_ALIAS:
        return FIELD_ALIAS[k]
    m = re.match(r"(\d+)화_?(개요|대본)$", k)
    if m:
        return f"{m.group(1)}화_{m.group(2)}"
    return None


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


def _post_code(channel: str, thread_ts: str, text: str) -> None:
    """정렬 유지가 필요한 촬영대본은 코드블록(monospace)으로. 줄 경계로 분할."""
    limit, buf = 3600, ""
    chunks = []
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    for c in chunks:
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"```\n{c}\n```")


_INPUT_HELP = (
    "입력 형식이에요 👇 (첫 줄=작품명, 다음 줄부터 `항목: 내용`)\n"
    "```\n[입력] 날 혐오하는 남편\n로그라인: 정략결혼한 여주가 남편을 살리고 도망친다\n"
    "인물: 연우(여주) 1~38화 활동 / 태식(남주) 38화까지 냉대\n줄거리: 결혼지옥 → 이탈 → 후회\n현재: 24화\n```\n"
    "항목: 로그라인·타겟·인물·줄거리·회차표·현재·N화개요·N화대본"
)


def _handle_convert(channel: str, thread_ts: str, query: str) -> None:
    """[변환]: 초안(명령 뒤 본문, 없으면 스레드 직전 봇 대본)을 촬영대본 포맷으로."""
    from bot import script_format
    raw = CONVERT_CMD_RE.sub("", query).strip()
    epm = re.search(r"(\d+)\s*화", raw)
    ep = epm.group(1) if epm else ""
    draft = raw
    if len(draft) < 30:  # 초안을 안 붙였으면 스레드 직전 봇 대본을 초안으로
        prior = [m["content"] for m in _thread_messages(channel, thread_ts)
                 if m["role"] == "assistant"]
        draft = prior[-1] if prior else ""
    if not draft:
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="변환할 초안을 `[변환]` 뒤에 붙여주세요. 대본이 있는 스레드에서는 `[변환]`만 보내도 직전 대본을 변환합니다.",
        )
        return
    try:
        result = script_format.convert_script(draft, generator.complete, episode_label=ep)
        _post_code(channel, thread_ts, result)
    except Exception:
        log.exception("script convert failed")
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="변환 중 오류가 났어요 (초안 구조를 못 읽었을 수 있어요). 다시 시도해 주세요.",
        )


def _handle_input(channel: str, thread_ts: str, sheet, query: str) -> None:
    """[입력] 블록 파싱 → 항목별 시트 upsert. 여러 줄 값 지원."""
    raw = INPUT_CMD_RE.sub("", query).strip()
    lines = [ln.rstrip() for ln in raw.splitlines() if ln.strip()]
    if not lines or FIELD_RE.match(lines[0]):
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=_INPUT_HELP)
        return
    work = lines[0].strip()

    # 항목 블록 조립: 알려진 '키:' 로 시작하면 새 항목, 아니면 이전 항목 값에 이어붙임
    items: list[list[str]] = []
    for ln in lines[1:]:
        m = FIELD_RE.match(ln)
        if m and _norm_field(m.group(1)):
            items.append([m.group(1), m.group(2)])
        elif items:
            items[-1][1] += "\n" + ln

    saved, skipped = [], []
    for key, val in items:
        kind = _norm_field(key)
        val = val.strip()
        if kind == "현재화":
            d = re.search(r"\d+", val)
            val = d.group() if d else val
        try:
            sheet.upsert(work, kind, val)
            saved.append(kind)
        except Exception:
            log.exception("input upsert failed")
            skipped.append(kind)
    sheet.invalidate(work)

    if not saved and not skipped:
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=_INPUT_HELP)
        return
    msg = f"✅ *{work}* 저장: {', '.join(saved) or '없음'}"
    if skipped:
        msg += f"\n⚠️ 실패: {', '.join(skipped)}"
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

    # 변환 모드: [변환] 줄글 초안 → 촬영대본 포맷 (창작 아님, 형식 재편)
    if CONVERT_CMD_RE.search(query):
        _handle_convert(channel, thread_ts, query)
        return

    # 입력 모드: [입력] 작품명 + 항목:내용 → 생성 없이 시트에 저장
    if INPUT_CMD_RE.search(query):
        if not sheet:
            app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text="시트가 아직 연결 안 됐어요 (SHEET_WEBAPP_URL 미설정).",
            )
            return
        _handle_input(channel, thread_ts, sheet, query)
        return

    # 생성용: 작품 지정([작품명])·대상 회차(N화) 파싱
    wm = WORK_RE.search(query)
    work = wm.group(1).strip() if wm else None
    body = WORK_RE.sub("", query).strip() if wm else query
    ep = EPISODE_RE.search(body)
    target = int(ep.group(1)) if ep else None

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
    log.info("app_mention 수신: ch=%s text=%r", event.get("channel"), (event.get("text") or "")[:60])
    _handle(event)


@app.event("message")
def on_message(event, ack):
    ack()
    log.info("message 수신: type=%s ch=%s bot=%s sub=%s",
             event.get("channel_type"), event.get("channel"),
             event.get("bot_id"), event.get("subtype"))
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
