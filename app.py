#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""co-writer-bot — 숏폼 드라마 보조 작가 슬랙 에이전트.

명령 = [명령] <작품> 경로 형식. 앞에 [명령]이 없으면 사용법 안내.

  [입력] <날혐남> 로그라인            → 신규 저장 (이미 있으면 경고)
  [수정] <날혐남> 로그라인            → 기존 값 고침 (없으면 경고)
  [입력] <날혐남> 인물 / 강태혁 / 설정  → 등장인물 소분류 저장
  [생성] <날혐남> 대본 / 24화         → 24화 개요+바이블 참고해 생성 + 시트 저장
  [변환] <줄글 초안…>                → 초안을 촬영대본 포맷으로 (창작 아님)
  [트렌드] 엔딩                      → 트렌드 조회
  [새로고침] / [리로드]              → 캐시 무효화

바이블 = 구글 시트(탭=작품). 대분류/중분류/소분류 계층으로 저장하고, [생성] 시 봇이 그 작품 탭을
통째로 읽어 PART D(시점·개요 준수)에 반영한다.

실행: python3 app.py  (Socket Mode — 공개 URL 불필요)
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
CMD_RE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*(.*)$", re.S)   # [명령] 나머지
SUB_RE = re.compile(r"^\s*<\s*([^>]+?)\s*>\s*(.*)$", re.S)      # <작품> 나머지

# 상위 명령 별칭
CMD_INPUT = {"입력", "저장", "input"}
CMD_EDIT = {"수정", "편집", "edit"}
CMD_GEN = {"생성", "generate", "gen"}
CMD_CONVERT = {"변환", "포맷", "대본변환"}
CMD_TREND = {"트렌드", "trend"}
CMD_REFRESH = {"새로고침", "refresh"}
CMD_RELOAD = {"리로드", "reload"}

_HELP = (
    "명령은 `[명령] <작품> 경로` 형식이에요 👇\n"
    "```\n"
    "[입력] <날혐남> 로그라인\n"
    "정략결혼한 여주가 남편을 살리고 도망친다\n"
    "\n"
    "[입력] <날혐남> 인물 / 강태혁      ← 소분류 여러 개 한 번에\n"
    "성별: 남\n"
    "나이: 32\n"
    "핵심대사: 알아들었으면 나가.\n"
    "\n"
    "[생성] <날혐남> 대본 / 24화     ← 24화 개요+바이블 참고해 생성\n"
    "[변환] (줄글 초안 붙여넣기)\n"
    "[트렌드] 엔딩\n"
    "```\n"
    "• `[입력]` 새로 저장 / `[수정]` 기존 고침 / `[생성]` 생성+저장 / `[변환]` 촬영대본 / `[트렌드]` 조회\n"
    "• 이름만: 로그라인·키워드·타겟층·핵심정서·줄거리·금지사항·진행상태 (뒤에 바로 내용)\n"
    "• 인물/회차분배: 소분류 하나 `인물/강태혁/성별 남` 또는 여러 개를 줄마다 `소분류: 값`\n"
    "  인물 소분류 = 성별·나이·포지션·설정·핵심대사·설명 / 회차분배 = 구간·화수·핵심사건\n"
    "• 개요 / <N화> · 대본 / <N화>"
)


def _clean(text: str) -> str:
    # 슬랙은 사용자가 친 < > & 를 HTML 엔티티로 보냄 → 되돌려야 <작품> 패턴이 잡힘
    text = MENTION_RE.sub("", text or "")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return text.strip()


def _reply(channel: str, thread_ts: str, text: str) -> None:
    app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


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
        messages.append({"role": role, "content": text})
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
        _reply(channel, thread_ts, c)


def _post_code(channel: str, thread_ts: str, text: str) -> None:
    """정렬 유지가 필요한 촬영대본은 코드블록(monospace)으로. 줄 경계로 분할."""
    limit, buf, chunks = 3600, "", []
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    for c in chunks:
        _reply(channel, thread_ts, f"```\n{c}\n```")


# ---------------- 명령별 처리 ----------------

