#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""co-writer-bot — 숏폼 드라마 보조 작가 슬랙 에이전트.

명령 = [명령] <작품> 경로 형식. 앞에 [명령]이 없으면 사용법 안내.

  [입력] <날혐남> 로그라인            → 신규 저장 (이미 있으면 경고)
  [수정] <날혐남> 로그라인            → 기존 값 고침 (없으면 경고)
  [입력] <날혐남> 인물 / 강태혁 / 설정  → 등장인물 소분류 저장
  [생성] <날혐남> 대본 / 24화         → 24화 개요+바이블 참고해 생성 + 시트 저장
  [변환] 휴대폰 보는 연우…            → 줄글 상황을 드라마 대본식 지문으로 구체화
  [트렌드] 엔딩                      → 트렌드 조회
  [새로고침] / [리로드]              → 캐시 무효화

바이블 = 구글 시트(탭=작품). 대분류/중분류/소분류 계층으로 저장하고, [생성] 시 봇이 그 작품 탭을
통째로 읽어 PART D(시점·개요 준수)에 반영한다.

실행: python3 app.py  (Socket Mode — 공개 URL 불필요)
"""
import json
import logging
import re
import threading
import time
import urllib.request

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from bot import config, generator, prefs, prompts, reference, verify

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
CMD_PLAN = {"기획", "기획안", "작품생성", "plan"}                          # 컨셉 → 기획안 초안(노션 구조)
CMD_CONVERT = {"변환", "포맷", "대본변환"}
CMD_STORYBOARD = {"스토리보드", "스보", "storyboard"}                     # 대본 → 씬 스토리보드
# 스토리보드 스레드에서 이 말이 오면 1단계(씬 설계) → 2단계(상세 스토리보드)로 넘어감
SB_GEN_RE = re.compile(r"^\s*(생성|생성해|생성해줘|상세\s*생성|만들어|만들어줘|ㄱㄱ|고고)\s*$")
# 단계 배지 (출력 맨 위에 붙여 슬랙에서 어느 단계인지·다음에 뭘 칠지 보이게). 마커([1/3]/[2/3])는 _sb_stage가 씀.
SB_BADGE_PLAN = "🎬 *[1/3단계] 씬 설계* — 씬 나누기·시간 고칠 것 있으면 말해주세요. 좋으면 「생성」\n\n"
SB_BADGE_BOARD = "🎬 *[2/3단계] 작가 확인용 스토리보드* — 대본 그대로예요. 고칠 것 있으면 말해주세요. (씨댄스 변환은 준비 중)\n\n"
CMD_TREND = {"트렌드", "trend"}
CMD_IDEA = {"아이디어", "아이디어 제시", "아이디어제시", "제안", "idea"}
CMD_SYNC = {"동기화", "노션동기화", "sync"}                              # 노션 붙여넣기 → 시트
CMD_FEEDBACK = {"피드백", "feedback", "평가", "리뷰", "review"}          # 둘 다
CMD_FB_FUN = {"재미", "피드백 재미", "피드백재미", "fun"}                 # 재미만
CMD_FB_LOGIC = {"개연성", "피드백 개연성", "피드백개연성", "논리", "logic"}  # 개연성만
CMD_STOP = {"멈춰", "멈춤", "중지", "정지", "스톱", "그만", "stop", "cancel"}
CMD_LIKE = {"좋아", "좋아요", "굿", "like", "👍"}
CMD_DISLIKE = {"별로", "별로야", "싫어", "노", "dislike", "👎"}
_CANCEL: set[str] = set()   # 취소 요청된 thread_ts (생성 결과를 버림)
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
    "[생성] <날혐남> 개요 / 11화     ← 다음 줄에 넣고 싶은 포인트 적으면 반영\n"
    "서아가 처음으로 반격하는 장면 꼭 넣어줘\n"
    "\n"
    "[생성] <날혐남> 대본 / 24화     ← 24화 개요+바이블 참고해 생성 (자동 검증 관문 ON)\n"
    "[생성] <날혐남> 대본 / 24화 검증생략   ← 빠르게: 바이블 준수 자동검증 끄기\n"
    "[변환] 휴대폰 보는 연우, 화내며 나감   ← 줄글 상황 → 드라마 대본식 지문으로\n"
    "[스토리보드] <날혐남> (대본 붙여넣기)   ← 대본 → 영상문법가이드 기준 씬 스토리보드(90초·씬 단위) (스레드서 대본 뽑았으면 명령만)\n"
    "[트렌드] 요즘 뭐가 유행?          ← 쉬운 요약\n"
    "[아이디어] <날혐남> 서아 힘든 거 어떻게 보여주지?  ← 구체적 상황 제안\n"
    "[피드백] <날혐남> (대본)  ← 재미+개연성 / [재미]·[개연성]로 따로도 가능\n"
    "[동기화] <날혐남> (노션 내용 통째로 붙여넣기)  ← 노션→시트 반영\n"
    "[좋아]/[별로] (생성물 스레드에서, 뒤에 이유)  ← 다음 생성에 학습 (별로는 바로 다시 뽑음)\n"
    "```\n"
    "• `[입력]` 새로 저장 / `[수정]` 기존 고침 / `[생성]` 초안 / `[아이디어]` 상황제안 / `[변환]` 줄글→대본식지문 / `[스토리보드]` 대본→씬 스토리보드 / `[트렌드]` 조회 / `[멈춰]` 중지\n"
    "• 이름만: 로그라인·키워드·타겟층·핵심정서·줄거리·금지사항·진행상태 (뒤에 바로 내용)\n"
    "• 인물/회차분배: 소분류 하나 `인물/강태혁/성별 남` 또는 여러 개를 줄마다 `소분류: 값`\n"
    "  인물 소분류 = 성별·나이·포지션·설정·핵심대사·설명 / 회차분배 = 구간·화수·핵심사건\n"
    "• 개요 / <N화> · 대본 / <N화>"
)


def _clean(text: str) -> str:
    # 슬랙은 사용자가 친 < > & 를 HTML 엔티티로 보냄 → 되돌려야 <작품> 패턴이 잡힘
    text = MENTION_RE.sub("", text or "")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    text = text.strip()
    # 슬랙이 자동으로 붙인 목록 기호(1. / 1) / - / • 등)를 앞에서 제거 → 명령이 '['로 시작하게
    text = re.sub(r"^\s*(?:\d+[.)]|[-*•·▪◦])\s+", "", text)
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


def _thinking(channel: str, thread_ts: str, note: str = "생성 중이에요… (몇 초~1분)") -> str | None:
    """진행 표시용 플레이스홀더 메시지. 완료되면 _post_chunks(replace_ts=...)로 교체됨."""
    try:
        r = app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"⏳ {note}")
        return r.get("ts")
    except Exception:
        return None


def _update_note(channel: str, ts: str | None, note: str) -> None:
    """진행 플레이스홀더 문구만 갱신 (검증 등 중간 단계 표시용). 실패는 무시."""
    if not ts:
        return
    try:
        app.client.chat_update(channel=channel, ts=ts, text=f"⏳ {note}")
    except Exception:
        pass


# 검증 관문 on/off 플래그 (요청문에서). '검증생략'/'빠르게'=off, '검증'=on, 없으면 기본값.
_VERIFY_OFF_RE = re.compile(r"검증\s*(생략|끄기|스킵|off)|빠르게|noverify", re.I)
_VERIFY_ON_RE = re.compile(r"검증", re.I)
_VERIFY_TOKENS_RE = re.compile(r"\s*(검증\s*(생략|끄기|스킵|off)?|빠르게|noverify)", re.I)


def _verify_gate_on(text: str, default: bool) -> bool:
    if _VERIFY_OFF_RE.search(text or ""):
        return False
    if _VERIFY_ON_RE.search(text or ""):
        return True
    return default


def _do_stop(channel: str, thread_ts: str) -> None:
    """[멈춰] — 이 스레드에서 진행 중인 생성 결과를 버리게 표시."""
    _CANCEL.add(thread_ts)
    _reply(channel, thread_ts, "🛑 멈출게요. 진행 중이던 초안은 결과를 버립니다.")


def _cancelled(channel: str, thread_ts: str, ph: str | None) -> bool:
    """생성 완료 후 호출: 취소 요청이 있었으면 결과를 버리고 True."""
    if thread_ts in _CANCEL:
        _CANCEL.discard(thread_ts)
        _post_chunks(channel, thread_ts, "🛑 멈췄어요. 초안은 버렸어요.", replace_ts=ph)
        return True
    return False


def _mrkdwn(text: str) -> str:
    """표준 마크다운 → 슬랙 mrkdwn. **볼드**→*볼드*, ## 헤더 → 굵은 줄."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text or "")          # **x** → *x*
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*(.+?)\s*$", r"*\1*", text)  # # 헤더 → *굵은 줄*
    return text


def _post_chunks(channel: str, thread_ts: str, text: str, replace_ts: str | None = None) -> None:
    """슬랙 메시지 길이 제한(4000자) 대응 — 문단 경계로 분할 전송.
    replace_ts가 있으면 첫 청크로 그 플레이스홀더를 교체(update)한다."""
    chunk, chunks = "", []
    for para in _mrkdwn(text or "(빈 응답)").split("\n\n"):
        if len(chunk) + len(para) + 2 > 3800:
            chunks.append(chunk)
            chunk = para
        else:
            chunk = f"{chunk}\n\n{para}" if chunk else para
    if chunk:
        chunks.append(chunk)
    for i, c in enumerate(chunks):
        if i == 0 and replace_ts:
            try:
                app.client.chat_update(channel=channel, ts=replace_ts, text=c)
                continue
            except Exception:
                pass
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


def _override_intensity(bible: dict | None, text: str) -> dict | None:
    """명령/피드백에 '강도 N'이 있으면 이번 호출만 그 레벨로 재보정 (캐시 원본은 안 건드림)."""
    if not bible:
        return bible
    im = re.search(r"강도\s*([1-5])", text or "")
    if not im:
        return bible
    b = dict(bible)
    b["intensity_level"] = int(im.group(1))
    b["intensity_map"] = {}       # 이번 지시가 타입별 설정보다 우선
    return b


DEFAULT_INTENSITY = 4   # [생성]·[재미] 기본 강도 (명시·시트값 없을 때)
IDEA_INTENSITY = 2      # [아이디어] 기본 강도 — 담백하게 (막장·과몰입 방지)


def _ensure_default_intensity(bible: dict | None, kind: str,
                              default: int = DEFAULT_INTENSITY) -> dict | None:
    """강도가 명시/저장 안 됐으면 기본값으로 채운다 (이 kind 기준)."""
    if not bible:
        return bible
    eff = (bible.get("intensity_map") or {}).get(kind) or bible.get("intensity_level")
    if eff:
        return bible
    b = dict(bible)
    b["intensity_level"] = default
    b["intensity_map"] = {}
    return b


def _idea_intensity(bible: dict | None, text: str) -> dict | None:
    """[아이디어]는 강도 IDEA_INTENSITY로 고정 — 작품 전체 강도(예: 4)에 끌려가지 않게.
    단, 질문에 '강도 N'이 있거나 시트에 '아이디어' 전용 강도가 있으면 그건 존중한다."""
    if not bible:
        return bible
    if re.search(r"강도\s*[1-5]", text or ""):          # 질문의 명시 강도 우선 (이미 _override로 반영)
        return bible
    if (bible.get("intensity_map") or {}).get("아이디어"):  # 시트의 아이디어 전용 강도 존중
        return bible
    return dict(bible, intensity_map={"아이디어": IDEA_INTENSITY}, intensity_level=None)


def _progress_episode(bible: dict | None, prefer: list[str]) -> int | None:
    """회차 미지정 시 진행상태에서 기본 화를 고른다. prefer 타입(개요/대본/회차분배) 우선,
    없으면 아무 진행 화, 그것도 없으면 current_episode."""
    if not bible:
        return None
    prog = bible.get("progress") or {}
    for t in prefer:
        if prog.get(t):
            return prog[t]
    return next(iter(prog.values()), None) or bible.get("current_episode")


def _thread_gen_context(messages: list[dict]) -> tuple:
    """스레드에서 마지막 [생성] 맥락(작품·타입·회차)과 마지막 생성물 초안을 추출."""
    from bot.sheet_bible import parse_path
    work = top = None
    episode = None
    draft = ""
    for m in messages:
        if m["role"] == "assistant" and len(m["content"]) > 60:
            draft = m["content"]
        elif m["role"] == "user":
            cm = CMD_RE.match(m["content"])
            if cm and cm.group(1).strip() in CMD_GEN:
                sm = SUB_RE.match(cm.group(2))
                if sm:
                    work = sm.group(1).strip()
                    pl = (sm.group(2).splitlines() or [""])[0]
                    tp = parse_path(re.sub(r"\s*강도\s*\S+", "", pl).strip())
                    if tp:
                        top = tp[0]
                    em = re.search(r"(\d+)\s*화", pl)
                    episode = int(em.group(1)) if em else episode
    return work, top, episode, draft


def _with_prefs(req: str, work: str | None, top: str, level: int | None = None) -> str:
    """생성 요청에 관련 선호 피드백(좋아/별로, 강도 일치 우선)을 검색해 붙인다."""
    if not work:
        return req
    pos, neg = prefs.retrieve(work, top, req, level=level)
    block = prefs.format_block(pos, neg)
    return (req + "\n\n" + block) if block else req


def _do_pref(channel: str, thread_ts: str, rest: str, sign: str) -> None:
    """[좋아]/[별로] — 스레드의 생성물에 대한 반응 저장. 별로면 반영해 재생성."""
    messages = _thread_messages(channel, thread_ts)
    work, top, episode, draft = _thread_gen_context(messages)
    if not work or len(draft) < 30:
        _reply(channel, thread_ts, "생성물 스레드에서 `[좋아]`/`[별로]` (뒤에 이유 적어도 됨)로 눌러 주세요.")
        return
    reason = rest.strip()
    # 강도 태깅: 이유에 '강도 N'/'N번'이 있으면 그 레벨, 없으면 생성물 배너의 강도
    lm = re.search(r"강도\s*([1-5])", reason) or re.search(r"([1-5])\s*번", reason)
    if not lm:
        lm = re.search(r"강도\s*([1-5])\s*단계", draft)
    level = int(lm.group(1)) if lm else None
    prefs.add(work, sign, top, episode, reason, draft, level=level)
    lv = f"강도 {level} " if level else ""
    if sign == "+":
        _reply(channel, thread_ts, f"👍 {lv}저장했어요. 다음 {top or '생성'}부터 이 방향을 살립니다.")
        return
    _reply(channel, thread_ts, f"👎 {lv}저장했어요. 반영해서 다시 뽑을게요…")
    if top:   # 같은 작품/타입/회차(+강도)로 재생성 — 누적 피드백 반영
        ep = f"/{episode}화" if episode else ""
        lv_cmd = f" 강도 {level}" if level else ""
        _do_generate(channel, thread_ts, f"<{work}> {top}{ep}{lv_cmd}")


def _notes_block(notes: str) -> str:
    """작가가 넣고 싶은 포인트를 '재료'로 주입. 그대로 반복하지 말고 살 붙여 전개 + 이후 사건까지."""
    notes = (notes or "").strip()
    if not notes:
        return ""
    return (
        "\n\n[작가가 넣고 싶은 포인트 — 이미 정해진 '재료'다]\n"
        f"{notes}\n"
        "※ 위 포인트를 그대로 옮겨 적거나 요약해서 되풀이하지 마라. 이건 결과물이 아니라 '넣어야 할 재료'다.\n"
        "  1) 각 포인트를 장면·행동·대치로 살을 붙여 전개하고,\n"
        "  2) **마지막 포인트 그 뒤에 자연스럽게 이어질 다음 사건·전개(엔딩 훅 포함)를 새로 만들어** 이번 화를 완성하라.\n"
        "  → 재료를 나열·재진술하는 게 아니라, 재료를 딛고 '그다음'을 쓰는 게 목적이다."
    )