def _parse_records(content: str, subs: list[str],
                   seed: str | None = None) -> tuple[list[tuple[str, list]], list[str]]:
    """여러 인물/막 블록 → [(이름, [(소분류,값)])], [모르는 키].
    소분류가 아닌 줄은 새 레코드(이름/막) 시작, 그 아래 '소분류: 값' 줄은 그 레코드의 필드.
    seed가 있으면(경로에 이름 지정) 첫 레코드를 그 이름으로 시작 → 그 이름의 필드가 먼저 오고,
    새 이름 줄이 나오면 그때부터 다음 인물."""
    records: list[tuple[str, list]] = []
    unknown: list[str] = []
    cur = None
    if seed:
        cur = (seed, [])
        records.append(cur)
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(\S+?)\s*[:=]\s*(.*)$", line)   # 'key: 값' / 'key=값' 형태 = 필드
        if m:
            k, v = m.group(1).strip(), m.group(2).strip()
            if k in subs and cur is not None:
                cur[1].append((k, v))
            else:
                unknown.append(k)                       # 모르는 소분류(또는 이름 앞) → 새 인물 아님
            continue
        bits = line.split(None, 1)                       # 구분자 없음
        if bits[0] in subs and cur is not None:          # '성별 남' 공백형 필드
            cur[1].append((bits[0], bits[1].strip() if len(bits) > 1 else ""))
        else:
            cur = (line, [])                             # 이름 줄(전체 줄이 이름/막)
            records.append(cur)
    return records, unknown


_BULLET_RE = re.compile(r"^([ \t]*)[•◦▪●‣·∙・]\s+", re.M)      # 슬랙 글머리 → 마크다운 '- '
_HWA_HEAD_RE = re.compile(r"^\s*(\d+)\s*화(?:[\s:.\-–—·]+(.*))?$")  # 'N화' 또는 'N화 제목' 줄


def _md_bullets(text: str) -> str:
    """슬랙 글머리 기호(●·•·▪…)를 마크다운 '- '로 치환. 번호목록·기존 '-'는 그대로."""
    return _BULLET_RE.sub(r"\1- ", text or "")


def _parse_outline_records(content: str, seed: str | None = None) -> list[tuple[str, str]]:
    """개요/대본: 'N화'(뒤에 제목 붙어도 됨) 줄을 헤더로, 그 아래 줄글 전체를 그 화 내용으로.
    seed(경로에 화 지정)가 있으면 첫 헤더 전 내용은 그 화에 붙는다. → [(N화, 내용)]"""
    records: list[list] = []
    cur = None
    if seed:
        cur = [seed, []]
        records.append(cur)
    for raw in content.splitlines():
        head_test = raw.strip().strip("*_~ ").strip()    # 슬랙 볼드/이탤릭(*1화*) 마커 제거 후 판정
        m = _HWA_HEAD_RE.match(head_test)
        if m:
            cur = [f"{m.group(1)}화", []]
            title = (m.group(2) or "").strip()
            if title:
                cur[1].append(title)                      # 제목은 내용 첫 줄로 보존
            records.append(cur)
            continue
        if cur is None:
            if not raw.strip():
                continue
            cur = ["", []]                                # 헤더 전 내용(화 미상) → 나중 제외
            records.append(cur)
        cur[1].append(raw)
    return [(hwa, "\n".join(lines).strip()) for hwa, lines in records if hwa]


def _do_input(channel: str, thread_ts: str, rest: str, mode: str) -> None:
    """[입력](신규) / [수정](기존) — <작품> 경로 + 다음 줄 내용 → 시트 저장.
    mode='create': 이미 있으면 거부 / mode='update': 없으면 거부."""
    from bot.sheet_bible import parse_path, split_command, TABLE_SUBS
    sheet = reference.sheet()
    if not sheet:
        _reply(channel, thread_ts, "시트가 아직 연결 안 됐어요 (SHEET_WEBAPP_URL 미설정).")
        return
    sm = SUB_RE.match(rest)
    if not sm:
        _reply(channel, thread_ts, _HELP)
        return
    work = sm.group(1).strip()
    after = sm.group(2).splitlines()
    first = after[0].strip() if after else ""
    path_line, inline = split_command(first)        # 한 줄에 붙여쓴 내용도 인식
    next_lines = "\n".join(after[1:]).strip()        # 다음 줄들도 내용
    content = "\n".join(x for x in (inline, next_lines) if x)  # 인라인 + 다음 줄 모두 (유실 방지)
    triple = parse_path(path_line)
    if not triple:
        _reply(channel, thread_ts,
               f"`{path_line}` 는 모르는 종류예요. 로그라인·키워드·타겟층·핵심정서·인물/<이름>·줄거리·회차분배·개요/<N화>·대본/<N화>")
        return
    top, mid, sub = triple
    if top in ("개요", "대본"):
        content = _md_bullets(content)               # 글머리 기호 → 마크다운 '-'
    # 여러 열을 갖는 표(인물·회차분배): 소분류 하나 직접 지정이 아니면 레코드 블록으로 처리
    if top in TABLE_SUBS:
        subs_list = TABLE_SUBS[top]
        subs = " · ".join(subs_list)
        key = "이름" if top == "등장인물" else "막"
        verb = "저장" if mode == "create" else "수정"
        icon = "✅" if mode == "create" else "✏️"
        if sub and sub not in subs_list:
            _reply(channel, thread_ts, f"⚠️ `{sub}` 는 {top} 소분류가 아니에요. 가능한 소분류: {subs}")
            return
        if not sub:
            # 경로 이름(mid)을 첫 레코드로 seed → 그 필드 먼저, 이후 새 이름 줄부터 다음 인물
            records, unknown = _parse_records(content, subs_list, seed=mid or None)
            if not records:  # 이름도 내용도 없음
                _reply(channel, thread_ts,
                       f"⚠️ {top}은 {key}이 필요해요. 하나만: `{top}/강태혁/성별 남`\n"
                       f"여러 개: `{top}` 하고 아래처럼 ↓\n"
                       f"```\n강태혁\n성별: 남\n나이: 32\n\n윤서아\n성별: 여\n```\n소분류: {subs}")
                return
            lines = []
            for name, pairs in records:
                for k, v in pairs:
                    r = sheet.upsert(work, top, name, k, v)
                    if isinstance(r, dict) and r.get("error"):
                        _reply(channel, thread_ts, f"⚠️ {name}/{k} {verb} 실패: {r['error']}")
                        return
                if not pairs:  # 이름/막만 → 행 등록
                    r = sheet.upsert(work, top, name, "", "")
                    if isinstance(r, dict) and r.get("error"):
                        _reply(channel, thread_ts, f"⚠️ {name} 등록 실패: {r['error']}")
                        return
                lines.append(f"· {name}: " + (", ".join(k for k, _ in pairs) if pairs else "(행만 등록)"))
            sheet.invalidate(work)
            if len(records) == 1:
                msg = f"{icon} *{work}* / {top} / {records[0][0]} — {lines[0].split(': ', 1)[1]} {verb}했어요."
            else:
                msg = f"{icon} *{work}* / {top} — {len(records)}개 {verb}했어요.\n" + "\n".join(lines)
            if unknown:
                msg += f"\n(모르는 소분류 건너뜀: {', '.join(dict.fromkeys(unknown))} · 가능: {subs})"
            _reply(channel, thread_ts, msg)
            return

    # 개요·대본: 경로에 화가 없거나 'N화' 헤더가 있으면 여러 화를 한 번에
    if top in ("개요", "대본"):
        records = _parse_outline_records(content, seed=mid or None)
        multi = (not mid) or len(records) > 1        # 단순 케이스(경로+단일 내용)는 기존 흐름에 맡김
        if multi:
            if not records:
                _reply(channel, thread_ts,
                       f"⚠️ {top}는 `{top}/11화` 하고 다음 줄에 내용, 또는 `{top}` 하고 아래처럼 ↓\n"
                       f"```\n11화\n(내용…)\n\n12화\n(내용…)\n```")
                return
            verb = "저장" if mode == "create" else "수정"
            icon = "✅" if mode == "create" else "✏️"
            for hwa, body in records:
                r = sheet.upsert(work, top, hwa, "", body)
                if isinstance(r, dict) and r.get("error"):
                    _reply(channel, thread_ts, f"⚠️ {hwa} {verb} 실패: {r['error']}")
                    return
            sheet.invalidate(work)
            names = ", ".join(hwa for hwa, _ in records)
            _reply(channel, thread_ts, f"{icon} *{work}* / {top} — {names} {verb}했어요.")
            return

    # 내용이 없어도 분류(경로)만 유효하면 빈 칸으로 저장 (나중에 채우기)
    label = " / ".join(x for x in [top, mid, sub] if x)
    exists = sheet.exists(work, top, mid, sub)  # None이면 확인 불가 → 그냥 진행
    if mode == "create" and exists is True:
        _reply(channel, thread_ts, f"⚠️ *{work}* / {label} 은 이미 있어요. 고치려면 `[수정]` 을 쓰세요.")
        return
    if mode == "update" and exists is False:
        _reply(channel, thread_ts, f"⚠️ *{work}* / {label} 은 아직 없어요. 새로 넣으려면 `[입력]` 을 쓰세요.")
        return

    try:
        res = sheet.upsert(work, top, mid, sub, content)
        if isinstance(res, dict) and res.get("error"):
            _reply(channel, thread_ts, f"⚠️ 저장 못 했어요: {res['error']}")
            return
        sheet.invalidate(work)
        verb = "저장" if mode == "create" else "수정"
        icon = "✅" if mode == "create" else "✏️"
        _reply(channel, thread_ts, f"{icon} *{work}* / {label} {verb}했어요.")
    except Exception:
        log.exception("input upsert failed")
        _reply(channel, thread_ts, "⚠️ 시트 저장에 실패했어요. 잠시 후 다시 시도해 주세요.")