def _do_generate(channel: str, thread_ts: str, rest: str) -> None:
    """[생성] <작품> 경로(대본/N화 등) → 바이블 참고 생성 + 시트 저장."""
    from bot.sheet_bible import parse_path
    sm = SUB_RE.match(rest)
    if not sm:
        _reply(channel, thread_ts, "형식: `[생성] <작품> 대본 / 24화`\n예: `[생성] <날혐남> 대본 / 24화`")
        return
    work = sm.group(1).strip()
    gen_lines = sm.group(2).splitlines()
    path_line = (gen_lines or [""])[0].strip()
    notes = "\n".join(gen_lines[1:]).strip()      # 경로 아래 줄 = 넣고 싶은 포인트/지시
    directive = path_line + "\n" + notes          # 강도/검증 지시 감지용(명령 줄 포함)
    # 검증 관문 on/off 결정 후, 플래그 토큰은 생성 프롬프트 오염 방지 위해 제거
    gate_on = _verify_gate_on(directive, config.VERIFY_GATE)
    path_line = _VERIFY_TOKENS_RE.sub("", path_line).strip()
    notes = _VERIFY_TOKENS_RE.sub("", notes).strip()
    # 강도 지시를 경로에서 떼어내 mid 오염 방지 (예: '개요/4화 강도 4' → '개요/4화')
    _INTPAT = r"강도\s*(전체|전부|모두|비교|1\s*[~\-]\s*5|1to5|[1-5])\S*"
    path_clean = re.sub(r"\s*" + _INTPAT, "", path_line).strip()
    triple = parse_path(path_clean)
    if not triple:
        _reply(channel, thread_ts, "형식: `[생성] <작품> 대본 / 24화` (또는 개요 / N화)")
        return
    top, mid, sub = triple
    # 개요·대본: 같은 줄에서 경로('개요/4화') 뒤에 붙인 텍스트는 mid가 아니라 '넣고 싶은 포인트'다.
    # mid는 'N화'만 남기고 나머지는 notes로 넘긴다. (예: '4화 과거 플래시백…' → mid='4화', 포인트='과거…')
    if top in ("개요", "대본") and mid:
        mm = re.match(r"\s*(\d+\s*화)\s*(.*)$", mid, re.S)
        if mm:
            mid = mm.group(1).strip()
            inline = mm.group(2).strip()
            if inline:
                notes = (inline + ("\n" + notes if notes else "")).strip()
    # 대상 회차: 경로 어디에 있든 'N화'를 잡음 (없으면 build가 진행상태 화로 fallback)
    epm = re.search(r"(\d+)\s*화", path_clean)
    target = int(epm.group(1)) if epm else None

    sheet = reference.sheet()
    bible = None
    if sheet and work:
        try:
            bible = sheet.get(work)
        except Exception:
            log.exception("sheet bible load failed")  # 못 읽어도 생성은 계속

    # 회차 안 적었으면 진행상태의 '타입별 진행 화' 사용 (예: 생성 개요 → 개요 진행 화)
    if target is None:
        target = _progress_episode(bible, [top])
    bible = _override_intensity(bible, directive)   # '강도 N'(명령 줄/포인트) 이번만 재보정
    log.info("생성 top=%s target=%s 강도lvl=%s map=%s", top, target,
             (bible or {}).get("intensity_level"), (bible or {}).get("intensity_map"))

    messages = _thread_messages(channel, thread_ts)
    if not messages:
        return
    # '강도 1~5 / 전체 / 비교' → 5단계 버전을 한 번에 뽑기
    all_lvls = re.search(r"강도\s*(전체|전부|모두|비교|1\s*[~\-]\s*5|1to5)", directive)
    what = " ".join(x for x in [mid, top] if x) or top
    if all_lvls:
        notes_c = re.sub(r"강도\s*(전체|전부|모두|비교|1\s*[~\-]\s*5|1to5)[^\n]*", "", notes).strip()
        req = f"'{work}' {what}를 생성해줘." + _notes_block(notes_c)
        # prefs는 루프에서 강도별로 붙임
        _CANCEL.discard(thread_ts)
        ph = _thinking(channel, thread_ts, f"{what} 강도 1~5단계 순서대로 뽑는 중이에요… (좀 걸려요)")
        first = True
        for lvl in range(1, 6):
            if _cancelled(channel, thread_ts, ph if first else None):
                return
            b_lvl = dict(bible or {}, intensity_level=lvl, intensity_map={})
            req_l = _with_prefs(req, work, top, level=lvl)   # 그 강도의 피드백 반영
            msgs = list(messages); msgs[-1] = {"role": "user", "content": req_l}
            try:
                ans = generator.generate(msgs, req, bible=b_lvl, target_episode=target, kind=top)
            except Exception:
                log.exception("generation failed (lvl %s)", lvl)
                ans = "생성 오류"
            _post_chunks(channel, thread_ts, f"*🎚️ 강도 {lvl}단계*\n\n{ans}", replace_ts=(ph if first else None))
            first = False
        return

    # 강도 명시 안 했으면 기본 4로 고정
    bible = _ensure_default_intensity(bible, top)
    # 이번 요청을 명확한 지시로 정리(명령 구문 제거) + 넣고 싶은 포인트는 '재료'로 (반복 금지)
    req = f"'{work}' {what}를 생성해줘." + _notes_block(notes)
    _eff_lvl = (bible.get("intensity_map") or {}).get(top) or bible.get("intensity_level") if bible else None
    req = _with_prefs(req, work, top, level=_eff_lvl)   # 관련(강도 일치) 좋아/별로 피드백 주입
    messages[-1] = {"role": "user", "content": req}
    _CANCEL.discard(thread_ts)                    # 이전 취소 플래그 정리
    ph = _thinking(channel, thread_ts, f"{what} 초안 쓰는 중이에요…")
    try:
        answer = generator.generate(messages, req, bible=bible, target_episode=target, kind=top)
    except Exception:
        log.exception("generation failed")
        _post_chunks(channel, thread_ts, "생성 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
        return
    if _cancelled(channel, thread_ts, ph):
        return

    # 3단계 검증 관문: 생성과 분리된 감사자가 바이블 준수 재검.
    #  · 금지사항(이진 위반)만 자동 최소 교정, 나머지 위반은 ⚠️ 플래그로 알림만(작가가 직접 판단).
    # 바이블 없으면(패턴·사례 기반 생성) 기준이 없어 건너뜀. '검증생략'/COWRITER_VERIFY_GATE=0로 off.
    gate_note = ""
    if gate_on and bible:
        _update_note(channel, ph, f"{what} 초안 검증 중이에요… (바이블 준수 점검)")
        v = verify.verify_draft(answer, bible, target_episode=target, kind=top,
                                llm=generator.complete)
        if v["checked"] and v["fails"]:
            if _cancelled(channel, thread_ts, ph):
                return
            forbidden = [f for f in v["fails"] if f.get("name") == "금지사항"]
            flagged = [f for f in v["fails"] if f.get("name") != "금지사항"]
            if forbidden:   # 금지사항만 자동 교정 (지키거나 못 지키거나의 이진 규칙)
                _update_note(channel, ph, f"{what} 금지사항 위반 교정 중이에요… ({len(forbidden)}건)")
                answer = verify.correct_draft(answer, forbidden, bible, target_episode=target,
                                              kind=top, llm=generator.complete)
            parts = []
            if forbidden:
                names = ", ".join(f.get("name", "?") for f in forbidden)
                parts.append(f"🔧 자동검증: 금지사항 {len(forbidden)}건 교정 ({names})")
            if flagged:     # 나머지는 고치지 않고 플래그만 — 작가가 보고 판단
                lines = "\n".join(f"  • *{f.get('name', '?')}*: {(f.get('reason') or '').strip()}"
                                  for f in flagged)
                parts.append(f"⚠️ 바이블 확인 필요 {len(flagged)}건 (자동 수정 안 함 — 직접 확인하세요)\n{lines}")
            gate_note = "\n".join(parts)
        elif v["checked"]:
            gate_note = "✅ 자동검증: 바이블 준수 이상 없음"
        if _cancelled(channel, thread_ts, ph):
            return

    # 강도가 적용됐으면 답변 맨 앞에 표시
    _lvl = (bible.get("intensity_map") or {}).get(top) or bible.get("intensity_level") if bible else None
    if _lvl:
        answer = f"*🎚️ 강도 {_lvl}단계*\n\n" + answer
    if gate_note:
        answer += f"\n\n{gate_note}"

    # 슬랙은 초안 생성만. 시트 저장은 사람이 검토 후 [입력]/[수정]으로 직접.
    label = " / ".join(x for x in [top, mid, sub] if x)
    if label:
        answer += f"\n\n_📝 초안입니다. 확정하려면 `[입력] <{work}> {label}` 로 저장하세요._"
    _post_chunks(channel, thread_ts, answer, replace_ts=ph)


def _do_convert(channel: str, thread_ts: str, rest: str) -> None:
    """[변환]: 대충 쓴 줄글 상황 → 드라마 대본식 지문으로 구체화 (원문에 없는 내용 추가 금지)."""
    q = rest.strip()
    work, bible = None, None
    wm = SUB_RE.match(q)
    if wm:                              # <작품> 지정 시 인물 이름·호칭 참고
        work = wm.group(1).strip()
        q = wm.group(2).strip()
        sheet = reference.sheet()
        if sheet:
            try:
                bible = sheet.get(work)
            except Exception:
                log.exception("convert bible load failed")
    epm = re.search(r"(\d+)\s*화", q[:200])
    target = int(epm.group(1)) if epm else None
    draft = q
    if len(draft) < 10:                 # 본문 거의 없음 → 스레드 직전 봇 출력을 변환
        prior = [m["content"] for m in _thread_messages(channel, thread_ts)
                 if m["role"] == "assistant"]
        draft = prior[-1] if prior else ""
    if len(draft) < 5:
        _reply(channel, thread_ts,
               "줄글로 상황을 써서 `[변환]` 뒤에 붙이면 드라마 대본식 지문으로 바꿔드려요.\n"
               "예: `[변환] 휴대폰 들여다보는 연우, 화나서 나감`")
        return
    _CANCEL.discard(thread_ts)
    ph = _thinking(channel, thread_ts, "대본식 지문으로 바꾸는 중이에요…")
    try:
        answer = generator.complete(prompts.convert_system(bible, target_episode=target),
                                    prompts.convert_user(draft), timeout=300).strip()
    except Exception:
        log.exception("convert failed")
        _post_chunks(channel, thread_ts, "변환 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
        return
    if _cancelled(channel, thread_ts, ph):
        return
    _post_chunks(channel, thread_ts, answer or "(빈 응답)", replace_ts=ph)


def _sb_script_from_bible(bible: dict | None, episode: int | None) -> str:
    """시트 바이블(노션 동기화분)에서 그 화의 '대본' 원문을 가져온다. 없으면 ''."""
    if not bible or not episode:
        return ""
    return ((bible.get("scripts") or {}).get(f"{episode}화") or "").strip()


def _do_storyboard(channel: str, thread_ts: str, rest: str) -> None:
    """[스토리보드] 1단계 = 씬 설계 (씬 분할 + 시간 배치)만 낸다.
    상세 스토리보드는 이 스레드에서 씬 설계를 손본 뒤 「생성」이라고 하면 2단계로 만든다.
    입력 우선순위: 붙여넣은 대본 → 시트(노션) 그 화 대본 → 스레드 직전 봇 출력."""
    q = rest.strip()
    work, bible = None, None
    wm = SUB_RE.match(q)
    if wm:                              # <작품> 지정 시 시트 바이블(대본·인물 등) 참고
        work = wm.group(1).strip()
        q = wm.group(2).strip()
        sheet = reference.sheet()
        if sheet:
            try:
                bible = sheet.get(work)
            except Exception:
                log.exception("storyboard bible load failed")
    # 대상 화: 명령에 'N화'가 있으면 그 화, 없으면 바이블 진행상태(대본→개요) 화
    epm = re.search(r"(\d+)\s*화", q[:200])
    target = int(epm.group(1)) if epm else _progress_episode(bible, ["대본", "개요"])
    draft = q
    src_note = ""
    if len(draft) < 10:                 # 붙여넣기 없음 → 시트(노션) 그 화 대본 → 스레드 직전 봇 출력
        sheet_script = _sb_script_from_bible(bible, target)
        if sheet_script:
            draft = sheet_script
            src_note = f"{target}화 대본을 시트(노션)에서 불러와 "
        else:
            prior = [m["content"] for m in _thread_messages(channel, thread_ts)
                     if m["role"] == "assistant"]
            draft = prior[-1] if prior else ""
    if len(draft) < 5:
        _reply(channel, thread_ts,
               "대본을 못 찾았어요. 아래 중 하나로 주세요:\n"
               "• `[스토리보드] <날혐남> 3화` — 노션(시트)에 저장된 그 화 대본을 자동으로 불러와요\n"
               "• `[스토리보드] <날혐남>` 뒤에 대본을 직접 붙여넣기\n"
               "• 대본을 방금 뽑은 스레드에서 `[스토리보드]`만 치기")
        return
    _CANCEL.discard(thread_ts)
    ph = _thinking(channel, thread_ts, src_note + "씬을 어떻게 나눌지(분할·시간) 설계하는 중이에요…")
    try:
        answer = generator.complete(prompts.storyboard_plan_system(bible, target_episode=target),
                                    prompts.storyboard_plan_user(draft), timeout=300).strip()
    except Exception:
        log.exception("storyboard plan failed")
        _post_chunks(channel, thread_ts, "씬 설계 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
        return
    if _cancelled(channel, thread_ts, ph):
        return
    _post_chunks(channel, thread_ts, SB_BADGE_PLAN + (answer or "(빈 응답)"), replace_ts=ph)


def _thread_origin_mode(messages: list[dict]) -> str:
    """스레드를 시작한 명령이 뭔지 → 후속 답글을 그 모드로 이어가기 위함."""
    for m in messages:
        if m["role"] != "user":
            continue
        cm = CMD_RE.match(m["content"])
        if not cm:
            continue
        cmd = cm.group(1).strip()
        if cmd in CMD_IDEA:
            return "idea"
        if cmd in CMD_TREND:
            return "trend"
        if cmd in CMD_FB_FUN:
            return "fun"
        if cmd in CMD_FB_LOGIC:
            return "logic"
        if cmd in CMD_FEEDBACK:
            return "feedback"
        if cmd in CMD_STORYBOARD:
            return "sb"
        if cmd in CMD_PLAN:
            return "plan"
        if cmd in CMD_GEN:
            return "gen"
    return "gen"


def _sb_stage(messages: list[dict]) -> str:
    """스토리보드 스레드가 지금 '설계안(plan)' 단계인지 '작가 확인용 스토리보드(detail)' 단계인지.
    가장 최근 봇 결과물이 스토리보드면 'detail', 씬 설계안이면 'plan'."""
    for m in reversed(messages):
        if m["role"] != "assistant":
            continue
        c = m["content"]
        if "[2/3단계]" in c or "화면(쉽게)" in c:   # 작가 확인용 스토리보드 (배지 우선, 본문 마커 폴백)
            return "detail"
        if "[1/3단계]" in c or "씬 설계안" in c:   # 씬 설계안
            return "plan"
    return "plan"


def _trend_orient(text: str) -> str | None:
    """텍스트에서 트렌드 성향(BL/GL/로맨스) 감지 — 스레드 후속에 성향 이어붙이기용."""
    if re.search(r"(?<![a-z])bl(?![a-z])", text or "", re.I):
        return "BL"
    if re.search(r"(?<![a-z])gl(?![a-z])", text or "", re.I) or "백합" in (text or ""):
        return "GL"
    if "로맨스" in (text or "") or "남녀" in (text or ""):
        return "로맨스"
    return None


def _convo_text(messages: list[dict]) -> str:
    lines = [f"[{'작가' if m['role'] == 'user' else '봇'}] {m['content']}" for m in messages]
    return "\n".join(lines) + "\n\n(위 대화 흐름을 그대로 이어서 답하라.)"


def _do_revise(channel: str, thread_ts: str, feedback: str) -> None:
    """스레드 후속 답글 → 스레드를 시작한 명령의 모드로 이어감 (아이디어는 아이디어, 생성은 수정 등)."""
    messages = _thread_messages(channel, thread_ts)
    if not messages:
        _reply(channel, thread_ts, _HELP)
        return
    joined = "\n".join(m["content"] for m in messages)
    wm = re.search(r"<\s*([^>]+?)\s*>", joined)
    work = wm.group(1).strip() if wm else None
    em = re.search(r"(\d+)\s*화", feedback) or re.search(r"(\d+)\s*화", joined)
    target = int(em.group(1)) if em else None
    bible = None
    if work:
        sheet = reference.sheet()
        if sheet:
            try:
                bible = sheet.get(work)
            except Exception:
                log.exception("revise bible load failed")

    bible = _override_intensity(bible, feedback)   # '강도 N으로 바꿔' → 이번 수정만 그 레벨로 재보정
    mode = _thread_origin_mode(messages)
    _CANCEL.discard(thread_ts)
    # 스토리보드 스레드: 지금 설계안(plan) 단계인지 상세(detail) 단계인지에 따라 후속 처리가 다름
    sb_stage = _sb_stage(messages) if mode == "sb" else None
    #  · 설계안 단계 + 「생성」 → 상세로 전개 / 그 외 설계안 단계 → 설계안 수정 / 상세 단계 → 상세 수정
    sb_generate = sb_stage == "plan" and bool(SB_GEN_RE.match(feedback.strip()))
    # 2단계(상세/수정)는 스레드에 안 실린 '원본 대본'(시트)을 다시 물려 충실도 유지
    sb_ref = _sb_script_from_bible(bible, target) if mode == "sb" else ""
    sb_ref_block = (f"\n\n[원본 대본 — 이 사건·행동·대사를 하나도 바꾸지 말고 그대로 씬에 반영하라]\n{sb_ref}"
                    if sb_ref else "")
    note = {"idea": "아이디어 이어가는 중이에요…", "trend": "트렌드 이어보는 중이에요…",
            "plan": "기획안 다듬는 중이에요…",
            "sb": "씬 설계 다듬는 중이에요…"}.get(mode, "수정하는 중이에요…")
    if sb_generate:
        note = "확정한 씬 설계로 작가 확인용 스토리보드 만드는 중이에요… (몇 초~1분)"
    elif sb_stage == "detail":
        note = "스토리보드 고치는 중이에요…"
    ph = _thinking(channel, thread_ts, note)

    try:
        if mode == "sb" and sb_generate:
            # 1단계→2단계: 확정된 '씬 설계안'의 씬 수·순서·시간을 그대로 지켜, 각 씬을 6칸 작가 확인용으로.
            answer = generator.complete(
                prompts.storyboard_system(bible, target_episode=target),
                _convo_text(messages) + sb_ref_block
                + "\n\n(위 대화에서 마지막으로 확정된 '씬 설계안'의 씬 수·순서·각 씬 시간을 그대로 지켜라. "
                  "각 씬을 6칸(제목·길이·장소·등장·화면(쉽게)·대사) 작가 확인용 스토리보드로 전개하되, "
                  "[원본 대본]의 사건·행동·대사는 하나도 바꾸지 마라. 카메라·조명은 넣지 마라.)",
                timeout=300)
            answer = SB_BADGE_BOARD + answer
        elif mode == "sb" and sb_stage == "detail":
            # 스토리보드가 이미 나온 뒤의 후속 피드백 → 바뀐 씬만 6칸 블록으로 재출력
            answer = generator.complete(
                prompts.storyboard_system(bible, target_episode=target),
                _convo_text(messages) + sb_ref_block
                + "\n\n(위 스토리보드에서 마지막 작가 요청대로 **바뀐 씬만** 6칸 블록으로 내라 "
                  "— 안 바뀐 씬은 다시 쓰지 말고, 맨 위에 '바꾼 점:' 한 줄. [원본 대본]과 어긋나지 않게, 대본 내용은 바꾸지 마라. 전체 재출력 금지.)",
                timeout=300)
            answer = SB_BADGE_BOARD + answer
        elif mode == "sb":
            # 설계안 단계: 씬 설계안만 피드백대로 수정 (상세로 넘어가지 않음). 바뀐 씬만 출력.
            answer = generator.complete(
                prompts.storyboard_plan_system(bible, target_episode=target),
                _convo_text(messages)
                + "\n\n(이번은 '씬 설계안 수정' 요청이다. 위에 이미 낸 설계안에서 **바뀐 씬만** 내라 "
                  "— 안 바뀐 씬은 다시 쓰지 말고, 맨 위에 '바꾼 점:' 한 줄. 전체 설계안 재출력 금지.)")
            answer = SB_BADGE_PLAN + answer
        elif mode == "plan":
            # 기획안 스레드 후속 → 대화 맥락 유지하며 요청대로 수정, 기획안 전체본 재출력
            answer = generator.complete(prompts.plan_system(joined), _convo_text(messages))
        elif mode == "idea":
            bible_i = _idea_intensity(bible, feedback)   # 아이디어 기본 강도 3 고정
            answer = generator.complete(prompts.idea_system(bible_i, feedback, target_episode=target),
                                        _convo_text(messages))
        elif mode == "trend":
            trend = reference.load_trend()
            # 스레드가 특정 성향(BL/GL/로맨스)으로 시작했으면 후속도 그 성향 데이터로 유지
            # (후속 텍스트에 성향어가 없으면 앞 대화의 성향을 앞에 붙여 스코프가 이어지게)
            tq = feedback
            o_prev, o_now = _trend_orient(joined), _trend_orient(feedback)
            if o_prev and not o_now:
                tq = f"{o_prev} {feedback}"
            raw = trend.answer(tq, llm=_trend_filter_llm) if trend else ""
            sys = prompts.trend_system(bible)
            answer = generator.complete(sys, _convo_text(messages)
                                        + f"\n\n[측정 데이터 참고]\n{raw}")
        elif mode in ("fun", "logic", "feedback"):
            # 피드백 대화 후속은 그 맥락에서 자유 답변
            answer = generator.complete(prompts.feedback_system(bible, target_episode=target,
                                        mode=(mode if mode in ("fun", "logic") else "both")),
                                        _convo_text(messages))
        else:  # gen — 초안 수정
            answer = generator.generate(messages, feedback, bible=bible, target_episode=target)
            if work:
                answer += "\n\n_📝 초안입니다. 확정은 `[입력]`/`[수정]` 으로._"
    except Exception:
        log.exception("revise failed")
        _post_chunks(channel, thread_ts, "이어가는 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
        return
    if _cancelled(channel, thread_ts, ph):
        return
    _post_chunks(channel, thread_ts, answer or "(빈 응답)", replace_ts=ph)


def _do_plan(channel: str, thread_ts: str, rest: str) -> None:
    """[기획] 컨셉·로그라인 → 노션 기획안 구조 초안 (로그라인·타겟·인물·줄거리·회차분배). 초안만, 자동저장 X."""
    concept = rest.strip()
    # 스레드에서 [기획]을 치면 그 스레드 대화(트렌드·아이디어 논의 등)를 근거로 삼는다.
    messages = _thread_messages(channel, thread_ts)
    thread_ctx = _convo_text(messages) if len(messages) > 1 else ""
    if len(concept) < 3 and not thread_ctx:
        _reply(channel, thread_ts,
               "형식: `[기획] <컨셉/로그라인/키워드>`\n"
               "예: `[기획] 라이벌 아이돌 룸메이트 BL, 스캔들 나면 끝장`\n"
               "(트렌드·아이디어 스레드에서 `[기획]`만 쳐도 위 대화로 기획안을 짭니다.)")
        return
    seed = concept if len(concept) >= 3 else " ".join(m["content"] for m in messages)[:300]
    trend_ctx = ""
    orient = _trend_orient(concept) or _trend_orient(seed)   # 성향(BL/GL/로맨스)이면 트렌드 근거
    if orient:
        trend = reference.load_trend()
        if trend:
            try:
                trend_ctx = trend.answer(orient)
            except Exception:
                log.exception("plan trend ctx failed")
    if thread_ctx:
        user_msg = (f"{thread_ctx}\n\n[요청]\n위 대화 흐름·아이디어를 바탕으로 기획안 초안을 만들어줘."
                    + (f" 특히 이 방향으로: {concept}" if len(concept) >= 3 else ""))
    else:
        user_msg = f"이 컨셉으로 기획안 초안을 만들어줘:\n{concept}"
    _CANCEL.discard(thread_ts)
    ph = _thinking(channel, thread_ts, "기획안 초안 짜는 중이에요… (몇 초~1분)")
    try:
        answer = generator.complete(prompts.plan_system(seed, trend_ctx), user_msg).strip()
    except Exception:
        log.exception("plan failed")
        _post_chunks(channel, thread_ts, "기획안 생성 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
        return
    if _cancelled(channel, thread_ts, ph):
        return
    answer += "\n\n_📝 기획안 초안입니다. 다듬어서 노션에 넣고 `[생성]`으로 개요·대본을 뽑으세요._"
    _post_chunks(channel, thread_ts, answer or "(빈 응답)", replace_ts=ph)


def _do_idea(channel: str, thread_ts: str, rest: str) -> None:
    """[아이디어 제시] — 추상적 고민을 구체적이고 간단한 상황 2~3개로. 작품 바이블+DB 근거."""
    q = rest.strip()
    work, bible = None, None
    wm = SUB_RE.match(q)
    if wm:
        work = wm.group(1).strip()
        q = wm.group(2).strip()
        sheet = reference.sheet()
        if sheet:
            try:
                bible = sheet.get(work)
            except Exception:
                log.exception("idea bible load failed")
    if not q:
        _reply(channel, thread_ts,
               "형식: `[아이디어] <작품> 4화에서 서아가 힘든 걸 보여주고 싶은데 어떻게?`\n"
               "추상적 고민을 주면 구체적인 상황을 제안해요. (N화를 넣으면 그 회차 흐름에 맞춰요)")
        return
    em = re.search(r"(\d+)\s*화", q)              # 질문에 회차가 있으면 그 화 흐름 앵커
    target = int(em.group(1)) if em else _progress_episode(bible, ["대본", "개요"])
    bible = _override_intensity(bible, q)     # 질문에 '강도 N' 있으면 그게 우선
    bible = _idea_intensity(bible, q)         # 아이디어는 기본 강도 3 고정(작품 전체 강도에 안 끌림)
    system = prompts.idea_system(bible, q, target_episode=target)
    _CANCEL.discard(thread_ts)
    ph = _thinking(channel, thread_ts, "아이디어 짜는 중이에요…")
    try:
        answer = generator.complete(system, q).strip()
    except Exception:
        log.exception("idea failed")
        _post_chunks(channel, thread_ts, "아이디어 생성 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
        return
    if _cancelled(channel, thread_ts, ph):
        return
    _post_chunks(channel, thread_ts, answer or "(빈 응답)", replace_ts=ph)


def _json_loads(raw: str) -> dict:
    """LLM 응답에서 JSON 객체만 안전하게 추출."""
    s = re.sub(r"^```(json)?", "", raw.strip()).strip()
    s = re.sub(r"```$", "", s).strip()
    a, b = s.find("{"), s.rfind("}")
    if a >= 0 and b > a:
        s = s[a:b + 1]
    return json.loads(s)


# 동기화: 단일 필드 → (대분류, 중분류, 소분류)
_SYNC_SINGLE = {
    "진행상태": ("진행상태", "", ""),
    "로그라인": ("로그라인/키워드", "로그라인", ""),
    "키워드": ("로그라인/키워드", "키워드", ""),
    "타겟층": ("타겟층/핵심정서", "타겟층", ""),
    "핵심정서": ("타겟층/핵심정서", "핵심정서", ""),
    "금지사항": ("금지사항", "", ""),
    "강도": ("강도", "", ""),
    "줄거리": ("줄거리", "", ""),
}


def _do_sync(channel: str, thread_ts: str, rest: str) -> None:
    """[동기화] <작품> (노션 내용 붙여넣기) → LLM이 스키마로 파싱 → 시트 upsert."""
    from bot.sheet_bible import CHAR_SUBS
    sheet = reference.sheet()
    if not sheet:
        _reply(channel, thread_ts, "시트가 아직 연결 안 됐어요 (SHEET_WEBAPP_URL 미설정).")
        return
    sm = SUB_RE.match(rest.strip())
    if not sm:
        _reply(channel, thread_ts, "형식: `[동기화] <작품>` 하고 아래에 노션 내용을 통째로 붙여넣어 주세요.")
        return
    work = sm.group(1).strip()
    content = sm.group(2).strip()
    src = "붙여넣은 내용"
    # 붙여넣기 없으면 → 등록된 노션 페이지를 토큰으로 직접 읽는다.
    page_id = (config.NOTION_PAGES or {}).get(work)
    if len(content) < 50 and config.NOTION_TOKEN and page_id:
        _CANCEL.discard(thread_ts)
        ph0 = _thinking(channel, thread_ts, "노션 페이지 읽는 중이에요…")
        try:
            from bot import notion_sync
            content = notion_sync.page_text(page_id)
            src = "노션 페이지"
        except Exception:
            log.exception("notion page fetch failed")
            _post_chunks(channel, thread_ts,
                         "노션 페이지를 못 읽었어요. 페이지가 통합에 연결됐는지 확인해 주세요.", replace_ts=ph0)
            return
    if len(content) < 50:
        hint = "" if page_id else "\n(또는 이 작품 노션 페이지를 등록하면 `[동기화] <작품>`만으로 자동 반영돼요.)"
        _reply(channel, thread_ts,
               "동기화할 노션 내용을 `<작품>` 뒤에 붙여넣어 주세요 (줄거리·인물·회차분배·개요 등)." + hint)
        return
    _CANCEL.discard(thread_ts)
    ph = _thinking(channel, thread_ts, f"{src} 정리해서 시트에 반영하는 중이에요…")
    try:
        done, failed, summary = _sync_apply(sheet, work, content)
    except ValueError:   # JSON 파싱 실패
        _post_chunks(channel, thread_ts,
                     "노션 내용을 구조로 못 읽었어요. 소제목(줄거리/등장인물/회차분배/개요)이 있으면 더 잘 됩니다.",
                     replace_ts=ph)
        return
    except Exception:
        log.exception("sync failed")
        _post_chunks(channel, thread_ts, "동기화 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
        return
    if _cancelled(channel, thread_ts, ph):
        return
    if not done:
        _post_chunks(channel, thread_ts,
                     "동기화했지만 반영된 항목이 없어요. 노션 내용/소제목을 확인해 주세요.", replace_ts=ph)
        return
    msg = f"✅ *{work}* {src} 동기화 — {done}개 반영.\n· " + "\n· ".join(summary)
    if failed:
        msg += f"\n⚠️ {failed}개는 네트워크 문제로 실패 — 다시 `[동기화]` 하면 그 부분만 채워집니다."
    _post_chunks(channel, thread_ts, msg, replace_ts=ph)


def _sync_apply(sheet, work: str, content: str) -> tuple[int, int, list]:
    """동기화 소스 텍스트 → LLM 스키마 파싱 → 시트 upsert. 슬랙 무관(백그라운드 재사용).
    반환 (done, failed, summary). JSON 파싱 실패 시 ValueError."""
    from bot.sheet_bible import CHAR_SUBS
    raw = generator.complete(prompts.SYNC_SYSTEM + content,
                             "위 문서를 스키마 JSON으로 변환하라.", timeout=600)
    data = _json_loads(raw)   # 실패 시 예외 → 호출부가 ValueError로 처리
    done = failed = 0
    summary: list = []

    def _up(top, mid, sub, val):
        nonlocal done, failed
        try:
            sheet.upsert(work, top, mid, sub, val); done += 1
        except Exception:
            failed += 1
            log.warning("sync upsert 실패: %s/%s/%s", top, mid, sub)

    for key, (top, mid, sub) in _SYNC_SINGLE.items():
        v = data.get(key)
        v = v.strip() if isinstance(v, str) else v
        if v:
            _up(top, mid, sub, v if isinstance(v, str) else str(v)); summary.append(key)
    chars = [r for r in (data.get("등장인물") or []) if (r.get("이름") or "").strip()]
    for r in chars:
        for k in CHAR_SUBS:
            if r.get(k):
                _up("등장인물", r["이름"].strip(), k, str(r[k]).strip())
    if chars:
        summary.append(f"인물 {len(chars)}명")
    plan = [r for r in (data.get("회차분배") or []) if (r.get("막") or "").strip()]
    for r in plan:
        for k in ("구간", "화수", "핵심사건"):
            if r.get(k):
                _up("회차분배", r["막"].strip(), k, str(r[k]).strip())
    if plan:
        summary.append(f"회차분배 {len(plan)}막")
    outs = [r for r in (data.get("개요") or []) if (r.get("화") or "").strip() and r.get("내용")]
    for r in outs:
        _up("개요", r["화"].strip(), "", str(r["내용"]).strip())
    if outs:
        summary.append(f"개요 {len(outs)}화")
    scr = [r for r in (data.get("대본") or []) if (r.get("화") or "").strip() and r.get("내용")]
    for r in scr:
        _up("대본", r["화"].strip(), "", str(r["내용"]).strip())
    if scr:
        summary.append(f"대본 {len(scr)}화")

    sheet.invalidate(work)
    return done, failed, summary


# [재미] 6기준 (①몰입 ②명확 ③기대 ④속도 ⑤주체 ⑥강약) + 가중치(합 125 → 100 환산)
_FUN_LABELS = ["몰입", "명확", "기대", "속도", "주체", "강약"]
_FUN_WEIGHTS = [25, 20, 25, 20, 5, 25]


def _verify_fun_score(text: str) -> str:
    """LLM이 매긴 6개 항목 점수를 코드로 가중합·환산해 종합점수를 맨 위에 붙인다(산수 오류 방지)."""
    scores = re.findall(r"(\d+)\s*/\s*10", text)   # '[8/10]' 또는 '점수 8/10' 모두
    if len(scores) < 6:
        return text
    s = [int(x) for x in scores[:6]]
    total = sum(a * b for a, b in zip(s, _FUN_WEIGHTS)) / sum(_FUN_WEIGHTS) * 10
    # 실무자 핵심 질문: "시청자가 봤을 때 재밌을까?" → 한눈 판정
    fun = ("🔥 재밌음" if total >= 75 else
           "🙂 볼 만함" if total >= 60 else
           "😐 애매함" if total >= 45 else "😴 재미없음")
    verdict = ("그대로 가도 됨" if total >= 85 else
               "이것만 고치면 됨" if total >= 70 else
               "약점 2개 이상 수술 필요" if total >= 50 else "구조부터 다시")
    detail = "·".join(f"{lbl}{v}" for lbl, v in zip(_FUN_LABELS, s))
    banner = f"*{fun}*  ·  종합 {total:.0f}/100 — {verdict}\n_({detail})_\n\n"
    return banner + text


def _do_feedback(channel: str, thread_ts: str, rest: str, mode: str = "both") -> None:
    """[피드백] 대본 평가. mode='both'(재미+개연성)/'fun'(재미만)/'logic'(개연성만)."""
    q = rest.strip()
    work, bible = None, None
    wm = SUB_RE.match(q)
    if wm:
        work = wm.group(1).strip()
        q = wm.group(2).strip()
        sheet = reference.sheet()
        if sheet:
            try:
                bible = sheet.get(work)
            except Exception:
                log.exception("feedback bible load failed")
    # 강도 렌즈: '강도 1~5/전체' → 5관점, '강도 N' → 그 관점. 대본에서 지시 줄 제거.
    if re.search(r"강도\s*(전체|전부|모두|비교|1\s*[~\-]\s*5|1to5)", q):
        lens_levels = [1, 2, 3, 4, 5]
    else:
        ms = re.search(r"강도\s*([1-5])", q)
        lens_levels = [int(ms.group(1))] if ms else None
    q = re.sub(r"(?m)^\s*강도\s*(전체|전부|모두|비교|1\s*[~\-]\s*5|1to5|[1-5])[^\n]*$", "", q).strip()
    # 명령에 강도 없으면 시트 저장 강도, 그것도 없으면 기본 4 관점으로 (라벨 표시됨)
    if lens_levels is None:
        stored = ((bible.get("intensity_map") or {}).get("재미") or bible.get("intensity_level")) if bible else None
        lens_levels = [stored or DEFAULT_INTENSITY]

    ep_cmd = re.search(r"(\d+)\s*화", q)                 # 명령에 'N화'가 있으면 그 화
    want_outline = ("개요" in q) and ("대본" not in q)   # '개요' 명시 → 개요, 기본은 대본
    src_kind = ""
    draft = q
    if len(draft) < 30:  # 대본 미첨부 → 스레드 직전 봇 대본/초안
        prior = [m["content"] for m in _thread_messages(channel, thread_ts) if m["role"] == "assistant"]
        draft = prior[-1] if prior else ""
    if len(draft) < 30 and bible and ep_cmd:            # 그래도 없으면 → 시트 저장본(개요/대본) 사용
        key = "outlines" if want_outline else "scripts"
        saved = (bible.get(key) or {}).get(f"{ep_cmd.group(1)}화", "")
        if len(saved.strip()) >= 30:
            draft = saved
            src_kind = f"시트의 {ep_cmd.group(1)}화 {'개요' if want_outline else '대본'}"
    if len(draft) < 30:
        _reply(channel, thread_ts,
               "형식: `[피드백] <작품> (대본 붙여넣기)` 또는 `[피드백] <작품> N화`"
               " (시트 저장본으로 평가 · 기본 대본, `N화 개요`라 쓰면 개요).")
        return
    em = re.search(r"(\d+)\s*화", draft[:200])   # 대본 앞부분에 회차가 있으면 그 흐름 앵커
    target = (ep_cmd and int(ep_cmd.group(1))) or (int(em.group(1)) if em else _progress_episode(bible, ["대본", "개요"]))
    _CANCEL.discard(thread_ts)
    note = {"fun": "재미 보는 중이에요…", "logic": "개연성 보는 중이에요…"}.get(mode, "대본 보는 중이에요…")
    if src_kind:
        note = f"{src_kind} 읽고 {note}"
    ph = _thinking(channel, thread_ts, note)
    first = True

    def _run(sys_text: str, user_text: str, post_fn=None):
        nonlocal first
        try:
            ans = generator.complete(sys_text, user_text, timeout=300).strip()  # 전체 대본 대비
        except Exception:
            log.exception("feedback failed")
            _post_chunks(channel, thread_ts, "피드백 생성 중 오류가 났어요. 잠시 후 다시 시도해 주세요.",
                         replace_ts=(ph if first else None))
            first = False
            return
        if post_fn:
            ans = post_fn(ans)
        _post_chunks(channel, thread_ts, ans or "(빈 응답)", replace_ts=(ph if first else None))
        first = False

    if mode in ("fun", "both"):     # v2.1 점수제 재미 평가 (+ 코드로 종합점수 검증)
        if lens_levels:             # 강도 관점별 재미 (1개 또는 1~5개)
            for lvl in lens_levels:
                if _cancelled(channel, thread_ts, ph if first else None):
                    return
                b_lvl = _override_intensity(bible, f"강도 {lvl}")
                _run(prompts.fun_system(b_lvl, target_episode=target),
                     prompts.fun_user(draft, lens_level=lvl),
                     post_fn=(lambda a, L=lvl: f"*🎚️ 강도 {L}단계 관점*\n\n" + _verify_fun_score(a)))
        else:
            _run(prompts.fun_system(bible, target_episode=target), prompts.fun_user(draft), post_fn=_verify_fun_score)
        if _cancelled(channel, thread_ts, ph if first else None):
            return
    if mode in ("logic", "both"):   # 개연성 지적 (엄격도: 명령 강도 N > 시트 개연성 강도)
        strict = (lens_levels[0] if lens_levels and len(lens_levels) == 1 else None)
        if strict is None and bible:
            strict = (bible.get("intensity_map") or {}).get("개연성")
        sys_text = prompts.feedback_system(bible, target_episode=target, mode="logic", strictness=strict)
        _run(sys_text, "‼️ 아래 [대본]에 실제로 적힌 것만 검토하라. [작품 바이블]은 대조용 배경일 뿐, "
                       f"그 줄거리·개요를 대본으로 착각하지 마라.\n\n[평가할 대본]\n{draft}")


def _trend_filter_llm(system: str, user: str) -> str:
    """트렌드 필터 분류용 단발 LLM 콜 (alias 미스 폴백). 짧은 타임아웃 — 분류가
    트렌드 응답 전체를 지연시키지 않게. 실패해도 answer()가 None 처리 → 전체 트렌드."""
    return generator.complete(system, user, timeout=30)


def _do_trend(channel: str, thread_ts: str, rest: str) -> None:
    """[트렌드] — 측정 데이터를 근거로, 쉬운 말 요약 + (작품 지정 시) 맞춤 아이디어 제안."""
    trend = reference.load_trend()
    if trend is None:
        _reply(channel, thread_ts, "트렌드 DB가 아직 없어요. `sync_reference.py`로 데이터 반영 후 다시 물어봐 주세요.")
        return
    q = rest.strip()
    # <작품> 지정 시 그 바이블을 근거로 맞춤 아이디어
    work, bible = None, None
    wm = SUB_RE.match(q)
    if wm:
        work = wm.group(1).strip()
        q = wm.group(2).strip()
        sheet = reference.sheet()
        if sheet:
            try:
                bible = sheet.get(work)
            except Exception:
                log.exception("trend bible load failed")
    try:
        raw = trend.answer(q or "트렌드", llm=_trend_filter_llm)  # 집계 데이터 (참고용, 사용자엔 안 보임)
    except Exception:
        log.exception("trend agg failed")
        _reply(channel, thread_ts, "트렌드 집계 중 오류가 났어요.")
        return
    system = prompts.trend_system(bible)
    user = (f"[작가 질문]\n{q or '요즘 뭐가 유행이야? 우리한테 쓸 만한 아이디어도 알려줘.'}\n\n"
            f"[측정 데이터 — 참고만, 수치·표를 그대로 옮기지 마라]\n{raw}")
    _CANCEL.discard(thread_ts)
    ph = _thinking(channel, thread_ts, "트렌드 정리하는 중이에요…")
    try:
        answer = generator.complete(system, user).strip() or raw
    except Exception:
        log.exception("trend summarize failed")
        answer = raw                                # 폴백: 원본 집계라도 보여줌
    if _cancelled(channel, thread_ts, ph):
        return
    _post_chunks(channel, thread_ts, answer, replace_ts=ph)


def _hwpx_text(raw: bytes) -> str:
    """.hwpx(ZIP+XML, OWPML) → 본문 텍스트만. 표준 라이브러리만 사용(서식·표는 버림).
    본문은 Contents/section*.xml 의 <hp:t> 런에 있고 문단은 <hp:p>. 실패 시 빈 문자열."""
    import io
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception:
        return ""
    names = sorted(n for n in zf.namelist()
                   if re.match(r"Contents/section\d+\.xml$", n))
    chunks = []
    for n in names:
        try:
            xml = zf.read(n).decode("utf-8", "replace")
        except Exception:
            continue
        xml = re.sub(r"</(?:\w+:)?p>", "\n", xml)                       # 문단 끝 → 줄바꿈
        xml = re.sub(r"<(?:\w+:)?t>(.*?)</(?:\w+:)?t>", r"\1", xml, flags=re.S)  # 텍스트 런 언랩
        xml = re.sub(r"<[^>]+>", "", xml)                               # 나머지 태그 제거(줄바꿈 유지)
        for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
                     ("&quot;", '"'), ("&apos;", "'")):
            xml = xml.replace(a, b)
        lines = [ln.strip() for ln in xml.split("\n")]
        text = "\n".join(ln for ln in lines if ln)                      # 빈 줄 정리, 문단 유지
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def _decode_text(data: bytes) -> str:
    """첨부 텍스트 디코딩. 한글 .txt는 윈도우 저장 시 CP949/EUC-KR가 흔해
    UTF-8만 쓰면 다 깨진다 → BOM·UTF-8 → CP949 → UTF-16 순으로 시도."""
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr", "utf-16"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")   # 최후: 깨져도 최대한


def _files_text(event: dict) -> tuple[str, int]:
    """메시지에 붙은 스니펫/텍스트/.hwpx 파일 내용을 봇 토큰으로 내려받아 합친다.
    반환: (내용, blocked) — blocked>0이면 권한 부족으로 로그인 HTML만 받은 것."""
    out, blocked = [], 0
    for f in event.get("files") or []:
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        name = (f.get("name") or "").lower()
        ftype = (f.get("filetype") or "").lower()
        try:
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
        except Exception:
            log.exception("첨부 파일 다운로드 실패")
            blocked += 1
            continue
        if name.endswith(".hwpx") or ftype == "hwpx":          # 신형 한글 = ZIP+XML → 텍스트 추출
            txt = _hwpx_text(data)
            if txt:
                out.append(txt)
            else:
                log.warning("hwpx 본문 추출 실패(빈 결과)")
            continue
        body = _decode_text(data)
        if body.lstrip()[:200].lower().find("<!doctype html") >= 0 or body.lstrip().lower().startswith("<html"):
            log.warning("첨부 다운로드가 로그인 HTML 반환 — files:read 권한 필요")
            blocked += 1
            continue
        out.append(body)
    return "\n".join(out).strip(), blocked


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
    # 스니펫/파일 첨부가 있으면 그 내용을 명령 뒤에 이어붙임 (긴 대본·노션 문서용)
    ft, blocked = _files_text(event)
    if blocked and not ft:   # 권한 부족으로 파일을 못 읽음
        _reply(channel, thread_ts,
               "⚠️ 첨부 파일을 못 읽었어요 — Slack 앱에 *files:read* 권한이 필요해요.\n"
               "설정(OAuth & Permissions)에서 권한 추가 후 재설치하거나, 스니펫 대신 **채팅에 직접 붙여넣어** 주세요.")
        return
    rest_f = (rest + "\n" + ft) if ft else rest

    if cmd in CMD_RELOAD:
        pulled = _reference_pull()               # 형제 repo에서 최신 받아오기 (있으면)
        reference.reload()
        _reply(channel, thread_ts,
               ("최신 레퍼런스를 받아 " if pulled else "") + "레퍼런스 DB·템플릿을 다시 불러왔어요.")
    elif cmd in CMD_REFRESH:
        sheet = reference.sheet()
        if sheet:
            sheet.invalidate()
        _reply(channel, thread_ts, "시트 바이블 캐시를 비웠어요. 다음 요청부터 최신으로 읽어옵니다.")
    elif cmd in CMD_CONVERT:
        _do_convert(channel, thread_ts, rest_f)
    elif cmd in CMD_STORYBOARD:
        _do_storyboard(channel, thread_ts, rest_f)
    elif cmd in CMD_TREND:
        _do_trend(channel, thread_ts, rest)
    elif cmd in CMD_SYNC:
        _do_sync(channel, thread_ts, rest_f)
    elif cmd in CMD_IDEA:
        _do_idea(channel, thread_ts, rest)
    elif cmd in CMD_PLAN:
        _do_plan(channel, thread_ts, rest_f)
    elif cmd in CMD_FEEDBACK:
        _do_feedback(channel, thread_ts, rest_f, mode="both")
    elif cmd in CMD_FB_FUN:
        _do_feedback(channel, thread_ts, rest_f, mode="fun")
    elif cmd in CMD_FB_LOGIC:
        _do_feedback(channel, thread_ts, rest_f, mode="logic")
    elif cmd in CMD_STOP:
        _do_stop(channel, thread_ts)
    elif cmd in CMD_LIKE:
        _do_pref(channel, thread_ts, rest, "+")
    elif cmd in CMD_DISLIKE:
        _do_pref(channel, thread_ts, rest, "-")
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


_NOTION_POLL_SEC = 600                                   # 노션 변경 확인 주기(초)
_NOTION_STATE = config.BASE_DIR / "data" / "notion_state.json"


def _load_notion_state() -> dict:
    try:
        return json.loads(_NOTION_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_notion_state(st: dict) -> None:
    try:
        _NOTION_STATE.parent.mkdir(parents=True, exist_ok=True)
        _NOTION_STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.warning("notion_state 저장 실패")


def _reference_pull() -> bool:
    """레퍼런스 소스 repo(story-v1-scripts)를 git pull. HEAD가 바뀌면 캐시 리로드 후 True.
    봇은 사본이 아니라 이 repo의 reference/를 직접 읽으므로, pull만 하면 최신이 반영됨."""
    import subprocess
    repo = config.REFERENCE_DIR.parent            # .../story-v1-scripts/reference → repo 루트
    if not (repo / ".git").exists():
        return False

    def _head() -> str:
        try:
            return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=15).stdout.strip()
        except Exception:
            return ""

    before = _head()
    try:
        subprocess.run(["git", "-C", str(repo), "pull", "--ff-only"],
                       capture_output=True, text=True, timeout=60)
    except Exception:
        log.warning("레퍼런스 git pull 실패")
        return False
    after = _head()
    if before and after and before != after:      # HEAD 변경 = 실제 갱신 (locale 무관)
        reference.reload()
        log.info("레퍼런스 갱신 → 리로드 (%s→%s)", before[:7], after[:7])
        return True
    return False


def _notion_autosync_loop() -> None:
    """배경 루프: ①레퍼런스 repo pull(갱신 시 리로드) ②등록 작품 노션 변경 감지→동기화.
    변경 없으면 아무것도 안 함(LLM 미사용). 실무자는 [동기화]도 안 쳐도 됨."""
    from bot import notion_sync
    time.sleep(20)   # 기동 직후 소켓 안정될 때까지 대기
    while True:
        try:
            _reference_pull()                     # 레퍼런스 DB 최신화 (사본 없이 직접)
        except Exception:
            log.exception("레퍼런스 pull 오류")
        try:
            sheet = reference.sheet()
            if sheet and config.NOTION_TOKEN and config.NOTION_PAGES:
                st = _load_notion_state()
                for work, page_id in config.NOTION_PAGES.items():
                    try:
                        le = notion_sync.page_last_edited(page_id)
                    except Exception:
                        log.warning("노션 수정시각 조회 실패: %s", work)
                        continue
                    if le and le != st.get(work):
                        log.info("노션 변경 감지 → 자동 동기화: %s", work)
                        try:
                            content = notion_sync.page_text(page_id)
                            done, failed, _ = _sync_apply(sheet, work, content)
                            st[work] = le
                            _save_notion_state(st)
                            log.info("자동 동기화 완료: %s (%d 반영, %d 실패)", work, done, failed)
                        except Exception:
                            log.exception("자동 동기화 실패: %s", work)
        except Exception:
            log.exception("autosync 루프 오류")
        time.sleep(_NOTION_POLL_SEC)


if __name__ == "__main__":
    generator.healthcheck()  # Anthropic 자격증명 확인 (내부 Claude Code 팀 로그인 or API 키)
    log.info("co-writer-bot 시작 (backend=%s, reference=%s)", config.BACKEND, config.REFERENCE_DIR)
    _ref_is_repo = (config.REFERENCE_DIR.parent / ".git").exists()
    if _ref_is_repo:
        _reference_pull()   # 기동 시 최신 레퍼런스 확보 (사본 없이 repo 직접 읽음)
    if (config.NOTION_TOKEN and config.NOTION_PAGES) or _ref_is_repo:
        threading.Thread(target=_notion_autosync_loop, daemon=True).start()
        log.info("배경 동기화 ON (레퍼런스 pull=%s · 노션 %d작품 · %d초 주기)",
                 _ref_is_repo, len(config.NOTION_PAGES or {}), _NOTION_POLL_SEC)
    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()