def _do_generate(channel: str, thread_ts: str, rest: str) -> None:
    """[생성] <작품> 경로(대본/N화 등) → 바이블 참고 생성 + 시트 저장."""
    from bot.sheet_bible import parse_path
    sm = SUB_RE.match(rest)
    if not sm:
        _reply(channel, thread_ts, "형식: `[생성] <작품> 대본 / 24화`\n예: `[생성] <날혐남> 대본 / 24화`")
        return
    work = sm.group(1).strip()
    path_line = (sm.group(2).splitlines() or [""])[0].strip()
    triple = parse_path(path_line)
    if not triple:
        _reply(channel, thread_ts, "형식: `[생성] <작품> 대본 / 24화` (또는 개요 / N화)")
        return
    top, mid, sub = triple
    # 대상 회차: 경로 어디에 있든 'N화'를 잡음 (없으면 build가 진행상태 화로 fallback)
    epm = re.search(r"(\d+)\s*화", path_line)
    target = int(epm.group(1)) if epm else None

    sheet = reference.sheet()
    bible = None
    if sheet and work:
        try:
            bible = sheet.get(work)
        except Exception:
            log.exception("sheet bible load failed")  # 못 읽어도 생성은 계속

    messages = _thread_messages(channel, thread_ts)
    if not messages:
        return
    req = " ".join(x for x in [work, mid, top] if x)
    try:
        answer = generator.generate(messages, req, bible=bible, target_episode=target)
    except Exception:
        log.exception("generation failed")
        _reply(channel, thread_ts, "생성 중 오류가 났어요. 잠시 후 다시 시도해 주세요.")
        return

    # 슬랙은 초안 생성만. 시트 저장은 사람이 검토 후 [입력]/[수정]으로 직접.
    label = " / ".join(x for x in [top, mid, sub] if x)
    if label:
        answer += f"\n\n_📝 초안입니다. 확정하려면 `[입력] <{work}> {label}` 로 저장하세요._"
    _post_chunks(channel, thread_ts, answer)


def _do_convert(channel: str, thread_ts: str, rest: str) -> None:
    """[변환]: 초안(명령 뒤 본문, 없으면 스레드 직전 봇 대본)을 촬영대본 포맷으로."""
    from bot import script_format
    raw = rest.strip()
    epm = re.search(r"(\d+)\s*화", raw)
    ep = epm.group(1) if epm else ""
    draft = raw
    if len(draft) < 30:  # 초안 미첨부 → 스레드 직전 봇 대본
        prior = [m["content"] for m in _thread_messages(channel, thread_ts)
                 if m["role"] == "assistant"]
        draft = prior[-1] if prior else ""
    if not draft:
        _reply(channel, thread_ts,
               "변환할 초안을 `[변환]` 뒤에 붙여주세요. 대본이 있는 스레드에서는 `[변환]`만 보내도 직전 대본을 변환합니다.")
        return
    try:
        result = script_format.convert_script(draft, generator.complete, episode_label=ep)
        _post_code(channel, thread_ts, result)
    except Exception:
        log.exception("script convert failed")
        _reply(channel, thread_ts, "변환 중 오류가 났어요 (초안 구조를 못 읽었을 수 있어요). 다시 시도해 주세요.")


def _do_revise(channel: str, thread_ts: str, feedback: str) -> None:
    """스레드 후속 피드백 → 이전 초안을 그 지시대로 수정해 전체본 재출력 (저장은 안 함)."""
    messages = _thread_messages(channel, thread_ts)
    if not messages:
        _reply(channel, thread_ts, _HELP)
        return
    # 스레드에서 작품·회차 추론 (초안 생성 때 쓴 <작품>·N화). 바이블을 계속 근거로.
    joined = "\n".join(m["content"] for m in messages)
    wm = re.search(r"<\s*([^>]+?)\s*>", joined)
    work = wm.group(1).strip() if wm else None
    em = re.search(r"(\d+)\s*화", joined)
    target = int(em.group(1)) if em else None

    bible = None
    if work:
        sheet = reference.sheet()
        if sheet:
            try:
                bible = sheet.get(work)
            except Exception:
                log.exception("revise bible load failed")  # 못 읽어도 진행
    try:
        answer = generator.generate(messages, feedback, bible=bible, target_episode=target)
    except Exception:
        log.exception("revise failed")
        _reply(channel, thread_ts, "수정 중 오류가 났어요. 잠시 후 다시 시도해 주세요.")
        return
    if work:
        answer += f"\n\n_📝 초안입니다. 확정은 `[입력]`/`[수정]` 으로._"
    _post_chunks(channel, thread_ts, answer)


def _do_trend(channel: str, thread_ts: str, rest: str) -> None:
    trend = reference.load_trend()
    if trend is None:
        _reply(channel, thread_ts, "트렌드 DB가 아직 없어요. `sync_reference.py`로 데이터 반영 후 다시 물어봐 주세요.")
        return
    try:
        _post_chunks(channel, thread_ts, trend.answer(rest.strip() or "트렌드"))
    except Exception:
        log.exception("trend search failed")
        _reply(channel, thread_ts, "트렌드 집계 중 오류가 났어요.")


def _handle(event: dict) -> None:
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    query = _clean(event.get("text", ""))

    m = CMD_RE.match(query)
    if not m:
        # 스레드 안의 후속 메시지(명령 없음) → 이전 초안 수정 지시로 처리
        in_thread = bool(event.get("thread_ts")) and event.get("thread_ts") != event.get("ts")
        if in_thread and query.strip():
            _do_revise(channel, thread_ts, query)
        else:
            _reply(channel, thread_ts, _HELP)
        return
    cmd, rest = m.group(1).strip(), m.group(2)

    if cmd in CMD_RELOAD:
        reference.reload()
        _reply(channel, thread_ts, "레퍼런스 DB·템플릿을 다시 불러왔어요.")
    elif cmd in CMD_REFRESH:
        sheet = reference.sheet()
        if sheet:
            sheet.invalidate()
        _reply(channel, thread_ts, "시트 바이블 캐시를 비웠어요. 다음 요청부터 최신으로 읽어옵니다.")
    elif cmd in CMD_CONVERT:
        _do_convert(channel, thread_ts, rest)
    elif cmd in CMD_TREND:
        _do_trend(channel, thread_ts, rest)
    elif cmd in CMD_INPUT:
        _do_input(channel, thread_ts, rest, mode="create")
    elif cmd in CMD_EDIT:
        _do_input(channel, thread_ts, rest, mode="update")
    elif cmd in CMD_GEN:
        _do_generate(channel, thread_ts, rest)
    else:
        _reply(channel, thread_ts, f"`[{cmd}]` 는 모르는 명령이에요.\n\n" + _HELP)


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
    generator.healthcheck()  # Anthropic 자격증명 확인 (내부 Claude Code 팀 로그인 or API 키)
    log.info("co-writer-bot 시작 (backend=%s, reference=%s)", config.BACKEND, config.REFERENCE_DIR)
    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()
