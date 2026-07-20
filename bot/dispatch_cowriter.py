"""dispatch_cowriter.py -- co-writer-only command handlers, mechanically extracted verbatim from co-writer-bot/app.py via ast line-span slicing. See extraction report for the rename/collision map and gap report."""
import difflib
import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.request
import uuid
from bot import config, generator, prefs, prompts, reference, verify
from bot.shared import job_ledger, works

from bot.shared.files import _files_text, _image_files, _decode_text, _hwpx_text, _parse_json_array
from bot.shared.slack_io import (
    app, log, _reply, _post_chunks, _thread_messages, _mrkdwn, _thinking, _update_note,
    _clean, _looks_like_mention, _convo_text, _last_assistant_with, _md_table_to_csv,
    _work_from_thread, BOT_USER_ID, _CANCEL,
)


MENTION_RE = re.compile(rf"<@{BOT_USER_ID}>\s*")

CMD_RE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*(.*)$", re.S)   # [명령] 나머지

SUB_RE = re.compile(r"^\s*<\s*([^>]+?)\s*>\s*(.*)$", re.S)      # <작품> 나머지

CMD_INPUT = {"입력", "저장", "input"}

CMD_EDIT = {"수정", "편집", "edit"}

CMD_GEN = {"생성", "generate", "gen"}

CMD_PLAN = {"기획", "기획안", "작품생성", "plan"}                          # 컨셉 → 기획안 초안(노션 구조)

CMD_CONVERT = {"변환", "포맷", "대본변환"}

CMD_STORYBOARD = {"스토리보드", "스토리보드1", "스보", "스보1", "씬설계", "storyboard", "storyboard1"}   # 1단계 씬 설계

CMD_STORYBOARD2 = {"스토리보드2", "스보2", "콘티", "상세콘티", "storyboard2"}                             # 2단계 상세 콘티

CMD_STORYBOARD_IMG = {"이미지", "스토리보드3", "스보3", "그리드", "image", "storyboard3"}                  # 3단계 이미지 그리드

SB_GEN_RE = re.compile(r"^\s*(생성|생성해|생성해줘|콘티|상세\s*콘티|만들어|만들어줘|ㄱㄱ|고고)\s*$")

SB_BADGE_PLAN = "🎬 *[1단계] 씬 설계* — 씬 나누기·시간 고칠 것 있으면 말해주세요. 좋으면 「생성」\n\n"

SB_BADGE_BOARD = "🎬 *[2단계] 상세 콘티* — 이 콘티를 GPT 이미지에 넣으면 그림 콘티가 나와요. 고칠 것 있으면 말해주세요.\n\n"

CMD_TREND = {"트렌드", "trend"}

CMD_IDEA = {"아이디어", "아이디어 제시", "아이디어제시", "제안", "idea"}

CMD_SYNC = {"동기화", "노션동기화", "sync"}                              # 노션 붙여넣기 → 시트

CMD_CHECK = {"확인", "조회", "check"}                                    # 바이블 한 줄 조회

CMD_ALIAS = {"별칭", "별명", "약칭", "닉네임", "alias"}                   # 작품에 부를 이름 추가

CMD_FEEDBACK = {"피드백", "feedback", "평가", "리뷰", "review"}          # 둘 다

CMD_FB_FUN = {"재미", "피드백 재미", "피드백재미", "fun"}                 # 재미만

CMD_FB_LOGIC = {"개연성", "피드백 개연성", "피드백개연성", "논리", "logic"}  # 개연성만

CMD_STOP = {"멈춰", "멈춤", "중지", "정지", "스톱", "그만", "stop", "cancel"}

CMD_FILE = {"파일", "내보내기", "다운로드", "export", "file", "md", "markdown", "txt", "csv"}

_EXPORT_TYPES = {
    "md": ".md", "markdown": ".md", "마크다운": ".md",
    "txt": ".txt", "text": ".txt", "텍스트": ".txt",
    "csv": ".csv", "시트": ".csv",
}

CMD_REF = {"참조", "레퍼런스", "캐릭터", "얼굴", "인물참조", "ref", "reference"}

_REF_SAVE_EXTS = (".png", ".jpg", ".jpeg", ".webp")     # openrouter_image._REF_EXTS와 동일해야 함

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".tiff")

CMD_LIKE = {"좋아", "좋아요", "굿", "like", "👍"}

CMD_DISLIKE = {"별로", "별로야", "싫어", "노", "dislike", "👎"}

# _CANCEL now imported from bot.shared.slack_io (2026-07-16, Phase 4 collision fix --
# see that module's comment: was a separate, disconnected set here before, so the merged
# router's STOP handler never actually reached co-writer's own cancel checks).

_CHAR_EDIT_PENDING: dict[str, dict] = {}   # thread_ts → {work,name,feedback} ('✏️ 수정' 클릭 후 다음 답글 대기)

_CHAR_DRAFT_CACHE: dict[str, dict] = {}

_FIELD_EDIT_PENDING: dict[str, dict] = {}   # thread_ts → {work,field,triple,feedback} ('✏️ 수정' 대기

_FIELD_DRAFT_CACHE: dict[str, dict] = {}    # 단일 필드(줄거리 등) 자연어 수정 초안 캐시(버튼 value용)

# ★2026-07-20 "개요 재생성 누르니깐 의견 안 물어보고 그냥 재생성함" — 🔄 재생성 클릭 후
# "어떻게 다시 만들까요?"를 묻고 다음 답글(수정 방향)을 대기하는 상태. thread_ts →
# {work, top, mid, level, ts}. 답글이 '그냥/그대로'류면 그대로 다시, 아니면 그 방향 반영.
_DRAFT_REGEN_PENDING: dict[str, dict] = {}

_DRAFT_CACHE_PATH = config.BASE_DIR / "data" / "draft_caches.json"

def _save_draft_caches() -> None:
    try:
        _DRAFT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DRAFT_CACHE_PATH.write_text(json.dumps({
            "char_draft": _CHAR_DRAFT_CACHE, "field_draft": _FIELD_DRAFT_CACHE,
            "char_pending": _CHAR_EDIT_PENDING, "field_pending": _FIELD_EDIT_PENDING,
            "draft_regen_pending": _DRAFT_REGEN_PENDING,
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.exception("draft cache save failed")

def _load_draft_caches() -> None:
    try:
        d = json.loads(_DRAFT_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    _CHAR_DRAFT_CACHE.update(d.get("char_draft") or {})
    _FIELD_DRAFT_CACHE.update(d.get("field_draft") or {})
    _CHAR_EDIT_PENDING.update(d.get("char_pending") or {})
    _FIELD_EDIT_PENDING.update(d.get("field_pending") or {})
    _DRAFT_REGEN_PENDING.update(d.get("draft_regen_pending") or {})

CMD_REFRESH = {"새로고침", "refresh"}

CMD_RELOAD = {"리로드", "reload"}

CMD_HELP = {"도움말", "help", "명령어", "가이드", "?"}

_ALL_CMD_NAMES = sorted(
    CMD_INPUT | CMD_EDIT | CMD_GEN | CMD_PLAN | CMD_CONVERT | CMD_STORYBOARD | CMD_STORYBOARD_IMG
    | CMD_TREND | CMD_IDEA | CMD_SYNC | CMD_CHECK | CMD_ALIAS | CMD_FEEDBACK | CMD_FB_FUN
    | CMD_FB_LOGIC | CMD_STOP | CMD_FILE | CMD_REF | CMD_LIKE | CMD_DISLIKE
    | CMD_REFRESH | CMD_RELOAD | CMD_HELP
)

_GUIDE = (
    "안녕하세요! 무엇을 도와드릴까요? 이렇게 시작하면 돼요 👇\n"
    "0️⃣ *가장 먼저*: 노션 기획안 페이지 **링크만** 이 채널에 붙여넣으면 자동으로 작품이 등록돼요\n"
    "   (또는 `[동기화] <노션링크>` 명령으로 등록해도 똑같아요)\n"
    "• *개요·대본*: `[생성] <작품> 2화 개요` (또는 그냥 `2화 개요 써줘`)\n"
    "• *기획안*: `[기획] 라이벌 아이돌 룸메 BL` (+노션링크 주면 그 페이지에 기록)\n"
    "• *검토*: `[피드백] <작품> 3화` / *발상*: `[아이디어] <작품> …`\n"
    "• *트렌드*: `[트렌드] 요즘 뭐가 유행?` / *조회*: `[확인] <작품> 캐릭터 누구 있지?`\n"
    "_스레드 안에선 작품 이름 없이 자연어로 이어 말해도 돼요. 전체 명령은 `[도움말]`._"
)

_GREETING_RE = re.compile(
    r"안녕|^hi\b|^hello\b|반가워|처음|뭐\s*할\s*수\s*있|어떻게\s*써|어떻게\s*쓰|사용법|시작\s*(어떻게|할까|하려)",
    re.I,
)

_ONBOARD_FIRST_CONTACT = (
    "안녕하세요! 처음이시군요 🙂 이 봇은 드라마 대본 작업을 도와줘요. 가장 먼저 할 일은 이거예요 👇\n\n"
    "1️⃣ *작품 등록* — 이 채널에 노션 기획안 페이지 **링크만 붙여넣기**만 하면 자동으로 등록돼요.\n"
    "   (`[동기화] <노션링크>` 라고 명령을 써도 되고, 그냥 링크만 붙여넣어도 똑같이 동작해요)\n"
    "2️⃣ *등록되면* — `[생성] <작품> 2화 개요` 처럼 써서 바로 초안을 뽑아볼 수 있어요.\n"
    "3️⃣ *궁금한 게 있으면* — `[확인] <작품> 캐릭터 누구 있지?` 처럼 물어보면 바이블(작품 설정 정보) 기반으로 답해드려요.\n\n"
    "_전체 명령이 궁금하면 `[도움말]`을 입력해 주세요._"
)

_HELP = (
    "명령은 `[명령] <작품> 경로` 형식이에요 👇\n\n"
    "*🔹 자주 쓰는 것*\n"
    "```\n"
    "[생성] <날혐남> 개요 / 11화     ← 다음 줄에 넣고 싶은 포인트 적으면 반영\n"
    "서아가 처음으로 반격하는 장면 꼭 넣어줘\n"
    "\n"
    "[생성] <날혐남> 대본 / 24화     ← 24화 개요+바이블(작품 설정 정보) 참고해 생성 (자동 검증관문(바이블 규칙 위반 자동 점검) ON)\n"
    "[생성] <날혐남> 대본 / 24화 검증생략   ← 빠르게: 바이블 준수 자동검증 끄기\n"
    "\n"
    "[입력] <날혐남> 로그라인\n"
    "정략결혼한 여주가 남편을 살리고 도망친다\n"
    "\n"
    "[기획] 라이벌 아이돌 룸메 BL (+노션링크)  ← 기획안 초안·수정(노션 기록)\n"
    "[피드백] <날혐남> (대본)  ← 재미+개연성 / [재미]·[개연성]로 따로도 가능\n"
    "```\n"
    "• `[생성]` 초안 뽑기 / `[입력]` 새로 저장 / `[기획]` 기획안 만들기 / `[피드백]` 검토\n\n"
    "———\n"
    "*🔸 전체 명령 (익숙해지면)*\n"
    "```\n"
    "[입력] <날혐남> 인물 / 강태혁      ← 소분류 여러 개 한 번에\n"
    "성별: 남\n"
    "나이: 32\n"
    "핵심대사: 알아들었으면 나가.\n"
    "\n"
    "[변환] 휴대폰 보는 연우, 화내며 나감   ← 줄글 상황 → 드라마 대본식 지문으로\n"
    "[트렌드] 요즘 뭐가 유행?          ← 쉬운 요약\n"
    "[아이디어] <날혐남> 서아 힘든 거 어떻게 보여주지?  ← 구체적 상황 제안\n"
    "[동기화] <날혐남> (노션 내용 통째로 붙여넣기)  ← 노션→시트 반영\n"
    "[좋아]/[별로] (생성물 스레드에서, 뒤에 이유)  ← 다음 생성에 학습 (별로는 바로 다시 뽑음)\n"
    "```\n"
    "• `[입력]` 새로 저장 / `[수정]` 기존 고침 / `[생성]` 초안 / `[아이디어]` 상황제안 / `[변환]` 줄글→대본식지문 / `[기획]` 기획안 / `[확인]` 조회 / `[트렌드]` 조회 / `[멈춰]` 중지\n"
    "• 이름만: 로그라인·키워드·타겟층·핵심정서·줄거리·금지사항·진행상태 (뒤에 바로 내용)\n"
    "• 인물/회차분배: 소분류 하나 `인물/강태혁/성별 남` 또는 여러 개를 줄마다 `소분류: 값`\n"
    "  인물 소분류 = 성별·나이·포지션·설정·핵심대사·설명 / 회차분배(회차별로 구간·화수·핵심사건을 나눠 배정) = 구간·화수·핵심사건\n"
    "• 개요 / <N화> · 대본 / <N화>"
)

def _is_dup_last(channel: str, thread_ts: str, text: str) -> bool:
    """스레드의 마지막 봇 메시지가 이 문구와 완전히 같은지(같은 오류/안내가 재시도 중
    연달아 두 번 나가는 것 방지, 2026-07-15)."""
    msgs = _thread_messages(channel, thread_ts)
    if not msgs or msgs[-1]["role"] != "assistant":
        return False
    return msgs[-1]["content"].strip() == text.strip()

def _reply_dedup(channel: str, thread_ts: str, text: str) -> None:
    """같은 안내/오류 문구가 스레드 마지막 봇 메시지와 완전히 같으면 다시 보내지 않는다."""
    if _is_dup_last(channel, thread_ts, text):
        return
    _reply(channel, thread_ts, text)

_MENTION_TOKEN_RE = re.compile(r"^[@#!]|^[UBWC][A-Z0-9]{6,}(\||$)")   # <@U..>/<#C..|이름>/<!here> 등

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

def _mark_stale_drafts(channel: str, thread_ts: str, work: str, top: str, mid: str) -> None:
    """같은 작품/종류/회차의 예전 [✅ 통과(저장)/🔄 재생성] 버튼 메시지가 남아있으면
    새 초안이 그걸 대체한다는 걸 알 수 있게 표시(2026-07-15, 4번) — 재시작으로 끊긴 생성이
    재실행되며 예전 초안과 새 초안이 둘 다 남아 어느 게 최신인지 헷갈리던 문제."""
    try:
        resp = app.client.conversations_replies(
            channel=channel, ts=thread_ts, limit=config.THREAD_HISTORY_LIMIT)
    except Exception:
        return
    for m in resp.get("messages", []):
        if not (m.get("user") == BOT_USER_ID or m.get("bot_id")):
            continue
        blocks = m.get("blocks") or []
        if not any(b.get("block_id") == "draft_actions" for b in blocks):
            continue
        try:
            val = json.loads(blocks[0]["elements"][0]["value"])
        except Exception:
            continue
        if val.get("w") == work and val.get("t") == top and val.get("m", "") == (mid or ""):
            try:
                app.client.chat_update(channel=channel, ts=m["ts"],
                                       text="⚠️ 이 초안은 중단됨(아래가 최신)", blocks=[])
            except Exception:
                pass

def _post_draft_actions(channel: str, thread_ts: str, work: str, top: str, mid: str,
                        level: int | None = None, compare_mode: bool = False) -> None:
    """생성 초안 밑에 [✅ 통과 (저장)] / [🔄 재생성] 버튼 메시지를 붙인다.
    버튼 클릭 → _on_draft_approve(저장) / _on_draft_regen(같은 걸 다시 생성).
    level(강도 비교 시): 그 단계 버튼임을 표시 + 저장 시 그 단계 초안을 콕 집어 저장 (2026-07-14, C2 —
    원래 5개 초안 비교 후 버튼이 하나뿐이라 [✅ 통과]가 늘 '가장 최근'(=강도5)만 저장했음).
    compare_mode(2026-07-16, C): 강도 1~5 비교 흐름에서는 저장 버튼 5개 대신 맨 끝에 드롭다운
    하나로 저장하게 바뀌어서(_post_level_picker) 여기선 재생성 버튼만 남긴다."""
    _mark_stale_drafts(channel, thread_ts, work, top, mid)
    label = " / ".join(x for x in [top, mid] if x)
    val = json.dumps({"w": work, "t": top, "m": mid or "", "l": level or ""}, ensure_ascii=False)
    btn_text = f"✅ 강도 {level} 저장" if level else "✅ 통과 (저장)"
    elements = [{"type": "button", "action_id": "draft_regen", "style": "danger",
                 "text": {"type": "plain_text", "text": "🔄 재생성"}, "value": val}]
    if not compare_mode:
        elements.insert(0, {"type": "button", "action_id": "draft_approve", "style": "primary",
                            "text": {"type": "plain_text", "text": btn_text}, "value": val})
    if compare_mode:
        msg_text = f"강도 {level} — 🔄 재생성만 다시 뽑고 싶으면 눌러주세요 (저장은 아래 드롭다운에서 한 번에)"
    else:
        msg_text = (f"강도 {level} — {btn_text} 또는 🔄 재생성" if level
                    else f"이 {label} 초안 — ✅ 통과(저장) 또는 🔄 재생성")
    try:
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=msg_text,
            blocks=[{"type": "actions", "block_id": "draft_actions", "elements": elements}])
    except Exception:
        log.exception("draft action buttons post failed")
        if label:
            _reply(channel, thread_ts, f"_📝 초안입니다. 확정: `[입력] <{work}> {label}`_")

def _post_level_picker(channel: str, thread_ts: str, work: str, top: str, mid: str) -> None:
    """강도 1~5 비교 흐름 끝에 붙이는 저장용 드롭다운 (2026-07-16, 온보딩 C).
    예전엔 5개 초안마다 각자 [✅ 강도 N 저장] 버튼이 따로 붙어서(총 5쌍) 첫 사용자에게
    설명 없이 뭘 눌러야 하는지 헷갈렸음 — 버튼 5개를 드롭다운 1개로 합치고, '강도'가 뭔지
    한 줄 설명을 붙인다. 저장 로직은 on_draft_approve와 동일 경로(_do_input)를 재사용."""
    options = []
    for lvl in range(1, 6):
        val = json.dumps({"w": work, "t": top, "m": mid or "", "l": lvl}, ensure_ascii=False)
        options.append({"text": {"type": "plain_text", "text": f"강도 {lvl}"}, "value": val})
    try:
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="어느 강도로 저장할까요? (1=약하게 ~ 5=세게)",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text":
                    "*어느 강도로 저장할까요?* (1=약하게 ~ 5=세게)\n"
                    "_강도 = 이 장면의 자극/전개 세기. 위 5개 버전을 비교해서 원하는 강도를 골라주세요._"}},
                {"type": "actions", "block_id": "draft_level_pick", "elements": [
                    {"type": "static_select", "action_id": "draft_level_select",
                     "placeholder": {"type": "plain_text", "text": "강도 선택"},
                     "options": options}]},
            ])
    except Exception:
        log.exception("draft level picker post failed")

def _post_revise_actions(channel: str, thread_ts: str, work: str,
                         kind: str, episode: int | None) -> None:
    """초안 수정 제안(revise gen 모드) 밑에 [🆕 <종류> 생성] / [✏️ 수정] 버튼.
    생성 → 제안대로 그 종류를 새로 생성 / 수정 → 원하는 수정 방향을 답글로 받도록 안내.
    work가 빈 문자열이면 생성 버튼 없이 수정 버튼만 표시."""
    val = json.dumps({"w": work, "k": kind, "e": episode or ""}, ensure_ascii=False)
    ep_l = f"{episode}화 " if episode else ""
    elements = []
    if work:
        elements.append({"type": "button", "action_id": "revise_generate", "style": "primary",
                          "text": {"type": "plain_text", "text": f"🆕 {kind} 생성"}, "value": val})
    elements.append({"type": "button", "action_id": "revise_specify",
                     "text": {"type": "plain_text", "text": "✏️ 수정"}, "value": val})
    label = f"이 방향으로 — " + (f"🆕 {ep_l}{kind} 생성 또는 " if work else "") + "✏️ 수정"
    try:
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=label,
            blocks=[{"type": "actions", "block_id": "revise_actions", "elements": elements}])
    except Exception:
        log.exception("revise action buttons post failed")
        _reply(channel, thread_ts, "_📝 초안입니다. 확정은 `[입력]`/`[수정]` 으로._")

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

_DRAFT_FOOT_RE = re.compile(r"(확정하려면|초안입니다|수정하고 싶은 부분|저장하세요|📝 초안|_✅|_⚠️|다듬어서)")

_BUTTON_PROMPT_RE = re.compile(
    r"통과\s*\(저장\)|✅.*재생성|🔄\s*재생성|수정\s*방향을.{0,10}답글로\s*적어주세요"
    # +2026-07-16(Bug5): "~답글로 알려주세요"/"~내용을 함께 적어주세요" 류도 이름/화차만
    # 들어간 UI 안내문일 뿐 실제 초안이 아닌데 top/mid 패턴에 걸리던 문제 — 일반화.
    r"|답글로\s*알려주세요|내용을\s*함께\s*적어주세요")

_IDEA_INTENT_RE = re.compile(
    r"아이디어|브레인스토|떠올려|뭐하지|뭐 하지|뭐가 좋을까|뭘 넣을까"
    r"|무슨 사건|어떤 사건|뭔 사건|어떻게 할까|제안 좀|뭐 없을까|어떤 게 좋")

_CHAR_ADD_RE = re.compile(r"(등장인물|캐릭터|인물).{0,15}(없|추가|넣어|만들어)")

_EDIT_INTENT_RE = re.compile(
    r"바꿔야|바꾸자|바꿀까|바꿔줘|바꿔주|바꾸는\s*게|수정해|수정할|고쳐야|고칠까|고쳐줘|고쳐주|손봐야|손볼까"
    r"|(?:으)?로\s*만들어[줘야줄]|게\s*해\s*줘|게\s*만들어"
    r"|반영해|다시\s*짜|재구성"
    r"|안\s*될까|안\s*되나|안\s*돼|가능할까|괜찮을까|어떨까요?\s*[?？]?\s*$")

_CHAR_FIELD_WORDS = ("설정", "포지션", "외형", "핵심대사", "성별", "나이", "설명",
                     "남주", "여주", "서브남주", "서브여주", "조연", "주연", "빌런", "어리게", "늙게")

_FIELD_EDIT_MAP = {
    "줄거리": ("줄거리", "", "", "plot"),
    "로그라인": ("로그라인/키워드", "로그라인", "", "logline"),
    "키워드": ("로그라인/키워드", "키워드", "", "keyword"),
    "타겟층": ("타겟층/핵심정서", "타겟층", "", "target"),
    "핵심정서": ("타겟층/핵심정서", "핵심정서", "", "emotion"),
    "금지사항": ("금지사항", "", "", "forbidden"),
    # 회차분배는 표(레코드 배열)라 다른 필드와 값 형식이 다름 — bkey="episode_plan" 을 보고
    # _do_field_edit_nl/on_field_save/on_field_regen에서 특별 처리 (2026-07-13, A1/A2).
    "회차분배": ("회차분배", "", "", "episode_plan"),
}

_CONFIRM_END_RE = re.compile(
    r"(?:확정|저장|입력)\s*(?:해\s*줘|해|하자|할게|해둬|요|좀|부탁해?)?\s*[.!~ ]*$"
    r"|(?:확정|저장|입력)\s*(?:하고|한\s*다음|한\s*후|해서|하자)\b")  # '확정하고 다음 진행하자' 류(2026-07-13)

_CONFIRM_NEG_RE = re.compile(r"안\s*[돼되]|못\s|하지\s*마|왜|어떻게|뭐가|뭐야|취소|말고|\?\s*$")

def _is_confirm(q: str) -> bool:
    """확정 의도 인식. 기존엔 30자 이하 + 문장이 확정/저장/입력으로 '끝나야'만 잡혔는데,
    '좋네 이거 4화 대본으로 확정하고 다음 진행하자'처럼 길거나 뒤에 말이 붙으면 놓쳤음
    (2026-07-13) — 길이 완화(80자) + '확정하고 ~' 중간 패턴 추가."""
    q = (q or "").strip()
    return len(q) <= 80 and bool(_CONFIRM_END_RE.search(q)) and not _CONFIRM_NEG_RE.search(q)

def _strip_draft_footer(text: str) -> str:
    """봇 초안 메시지에서 안내 꼬리말(📝 초안입니다·확정하려면 [입력]…) 제거 → 본문만."""
    lines = (text or "").split("\n")
    cut = next((i for i, ln in enumerate(lines) if _DRAFT_FOOT_RE.search(ln)), None)
    if cut is not None:
        lines = lines[:cut]
    # 맨 앞 '🎚️ 강도 N단계' 헤더도 제거 (본문 아님)
    body = "\n".join(lines).strip()
    body = re.sub(r"^\*?🎚️[^\n]*\*?\s*\n+", "", body).strip()
    return body

def _last_assistant_draft(channel: str, thread_ts: str, top: str | None = None, mid: str | None = None,
                          level: int | None = None) -> str:
    """스레드에서 가장 최근의 '실제 초안' 본문. 오류·확정 안내 텍스트에 안 흔들리게,
    초안 꼬리말(초안입니다·확정하려면·[입력] …)이 붙은 메시지를 우선으로 찾는다.
    top·mid를 주면 그 화/종류(예: 개요 2화)에 해당하는 초안을 먼저 고른다.
    level을 주면(강도 비교 결과 중 특정 단계 저장, 2026-07-14 C2) 그 '🎚️ 강도 N단계' 메시지를 콕 집는다
    — 안 그러면 5개 초안이 다 top/mid가 똑같아서 항상 '가장 최근'(=강도5)만 저장됐음."""
    msgs = _thread_messages(channel, thread_ts)
    if level:
        pat = re.compile(rf"🎚️\s*강도\s*{level}\s*단계")
        for m in reversed(msgs):
            if m["role"] == "assistant" and pat.search(m["content"]):
                t = _strip_draft_footer(m["content"])
                if len(t) >= 20:
                    return t
    # 0) 대상(개요/대본 + N화) 지정 시 그 초안 우선 ('개요 / 2화' 꼬리말 or '2화 개요' 본문)
    # Bug4(2026-07-16): 이 패턴이 본문 어디든 매칭되면, "다음 5화 개요에서 이어집니다"처럼 다른
    # 화(4화)의 초안 본문 뒤쪽에 있는 순전한 forward-reference에도 걸려 엉뚱한 화의 초안을 그
    # 화의 것으로 오인·반환할 수 있었다. 봇이 매번 고정된 헤더를 붙이는 건 아니라 헤더 텍스트에
    # 앵커링할 수는 없지만, 실제 헤더/제목은 항상 메시지 맨 앞쪽에 오고 본문 중간의 참조 문구는
    # 뒤쪽에 오는 경향이 있어 — 매칭 범위를 메시지 앞 50자로 제한하는 보수적 휴리스틱을 적용.
    if top and mid:
        pat = re.compile(rf"{re.escape(top)}\s*/\s*{re.escape(mid)}|{re.escape(mid)}\s*{re.escape(top)}")
        for m in reversed(msgs):
            if (m["role"] == "assistant" and pat.search(m["content"][:50])
                    and not _BUTTON_PROMPT_RE.search(m["content"])):
                t = _strip_draft_footer(m["content"])
                if len(t) >= 20:
                    return t
    # 1) 초안 꼬리말이 있는 봇 메시지 = 진짜 생성 초안 (에러/확정성공 메시지엔 꼬리말 없음)
    for m in reversed(msgs):
        if (m["role"] != "assistant" or not _DRAFT_FOOT_RE.search(m["content"])
                or _BUTTON_PROMPT_RE.search(m["content"])):
            continue
        t = _strip_draft_footer(m["content"])
        if len(t) >= 20:
            return t
    # 2) 폴백: 가장 최근의 충분히 긴 봇 메시지 — 버튼 안내문(내용 없는 UI 텍스트)은 건너뜀
    #    ('이 개요 초안 — ✅ 통과(저장) 또는 🔄 재생성' 같은 문구를 대본/개요 본문으로 오인하던 문제)
    for m in reversed(msgs):
        if m["role"] != "assistant" or _BUTTON_PROMPT_RE.search(m["content"]):
            continue
        t = _strip_draft_footer(m["content"])
        if len(t) >= 40:
            return t
    return ""

def _draft_save_cmd(channel: str, thread_ts: str) -> str | None:
    """스레드 직전 초안 꼬리말에 박힌 `[입력] <작품> 경로` 에서 '<작품> 경로' 부분 회수.
    도움말/오류 메시지의 템플릿 예시('<작품>' 리터럴)는 무시."""
    for m in reversed(_thread_messages(channel, thread_ts)):
        if m["role"] != "assistant":
            continue
        mm = re.search(r"\[\s*입력\s*\]\s*(<[^>]+>[^`_\n]*)", m["content"])
        if mm:
            result = mm.group(1).strip()
            wm = re.match(r"<([^>]+)>", result)
            if wm and wm.group(1).strip() == "작품":
                continue  # 도움말/오류 메시지 예시 텍스트는 건너뜀
            return result
    return None

def _clean_draft(text: str) -> str:
    """초안 본문에서 강도 뱃지 줄(🎚️/:level_slider:/강도 N단계) 제거 — 시트·노션에 안 섞이게."""
    out = []
    for ln in (text or "").split("\n"):
        s = ln.strip()
        if re.match(r"^\*?\s*(🎚️|:level_slider:)", s):
            continue
        if re.match(r"^\*?\s*강도\s*\d+\s*단계\s*\*?$", s):
            continue
        if re.match(r"^```[a-zA-Z]*$", s):     # 코드블록 펜스 줄 제거(대본 코드블록 캡처 시)
            continue
        out.append(ln)
    return "\n".join(out).strip()

_META_LINE_RE = re.compile(
    r"^\s*(?:📝|💡|ℹ️|🔧|:memo:|:bulb:|메모\s*[:：]|참고\s*[:：]|요청\s*확인\s*[:：]|바꾼\s*점\s*[:：])")

_SAVE_TAIL_RE = re.compile(
    r"(?m)^[ \t]*(?:"
    r"\**\s*\(?\s*끝\s*[/·)]"                     # (끝 / 약 90초 / 씬 4개)
    r"|(?:✅\s*)?#*\s*\**\s*체크\s*리스트"          # ✅ 체크리스트 / ## 체크리스트
    r"|(?:✅\s*)?#*\s*\**\s*자체\s*점검"            # 자체 점검
    r"|(?:✅\s*)?\**\s*(?:\d+\s*화\s*)?(?:초안|개요|대본)?\s*완성\s*(?:했?습니다)?\s*\**\s*$"
                                                    # 완성했습니다 / ✅ 4화 대본 완성
    r"|\**\s*수정\s*(?:이\s*)?필요"                # 수정 필요한 부분 있으면…
    r"|\**\s*이\s*(?:개요|대본|초안)(?:로|으로)\s*(?:확정|진행)"       # 이 개요로 진행할까요?
    r"|\**\s*(?:수정할|고칠|바꿀)\s*부분.{0,10}(?:있으면|있으시)"      # 수정할 부분 있으면 말씀해주세요
    r")")

_SAVE_HEAD_RE = re.compile(
    r"\A\s*(?:"
    r"(?:네|넵|예|알겠|알았|좋아|그래|오케이|오키|ok|물론|당연|확인)"
    r"[^\n]*?(?:쓸게요|쓸게|만들게요|만들게|작성|드릴게요|드릴게|할게요|볼게요|해드릴게요"
    r"|반영|추가|정리했|고칠게요|고칠게|수정할게요|바꿀게요|다시)[^\n]*"
    r"|\**[^\n]{0,40}?(?:생성|작성|만들)(?:해|할게요|할께요)[!.]?\**"  # **N화 대본**을 생성할게요!
    r")\n+"
    r"(?:[ \t]*[-—]{2,}[ \t]*\n+)?",              # 뒤따르는 --- 구분선도 함께 제거
    re.I)

def _clean_for_save(text: str, top: str | None = None, mid: str | None = None) -> str:
    """저장용 정리: 강도 뱃지 + 모델 메모(📝/:memo:)·'요청 확인:'·앞머리 인사멘트·꼬리 메타(체크리스트/완성멘트)·중복 제목 제거."""
    text = _clean_draft(text)                     # 강도 뱃지
    lines = [ln for ln in text.split("\n") if not _META_LINE_RE.match(ln.strip())]
    text = "\n".join(lines).strip()
    text = _SAVE_HEAD_RE.sub("", text, count=1).strip()   # 앞머리 대화체 인사/확인 멘트 제거
    m = _SAVE_TAIL_RE.search(text)                # 체크리스트·완성멘트 등 꼬리 메타 절단
    if m:
        text = text[:m.start()]
    # 개요/줄거리는 본문 뒤에 '---'로 나눠 '전개 포인트'·'참고'(부가 설명·확인 질문) 섹션이
    # 붙는 경우가 있어 첫 '---' 이후는 통째로 잘라낸다. (대본은 씬 구분자로 '---'을 정당하게
    # 여러 번 쓰므로 절대 적용하지 않음)
    if top in ("개요", "줄거리"):
        dm = re.search(r"(?m)^[ \t]*[-—]{2,}[ \t]*$", text)
        if dm:
            text = text[:dm.start()].strip()
    text = re.sub(r"(?:\n[ \t]*[-—]{2,}[ \t]*)+\s*$", "", text).strip()  # 남은 구분선(---) 정리
    text = re.sub(r"\A(?:[ \t]*[-—]{2,}[ \t]*\n+)+", "", text).strip()   # 앞머리 남은 --- 정리
    if top and mid:                               # 맨 앞 중복 제목('… 3화 대본' / '대본/3화') 제거
        text = re.sub(rf"(?m)\A\**\s*(?:\S+\s+)*{re.escape(mid)}\s*{re.escape(top)}\**\s*\n+", "", text)
        text = re.sub(rf"(?m)\A\**\s*{re.escape(top)}\s*/?\s*{re.escape(mid)}\**\s*\n+", "", text)
    return text.strip()

def _slack_to_notion_md(text: str) -> str:
    """슬랙식 단일 *볼드* → 노션 **볼드**. (이미 **인 것은 안 건드림)"""
    return re.sub(r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])", r"**\1**", text or "")

def _push_section_to_notion(work: str, top: str, mid: str, content: str) -> bool:
    """개요/대본/줄거리를 작품의 노션 페이지에 섹션 업서트(있으면 교체·없으면 추가). 반영되면 True."""
    if top not in ("개요", "대본", "줄거리") or not content.strip() or not config.NOTION_TOKEN:
        return False
    pid = works.page_of(work)
    if not pid:
        log.info("노션 push 생략(페이지 미등록): %s / %s %s", work, top, mid)
        return False
    from bot.shared import notion_sync
    heading = "줄거리" if top == "줄거리" else f"{top} {mid}".strip()
    try:
        notion_sync.upsert_section(pid, heading, _slack_to_notion_md(content))
        log.info("노션 push 완료: %s / %s", work, heading)
        # 봇이 직접 쓴 변경은 자동 동기화가 재읽지 않도록 state에 기록
        try:
            new_le = notion_sync.page_last_edited(pid)
            if new_le:
                st = _load_notion_state()
                st[work] = new_le
                _save_notion_state(st)
        except Exception:
            pass   # 실패해도 push 자체는 성공이므로 무시
        return True
    except Exception:
        log.exception("notion 섹션 업서트 실패: %s / %s", work, heading)
        return False

def _char_notion_card(name: str, data: dict) -> str:
    """캐릭터 카드 → 노션 '등장인물' 섹션의 기존 카드들과 같은 포맷(**이름 (성별, 나이) / 포지션**
    + 불릿)으로. (2026-07-13: 새 캐릭터가 시트에만 저장되고 노션엔 안 들어가던 문제 수정)"""
    tags = ", ".join(x for x in [data.get("성별"), (f"{data['나이']}" if data.get("나이") else "")] if x)
    head = f"**{name}" + (f" ({tags})" if tags else "") + (f" / {data['포지션']}" if data.get("포지션") else "") + "**"
    lines = [head]
    if data.get("핵심대사"):
        lines.append(f'- 핵심대사: "{data["핵심대사"]}"')
    if data.get("외형"):
        lines.append(f"- 외형: {data['외형']}")
    if data.get("설정"):
        lines.append(f"- 설정: {data['설정']}")
    if data.get("설명"):
        lines.append(f"- 설명: {data['설명']}")
    return "\n".join(lines)

def _push_character_to_notion(work: str, name: str, data: dict) -> bool:
    """새 등장인물을 노션 페이지의 '등장인물' 섹션에도 반영 — 기존 목록 뒤에 카드로 추가(자리 유지,
    페이지 맨 끝에 뜬금없이 붙지 않게). 섹션 자체가 없으면 조용히 생략."""
    if not config.NOTION_TOKEN:
        return False
    pid = works.page_of(work)
    if not pid:
        return False
    from bot.shared import notion_sync
    try:
        full = notion_sync.page_text(pid)
        m = re.search(r"^\s*#{1,3}\s*.*등장인물.*$", full, re.M)
        if not m:
            log.info("노션 등장인물 섹션 없음(생략): %s", work)
            return False
        heading_text = re.sub(r"^\s*#{1,3}\s*", "", m.group()).strip()
        nxt = re.search(r"^\s*#{1,3}\s", full[m.end():], re.M)
        body_end = m.end() + nxt.start() if nxt else len(full)
        body = full[m.end():body_end].strip()
        card = _char_notion_card(name, data)
        # 기존 인물 수정이면 그 인물의 기존 카드 블록을 in-place 교체(2026-07-14, C3 — 원래
        # 신규 추가만 지원해서 기존 인물 수정 시 "노션은 직접 고쳐주세요"로 반쪽 저장이었음).
        # 카드 블록 = '**이름'으로 시작하는 줄부터 다음 '**...**' 헤딩 줄 직전까지.
        card_re = re.compile(rf"^\*\*{re.escape(name)}\b.*$(?:\n(?!\*\*[^\n]+\*\*\s*$).*)*", re.M)
        m2 = card_re.search(body)
        if m2:
            rest = body[m2.end():].lstrip("\n")
            new_body = (body[:m2.start()] + card + ("\n\n" + rest if rest else "")).strip()
        else:
            new_body = (body + "\n\n" + card).strip() if body else card
        notion_sync.upsert_section(pid, heading_text, new_body)
        log.info("노션 등장인물 섹션 갱신 완료: %s / %s", work, name)
        try:
            new_le = notion_sync.page_last_edited(pid)
            if new_le:
                st = _load_notion_state()
                st[work] = new_le
                _save_notion_state(st)
        except Exception:
            pass
        return True
    except Exception:
        log.exception("노션 등장인물 섹션 갱신 실패: %s / %s", work, name)
        return False

def _load_script_summaries() -> dict:
    try:
        return json.loads(config.SCRIPT_SUMMARIES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_script_summaries(st: dict) -> None:
    try:
        config.SCRIPT_SUMMARIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.SCRIPT_SUMMARIES_PATH.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.warning("script_summaries 저장 실패")

def _summarize_script(work: str, hwa: str, content: str) -> None:
    """대본 확정 저장 시 다음 화 연속성 참고용 흐름 요약을 생성해 캐시.
    내용이 그대로면(해시 일치) 재생성 생략. 실패해도 저장 자체는 막지 않음."""
    if not (content or "").strip():
        return
    h = hashlib.md5(content.encode("utf-8")).hexdigest()
    st = _load_script_summaries()
    cur = st.get(work, {}).get(hwa)
    if cur and cur.get("hash") == h:
        return
    try:
        summary = generator.complete(
            prompts.script_summary_system(), prompts.script_summary_user(content), timeout=60
        ).strip()
    except Exception:
        log.exception("대본 요약 생성 실패: %s / %s", work, hwa)
        return
    if not summary:
        return
    st.setdefault(work, {})[hwa] = {"hash": h, "summary": summary}
    _save_script_summaries(st)

def _do_input(channel: str, thread_ts: str, rest: str, mode: str) -> None:
    """[입력](신규) / [수정](기존) / 'save'(확정 저장) — <작품> 경로 + 내용 → 시트 저장 + 노션 반영.
    내용이 비고 개요/대본/줄거리면 스레드 직전 봇 초안을 자동으로 가져와 저장.
    mode='create': 이미 있으면 거부 / 'update': 없으면 거부 / 'save': 게이트 없이 upsert."""
    from bot.sheet_bible import parse_path, split_command, TABLE_SUBS
    sheet = reference.sheet()
    if not sheet:
        _reply(channel, thread_ts, "시트가 아직 연결 안 됐어요 (SHEET_WEBAPP_URL 미설정).")
        return
    # '강도 N으로 저장' 류 — 강도 비교 결과 중 특정 단계를 콕 집어 저장 (2026-07-14, C2).
    # 경로 파싱을 오염시키지 않게 먼저 떼어낸다.
    _lvl_m = re.search(r"강도\s*([1-5])\s*(?:로|으로)?\s*저장", rest)
    save_level = int(_lvl_m.group(1)) if _lvl_m else None
    if _lvl_m:
        rest = rest[:_lvl_m.start()] + rest[_lvl_m.end():]
    sm = SUB_RE.match(rest)
    if not sm:
        _reply(channel, thread_ts, _HELP)
        return
    _raw_w = sm.group(1).strip()
    work = works.resolve(_raw_w) or _raw_w
    if work == _raw_w:   # resolve 실패 → 등록된 이름과 오타 수준으로 비슷하면 확인부터
        _typo = _typo_suspect(_raw_w)
        if _typo:
            _reply(channel, thread_ts,
                   f"⚠️ *{_raw_w}* 로는 등록된 작품이 없어요. 혹시 *{_typo}* 를 말씀하신 거면 그 이름으로 다시 보내주세요.\n"
                   f"정말 새 작품이면 먼저 `[동기화] <{_raw_w}> <노션링크>` 로 등록해 주세요.")
            return
    after = sm.group(2).splitlines()
    first = after[0].strip() if after else ""
    path_line, inline = split_command(first)        # 한 줄에 붙여쓴 내용도 인식
    next_lines = "\n".join(after[1:]).strip()        # 다음 줄들도 내용
    content = "\n".join(x for x in (inline, next_lines) if x)  # 인라인 + 다음 줄 모두 (유실 방지)
    triple = parse_path(path_line)
    if not triple:
        _reply(channel, thread_ts,
               f"`{path_line}` 는 모르는 종류예요. 로그라인·키워드·타겟층·핵심정서·인물/<이름>·줄거리·회차분배·개요/<N화>·대본/<N화>\n"
               f"예: `[입력] <{work}> 로그라인` 다음 줄에 내용, 또는 `[입력] <{work}> 인물 / 강태혁` 다음 줄에 `성별: 남`")
        return
    top, mid, sub = triple
    # 내용을 안 적었고 서사 항목이면 → 스레드 직전 봇 초안을 확정 저장 (사람이 "이걸로 확정" 하는 흐름)
    captured = False
    if not content.strip() and top in ("개요", "대본", "줄거리"):
        draft = _last_assistant_draft(channel, thread_ts, top, mid, level=save_level)   # 그 화/종류(+강도) 초안 우선
        # 방어선: 만에 하나 UI 안내문이 초안으로 잘못 잡혀도 저장/노션반영까지 가지 않게
        # 한 번 더 확인 (2026-07-16 실측 사고 — 안내문이 시트+노션에 그대로 저장된 적 있음).
        if draft and _BUTTON_PROMPT_RE.search(draft):
            draft = None
        if draft:
            content = _clean_for_save(draft, top, mid)   # 강도 뱃지·메모·요청확인·중복제목 제거
            captured = True
            mode = "save"                # 초안 확정 = 덮어쓰기 (빈 행/기존값 있어도 저장)
        else:
            _reply(channel, thread_ts,
                   f"저장할 초안이 안 보여요. 먼저 `[생성] <{work}> {' / '.join(x for x in [top, mid, sub] if x)}` 로 초안을 만든 뒤 이 스레드에서 확정해 주세요.")
            return
    if top in ("개요", "대본"):
        content = _md_bullets(content)               # 글머리 기호 → 마크다운 '-'
    # 여러 열을 갖는 표(인물·회차분배): 소분류 하나 직접 지정이 아니면 레코드 블록으로 처리
    if top in TABLE_SUBS:
        subs_list = TABLE_SUBS[top]
        subs = " · ".join(subs_list)
        key = "이름" if top == "등장인물" else "막"
        verb = "수정" if mode == "update" else "저장"
        icon = "✏️" if mode == "update" else "✅"
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
        # 화(mid)가 지정됐는데 초안 캡처면 통째로 그 화에 저장 (초안 안에 'N화'가 있어도 쪼개지 않음)
        multi = (not mid) or (len(records) > 1 and not captured)
        if multi:
            if not records:
                _reply(channel, thread_ts,
                       f"⚠️ {top}는 `{top}/11화` 하고 다음 줄에 내용, 또는 `{top}` 하고 아래처럼 ↓\n"
                       f"```\n11화\n(내용…)\n\n12화\n(내용…)\n```")
                return
            verb = "수정" if mode == "update" else "저장"
            icon = "✏️" if mode == "update" else "✅"
            pushed = 0
            for hwa, body in records:
                r = sheet.upsert(work, top, hwa, "", body)
                if isinstance(r, dict) and r.get("error"):
                    _reply(channel, thread_ts, f"⚠️ {hwa} {verb} 실패: {r['error']}")
                    return
                if _push_section_to_notion(work, top, hwa, body):
                    pushed += 1
                if top == "대본":
                    threading.Thread(target=_summarize_script, args=(work, hwa, body), daemon=True).start()
            sheet.invalidate(work)
            names = ", ".join(hwa for hwa, _ in records)
            note = " · 노션에도 반영" if pushed else ""
            _reply(channel, thread_ts, f"{icon} *{work}* / {top} — {names} {verb}했어요.{note}")
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
        note = " · 노션에도 반영" if _push_section_to_notion(work, top, mid, content) else ""
        if top == "대본" and mid:
            threading.Thread(target=_summarize_script, args=(work, mid, content), daemon=True).start()
        verb = "수정" if mode == "update" else "저장"
        icon = "✏️" if mode == "update" else "✅"
        _reply(channel, thread_ts, f"{icon} *{work}* / {label} {verb}했어요.{note}")
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
                    work = works.resolve(sm.group(1).strip()) or sm.group(1).strip()
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

_GEN_INTPAT = r"강도\s*(?:전체|전부|모두|비교|1\s*[~\-]\s*5|1to5|[1-5])\S*"

def _gen_episodes_in(s: str) -> list[int]:
    """구절에서 회차 뽑기. 'N~M' 범위 우선, 없으면 개별 숫자들. (강도 숫자는 미리 제거)"""
    s = re.sub(_GEN_INTPAT, "", s or "")
    m = re.search(r"(\d+)\s*[~\-–]\s*(\d+)", s)     # 1~3, 에피1~3, 1-3화
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b and b - a <= 100:
            return list(range(a, b + 1))
    return [int(n) for n in re.findall(r"\d+", s)]

def _parse_gen_jobs(text: str) -> list[tuple[str, int | None]]:
    """자연어 생성 요청 → [(종류, 회차|None)]. 예:
      '전체 줄거리랑, 에피1~3의 대본' → [('줄거리',None),('대본',1),('대본',2),('대본',3)]
      '1화 개요 좀 작성해줘'          → [('개요',1)]"""
    text = _VERIFY_TOKENS_RE.sub("", re.sub(_GEN_INTPAT, "", text or ""))
    jobs: list[tuple[str, int | None]] = []
    # 절 분리: 랑/과/그리고/및/또 + (뒤에 숫자 없는)쉼표  — '1,2,3화' 같은 숫자목록은 안 쪼갬
    for clause in re.split(r"\s*(?:랑|이랑|과|와|하고|및|그리고|또는|또|,(?!\s*\d))\s*", text):
        types = []
        if re.search(r"줄거리|시놉시스|시놉|전체\s*스토리|전체\s*줄거리", clause):
            types.append("줄거리")
        if "개요" in clause:
            types.append("개요")
        if re.search(r"대본|스크립트|각본", clause):
            types.append("대본")
        if not types:
            continue
        eps = _gen_episodes_in(clause)
        for t in types:
            if t == "줄거리":
                jobs.append(("줄거리", None))          # 줄거리는 전체 스토리(회차 무관)
            elif eps:
                jobs += [(t, e) for e in eps]
            else:
                jobs.append((t, None))                 # 회차 안 적음 → 진행 화 사용
    out, seen = [], set()
    for j in jobs:
        if j not in seen:
            seen.add(j); out.append(j)
    return out

def _typo_suspect(raw_work: str) -> str | None:
    """등록된 작품명/별칭과 비슷한데 정확히 일치하진 않는 이름이면 그 정식명을 반환 (2026-07-14, B3 —
    오타(예: '날협남'≠'날혐남')가 resolve 실패 후 그대로 새 시트 탭을 만들던 문제 방지).
    이미 정확히 일치하면(신규 등록 의도일 수 있어) None — resolve()가 이미 잡아줌."""
    reg = works.all_works()
    names = list(reg) + [a for v in reg.values() for a in (v.get("aliases") or [])]
    if raw_work in names or not raw_work:
        return None
    m = difflib.get_close_matches(raw_work, names, n=1, cutoff=0.6)
    if not m:
        return None
    hit = m[0]
    return hit if hit in reg else works.resolve(hit)

def _single_registered_work() -> str | None:
    """등록된 작품이 정확히 1개면 그 이름을 반환 (2026-07-14, B2 —
    원래 [확인]에만 있던 폴백을 [생성]·[아이디어]·[피드백]에도 동일 적용해 기능 간 비일관 제거)."""
    reg = works.all_works()
    return next(iter(reg)) if len(reg) == 1 else None

def _prereq_guidance(work: str, top: str, target, bible) -> str | None:
    """★2026-07-20 "선행 정보가 없으면 엉뚱하게 생성하지 말고 안내문을 내보내라" —
    생성 종류별로 반드시 있어야 할 상위 정보가 노션/시트(바이블)에 하나도 없으면, 생성 대신
    무엇부터 채워야 하는지 안내 문구를 반환한다(있으면 None → 그대로 생성 진행).
    - 대본: 그 화 개요 또는 전체 줄거리 중 하나는 있어야 함(없으면 인물·상황을 지어내 엉뚱한
      대본이 나옴 — 실사용 사고).
    - 개요: 로그라인·전체 줄거리·회차분배 중 최소 하나는 있어야 함(작품 페이지가 사실상 비어
      있으면 개요가 근거 없이 창작됨).
    다른 종류(줄거리·회차분배·인물 등 최상위 설정)는 게이트하지 않는다 — 그 자체가 바탕이므로."""
    b = bible or {}
    has_plot = bool((b.get("plot") or "").strip())
    if top == "대본":
        has_outline = bool((b.get("outlines") or {}).get(f"{target}화")) if target else False
        if not has_outline and not has_plot:
            ep = f"{target}화 " if target else ""
            ep_cmd = f"{target}화" if target else "N화"
            return (f"⚠️ <{work}>의 {ep}대본을 만들려면 먼저 *개요*나 *전체 줄거리*가 있어야 해요 "
                    "— 지금은 노션/시트에 둘 다 없어서, 그대로 만들면 인물·상황을 지어낸 엉뚱한 "
                    "대본이 나와요.\n"
                    f"• `[생성] <{work}> {ep_cmd} 개요` 로 그 화 개요부터 만들거나\n"
                    f"• `[생성] <{work}> 전체 줄거리` 로 줄거리를 먼저 잡아주세요.\n"
                    "• 이미 노션 기획안에 있다면 그 페이지 링크를 다시 붙여넣어 동기화해주세요.")
    elif top == "개요":
        has_logline = bool((b.get("logline") or "").strip())
        has_plan = bool(b.get("episode_plan"))
        if not (has_plot or has_logline or has_plan):
            return (f"⚠️ <{work}>는 아직 개요를 만들 *바탕 정보*가 없어요 — 로그라인·전체 줄거리·"
                    "회차분배 중 최소 하나는 있어야 근거 없이 지어내지 않아요.\n"
                    "• 노션 기획안 페이지에 로그라인/줄거리를 채우고 링크를 다시 붙여넣어 동기화하거나\n"
                    f"• `[생성] <{work}> 전체 줄거리` 로 줄거리부터 잡아주세요.")
    return None

def _do_generate(channel: str, thread_ts: str, rest: str, files_text: str = "",
                 force_generic: bool = False) -> None:
    """[생성] <작품> 경로(대본/N화 등) 또는 자연어('에피1~3 대본 써줘') → 바이블 참고 생성 + 시트 저장.
    files_text: 첨부 파일 내용(있으면) — 명령 파싱과 분리해 '참고 자료'로 주입.
    force_generic=True: 작품 없이 '일반 초안'으로 바로 생성(사용자가 그렇게 골랐을 때)."""
    from bot.sheet_bible import parse_path
    sm = SUB_RE.match(rest)
    if sm:
        work = works.resolve(sm.group(1).strip()) or sm.group(1).strip()   # 별칭 → 정식 작품명
        raw_gen = sm.group(2)
    else:
        # <작품> 안 쓰면 스레드(첫 댓글의 작품/노션 링크)에서 회수
        w = _work_from_thread("\n".join(m["content"] for m in _thread_messages(channel, thread_ts)))
        work = (works.resolve(w) or w) if w else None
        work = work or _single_registered_work()
        raw_gen = rest
        if not work and not force_generic:   # 작품 못 잡음 → 일반으로 할지 물어봄
            _reply(channel, thread_ts,
                   "작품명이 없어요 🙂 이대로면 작품 설정(바이블) 없이 **일반 초안**으로 만들어요 — 시트 저장은 안 돼요.\n"
                   "• 그대로 원하면 이 스레드에 `응`(또는 `일반으로`) 답글\n"
                   "• 더 정확·저장까지 하려면 작품을 넣어 다시: `[생성] <작품> 2화 개요`\n"
                   "• 아직 작품을 등록 안 하셨다면: 노션 기획안 페이지 **링크만 붙여넣기**(또는 `[동기화] <노션링크>`)하면 자동 등록돼요")
            return

    # ── 자연어 모드: 여러 종류/회차를 한 문장에 요청하면 각각을 표준 경로로 재구성해 순차 생성 ──
    jobs = _parse_gen_jobs(raw_gen)
    _first_line = (raw_gen.splitlines() or [""])[0]
    _pl = re.sub(r"\s*" + _GEN_INTPAT, "", _VERIFY_TOKENS_RE.sub("", _first_line)).strip()
    _strict_ok = parse_path(_pl) is not None
    if work and (len(jobs) >= 2 or (len(jobs) == 1 and not _strict_ok)):
        _im = re.search(_GEN_INTPAT, raw_gen)
        strength = " " + _im.group(0) if _im else ""
        _reply(channel, thread_ts,
               "요청 확인: " + ", ".join(
                   (t if e is None else f"{e}화 {t}") for t, e in jobs) + " 순서대로 만들게요.")
        for top, ep in jobs:
            path = f"<{work}> {top}" + (f" / {ep}화" if ep is not None else "") + strength
            _do_generate(channel, thread_ts, path, files_text=files_text)
        return

    gen_lines = raw_gen.splitlines()
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
        _reply(channel, thread_ts,
               "무엇을 만들지 못 알아들었어요. 이렇게 해보세요:\n"
               "• `[생성] <작품> 2화 개요` / `[생성] <작품> 3화 대본` / `[생성] <작품> 전체 줄거리`\n"
               "• 자연어도 OK: `[생성] <작품> 1~3화 대본 써줘`\n"
               "• 스레드 안에선 작품 생략 가능: `2화 개요 써줘`")
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

    # ★2026-07-20 "선행 정보 없으면 엉뚱하게 생성하지 말고 안내" — 첨부 자료(files_text)가 있으면
    # 그게 바탕이 되니 게이트를 건너뛰고, 작품이 지정된 정식 생성일 때만 선행조건을 확인한다
    # (force_generic=일반 초안은 바이블 자체가 없는 게 당연하므로 제외).
    if work and not force_generic and not (files_text and files_text.strip()):
        _guide = _prereq_guidance(work, top, target, bible)
        if _guide:
            _reply(channel, thread_ts, _guide)
            return

    messages = _thread_messages(channel, thread_ts)
    if not messages:
        return
    # '강도 1~5 / 전체 / 비교' → 5단계 버전을 한 번에 뽑기
    all_lvls = re.search(r"강도\s*(전체|전부|모두|비교|1\s*[~\-]\s*5|1to5)", directive)
    what = " ".join(x for x in [mid, top] if x) or top
    # 첨부 파일(있으면) → 명령과 분리해 '참고 자료'로 주입 (경로 파싱 오염 방지)
    file_ctx = ""
    if files_text and files_text.strip():
        file_ctx = ("\n\n[첨부 참고 자료 — 이 작품의 설정·줄거리·대본. 바이블처럼 참고하되, "
                    f"여기 없는 사실을 지어내지 마라]\n{files_text.strip()[:12000]}")
    if all_lvls:
        notes_c = re.sub(r"강도\s*(전체|전부|모두|비교|1\s*[~\-]\s*5|1to5)[^\n]*", "", notes).strip()
        req = (f"'{work}' " if work else "") + f"{what}를 생성해줘." + _notes_block(notes_c) + file_ctx
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
            # 초안 대신 확인 질문이면(작품/화 특정 불가) 나머지 강도는 생성 안 하고 질문만 보여줌
            if ans.strip().startswith("[확인필요]"):
                _post_chunks(channel, thread_ts, ans.strip()[len("[확인필요]"):].strip(),
                             replace_ts=(ph if first else None))
                return
            _post_chunks(channel, thread_ts, f"*🎚️ 강도 {lvl}단계*\n\n{_clean_draft(ans)}", replace_ts=(ph if first else None))
            first = False
            # 강도 비교: 각 단계 바로 밑에 그 단계 전용 저장 버튼 (2026-07-14, C2 — 끝에 버튼 하나만
            # 있으면 [✅ 통과]가 항상 '가장 최근 초안'(=강도5)을 저장해서 특정 단계 선택 저장이 안 됐음)
            if work and top in ("개요", "대본", "줄거리"):
                _post_draft_actions(channel, thread_ts, work, top, mid, level=lvl, compare_mode=True)
        if work and top in ("개요", "대본", "줄거리"):
            _post_level_picker(channel, thread_ts, work, top, mid)
        return

    # 강도 명시 안 했으면 기본 4로 고정
    bible = _ensure_default_intensity(bible, top)
    # 이번 요청을 명확한 지시로 정리(명령 구문 제거) + 넣고 싶은 포인트는 '재료'로 (반복 금지)
    req = (f"'{work}' " if work else "") + f"{what}를 생성해줘." + _notes_block(notes) + file_ctx
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

    # 모델이 초안 대신 확인 질문을 낸 경우(작품/화를 특정 못함) — 초안이 아니므로 검증·저장
    # 버튼 없이 질문 그대로만 보여준다. (FAILSAFE 프롬프트의 '[확인필요]' 마커 규칙과 짝)
    if answer.strip().startswith("[확인필요]"):
        _post_chunks(channel, thread_ts, answer.strip()[len("[확인필요]"):].strip(), replace_ts=ph)
        return

    # 3단계 검증 관문: 생성과 분리된 감사자가 바이블 준수 재검.
    #  · 금지사항(이진 위반)만 자동 최소 교정, 나머지 위반은 ⚠️ 플래그로 알림만(작가가 직접 판단).
    # 바이블 없으면(패턴·사례 기반 생성) 기준이 없어 건너뜀. '검증생략'/COWRITER_VERIFY_GATE=0로 off.
    gate_note = ""
    if gate_on and bible:
        _update_note(channel, ph, f"{what} 초안 검증 중이에요… (바이블(작품 설정 정보) 준수 점검)")
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
                parts.append(f"⚠️ 바이블(작품 설정 정보) 확인 필요 {len(flagged)}건 (자동 수정 안 함 — 직접 확인하세요)\n{lines}")
            gate_note = "\n".join(parts)
        elif v["checked"]:
            gate_note = "✅ 자동검증: 바이블 준수 이상 없음"
        if _cancelled(channel, thread_ts, ph):
            return

    # 모델이 스스로 낸 강도 줄 제거(중복 방지)
    answer = _clean_draft(answer)
    _lvl = (bible.get("intensity_map") or {}).get(top) or bible.get("intensity_level") if bible else None
    label = " / ".join(x for x in [top, mid, sub] if x)
    saveable = bool(work) and top in ("개요", "대본", "줄거리")   # 통과(저장)/재생성 버튼 대상
    foot = f"_📝 초안입니다. 확정하려면 `[입력] <{work}> {label}` 로 저장하세요._" if (label and not saveable) else ""

    if top == "대본":
        # 대본은 정렬·가독성 위해 코드블록으로. 배지·검증·확정안내는 밖에.
        header = ((f"*🎚️ 강도 {_lvl}단계*  " if _lvl else "") + "🎬 *대본 초안*"
                  + (f"\n{gate_note}" if gate_note else ""))
        if ph:
            _post_chunks(channel, thread_ts, header, replace_ts=ph)
        else:
            _reply(channel, thread_ts, header)
        _post_code(channel, thread_ts, answer)           # 코드블록(monospace)
        if saveable:
            _post_draft_actions(channel, thread_ts, work, top, mid)
        elif foot:
            _reply(channel, thread_ts, foot)
        return

    if _lvl:
        answer = f"*🎚️ 강도 {_lvl}단계*\n\n" + answer
    if gate_note:
        answer += f"\n\n{gate_note}"
    if foot:
        answer += f"\n\n{foot}"
    _post_chunks(channel, thread_ts, answer, replace_ts=ph)
    if saveable:
        _post_draft_actions(channel, thread_ts, work, top, mid)

def _do_convert(channel: str, thread_ts: str, rest: str) -> None:
    """[변환]: 대충 쓴 줄글 상황 → 드라마 대본식 지문으로 구체화 (원문에 없는 내용 추가 금지)."""
    q = rest.strip()
    work, bible = None, None
    wm = SUB_RE.match(q)
    if wm:                              # <작품> 지정 시 인물 이름·호칭 참고
        work = works.resolve(wm.group(1).strip()) or wm.group(1).strip()
        q = wm.group(2).strip()
    if not work:                        # <작품> 없으면 스레드에서 회수 (인물 이름 매칭용)
        w = _work_from_thread("\n".join(m["content"] for m in _thread_messages(channel, thread_ts)))
        if w:
            work = works.resolve(w) or w
    if work:
        sheet = reference.sheet()
        if sheet:
            try:
                bible = sheet.get(work)
            except Exception:
                log.exception("convert bible load failed")
    epm = re.search(r"(\d+)\s*화", q[:200])
    target = int(epm.group(1)) if epm else None
    draft = q
    if len(draft) < 10:                 # 본문 거의 없음 → 스레드 직전 봇 초안을 변환 (오류·확정문 제외)
        draft = _last_assistant_draft(channel, thread_ts) or ""
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

def _do_storyboard(channel: str, thread_ts: str, rest: str, stage: int = 1) -> None:
    """스토리보드 — 단계를 '명령'으로 지정하고, 스레드 내용을 직접 읽어 이어간다.
      [스토리보드1] = 1단계 씬 설계(분할·시간)
      [스토리보드2] = 2단계 상세 콘티(GPT 이미지용)
    rest 안에 <작품>·N화·(선택)수정지시를 넣을 수 있다. 대본은 시트(노션)→스레드 순으로 확보.
    두 단계 모두 스레드를 읽으므로, 재시작·맥락 유실에도 흐름이 끊기지 않는다."""
    q = rest.strip()
    work, bible = None, None
    wm = SUB_RE.match(q)
    if wm:
        work = wm.group(1).strip()
        q = wm.group(2).strip()
    msgs = _thread_messages(channel, thread_ts)
    joined = "\n".join(m["content"] for m in msgs)
    # 작품: rest → 스레드에서 회수 (노션 링크/·<작품>, URL 오인 방지)
    if not work:
        work = _work_from_thread(joined)
    if work:
        work = works.resolve(work) or work          # 별칭 → 정식 작품명
        sheet = reference.sheet()
        if sheet:
            try:
                bible = sheet.get(work)
            except Exception:
                log.exception("storyboard bible load failed")
    # 대상 화: rest → 스레드
    epm = re.search(r"(\d+)\s*화", q) or re.search(r"(\d+)\s*화", joined)
    target = int(epm.group(1)) if epm else _progress_episode(bible, ["대본", "개요"])
    instr = re.sub(r"(\d+)\s*화", "", q).strip()          # 수정 지시/추가 포인트(있으면)
    script = _sb_script_from_bible(bible, target)
    ref_block = (f"\n\n[원본 대본 — 사건·행동·대사 하나도 바꾸지 마라]\n{script}" if script else "")
    prior_plan = _last_assistant_with(msgs, ["[1단계]", "씬 설계안"])
    _CANCEL.discard(thread_ts)

    if stage == 1:
        # 대본 확보: 시트 → rest에 붙여넣은 본문 → 스레드 직전 봇 출력(설계안/콘티 제외)
        draft = script
        if not draft:
            if len(q) >= 20 and not wm:
                draft = q
            else:
                prior = [m["content"] for m in msgs if m["role"] == "assistant"
                         and "[1단계]" not in m["content"] and "[2단계]" not in m["content"]]
                draft = prior[-1] if prior else ""
        if not draft and not prior_plan:
            _reply(channel, thread_ts,
                   "대본을 못 찾았어요:\n"
                   "• `[스토리보드1] <날혐남> 3화` — 노션(시트) 그 화 대본을 자동으로 불러와요\n"
                   "• 또는 `[스토리보드1] <날혐남>` 뒤에 대본을 붙여넣기")
            return
        ph = _thinking(channel, thread_ts,
                       (f"{target}화 대본으로 " if script else "") + "씬 설계 중이에요…")
        try:
            if prior_plan and instr:      # 스레드에 설계안이 이미 있고 수정 지시가 오면 → 수정
                ans = generator.complete(
                    prompts.storyboard_plan_system(bible, target_episode=target),
                    _convo_text(msgs) + ref_block
                    + f"\n\n(이번은 씬 설계안 수정 요청이다: '{instr}'. 바뀐 씬만, 맨 위 '바꾼 점:' 한 줄. 전체 재출력 금지.)")
            else:
                ans = generator.complete(prompts.storyboard_plan_system(bible, target_episode=target),
                                         prompts.storyboard_plan_user(draft), timeout=300)
        except Exception:
            log.exception("storyboard plan failed")
            _post_chunks(channel, thread_ts, "씬 설계 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
            return
        answer = SB_BADGE_PLAN + ans.strip()
    else:  # stage 2 — 상세 콘티
        if not prior_plan:
            _reply(channel, thread_ts,
                   "먼저 `[스토리보드1] <날혐남> 3화` 로 씬 설계부터 해주세요. "
                   "(이 스레드에 씬 설계안이 있어야 상세 콘티를 만들어요.)")
            return
        prior_conti = _last_assistant_with(msgs, ["[2단계]"])
        ph = _thinking(channel, thread_ts, "상세 콘티(GPT 이미지용) 만드는 중이에요… (몇 초~1분)")
        # ※ 1단계 수정은 '바뀐 씬만' 나오므로, 마지막 설계안 조각만 쓰면 나머지가 샌다.
        #    대화 전체(최초 전체 설계안 + 이후 수정들)를 주고 '최종 씬 구성'을 재구성하게 한다.
        base = _convo_text(msgs) + ref_block
        recon = ("위 대화에는 '씬 설계안' 전체본과 그 뒤 부분 수정('바꾼 점' + 바뀐 씬)들이 섞여 있다. "
                 "이 둘을 합쳐 **최종 씬 구성(씬 수·순서·각 씬 시간)** 을 스스로 재구성하라. "
                 "(수정된 씬은 최신본으로, 안 바뀐 씬은 최초본대로.)")
        if prior_conti and instr:         # 이미 콘티가 있고 수정 지시 → 바뀐 부분만
            user = base + (f"\n\n(위 상세 콘티를 이 요청대로 고쳐라: '{instr}'. "
                           "바뀐 샷/구간만, 맨 위 '바꾼 점:' 한 줄. 대본 내용 불변. 전체 재출력 금지.)")
        else:
            user = base + (f"\n\n({recon} 그 최종 구성의 씬 순서·시간을 지켜, [원본 대본]을 "
                           "영상문법가이드 정본 예시처럼 샷 단위 상세 콘티로 전개하라. 대본의 사건·행동·대사는 하나도 바꾸지 마라.)")
        try:
            ans = generator.complete(prompts.storyboard_system(bible, target_episode=target),
                                     user, timeout=300)
        except Exception:
            log.exception("storyboard conti failed")
            _post_chunks(channel, thread_ts, "상세 콘티 생성 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
            return
        answer = SB_BADGE_BOARD + ans.strip()

    if _cancelled(channel, thread_ts, ph):
        return
    _post_chunks(channel, thread_ts, answer or "(빈 응답)", replace_ts=ph)

def _do_storyboard_images(channel: str, thread_ts: str, rest: str) -> None:
    """[이미지] 스레드의 상세 콘티 → 컷별 GPT 이미지(참조로 얼굴 유지) → 그리드 1장 슬랙 업로드."""
    from bot import openrouter_image as oi, storyboard_grid as grid
    if not oi.available():
        _reply(channel, thread_ts,
               "이미지 기능이 꺼져 있어요 — `.env`에 `OPENROUTER_API_KEY`를 넣고 봇을 재시작하세요.")
        return
    ok, msg = grid.available()
    if not ok:
        _reply(channel, thread_ts, msg)
        return
    q = rest.strip()
    wm = SUB_RE.match(q)
    work = wm.group(1).strip() if wm else None
    if wm:
        q = wm.group(2).strip()
    # [이미지] <작품> 30  → 목표 컷 수 30 (과잉분할 방지). 없으면 자동.
    tm = re.search(r"(\d+)", q)
    target = int(tm.group(1)) if tm else None
    msgs = _thread_messages(channel, thread_ts)
    joined = "\n".join(m["content"] for m in msgs)
    if not work:
        work = _work_from_thread(joined)
    bible = None
    if work:
        sheet = reference.sheet()
        if sheet:
            try:
                bible = sheet.get(work)
            except Exception:
                log.exception("image bible load failed")
    conti = _last_assistant_with(msgs, ["[2단계]"])
    if not conti:
        _reply(channel, thread_ts,
               "먼저 `[스토리보드2] <작품>` 로 상세 콘티를 만든 뒤, 이 스레드에서 `[이미지]`를 쳐주세요.")
        return

    _CANCEL.discard(thread_ts)
    ph = _thinking(channel, thread_ts,
                   (f"콘티를 {target}컷으로 나누는 중이에요…" if target else "콘티를 컷(샷) 리스트로 나누는 중이에요…"))
    # 1) 콘티 → 샷 리스트(JSON) — OpenRouter(HTTP)로. agent(claude CLI) 동시호출 충돌/지연 회피.
    try:
        raw = oi.chat(prompts.storyboard_shots_system(bible, target=target),
                      prompts.storyboard_shots_user(conti), timeout=300)
        shots = [s for s in _parse_json_array(raw) if isinstance(s, dict) and s.get("prompt")]
        if target and len(shots) > target:
            shots = shots[:target]
    except Exception as e:
        log.exception("shots split failed")
        _post_chunks(channel, thread_ts, f"컷 분해 중 오류가 났어요: {e}", replace_ts=ph)
        return
    if not shots:
        _post_chunks(channel, thread_ts, "컷을 만들지 못했어요. 콘티를 다시 확인해 주세요.", replace_ts=ph)
        return

    n = len(shots)
    _update_note(channel, ph, f"컷 {n}개 이미지 생성 중이에요… (몇 분 걸려요) 0/{n}")
    # 2) 컷별 이미지 생성 (병렬, 순서 보존)
    import concurrent.futures as cf
    results: list[bytes | None] = [None] * n
    total_cost = 0.0
    done = 0

    def _one(i: int, s: dict):
        refs = oi.character_refs(work, s.get("characters") or [])
        png, cost = oi.generate(s["prompt"], aspect_ratio=config.OPENROUTER_PANEL_ASPECT, refs=refs)
        return i, png, cost

    with cf.ThreadPoolExecutor(max_workers=config.OPENROUTER_IMG_WORKERS) as ex:
        futs = [ex.submit(_one, i, s) for i, s in enumerate(shots)]
        for fut in cf.as_completed(futs):
            try:
                i, png, cost = fut.result()
                results[i] = png
                total_cost += cost
            except Exception:
                log.exception("image gen failed (한 컷)")
            done += 1
            if done % 3 == 0 or done == n:
                _update_note(channel, ph, f"컷 이미지 생성 중… {done}/{n}")

    panels = [(results[i], shots[i].get("n") or (i + 1), shots[i].get("caption") or "")
              for i in range(n) if results[i]]
    if not panels:
        _post_chunks(channel, thread_ts,
                     "이미지 생성이 모두 실패했어요. (OpenRouter 키/모델/쿼터를 확인해 주세요)", replace_ts=ph)
        return

    # 3) 그리드 합성
    _update_note(channel, ph, f"{len(panels)}컷을 그리드로 합치는 중이에요…")
    try:
        grid_png = grid.build_grid(panels, cols=config.OPENROUTER_GRID_COLS)
    except Exception as e:
        log.exception("grid build failed")
        _post_chunks(channel, thread_ts, f"그리드 합성 중 오류가 났어요: {e}", replace_ts=ph)
        return

    # 4) 슬랙 업로드
    miss = n - len(panels)
    cost_s = f" · 생성비 ~${total_cost:.2f}" if total_cost else ""
    caption = (f"🖼️ 스토리보드 그리드 — {len(panels)}컷"
               + (f" ({miss}컷 실패)" if miss else "") + cost_s)
    try:
        app.client.files_upload_v2(
            channel=channel, thread_ts=thread_ts, file=grid_png,
            filename=f"storyboard_{work or 'ep'}.png",
            title=f"스토리보드 {len(panels)}컷", initial_comment=caption)
        _update_note(channel, ph, "✅ 스토리보드 그리드 완성 (아래 이미지)")
    except Exception as e:
        log.exception("slack upload failed")
        _post_chunks(channel, thread_ts,
                     f"이미지는 만들었는데 슬랙 업로드에서 막혔어요: {e}\n(앱에 files:write 권한 필요)",
                     replace_ts=ph)

def _thread_origin_mode(messages: list[dict]) -> str:
    """스레드에서 '가장 최근' 명령의 모드 → 후속 답글을 그 활동으로 이어감.
    (한 스레드에서 [기획]→[생성]처럼 활동을 바꿔도, 마지막으로 한 활동을 따라감)"""
    for m in reversed(messages):
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
        if "[2단계]" in c:                          # 상세 콘티 (배지로 감지)
            return "detail"
        if "[1단계]" in c or "씬 설계안" in c:      # 씬 설계안
            return "plan"
    return "plan"

def _plan_sections(md: str) -> list[str]:
    """기획안 마크다운을 '## 섹션' 단위로 분리 (헤딩 포함)."""
    return [p.strip() for p in re.split(r"(?m)(?=^##\s)", md or "") if p.strip()]

def _is_valid_plan(md: str) -> bool:
    """진짜 기획안인지 판별 — 에러/타임아웃/빈 텍스트 배제.
    마크다운(##) 없어도 섹션 키워드로 인식 (노션에 일반 텍스트로 넣어도 OK)."""
    if not md or len(md.strip()) < 60:
        return False
    bad = ("⏱️", "이어가는 중 오류", "기획안 생성 중 오류", "수정 중 오류", "오류가 났어요",
           "원본 기획안", "현재 기획안 내용이", "(빈 응답)")
    head = md.strip()[:80]
    if any(b in head for b in bad):
        return False
    if len(re.findall(r"(?m)^#{1,3}\s", md)) >= 2:        # ①마크다운 헤딩(#/##/###) 2개+
        return True
    kws = ("로그라인", "키워드", "타겟", "정서", "등장인물", "인물", "줄거리", "회차분배", "회차 분배")
    return sum(1 for k in kws if k in md) >= 3            # ②마크다운 없어도 섹션 키워드 3개+

def _first_changed_section(prev_md: str, new_md: str) -> int | None:
    """직전 기획안 대비 처음으로 바뀐 '## 섹션' 인덱스. 변화 없으면 None.
    노션에서 읽어온 텍스트는 볼드(**)·불릿(-) 마커가 없으므로, 마커 무시하고 내용만 비교."""
    norm = lambda s: re.sub(r"[*#>\-\s]+", " ", s or "").strip().lower()
    ps, ns = _plan_sections(prev_md), _plan_sections(new_md)
    for i in range(len(ns)):
        if norm(ns[i]) != norm(ps[i] if i < len(ps) else ""):
            return i
    return None

def _plan_changed_view(prev_md: str, new_md: str) -> str | None:
    """직전 대비 실제로 바뀐 섹션들만 뽑아 슬랙용 뷰로. 변화 없으면 None.
    (부분수정 시 슬랙에 전체 기획안 대신 바뀐 섹션만 보여줌 — 노션 갱신과 일관)"""
    norm = lambda s: re.sub(r"[*#>\-\s]+", " ", s or "").strip().lower()
    ps, ns = _plan_sections(prev_md), _plan_sections(new_md)
    idxs = [i for i in range(len(ns)) if norm(ns[i]) != norm(ps[i] if i < len(ps) else "")]
    if not idxs:
        return None
    labels = "·".join(str(i + 1) for i in idxs)
    body = "\n\n".join(ns[i] for i in idxs)
    return f"🔧 *수정된 섹션 {labels}*\n\n{body}"

def _trend_orient(text: str) -> str | None:
    """텍스트에서 트렌드 성향(BL/GL/로맨스) 감지 — 스레드 후속에 성향 이어붙이기용."""
    if re.search(r"(?<![a-z])bl(?![a-z])", text or "", re.I):
        return "BL"
    if re.search(r"(?<![a-z])gl(?![a-z])", text or "", re.I) or "백합" in (text or ""):
        return "GL"
    if "로맨스" in (text or "") or "남녀" in (text or ""):
        return "로맨스"
    return None

def _find_name_context(name: str, bible: dict | None, thread_text: str = "", max_len: int = 2000) -> str:
    """이 이름이 이미 언급된 문단들(대본·개요·이 스레드 대화)을 모아준다 — 캐릭터를 맥락과
    무관하게 딴사람으로 지어내지 않고, 이미 등장한 장면·역할에 맞춰 만들게 하기 위함
    (2026-07-13: '민재' 재생성 시 4화 대본 속 역할과 무관한 사람이 나온 문제 수정)."""
    if not name:
        return ""
    texts = []
    if bible:
        texts += list((bible.get("scripts") or {}).values())
        texts += list((bible.get("outlines") or {}).values())
    if thread_text:
        texts.append(thread_text)
    seen, hits = set(), []
    for t in texts:
        for para in re.split(r"\n{2,}", t or ""):
            if name in para and para not in seen:
                seen.add(para)
                hits.append(para.strip())
    return "\n---\n".join(hits)[:max_len]

_CHAR_DISPLAY_KEYS = ["성별", "나이", "포지션", "외형", "설정", "핵심대사", "설명"]

_APPEARANCE_PREFIX_RE = re.compile(r"^외형:\s*(.*?)\n\n(.*)$", re.S)

def _split_appearance(char: dict) -> dict:
    """저장 시 '설정' 안에 합쳐 넣은 '외형: ...' 을 다시 별도 키로 복원 (2026-07-14, C4 —
    안 그러면 기존 인물을 다시 수정할 때 카드에 외형이 안 보이고, 모델이 재작성하며 설정과 섞을 위험)."""
    out = dict(char)
    m = _APPEARANCE_PREFIX_RE.match(out.get("설정") or "")
    if m and not out.get("외형"):
        out["외형"], out["설정"] = m.group(1).strip(), m.group(2).strip()
    return out

def _generate_char_card(work: str | None, name: str, feedback: str,
                        bible: dict | None, context: str = "") -> tuple[dict | None, str | None]:
    """새 인물 카드 생성. 반환 (data, error_msg) — 성공하면 error_msg=None."""
    try:
        raw = generator.complete(prompts.char_add_system(bible), prompts.char_add_user(name, feedback, context),
                                 timeout=90)
        data = _json_loads(raw)
    except Exception:
        log.exception("character add generation failed")
        return None, "캐릭터 설정 생성 중 오류가 났어요. 잠시 후 다시 시도해 주세요."
    data = {k: str(data[k]).strip() for k in _CHAR_DISPLAY_KEYS if data.get(k)}
    if not data:
        return None, "모델이 캐릭터 정보를 못 만들었어요. 잠시 후 다시 시도해 주세요."
    return data, None

def _generate_char_edit(work: str | None, name: str, existing: dict, instruction: str,
                        bible: dict | None, context: str = "") -> tuple[dict | None, str | None]:
    """이미 있는 인물을 자연어 지시로 수정. 반환 (data, error_msg)."""
    try:
        raw = generator.complete(prompts.char_edit_system(bible),
                                 prompts.char_edit_user(name, existing, instruction, context), timeout=90)
        data = _json_loads(raw)
    except Exception:
        log.exception("character edit generation failed")
        return None, "캐릭터 수정 중 오류가 났어요. 잠시 후 다시 시도해 주세요."
    data = {k: str(data[k]).strip() for k in _CHAR_DISPLAY_KEYS if data.get(k)}
    if not data:
        return None, "모델이 수정안을 못 만들었어요. 잠시 후 다시 시도해 주세요."
    return data, None

def _char_card_text(name: str, data: dict) -> str:
    lines = [f"👤 *새 등장인물 카드 — {name}* (아직 저장 전 — 검토해 주세요)"]
    for k in _CHAR_DISPLAY_KEYS:
        if data.get(k):
            lines.append(f"- *{k}*: {data[k]}")
    return "\n".join(lines)

def _post_char_draft(channel: str, thread_ts: str, work: str, name: str, feedback: str,
                     data: dict, ph: str | None = None, context: str = "",
                     existing: dict | None = None) -> None:
    """인물 카드 초안 + [✅ 저장]/[🔄 재생성]/[✏️ 수정] 버튼. 이 시점엔 아직 저장 안 됨.
    existing이 있으면(기존 인물 자연어 수정) 재생성 시 그 인물다움을 유지하는 수정 프롬프트를 쓴다."""
    _post_chunks(channel, thread_ts, _char_card_text(name, data), replace_ts=ph)
    key = uuid.uuid4().hex[:12]
    _CHAR_DRAFT_CACHE[key] = {"w": work, "n": name, "fb": feedback, "d": data, "ctx": context, "ex": existing}
    _save_draft_caches()
    val = json.dumps({"id": key}, ensure_ascii=False)
    try:
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"{name} 카드 — 저장/재생성/수정",
            blocks=[{"type": "actions", "block_id": "char_actions", "elements": [
                {"type": "button", "action_id": "char_save", "style": "primary",
                 "text": {"type": "plain_text", "text": "✅ 저장"}, "value": val},
                {"type": "button", "action_id": "char_regen", "style": "danger",
                 "text": {"type": "plain_text", "text": "🔄 재생성"}, "value": val},
                {"type": "button", "action_id": "char_edit",
                 "text": {"type": "plain_text", "text": "✏️ 수정"}, "value": val}]}])
    except Exception:
        log.exception("char draft buttons post failed")

def _do_char_add(channel: str, thread_ts: str, work: str | None, feedback: str,
                 bible: dict | None) -> None:
    """'<이름> 캐릭터 설정 없네 추가해줘' 류 — 실제로 바이블에 없는지 확인 후, 없으면
    이 작품 톤에 맞는 카드를 만들어 초안으로 보여준다(버튼으로 저장/재생성/수정 — 바로 저장 안 함)."""
    if not work:
        _reply(channel, thread_ts, "어떤 작품인지 못 찾았어요. `<작품>` 을 붙여서 다시 말씀해주세요.")
        return
    nm = re.search(r"[\"'“”']([^\"'“”]{1,10})[\"'“”']", feedback)
    name = nm.group(1).strip() if nm else None
    if not name:
        guess = generator.complete(
            "다음 문장에서 새로 추가하려는 등장인물 이름만 한 단어로 답하라(따옴표·설명 없이). "
            "이름을 못 찾으면 정확히 '없음'이라고만 답하라.",
            feedback, timeout=30).strip()
        name = guess if guess and guess != "없음" and len(guess) <= 10 else None
    if not name:
        _reply(channel, thread_ts,
               "어떤 인물을 추가할지 이름을 못 찾았어요. "
               f"`[입력] {work} 인물 / 이름 / 설정 / 내용` 으로 직접 넣어주세요.")
        return
    existing = (bible or {}).get("characters") or {}
    if name in existing:
        _reply(channel, thread_ts,
               f"*{name}*는 이미 바이블에 있어요 — 바꾸려면 "
               f"`[수정] {work} 인물 / {name} / 설정 / 새 내용` 으로 해주세요.")
        return
    if not reference.sheet():
        _reply(channel, thread_ts, "시트가 아직 연결 안 됐어요 (SHEET_WEBAPP_URL 미설정).")
        return
    joined = "\n".join(m["content"] for m in _thread_messages(channel, thread_ts))
    context = _find_name_context(name, bible, joined)
    ph = _thinking(channel, thread_ts, f"{name} 없는 거 확인했어요 — 캐릭터 설정 새로 만드는 중이에요…")
    data, err = _generate_char_card(work, name, feedback, bible, context)
    if err:
        _post_chunks(channel, thread_ts, err, replace_ts=ph)
        return
    _post_char_draft(channel, thread_ts, work, name, feedback, data, ph=ph, context=context)

def _do_char_edit_nl(channel: str, thread_ts: str, work: str | None, name: str,
                     feedback: str, bible: dict | None) -> None:
    """'민재 설정은 서브남주로 바꿔야겠어' 류 — 이미 있는 인물을 자연어로 수정 (2026-07-13).
    기존 값 + 지시사항으로 수정 초안을 만들어 저장/재생성/수정 버튼으로 보여준다(바로 저장 X)."""
    if not work:
        _reply(channel, thread_ts, "어떤 작품인지 못 찾았어요. `<작품>` 을 붙여서 다시 말씀해주세요.")
        return
    if not reference.sheet():
        _reply(channel, thread_ts, "시트가 아직 연결 안 됐어요 (SHEET_WEBAPP_URL 미설정).")
        return
    existing = _split_appearance(((bible or {}).get("characters") or {}).get(name) or {})
    joined = "\n".join(m["content"] for m in _thread_messages(channel, thread_ts))
    context = _find_name_context(name, bible, joined)
    ph = _thinking(channel, thread_ts, f"{name} 설정 수정하는 중이에요…")
    data, err = _generate_char_edit(work, name, existing, feedback, bible, context)
    if err:
        _post_chunks(channel, thread_ts, err, replace_ts=ph)
        return
    _post_char_draft(channel, thread_ts, work, name, feedback, data, ph=ph, context=context, existing=existing)

def _find_field_edit(feedback: str) -> tuple[str | None, tuple | None]:
    for word, triple in _FIELD_EDIT_MAP.items():
        if word in feedback:
            return word, triple
    return None, None

def _field_draft_text(field_name: str, new_val: str) -> str:
    if field_name == "회차분배":
        try:
            rows = _json_loads_array(new_val)
            new_val = "\n".join(
                f"· {r.get('막', '?')} | {r.get('구간', '')} | {r.get('화수', '')} | {r.get('핵심사건', '')}"
                for r in rows)
        except Exception:
            pass
    return f"📝 *{field_name} 수정안* (아직 저장 전 — 검토해 주세요)\n\n{new_val}"

def _post_field_draft(channel: str, thread_ts: str, work: str, field_name: str, triple: tuple,
                      feedback: str, new_val: str, ph: str | None = None) -> None:
    """단일 필드(줄거리 등) 수정안 + [✅ 저장]/[🔄 재생성]/[✏️ 수정] 버튼."""
    _post_chunks(channel, thread_ts, _field_draft_text(field_name, new_val), replace_ts=ph)
    key = uuid.uuid4().hex[:12]
    _FIELD_DRAFT_CACHE[key] = {"w": work, "f": field_name, "t": list(triple), "fb": feedback, "v": new_val}
    _save_draft_caches()
    val = json.dumps({"id": key}, ensure_ascii=False)
    try:
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"{field_name} 수정안 — 저장/재생성/수정",
            blocks=[{"type": "actions", "block_id": "field_actions", "elements": [
                {"type": "button", "action_id": "field_save", "style": "primary",
                 "text": {"type": "plain_text", "text": "✅ 저장"}, "value": val},
                {"type": "button", "action_id": "field_regen", "style": "danger",
                 "text": {"type": "plain_text", "text": "🔄 재생성"}, "value": val},
                {"type": "button", "action_id": "field_edit",
                 "text": {"type": "plain_text", "text": "✏️ 수정"}, "value": val}]}])
    except Exception:
        log.exception("field draft buttons post failed")

def _do_field_edit_nl(channel: str, thread_ts: str, work: str | None, field_name: str,
                      triple: tuple, feedback: str, bible: dict | None) -> None:
    """'줄거리를 좀 더 재미있게 바꿔야겠어' 류 — 단일 바이블 필드 자연어 수정 (2026-07-13)."""
    if not work:
        _reply(channel, thread_ts, "어떤 작품인지 못 찾았어요. `<작품>` 을 붙여서 다시 말씀해주세요.")
        return
    if not reference.sheet():
        _reply(channel, thread_ts, "시트가 아직 연결 안 됐어요 (SHEET_WEBAPP_URL 미설정).")
        return
    bkey = triple[3]
    is_plan = bkey == "episode_plan"
    ph = _thinking(channel, thread_ts, f"{field_name} 수정하는 중이에요…")
    try:
        if is_plan:
            current = (bible or {}).get(bkey, {})
            new_val = generator.complete(prompts.episode_plan_edit_system(),
                                         prompts.episode_plan_edit_user(current, feedback), timeout=90).strip()
        else:
            current = (bible or {}).get(bkey, "")
            new_val = generator.complete(prompts.field_edit_system(field_name),
                                         prompts.field_edit_user(field_name, current, feedback), timeout=90).strip()
    except Exception:
        log.exception("field edit generation failed")
        _post_chunks(channel, thread_ts, "수정 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
        return
    if not new_val:
        _post_chunks(channel, thread_ts, "모델이 수정안을 못 만들었어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
        return
    _post_field_draft(channel, thread_ts, work, field_name, triple, feedback, new_val, ph=ph)

_QUESTION_RE = re.compile(
    r"[?？]|뭐야|뭐지|뭔가요|무엇인가요|누구야|누구지|누구인가요|뭐가\s*있을까|뭐\s*없을까"
    r"|몇\s*화(?:야|지|인가요)|언제야|언제지|어디야|어디지|어떻게\s*되|왜\s*(?:이런|그런|저런)")

_PROGRESS_NL_RE = re.compile(
    r"(?:지금|이제|현재)?\s*(\d+)\s*화\s*(개요|대본|회차분배)?\s*"
    r"(?:작업|하고\s*있|진행\s*중|쓰는|쓰고\s*있|만들고\s*있)")

_VALIDATION_PHRASE_RE = re.compile(r"이대로\s*(?:되|괜찮|맞)|그대로\s*(?:되|괜찮)|이거\s*(?:맞|괜찮)")

def _is_pure_validation_q(feedback: str) -> bool:
    """검증질문 전용 판별. 문장이 검증 어구를 포함하고 '?'/'까요?' 등 질문으로 끝나야만 True —
    "이대로 괜찮을까, 근데 이것도 바꿔줘"처럼 뒤에 별도 지시가 붙으면(질문으로 안 끝남) False가
    돼서 기존 수정-분기 동작을 그대로 유지한다(보수적 접근)."""
    fb = (feedback or "").strip()
    if not fb or not _VALIDATION_PHRASE_RE.search(fb):
        return False
    return bool(re.search(r"[?？]\s*$|까요?\s*[.]?\s*$", fb))

_RESYNC_NL_RE = re.compile(
    r"노션.{0,10}(?:고쳤|수정했|바꿨|업데이트).{0,10}(?:다시\s*읽|재\s*동기화|반영|새로고침)"
    r"|노션.{0,15}(?:다시\s*읽|재\s*동기화)"
    r"|(?:다시\s*읽|재\s*동기화)\S*.{0,10}노션")

def _do_resync_nl(channel: str, thread_ts: str, work: str) -> None:
    """자연어로 '노션 다시 읽어줘' 요청 → 실제 노션 재조회 후 시트 반영 (2026-07-13)."""
    from bot.shared import works
    if works.page_of(work) and config.NOTION_TOKEN:
        _do_sync(channel, thread_ts, f"<{work}>")
    else:
        _reply(channel, thread_ts, f"*{work}* 은 등록된 노션 페이지가 없어서 다시 읽어올 게 없어요. "
                                    "`[동기화] <노션링크>` 로 먼저 등록해주세요.")

_LENGTH_CHECK_RE = re.compile(r"(?:분량|90\s*초|시간).{0,10}(?:맞|괜찮|넘|넘치|모자라|긴가|짧|충분)")

def _do_length_check(channel: str, thread_ts: str, feedback: str = "",
                     work: str | None = None, bible: dict | None = None) -> None:
    """대본 분량(≈90초) 체크. "날혐남 3화 대본 90초 맞아?"처럼 작품+화를 직접 말하면 그 작품의
    실제 대본(시트/노션)을 바로 찾아 체크하고, 특정 안 하면 스레드 직전 초안으로 폴백(2026-07-15).
    글자수 기반 기계적 계산 대신 LLM에 맡기는 이유: 대사·지문·나레이션 읽는 속도가 달라서
    단순 글자수/속도 나눗셈보다 LLM이 실제 낭독 느낌을 더 잘 추정함."""
    draft = None
    label = ""
    em = re.search(r"(\d+)\s*화", feedback)
    if em and work:
        _b = bible
        if not _b:
            sheet = reference.sheet()
            if sheet:
                try:
                    _b = sheet.get(work)
                except Exception:
                    log.exception("length check bible load failed")
        script = (_b or {}).get("scripts", {}).get(f"{em.group(1)}화")
        if script:
            draft, label = script, f"*{work}* {em.group(1)}화 대본 — "
    if not draft:
        draft = _last_assistant_draft(channel, thread_ts, "대본", None)
    if not draft:
        _reply(channel, thread_ts, "체크할 대본을 못 찾았어요. `<작품> N화 대본 90초 맞아?` 처럼 물어보거나, "
                                    "먼저 스레드에서 대본을 생성해 주세요.")
        return
    ph = _thinking(channel, thread_ts, "분량 체크 중이에요…")
    try:
        verdict = generator.complete(
            "너는 숏폼 드라마 분량 체크 도우미다. 아래 대본을 배우가 연기하듯 소리 내어 읽으면 "
            "대략 몇 초 걸릴지 추정하고, 목표(90초) 기준으로 많이 넘치는지/모자라는지 딱 한두 문장으로만 "
            "판정하라. 초 단위 추정치 + 판정만, 다른 설명·인사말 금지.",
            draft, timeout=60).strip()
    except Exception:
        log.exception("length check failed")
        _post_chunks(channel, thread_ts, "분량 체크 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
        return
    _post_chunks(channel, thread_ts, label + (verdict or "(빈 응답)"), replace_ts=ph)

def _do_progress_nl(channel: str, thread_ts: str, work: str, m: re.Match) -> None:
    """'지금 4화 작업 중이야' 류 — 진행상태(회차 생략 시 기준값)를 자연어로 즉시 갱신 (2026-07-13).
    짧은 상태 마커라 초안 검토 없이 바로 반영 — 잘못돼도 다시 말하면 그만이라 저위험."""
    ep, kind = m.group(1), m.group(2)
    status = f"{ep}화 {kind} 작업 중" if kind else f"{ep}화 작업 중"
    sheet = reference.sheet()
    if not sheet:
        _reply(channel, thread_ts, "시트가 아직 연결 안 됐어요 (SHEET_WEBAPP_URL 미설정).")
        return
    r = sheet.upsert(work, "진행상태", "", "", status)
    if isinstance(r, dict) and r.get("error"):
        _reply(channel, thread_ts, f"⚠️ 진행상태 저장 실패: {r['error']}")
        return
    sheet.invalidate(work)
    _reply(channel, thread_ts, f"✅ 진행상태를 *{status}* 로 갱신했어요. (회차 생략 시 이 화 기준으로 생성돼요)")

# ★2026-07-20 "자연어로도 피드백 인식" — [피드백] 명령 없이 "1화 대본 피드백 좀", "이 개요
# 평가해줘", "개연성 봐줘"처럼 자연어로 온 순수 피드백 요청도 구조화된 _do_feedback으로 라우팅
# 하려는 감지기. '피드백/리뷰'는 그 자체로 명확한 신호, '평가'는 동사와 함께일 때만(오탐 방지),
# '개연성/재미/완성도/퀄리티'는 평가를 묻는 동사와 함께일 때만 잡는다. 생성 동사가 같이 있는
# 복합 요청("평가하고 생성해줘")은 _do_freeform/_do_revise의 생성 분기가 먼저 처리한다.
_FEEDBACK_NL_RE = re.compile(
    r"피드백|리뷰|review|"
    r"평가\s*(?:해줘|해주|해봐|해도|좀|부탁|받|가능|해\b)|"
    r"(?:개연성|재미|완성도|퀄리티)\s*(?:좀\s*)?(?:어때|어떤지|봐\s*줘?|봐주|체크|점검|괜찮은지|있는지|있나|평가)")

def _feedback_nl_mode(q: str) -> str:
    """자연어 피드백 요청에서 재미만/개연성만/둘 다 판별."""
    fun, logic = bool(re.search(r"재미", q)), bool(re.search(r"개연성", q))
    return "fun" if fun and not logic else "logic" if logic and not fun else "both"

def _do_revise(channel: str, thread_ts: str, feedback: str) -> None:
    """스레드 후속 답글 → 스레드를 시작한 명령의 모드로 이어감 (아이디어는 아이디어, 생성은 수정 등)."""
    # '✏️ 수정' 버튼 클릭 후 대기 중인 캐릭터 카드가 있으면, 이 답글을 그 수정 지시로 반영
    # Bug6(2026-07-16): pending 상태가 디스크에 영구 저장되고(재시작 후에도 살아남음) 만료가
    # 없어서, 며칠 전 '✏️ 수정' 클릭 후 방치된 스레드에 완전히 무관한 후속 메시지("고마워!")가
    # 오면 그걸 옛 수정 지시로 오인·반영했다. 생성 시각(ts)을 기록해두고, 너무 오래됐으면
    # (30분 — 사용자가 버튼 누르고 답장 준비하는 데 걸릴 법한 시간보다 넉넉히 크게 잡음) 조용히
    # 버리고 일반 메시지 처리로 넘어간다.
    _PENDING_EDIT_TTL_SEC = 30 * 60
    # ★2026-07-20 🔄 재생성 클릭 후 "어떻게 다시 만들까요?"에 대한 답글 처리.
    rpend = _DRAFT_REGEN_PENDING.pop(thread_ts, None)
    if rpend and (time.time() - rpend.get("ts", 0)) > _PENDING_EDIT_TTL_SEC:
        rpend = None
        _save_draft_caches()
    if rpend:
        _save_draft_caches()
        _w, _top, _mid, _lv = rpend["work"], rpend["top"], rpend.get("mid"), rpend.get("level")
        path = f"<{_w}> {_top}" + (f" / {_mid}" if _mid else "") + (f" 강도 {_lv}" if _lv else "")
        # '그냥/그대로/응'류면 예전처럼 같은 걸 그대로 다시 뽑고, 아니면 이 답글을 수정 방향으로
        # 반영한다. _do_generate는 첫 줄을 경로로, 이후 줄을 '넣고 싶은 포인트/지시'로 쓰므로
        # 방향을 둘째 줄로 넘긴다(경로 파싱을 안 깨뜨림).
        if re.fullmatch(r"\s*(그냥|그대로|그대로\s*다시|똑같이|응|네|ok|okay|yes)\s*[.!~]*", feedback, re.I):
            _do_generate(channel, thread_ts, path)
        else:
            _do_generate(channel, thread_ts, f"{path}\n{feedback}")
        return
    pend = _CHAR_EDIT_PENDING.pop(thread_ts, None)
    if pend and (time.time() - pend.get("ts", 0)) > _PENDING_EDIT_TTL_SEC:
        pend = None
        _save_draft_caches()
    if pend:
        _save_draft_caches()
        bible = None
        sheet = reference.sheet()
        if sheet and pend["work"]:
            try:
                bible = sheet.get(pend["work"])
            except Exception:
                log.exception("char edit bible load failed")
        combined_fb = pend["feedback"] + f"\n(추가 수정 지시: {feedback})"
        ctx = pend.get("context", "")
        existing = pend.get("existing")
        ph = _thinking(channel, thread_ts, f"{pend['name']} 카드에 반영해서 다시 만드는 중이에요…")
        if existing:
            data, err = _generate_char_edit(pend["work"], pend["name"], existing, combined_fb, bible, ctx)
        else:
            data, err = _generate_char_card(pend["work"], pend["name"], combined_fb, bible, ctx)
        if err:
            _post_chunks(channel, thread_ts, err, replace_ts=ph)
            return
        _post_char_draft(channel, thread_ts, pend["work"], pend["name"], combined_fb, data, ph=ph,
                         context=ctx, existing=existing)
        return
    # '✏️ 수정' 버튼 클릭 후 대기 중인 단일 필드(줄거리 등) 수정안이 있으면 반영 (Bug6: 위와 동일하게 만료 체크)
    fpend = _FIELD_EDIT_PENDING.pop(thread_ts, None)
    if fpend and (time.time() - fpend.get("ts", 0)) > _PENDING_EDIT_TTL_SEC:
        fpend = None
        _save_draft_caches()
    if fpend:
        _save_draft_caches()
        bible = None
        sheet = reference.sheet()
        if sheet and fpend["work"]:
            try:
                bible = sheet.get(fpend["work"])
            except Exception:
                log.exception("field edit bible load failed")
        combined_fb = fpend["feedback"] + f"\n(추가 수정 지시: {feedback})"
        bkey = fpend["triple"][3]
        current = (bible or {}).get(bkey, "")
        ph = _thinking(channel, thread_ts, f"{fpend['field']} 반영해서 다시 만드는 중이에요…")
        try:
            new_val = generator.complete(prompts.field_edit_system(fpend["field"]),
                                         prompts.field_edit_user(fpend["field"], current, combined_fb),
                                         timeout=90).strip()
        except Exception:
            log.exception("field edit(pending) generation failed")
            _post_chunks(channel, thread_ts, "수정 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
            return
        _post_field_draft(channel, thread_ts, fpend["work"], fpend["field"], fpend["triple"],
                          combined_fb, new_val, ph=ph)
        return
    messages = _thread_messages(channel, thread_ts)
    if not messages:
        _reply(channel, thread_ts, _HELP)
        return
    joined = "\n".join(m["content"] for m in messages)
    work = _work_from_thread(joined)
    work = (works.resolve(work) or work) if work else None    # 별칭 → 정식 작품명

    # 1순위: 답글에 '대본/개요/줄거리 + 만들어줘' 류 생성 요청이 있으면 → 그 종류를 새로 생성
    #        (예: "그럼 2화 대본도 써줘", "전체 줄거리 만들어줘")
    # 질문형("2화 대본 다시 봐줄래? 나레이션 줄일 데 있을까?")은 제외 — 원래 여기 가드가 없어서
    # 조언을 구하는 질문이 "대본 만들게요"로 오인돼 원치 않는 생성이 시작되던 문제(2026-07-14, F3②).
    _jobs = _parse_gen_jobs(feedback)
    # Bug2(2026-07-16): '다시'는 원래 생성 동사로 쳤는데, "3화 대본 다시 확인해줘"처럼 다시가
    # 확인/검토류 동사를 데리고 오면 "재확인"이지 "재생성"이 아니다. 그 조합만 동사 판정에서
    # 빼서(다른 데 '써/쓰/짜' 등 진짜 생성 동사가 따로 있으면 그건 그대로 유효) 오발화 방지.
    _gen_verb_src = re.sub(r"다시\s*(?:확인|봐|보여|읽어|검토)", "", feedback)
    if (_jobs and work and re.search(r"(만들|작성|생성|뽑|그려|써|쓰|짜|추가|다시)", _gen_verb_src)
            and not _QUESTION_RE.search(feedback)
            # Bug3(2026-07-16): "지금 4화 대본 쓰고 있어"류 진행상태 보고는 gen-jobs보다 더
            # 구체적인 신호(_PROGRESS_NL_RE)이므로, 겹치면 진행상태 분기(아래)에 양보한다.
            and not _PROGRESS_NL_RE.search(feedback)):
        _CANCEL.discard(thread_ts)
        _reply(channel, thread_ts, "요청 확인: " + ", ".join(
            (t if e is None else f"{e}화 {t}") for t, e in _jobs) + " 만들게요.")
        for top, ep in _jobs:
            _do_generate(channel, thread_ts,
                         f"<{work}> {top}" + (f" / {ep}화" if ep is not None else ""))
        return
    # 순수 자연어 피드백("이 대본 피드백 좀", "개연성 봐줘") → 구조화된 피드백으로(2026-07-20).
    # 생성 의도(위 분기)는 이미 처리됐고, 질문형("어때?")은 _FEEDBACK_NL_RE가 피드백 단어 없인
    # 안 잡으므로 아래 질문 분기로 그대로 간다.
    if _FEEDBACK_NL_RE.search(feedback):
        _do_feedback(channel, thread_ts, feedback, mode=_feedback_nl_mode(feedback))
        return

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
    # 답글이 '아이디어 떠올려줘 / 사건 뭐하지?' 류면 직전 활동과 무관하게 아이디어 모드로
    # 단, 원래 트렌드 스레드였으면(2026-07-14) 그 트렌드 데이터를 아이디어 프롬프트로 넘겨서
    # "방금 그 데이터 기반으로 구체적으로"를 요청했는데 데이터 근거가 끊기는 문제 방지
    # (실측: cathy가 "신분숨김 재회물 구체적인 아이디어" 요청 시 트렌드 근거 없이 일반 창작만 나옴).
    _idea_trend_ctx = ""
    if _IDEA_INTENT_RE.search(feedback):
        if mode == "trend":
            try:
                _tr = reference.load_trend()
                if _tr:
                    _idea_trend_ctx = _tr.answer(feedback, llm=_trend_filter_llm)
            except Exception:
                log.exception("idea-from-trend 데이터 로드 실패")
        mode = "idea"
    # 스레드 모드와 무관하게 '이미 있는 인물 이름 + 필드어(설정/포지션/외형 등) + 바꿔야겠어' 류면
    # 자연어 캐릭터 수정으로 인식 (2026-07-13, ex: "민재 설정은 서브남주로 바꿔야겠어")
    existing_chars = (bible or {}).get("characters") or {}
    _mentioned = next((nm for nm in existing_chars if nm in feedback), None)
    if (_mentioned and _EDIT_INTENT_RE.search(feedback)
            and any(w in feedback for w in _CHAR_FIELD_WORDS)
            and not _is_pure_validation_q(feedback)):
        _do_char_edit_nl(channel, thread_ts, work, _mentioned, feedback, bible)
        return
    # 스레드 모드(예: [생성]에서 이어진 'gen')와 무관하게 '인물/캐릭터 설정 추가' 요청이면
    # 여기서 자유 텍스트로 답하면 실제 저장 없이 "노션에도 반영"처럼 지어낸 완료 멘트가 나가던
    # 문제가 있었음(2026-07-13) — 실제로 없는지 확인하고, 없으면 만들어서 시트에 진짜로 저장한다.
    if _CHAR_ADD_RE.search(feedback) and mode == "gen":
        _do_char_add(channel, thread_ts, work, feedback, bible)
        return
    # '노션 방금 고쳤어, 다시 읽어줘' 류 — 즉시 재동기화 (2026-07-13)
    if _RESYNC_NL_RE.search(feedback) and work:
        _do_resync_nl(channel, thread_ts, work)
        return
    # '지금 4화 작업 중이야' 류 — 진행상태 자연어 즉시 갱신 (2026-07-13)
    _pm = _PROGRESS_NL_RE.search(feedback)
    if _pm and work:
        _do_progress_nl(channel, thread_ts, work, _pm)
        return
    # 단일 바이블 필드(줄거리 등)를 자연어로 바꿔달라는 요청 (2026-07-13,
    # ex: "줄거리를 좀 더 재미있게 바꿔야겠어")
    _fword, _ftriple = _find_field_edit(feedback)
    if (_fword and _EDIT_INTENT_RE.search(feedback) and work
            and not _is_pure_validation_q(feedback)):
        _do_field_edit_nl(channel, thread_ts, work, _fword, _ftriple, feedback, bible)
        return
    # '이거 90초 맞아?'/'분량 괜찮아?' 류 — 물었을 때만 체크(2026-07-15). _QUESTION_RE도
    # "?"로 끝나서 걸리므로, 일반 질문 분기보다 먼저 검사해서 실제 대본을 보고 판정하게 한다.
    if mode == "gen" and _LENGTH_CHECK_RE.search(feedback):
        _do_length_check(channel, thread_ts, feedback, work=work, bible=bible)
        return
    # 순수 질문("민재 설정 뭐야?")은 'gen' 스레드 안에서도 대본/개요 재생성 초안으로 새지 않고
    # 바로 답만 하고 끝냄 (2026-07-14, E2 — 질문 vs 수정 지시 구분 없이 다 revise로 새서
    # 질문했는데 수정 초안이 나오던 문제). 수정 의도 신호(_EDIT_INTENT_RE)가 같이 있으면
    # 지시로 보고 이 분기를 건너뛴다.
    if (mode == "gen" and _QUESTION_RE.search(feedback)
            and (not _EDIT_INTENT_RE.search(feedback) or _is_pure_validation_q(feedback))):
        qph = _thinking(channel, thread_ts, "확인 중이에요…")
        qsys = prompts.freeform_system(bible)
        qem = re.search(r"(\d+)\s*화", feedback)
        if bible and qem:
            qextra = prompts.freeform_episode_context(bible, int(qem.group(1)))
            if qextra:
                qsys += "\n\n" + qextra
        try:
            qans = generator.complete(qsys, feedback).strip()
        except Exception:
            log.exception("revise question answer failed")
            _post_chunks(channel, thread_ts, "답하는 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=qph)
            return
        # FREEFORM_ROLE은 무관한 질문이면 정확히 '도움말'이라고만 답하게 하는데, _do_freeform엔
        # 그걸 실제 안내문(_GUIDE)으로 바꿔주는 안전망이 있었지만 여기(E2)엔 빠져 있어서 사용자에게
        # 진짜로 "도움말" 한 마디만 나간 적이 있었음(2026-07-15) — 동일한 안전망 적용.
        if not qans or qans.replace("*", "").strip() in ("도움말", "(빈 응답)"):
            _post_chunks(channel, thread_ts, _GUIDE, replace_ts=qph)
        else:
            _post_chunks(channel, thread_ts, qans, replace_ts=qph)
        return
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
        note = "확정한 씬 설계로 상세 콘티(GPT 이미지용) 만드는 중이에요… (몇 초~1분)"
    elif sb_stage == "detail":
        note = "상세 콘티 고치는 중이에요…"
    ph = _thinking(channel, thread_ts, note)

    gen_buttons = None       # gen(초안 수정) 모드일 때만 [<종류> 생성]/[수정] 버튼 부착
    try:
        if mode == "sb" and sb_generate:
            # 1단계→2단계: 확정된 '씬 설계안'의 씬 순서·시간을 지켜, 대본을 샷 단위 '상세 콘티'로 전개.
            answer = generator.complete(
                prompts.storyboard_system(bible, target_episode=target),
                _convo_text(messages) + sb_ref_block
                + "\n\n(위 대화에서 마지막으로 확정된 '씬 설계안'의 씬 순서·시간 배분을 지켜라. "
                  "[원본 대본]을 영상문법가이드 정본 예시처럼 **샷 단위 상세 콘티**(프레임에 잡히는 것·카메라·대사·나레이션 처리·연기 뉘앙스 명시)로 전개하되, "
                  "대본의 사건·행동·대사는 하나도 바꾸지 마라.)",
                timeout=300)
            answer = SB_BADGE_BOARD + answer
        elif mode == "sb" and sb_stage == "detail":
            # 상세 콘티가 이미 나온 뒤의 후속 피드백 → 바뀐 샷/구간만 재출력
            answer = generator.complete(
                prompts.storyboard_system(bible, target_episode=target),
                _convo_text(messages) + sb_ref_block
                + "\n\n(위 상세 콘티에서 마지막 작가 요청대로 **바뀐 샷/구간만** 내라 "
                  "— 안 바뀐 데는 다시 쓰지 말고, 맨 위에 '바꾼 점:' 한 줄. [원본 대본]과 어긋나지 않게, 대본 내용은 바꾸지 마라. 전체 재출력 금지.)",
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
            # 기획안 스레드 후속 → '직전 기획안 + 이번 요청'만 넣어 수정(전체 대화 넣으면 입력 비대 → agent max-turns 에러)
            prev_md = next((m["content"] for m in reversed(messages) if m["role"] == "assistant"), "")
            pu = (f"[현재 기획안]\n{prev_md}\n\n[요청]\n{feedback}\n"
                  "위 기획안에서 요청대로만 고치고, 같은 구조로 전체 기획안을 다시 내라. 안 바뀐 부분은 그대로 유지.")
            answer = generator.complete(prompts.plan_system(feedback), pu)
            # 스레드에 노션 링크가 있으면 → 바뀐 섹션만 그 페이지에서 교체
            nmv = re.search(r"https?://\S*notion\.\S+", joined)
            pid = None
            if nmv:
                from bot.shared import notion_sync
                pid = notion_sync.extract_page_id(nmv.group(0))
            if pid and config.NOTION_TOKEN and prev_md and not _is_valid_plan(answer):
                answer += "\n\n_⚠️ 결과가 기획안 형식이 아니라 노션엔 안 썼어요._"
            elif pid and config.NOTION_TOKEN and prev_md:
                idx = _first_changed_section(prev_md, answer)
                if idx is None:
                    answer += "\n\n_(노션: 바뀐 섹션이 없어 그대로 둠)_"
                else:
                    try:
                        notion_sync.replace_from_section(pid, answer, idx)
                        answer += f"\n\n_✅ 노션 페이지의 {idx + 1}번째 섹션부터 업데이트했어요._"
                    except Exception:
                        log.exception("plan section replace failed")
                        answer += "\n\n_⚠️ 노션 업데이트 실패 — 권한/연결 확인. (초안은 위에)_"
        elif mode == "idea":
            bible_i = _idea_intensity(bible, feedback)   # 아이디어 기본 강도 3 고정
            _idea_user = _convo_text(messages)
            if _idea_trend_ctx:
                _idea_user += f"\n\n[참고 트렌드 데이터 — 방금 트렌드 대화에서 이어짐]\n{_idea_trend_ctx}"
            answer = generator.complete(prompts.idea_system(bible_i, feedback, target_episode=target),
                                        _idea_user)
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
            if answer.strip().startswith("[확인필요]"):
                # 초안이 아니라 확인 질문 — 버튼 없이 질문만 보여줌
                answer = answer.strip()[len("[확인필요]"):].strip()
            else:
                # work 유무와 무관하게 항상 버튼 부착 (work=None이면 "생성" 버튼만 숨김)
                _kind = next((k for k in ("개요", "대본", "줄거리") if k in feedback), None)
                if not _kind:
                    _kind = _thread_gen_context(messages)[1] \
                        or next((k for k in ("개요", "대본", "줄거리") if k in joined), None)
                gen_buttons = (work or "", _kind or "개요", target)
    except Exception:
        log.exception("revise failed")
        _post_chunks(channel, thread_ts, "이어가는 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
        return
    if _cancelled(channel, thread_ts, ph):
        return
    if gen_buttons:
        # LLM이 스레드 컨텍스트에서 복사한 확정 꼬리말 제거 (버튼으로 대체되므로 불필요)
        answer = re.sub(r"\n+_?📝\s*초안[^\n]*저장하세요[._]*\s*$", "", answer or "",
                        flags=re.S | re.I).strip()
        answer = re.sub(r"\n+_?📝\s*초안[^\n]*\[입력\][^\n]*\s*$", "", answer,
                        flags=re.S | re.I).strip()
    # 스레드가 이어받은 모드가 'gen'(대본/개요 수정)이 아니면 답 위에 짧게 표시 (2026-07-14, E1 —
    # 원래 _thinking 플레이스홀더에만 모드가 잠깐 떴다가 답으로 교체되며 사라져서, 이 스레드가
    # 지금 어떤 모드로 이어지고 있는지(예: 여전히 '아이디어'로 잡혀서 수정 지시가 또 아이디어로
    # 나가는 경우) 사용자가 눈치채기 어려웠음.
    _mode_tag = {"idea": "💡 아이디어 모드", "trend": "📈 트렌드 모드", "plan": "📋 기획안 모드",
                 "sb": "🎬 스토리보드 모드", "fun": "🎭 피드백(재미) 모드",
                 "logic": "🧩 피드백(개연성) 모드", "feedback": "📝 피드백 모드"}.get(mode)
    if _mode_tag:
        banner = f"_{_mode_tag}로 이어서 답할게요 — 대본/개요를 고치려면 `[생성]`으로 다시 불러주세요._"
        # 한 메시지에 배너가 두 번 붙는 사고 방지(2026-07-15, 8번) — 답변에 이미 같은
        # 배너가 섞여 있으면 먼저 지운다.
        answer = re.sub(re.escape(banner) + r"\n*", "", answer or "").strip()
        # 이 스레드의 직전 봇 메시지가 이미 같은 모드 배너로 시작했으면(=모드가 안 바뀜)
        # 또 붙이지 않는다 — 원래는 거의 매 응답마다 붙어서 소음이었음.
        _prev_assistant = next((m["content"] for m in reversed(messages)
                                if m["role"] == "assistant"), "")
        if not _prev_assistant.strip().startswith(banner):
            answer = f"{banner}\n\n" + answer
    _post_chunks(channel, thread_ts, answer or "(빈 응답)", replace_ts=ph)
    if gen_buttons:
        # ★2026-07-20 "저장 누르니깐 저장 안 하고 재생성함" — 예전엔 수정된 초안에 [🆕 생성]/
        # [✏️ 수정] 버튼(_post_revise_actions)만 붙어서 '저장' 버튼이 아예 없었다. 사용자가 방금
        # 고친 초안을 저장하려고 눈에 띄는 [🆕 생성]을 누르면 저장이 아니라 새로 생성돼버림.
        # 수정 결과도 엄연한 초안이므로 [✅ 통과(저장)]/[🔄 재생성] 버튼(_post_draft_actions)을
        # 붙여, 마음에 들면 바로 저장하고 아니면 재생성(방향 다시 묻기)할 수 있게 한다.
        _gb_work, _gb_kind, _gb_ep = gen_buttons
        if _gb_work:
            _post_draft_actions(channel, thread_ts, _gb_work, _gb_kind,
                                f"{_gb_ep}화" if _gb_ep else "")
        else:
            # 작품 미등록이면 시트 저장 자체가 안 되므로 예전처럼 수정 안내만.
            _post_revise_actions(channel, thread_ts, *gen_buttons)

def _do_plan(channel: str, thread_ts: str, rest: str, files_text: str = "", in_thread: bool = False) -> None:
    """[기획] 컨셉·로그라인 → 노션 기획안 구조 초안 (로그라인·타겟·인물·줄거리·회차분배). 초안만, 자동저장 X.
    링크 페이지에 기획안이 있으면 수정:
      · 첫 호출(스레드 아님) → 원본을 시트로 백업(원본 보존·시트 생성) + 전체 수정 + 노션 싹 교체
      · 스레드 답글 후속 → 바뀐 섹션만 부분 교체
    files_text: 첨부 파일(기획서·인물 등) — 명령과 분리해 '참고 자료'로 주입."""
    concept = rest.strip()
    file_ctx = ""
    if files_text and files_text.strip():
        file_ctx = ("\n\n[첨부 참고 자료 — 이 작품의 설정·자료. 바탕으로 삼되 없는 사실은 지어내지 마라]\n"
                    + files_text.strip()[:12000])
    from bot.shared import notion_sync, works
    nm = re.search(r"https?://\S*notion\.\S+", concept)
    write_page_id = notion_sync.extract_page_id(nm.group(0)) if nm else None
    if nm:
        concept = concept.replace(nm.group(0), "").strip()   # 링크는 컨셉에서 제거
    if not write_page_id and in_thread and config.NOTION_TOKEN:   # 스레드 후속: 앞선 링크 회수
        joined = "\n".join(m["content"] for m in _thread_messages(channel, thread_ts))
        tm = re.search(r"https?://\S*notion\.\S+", joined)
        if tm:
            write_page_id = notion_sync.extract_page_id(tm.group(0))

    # 링크 페이지에 이미 기획안이 있으면 → 수정 모드
    if write_page_id and config.NOTION_TOKEN:
        try:
            current_md = notion_sync.page_text(write_page_id)
        except Exception:
            current_md = ""
        if _is_valid_plan(current_md):
            if len(concept) < 2:
                _reply(channel, thread_ts,
                       "그 페이지엔 이미 기획안이 있어요. 고칠 내용을 함께 적어주세요.\n"
                       "예: `[기획] <링크> 여주를 더 능동적으로, 회차분배 5막으로`")
                return
            _CANCEL.discard(thread_ts)
            user_msg = (f"[현재 기획안]\n{current_md}\n\n[요청]\n{concept}\n"
                        "위 기획안에서 요청대로만 고치고, 같은 구조로 전체 기획안을 다시 내라. "
                        "안 바뀐 부분은 그대로 유지." + file_ctx)

            if not in_thread:
                # ── 첫 [기획]: 원본을 새 시트로 백업(보존) + 전체 수정 + 노션 싹 교체 ──
                sheet = reference.sheet()
                work = works.work_by_page(write_page_id)
                new_work = work is None
                if not work:
                    try:
                        work = works.sanitize(notion_sync.page_title(write_page_id)) or "제목없음"
                    except Exception:
                        work = "제목없음"
                works.register(work, write_page_id)
                ph = _thinking(channel, thread_ts,
                               f"🛠 원본을 '{work}' 시트에 백업하고, 기획안 전체를 수정하는 중이에요…")
                if sheet:                              # 원본 보존 = 시트 생성/갱신 (백업 실패해도 진행 안 함)
                    try:
                        _sync_apply(sheet, work, current_md)
                    except Exception:
                        log.exception("plan backup-to-sheet failed")
                        _post_chunks(channel, thread_ts,
                                     "원본을 시트에 백업하지 못해서 노션은 건드리지 않았어요. 잠시 후 다시 시도해 주세요.",
                                     replace_ts=ph)
                        return
                try:
                    answer = generator.complete(prompts.plan_system(concept), user_msg).strip()
                except Exception:
                    log.exception("plan whole-modify failed")
                    _post_chunks(channel, thread_ts,
                                 "수정 중 오류가 났어요. (원본은 시트에 백업돼 있어요.)", replace_ts=ph)
                    return
                if _cancelled(channel, thread_ts, ph):
                    return
                if not _is_valid_plan(answer):
                    _post_chunks(channel, thread_ts,
                                 (answer or "(빈 응답)") + "\n\n_⚠️ 결과가 기획안 형식이 아니라 노션엔 안 썼어요 (원본은 시트에 백업)._",
                                 replace_ts=ph)
                    return
                try:
                    notion_sync.replace_markdown(write_page_id, answer)   # 페이지 싹 교체
                    foot = (f"\n\n_✅ 노션 페이지를 수정본으로 교체했어요. 원본은 *{work}* 시트에 백업"
                            + ("(새 작품 등록). " if new_work else ". ")
                            + "이어서 이 스레드에 답글로 고치면 바뀐 부분만 반영해요._")
                    if new_work:
                        foot += (f"\n\n📌 작품명은 *<{work}>* 이에요! → `[생성] <{work}> 3화` "
                                 "(답글 `[별칭] 짧은이름`으로 줄일 수 있어요.)")
                except Exception:
                    log.exception("plan replace_markdown failed")
                    foot = "\n\n_⚠️ 노션 교체 실패 — 통합 연결/권한 확인 (원본은 시트에 백업)._"
                _post_chunks(channel, thread_ts, answer + foot, replace_ts=ph)
                return

            # ── 스레드 후속: 바뀐 섹션만 부분 교체 ──
            ph = _thinking(channel, thread_ts, "기획안 부분 수정 중이에요…")
            try:
                answer = generator.complete(prompts.plan_system(concept), user_msg).strip()
            except Exception:
                log.exception("plan revise failed")
                _post_chunks(channel, thread_ts, "수정 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
                return
            if _cancelled(channel, thread_ts, ph):
                return
            if not _is_valid_plan(answer):             # 결과가 기획안 형식이 아니면 노션 오염 방지 위해 안 씀
                _post_chunks(channel, thread_ts,
                             (answer or "(빈 응답)") + "\n\n_⚠️ 결과가 기획안 형식이 아니라 노션엔 안 썼어요. 다시 시도해 주세요._",
                             replace_ts=ph)
                return
            idx = _first_changed_section(current_md, answer)
            view = _plan_changed_view(current_md, answer)   # 슬랙엔 바뀐 섹션만
            if idx is None or view is None:
                _post_chunks(channel, thread_ts, "🔧 요청 반영했지만 기존과 달라진 섹션이 없어요.", replace_ts=ph)
                return
            try:
                notion_sync.replace_from_section(write_page_id, answer, idx)
                foot = f"\n\n_✅ 노션 페이지에 반영했어요 ({idx + 1}번째 섹션부터)._"
            except Exception:
                log.exception("plan revise replace failed")
                foot = "\n\n_⚠️ 노션 수정 실패 — 권한/연결 확인._"
            _post_chunks(channel, thread_ts, view + foot, replace_ts=ph)
            return

    # 스레드에서 [기획]을 치면 그 스레드 대화(트렌드·아이디어 논의 등)를 근거로 삼는다.
    messages = _thread_messages(channel, thread_ts)
    thread_ctx = _convo_text(messages) if len(messages) > 1 else ""
    if len(concept) < 3 and not thread_ctx and not file_ctx:
        _reply(channel, thread_ts,
               "형식: `[기획] <컨셉/로그라인/키워드>`\n"
               "예: `[기획] 라이벌 아이돌 룸메이트 BL, 스캔들 나면 끝장`\n"
               "(트렌드·아이디어 스레드에서 `[기획]`만 쳐도, 또는 기획서·인물 파일을 첨부해도 됩니다.)")
        return
    seed = concept if len(concept) >= 3 else (" ".join(m["content"] for m in messages)[:300] or files_text[:300])
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
                    + (f" 특히 이 방향으로: {concept}" if len(concept) >= 3 else "") + file_ctx)
    else:
        base = f"이 컨셉으로 기획안 초안을 만들어줘:\n{concept}" if len(concept) >= 3 else "첨부 자료를 바탕으로 기획안 초안을 만들어줘."
        user_msg = base + file_ctx
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
    # 링크가 주어졌으면 그 노션 페이지에 기획안 본문을 기록
    footer = ("\n\n_📝 기획안 초안입니다. 다듬어서 `[생성]`으로 개요·대본을 뽑으세요._"
              "\n_(노션 링크를 주면 작품으로 기록돼요.)_")
    if write_page_id and config.NOTION_TOKEN and not _is_valid_plan(answer):
        footer = "\n\n_⚠️ 결과가 기획안 형식이 아니라 노션엔 안 썼어요. 다시 시도해 주세요._"
    elif write_page_id and config.NOTION_TOKEN:
        try:
            notion_sync.append_markdown(write_page_id, answer)
            new_work = works.work_by_page(write_page_id) is None
            wname = works.work_by_page(write_page_id) or ""
            if new_work:                              # 새 작품으로 등록 → 이후 부를 수 있게
                try:
                    wname = works.sanitize(notion_sync.page_title(write_page_id)) or "제목없음"
                except Exception:
                    wname = "제목없음"
                works.register(wname, write_page_id)
            footer = f"\n\n_✅ 위 기획안을 노션 페이지에 기록했어요 (작품: *{wname}*)._"
            if new_work and wname:                    # 새 작품이면 짧은 별칭 권장
                footer += f"\n_이름이 길면 답글로 `[별칭] {wname} 짧은이름` 하면 짧게 부를 수 있어요._"
        except Exception:
            log.exception("notion append failed")
            footer = ("\n\n_⚠️ 노션 기록 실패 — 통합에 '콘텐츠 삽입/업데이트' 권한이 있고 "
                      "그 페이지가 통합에 연결됐는지 확인해 주세요. (초안은 위에 있어요)_")
    _post_chunks(channel, thread_ts, (answer or "(빈 응답)") + footer, replace_ts=ph)

def _do_idea(channel: str, thread_ts: str, rest: str, force_generic: bool = False) -> None:
    """[아이디어 제시] — 추상적 고민을 구체적이고 간단한 상황 2~3개로. 작품 바이블+DB 근거.
    force_generic=True: 작품 없이 '일반 아이디어'로 바로 생성(사용자가 그렇게 골랐을 때)."""
    q = rest.strip()
    work, bible = None, None
    wm = SUB_RE.match(q)
    if wm:
        work = works.resolve(wm.group(1).strip()) or wm.group(1).strip()
        q = wm.group(2).strip()
    if not work:                       # <작품> 없으면 스레드(첫 댓글의 작품/노션 링크)에서 회수
        w = _work_from_thread("\n".join(m["content"] for m in _thread_messages(channel, thread_ts)))
        if w:
            work = works.resolve(w) or w
    work = work or _single_registered_work()
    if not work and q and not force_generic:   # 작품 못 잡음 → 일반으로 할지 물어봄
        _reply(channel, thread_ts,
               "작품명이 없어요 🙂 이대로면 작품 설정 없이 **일반 아이디어**로 드려요.\n"
               "• 그대로 원하면 이 스레드에 `응`(또는 `일반으로`) 답글\n"
               f"• 더 정확히 하려면 작품을 넣어 다시: `[아이디어] <작품> {q[:40]}…`")
        return
    if work:
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

def _json_loads_array(raw: str) -> list:
    """회차분배 등 LLM 응답에서 JSON 배열만 안전하게 추출 (2026-07-13, A1/A2)."""
    s = re.sub(r"^```(json)?", "", raw.strip()).strip()
    s = re.sub(r"```$", "", s).strip()
    a, b = s.find("["), s.rfind("]")
    if a >= 0 and b > a:
        s = s[a:b + 1]
    return json.loads(s)

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

def _alias_clean(tok: str) -> str:
    """별칭 토큰에서 자연어 앞머리·꼬리·따옴표·괄호 제거.
    예: '이제부터 코니로 불러줘' → '코니', '코니테스트라고 불러줘' → '코니테스트'."""
    t = (tok or "").strip().strip("\"'`<>[]（）()「」《》 ").strip()
    t = re.sub(r"^(?:이제부터|이제|앞으로|그냥|부터|우리|얘를?|이거를?|이걸|작품을?)\s*", "", t).strip()
    t = re.sub(r"\s*(?:이?라고|으?로)?\s*(?:불러줘|불러|부를게|불러라|해줘|해|하자|할게|등록해?줘?|등록)?$", "", t).strip()
    return t

def _do_alias(channel: str, thread_ts: str, rest: str) -> None:
    """[별칭] — 작품에 부를 이름 추가. 자연스러운 형태 지원:
      · [별칭] <작품> 코니테스트, 테스트      · [별칭] cony 테스트 작품 = 코니테스트
      · (스레드에서) [별칭] 코니테스트          ← 그 스레드가 다룬 작품에 자동 연결"""
    txt = rest.strip()
    work = None
    from_link = False
    # 0) 노션 링크가 함께 왔으면 그 링크가 가리키는 작품을 최우선으로 확정
    #    (스레드가 기억한 이전 작품에 엉뚱하게 별칭이 등록되던 문제, 2026-07-15)
    txt_unwrapped = re.sub(r"<(https?://[^>|]+)(?:\|[^>]*)?>", r"\1", txt)
    lm = _NOTION_LINK.search(txt_unwrapped)
    if lm:
        no_link = _NOTION_LINK.sub("", txt_unwrapped).strip()
        _sm = SUB_RE.match(no_link)
        _exp = (works.resolve(_sm.group(1).strip()) or _sm.group(1).strip()) if _sm else None
        w = _autosync_link(channel, thread_ts, lm.group(0), explicit=_exp)
        if w:
            work = w
            from_link = True
            txt = _sm.group(2).strip() if _sm else no_link
    wm = None if from_link else SUB_RE.match(txt)            # 1) <작품> 명시
    if wm:
        work = wm.group(1).strip()
        txt = wm.group(2).strip()
    if not work:                                             # 2) 'A = B' / 'A: B' / 'A -> B'
        parts = re.split(r"\s*(?:=|:|→|->|⇒)\s*", txt, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            work, txt = parts[0].strip(), parts[1].strip()
    if not work:                                             # 3) 스레드에서 다룬 작품 회수 (링크 없을 때만)
        joined = "\n".join(m["content"] for m in _thread_messages(channel, thread_ts))
        work = _work_from_thread(joined)
    work = (works.resolve(work) or work) if work else None
    aliases_raw = [_alias_clean(a) for a in re.split(r"[,、/]| 그리고 ", txt) if _alias_clean(a)]
    # 2) 자연어 문장 전체가 별칭으로 딸려오는 것 방지: 너무 길거나(>15자) 문장 구조(괄호/조사)면 거부
    _SENTENTIAL_RE = re.compile(r"[()（）]|(?:는데요|인데요|거든요|이에요|예요|습니다|입니다)")
    aliases, rejected = [], []
    for a in aliases_raw:
        if len(a) > 15 or _SENTENTIAL_RE.search(a):
            rejected.append(a)
        else:
            aliases.append(a)
    if not work:
        known = ", ".join(works.all_works().keys()) or "(아직 없음)"
        _reply(channel, thread_ts,
               "어느 작품의 별칭인가요? 작품을 앞에 적어주세요 → `[별칭] <작품> 짧은이름`\n"
               f"등록된 작품: {known}\n"
               "_(작품이 아직 없으면 노션 링크를 붙여 등록하거나 `[기획]`으로 새로 만든 뒤 별칭을 다세요.)_")
        return
    if not works.resolve(work):
        _reply(channel, thread_ts, f"'{work}' 라는 작품을 아직 못 찾았어요. 먼저 노션 링크로 등록해 주세요.")
        return
    if rejected and not aliases:
        _reply(channel, thread_ts,
               f"*{work}* 에 등록하려던 게 문장처럼 길어서(`{rejected[0][:30]}{'…' if len(rejected[0]) > 30 else ''}`) "
               "별칭으로 등록하지 않았어요. 15자 이내 짧은 이름으로 다시 알려주세요 → "
               f"`[별칭] <{work}> 짧은이름`")
        return
    if not aliases:
        _reply(channel, thread_ts, f"`{work}`을(를) 뭐라고 부를까요? 예: `[별칭] <{work}> 코니테스트`")
        return
    canon = works.add_aliases(work, aliases)
    nice = ", ".join(f"`{a}`" for a in aliases)
    note = f"\n_(너무 길어서 등록 안 함: `{rejected[0][:30]}…`)_" if rejected else ""
    _reply(channel, thread_ts,
           f"✅ *{canon}* 에 별칭 등록: {nice}{note}\n예: `[생성] <{aliases[0]}> 3화`")

def _do_freeform(channel: str, thread_ts: str, query: str) -> None:
    """명령어 없이 던진 자유 질문 → 실제로 답변. 명확한 의도는 해당 기능으로 라우팅,
    아니면 (작품 맥락 있으면 바이블 근거로) 일반 답변, 영 아니면 도움말 안내."""
    q = query.strip()
    if not q:
        _reply(channel, thread_ts, _GUIDE)
        return
    # 0) 첫인사/모호한 첫 대화 → 의도 판별 캐스케이드 전에 먼저 걸러서 항상 같은
    # 온보딩 안내로 응답 (2026-07-16, 오디오 A). 예전엔 '안녕'/'뭐 할 수 있어?' 같은
    # 말이 의도 캐스케이드를 타면서 LLM 자유응답이 나오거나 _GUIDE가 나오는 게 뒤죽박죽이었음.
    if _GREETING_RE.search(q) and not SUB_RE.match(q):
        _reply(channel, thread_ts, _ONBOARD_FIRST_CONTACT)
        return
    # 1) 명확한 의도 → 해당 기능
    # 생성 의도('<작품> 2화 대본 만들어줘', '개요 써줘') → 실제 생성으로 라우팅.
    # 질문형('줄거리 뭐였지?', '대본 어떻게 써?')은 제외하고 일반 답변으로 넘김.
    _gm = SUB_RE.match(q)
    _gen_src = _gm.group(2).strip() if _gm else q
    if (_parse_gen_jobs(_gen_src)
            and re.search(r"(만들|작성|생성|뽑|그려|써|쓰|짜)", _gen_src)
            and not re.search(r"(뭐|뭔|무엇|어때|어떻|어케|알려|설명|였지|궁금|인가|일까|해야|\?)", _gen_src)):
        # 복합 요청('로그라인과 키워드 평가하고 1~3화 개요를 써봐')이 생성 의도로만 라우팅되면서
        # 평가(피드백) 절반이 조용히 버려지던 문제(2026-07-15, 6번) — 평가 동사가 같이 있으면
        # 먼저 피드백을 실행하고 이어서 생성한다.
        if re.search(r"평가|피드백|리뷰|review", _gen_src):
            _reply(channel, thread_ts, "①피드백 ②개요/대본 생성 순서로 진행할게요.")
            _do_feedback(channel, thread_ts, q, mode="both")
        _do_generate(channel, thread_ts, q)
        return
    # 순수 자연어 피드백("1화 대본 피드백 좀", "개연성 봐줘") → 구조화된 피드백으로(2026-07-20).
    # 위 복합(생성+평가) 분기를 지난 뒤이므로 생성 의도가 없는 피드백 요청만 여기 걸린다.
    if _FEEDBACK_NL_RE.search(q):
        _do_feedback(channel, thread_ts, q, mode=_feedback_nl_mode(q))
        return
    if re.search(r"트렌드|유행|요즘 (뭐|뭔)|뜨는|인기\s*(있|많|글)", q):
        _do_trend(channel, thread_ts, q)
        return
    if _IDEA_INTENT_RE.search(q):
        _do_idea(channel, thread_ts, q)     # 작품 없으면 자체적으로 안내
        return
    if _MAKE_WORK_RE.search(q):
        _reply(channel, thread_ts,
               "새 작품·기획안을 만들까요? `[기획] <컨셉/로그라인>` (노션 링크 주면 그 페이지에 기록)\n"
               "예: `[기획] 라이벌 아이돌 룸메 BL, 스캔들 나면 끝장`")
        return
    # 2) 작품 맥락(명시 <작품> or 문장 속 등록명/노션링크) 있으면 바이블 근거로 일반 답변.
    # (2026-07-14) 기존엔 <작품> 태그가 없으면 바로 work=None으로 빠져서, 'N화 나레이션 줄일
    # 데 있을까' 같은 화-특정 질문도 바이블 없이(=그 화 대본 못 읽고) 답하던 문제 —
    # _do_generate 등과 같은 순서(태그 → 문장 속 작품명/링크 → 등록 1개뿐이면 그거)로 회수.
    work, bible = None, None
    wm = SUB_RE.match(q)
    if wm:
        work = works.resolve(wm.group(1).strip()) or wm.group(1).strip()
        q = wm.group(2).strip() or q
    if not work:
        w = _work_from_thread(q)
        work = (works.resolve(w) or w) if w else None
    if not work:
        work = _single_registered_work()
    # 등록된 작품이 하나도 없으면(연동 자체가 안 된 상태) 애매한 자유 답변을 LLM에 그냥
    # 던지는 대신(예: '저연프' 같은 알아듣기 힘든 한 단어에 엉뚱하게 되묻던 문제,
    # 2026-07-20) 노션 연결부터 안내한다.
    if not work and not works.all_works():
        _reply(channel, thread_ts,
               "아직 연동된 작품이 없어요 🙂 `[동기화] <노션링크>` 로 작품 노션 페이지를 먼저 "
               "연결해주세요. 그다음부터 이 스레드에서 편하게 물어보면 돼요.")
        return
    # 화 번호까지 콕 집었는데 어느 작품인지 여러 개라 못 골랐으면, 바이블 없이 얼버무려 답하는
    # 대신 바로 되물어서 정확한 답을 받게 한다(오늘 실측: 2화 대본을 못 읽어 애매하게 답함).
    if not work and re.search(r"\d+\s*화", q) and len(works.all_works()) > 1:
        names = ", ".join(works.all_works().keys())
        _reply(channel, thread_ts,
               f"어느 작품인지 알려주세요 🙂 그래야 그 화 대본을 실제로 보고 답할 수 있어요.\n"
               f"등록된 작품: {names}\n예: `<작품명> {q}`")
        return
    if work:
        sheet = reference.sheet()
        if sheet:
            try:
                bible = sheet.get(work)
            except Exception:
                log.exception("freeform bible load failed")
    # '날혐남 3화 대본 90초 맞아?' 류 — 첫 멘션에서도(스레드 없이) 동작 (2026-07-15)
    if _LENGTH_CHECK_RE.search(q):
        _do_length_check(channel, thread_ts, q, work=work, bible=bible)
        return
    _CANCEL.discard(thread_ts)
    ph = _thinking(channel, thread_ts, "생각 중이에요…")
    system = prompts.freeform_system(bible)
    if bible:
        # 질문이 특정 화를 콕 집으면(예: '2화 나레이션 줄일 데') 그 화 자체의 개요·대본을
        # 직접 보여준다 — build_bible_block은 '다음 화 참고'용이라 질문 대상 화 자체는 안 보여줌
        em = re.search(r"(\d+)\s*화", q)
        if em:
            extra = prompts.freeform_episode_context(bible, int(em.group(1)))
            if extra:
                system += "\n\n" + extra
    try:
        ans = generator.complete(system, q).strip()
    except Exception:
        log.exception("freeform failed")
        _post_chunks(channel, thread_ts, _GUIDE, replace_ts=ph)
        return
    if _cancelled(channel, thread_ts, ph):
        return
    if not ans or ans.replace("*", "").strip() in ("도움말", "(빈 응답)"):
        _post_chunks(channel, thread_ts, _GUIDE, replace_ts=ph)   # 영 아니면 도움말
    else:
        _post_chunks(channel, thread_ts, ans, replace_ts=ph)

def _do_check(channel: str, thread_ts: str, rest: str) -> None:
    """[확인] <작품> 질문 → 바이블 근거로 한 문장만 답. (작품명·캐릭터 등 빠른 조회)"""
    q = rest.strip()
    work = None
    wm = SUB_RE.match(q)
    if wm:
        work = works.resolve(wm.group(1).strip()) or wm.group(1).strip()
        q = wm.group(2).strip()
    if not work:                                   # 스레드에서 작품 회수 (노션 링크/·<작품>)
        joined = "\n".join(m["content"] for m in _thread_messages(channel, thread_ts))
        w2 = _work_from_thread(joined)
        if w2:
            work = works.resolve(w2) or w2
    work = work or _single_registered_work()      # 등록 작품이 하나뿐이면 그걸로
    if not work:
        _reply(channel, thread_ts, "어느 작품인지 알려주세요: `[확인] <작품> 지금 캐릭터 누구 있지?`")
        return
    if not q:
        _reply(channel, thread_ts, f"뭘 확인할까요? 예: `[확인] <{work}> 지금 캐릭터 누구 있지?`")
        return
    # 별칭/작품명 질문은 바이블이 아니라 등록소에서 바로 답
    if re.search(r"별칭|별명|약칭|닉네임|뭐라고\s*(부|불)|어떻게\s*(부|불)|무슨\s*이름|이름.*(뭐|무엇)", q):
        al = (works.all_works().get(work, {}).get("aliases")) or []
        if al:
            _reply(channel, thread_ts, f"*{work}* 의 별칭: " + ", ".join(al))
        else:
            _reply(channel, thread_ts,
                   f"*{work}* 은 아직 별칭이 없어요. `[별칭] 짧은이름` 으로 추가할 수 있어요.")
        return
    sheet = reference.sheet()
    bible = None
    if sheet:
        try:
            bible = sheet.get(work)
        except Exception:
            log.exception("check bible load failed")
    if not bible:
        _reply(channel, thread_ts,
               f"'{work}' 바이블(작품 설정 정보)을 아직 못 찾았어요. 먼저 노션 링크로 동기화해 주세요.\n"
               "예: `[동기화] <노션링크>` 또는 그냥 노션 페이지 링크를 이 채널에 붙여넣기만 해도 등록돼요.")
        return
    _CANCEL.discard(thread_ts)
    ph = _thinking(channel, thread_ts, "확인 중이에요…")
    try:
        ans = generator.complete(prompts.check_system(bible), f"[질문] {q}").strip()
    except Exception:
        log.exception("check failed")
        _post_chunks(channel, thread_ts, "확인 중 오류가 났어요. 잠시 후 다시.", replace_ts=ph)
        return
    if _cancelled(channel, thread_ts, ph):
        return
    ans = " ".join((ans or "(빈 응답)").split())    # 여러 줄이 와도 한 줄로 눌러줌
    _post_chunks(channel, thread_ts, ans, replace_ts=ph)

_NOTION_LINK = re.compile(r"https?://\S*notion\.\S+")

def _autosync_link(channel: str, thread_ts: str, url: str, explicit: str | None = None) -> str | None:
    """노션 링크 → (필요시) 새 작품 등록 + 시트 동기화. 반환: 정식 작품명(실패 시 None).
    명령어(예: [생성])에 처음 보는 링크가 섞였을 때 '먼저 등록·동기화' 용."""
    from bot.shared import notion_sync, works
    sheet = reference.sheet()
    if not sheet or not config.NOTION_TOKEN:
        return None
    pid = notion_sync.extract_page_id(url)
    if not pid:
        return None
    existing = works.work_by_page(pid)
    try:                                              # 읽기 성공 = MCP(통합) 연결 확인
        title = notion_sync.page_title(pid)
        content = notion_sync.page_text(pid)
    except Exception:
        log.exception("autosync link fetch failed")
        _reply(channel, thread_ts,
               "노션 링크를 못 읽었어요. 그 페이지 `•••` → *연결* → 통합(MCP) 추가 후 다시 시도해 주세요.")
        return existing            # 이미 등록된 작품이면 옛 바이블로라도 진행
    work = explicit or existing or works.sanitize(title) or "제목없음"
    works.register(work, pid)
    ph = _thinking(channel, thread_ts, f"노션 '{work}' 동기화하는 중이에요…")
    try:
        done, failed, summary = _sync_apply(sheet, work, content, page_id=pid)
    except Exception:
        log.exception("autosync link apply failed")
        _post_chunks(channel, thread_ts,
                     f"'{work}' 노션 동기화 중 오류가 났어요 (명령은 계속 진행).", replace_ts=ph)
        return work if existing else None
    if existing is None and not explicit:
        _post_chunks(channel, thread_ts,
                     f"🆕 새 작품 *<{work}>* 등록·동기화 완료 — {done}개 반영. "
                     f"이렇게 부르면 돼요 → `[생성] <{work}> 3화` (답글 `[별칭] 짧은이름`으로 줄일 수 있어요). "
                     "이어서 요청 처리할게요…", replace_ts=ph)
    else:
        _post_chunks(channel, thread_ts,
                     f"🔄 *{work}* 노션 동기화 완료 — {done}개 반영. 이어서 요청 처리할게요…", replace_ts=ph)
    return work

def _do_sync(channel: str, thread_ts: str, rest: str) -> None:
    """[동기화] — 노션 링크만 주면: ①MCP 연결 확인 ②처음 보는 페이지면 제목으로 새 작품(시트) 생성.
    링크 대신 `<작품>` + 내용 붙여넣기도 지원. LLM이 스키마로 파싱 → 시트 upsert."""
    from bot.sheet_bible import CHAR_SUBS
    from bot.shared import notion_sync, works
    sheet = reference.sheet()
    if not sheet:
        _reply(channel, thread_ts, "시트가 아직 연결 안 됐어요 (SHEET_WEBAPP_URL 미설정).")
        return
    raw = rest.strip()
    if not raw:
        _reply(channel, thread_ts,
               "형식: `[동기화] <노션링크>` (링크만 주면 페이지 제목으로 새 작품이 만들어져요)\n"
               "또는 `[동기화] <작품>` 하고 아래에 노션 내용을 통째로 붙여넣기.")
        return
    src = "붙여넣은 내용"
    new_work = False
    # 선택적 <작품> 지정 파싱 (없으면 링크/제목에서 작품명을 얻는다)
    sm = SUB_RE.match(raw)
    _raw_work = sm.group(1).strip() if sm else ""
    if _raw_work.startswith("http") or "notion." in _raw_work:   # <https://…> = 슬랙이 감싼 링크, 작품명 아님
        _raw_work = ""
    explicit = (works.resolve(_raw_work) or _raw_work) if _raw_work else None
    body = (sm.group(2) if sm else raw).strip()
    nm = re.search(r"https?://\S*notion\.\S+", raw)     # 링크는 <작품> 앞뒤 어디에 있어도 인식
    if nm:
        pid = notion_sync.extract_page_id(nm.group(0))
        if not pid:
            _reply(channel, thread_ts, "노션 링크에서 페이지 ID를 못 찾았어요. 링크를 다시 확인해 주세요.")
            return
        page_id = pid   # ★2026-07-20: 아래 else 분기의 page_id와 이름을 맞춰 _sync_apply 호출부를 공유
        if not config.NOTION_TOKEN:
            _reply(channel, thread_ts, "노션 토큰이 설정 안 돼서 링크를 못 읽어요. `<작품>` + 내용 붙여넣기로 해주세요.")
            return
        _CANCEL.discard(thread_ts)
        ph0 = _thinking(channel, thread_ts, "노션 페이지 읽는 중이에요… (연결 확인)")
        try:                                            # ① 읽기 성공 = MCP(통합) 연결 확인
            title = notion_sync.page_title(pid)
            content = notion_sync.page_text(pid)
            src = "노션 페이지"
        except Exception:
            log.exception("notion page fetch failed")
            _post_chunks(channel, thread_ts,
                         "이 페이지를 못 읽었어요. 노션에서 그 페이지 `•••` → *연결* → 통합(MCP)을 추가한 뒤 다시 시도해 주세요.",
                         replace_ts=ph0)
            return
        # ② 작품명 결정: 명시 > 이미 등록된 id > 페이지 제목(=신규 작품)
        existing = works.work_by_page(pid)
        work = explicit or existing or works.sanitize(title) or "제목없음"
        new_work = existing is None and not explicit
        works.register(work, pid)                       # 신규면 등록, 기존이면 매핑 갱신
        # ★2026-07-20 "노션에도 필수로 추가" — 이 page_id가 처음 등록되는 것이면(existing is
        # None, <작품>을 명시했든 안 했든 상관없이 — new_work 플래그와 별개 조건) 노션 본문에서
        # 장르(실사화/2D 애니메이션)를 찾아본다. 있으면 그대로 반영, 없으면 이미 지정된 게
        # 아닌 이상 "장르 필수" 표시를 남겨 스틸컷/영상 생성 전에 반드시 `[스타일]`로 지정하게
        # 만든다(works.genre_required — 이미 등록돼 있던 기존 작품은 이 표시가 절대 안 붙으므로
        # 기존 작품들의 생성 흐름은 전혀 영향받지 않는다).
        if existing is None:
            genre_key = works.parse_style_key(content)
            if genre_key:
                works.set_style(work, genre_key)
            elif not works.get_style(work):
                works.mark_genre_required(work)
        ph = ph0
    else:
        # 링크 없음 → <작품> 필수 + 붙여넣은 내용/등록된 페이지 사용
        if not explicit:
            _reply(channel, thread_ts,
                   "노션 링크를 주거나, `[동기화] <작품>` 뒤에 노션 내용을 붙여넣어 주세요.")
            return
        work = explicit
        content = body
        page_id = works.page_of(work) or (config.NOTION_PAGES or {}).get(work)
        if len(content) < 50 and config.NOTION_TOKEN and page_id:
            _CANCEL.discard(thread_ts)
            ph0 = _thinking(channel, thread_ts, "등록된 노션 페이지 읽는 중이에요…")
            try:
                content = notion_sync.page_text(page_id)
                src = "노션 페이지"
            except Exception:
                log.exception("notion page fetch failed")
                _post_chunks(channel, thread_ts,
                             "노션 페이지를 못 읽었어요. 페이지가 통합에 연결됐는지 확인해 주세요.", replace_ts=ph0)
                return
        if len(content) < 50:
            hint = "" if page_id else "\n(처음이면 `[동기화] <노션링크>` 로 페이지를 등록하세요 — 이후엔 자동 반영.)"
            _reply(channel, thread_ts,
                   "동기화할 노션 내용을 `<작품>` 뒤에 붙여넣거나 노션 링크를 주세요 (줄거리·인물·회차분배·개요 등)." + hint)
            return
        ph = None
    _CANCEL.discard(thread_ts)
    note = f"{src} 정리해서 시트에 반영하는 중이에요…"
    if ph:
        _update_note(channel, ph, note)            # 읽는 중 플레이스홀더 재사용
    else:
        ph = _thinking(channel, thread_ts, note)
    try:
        done, failed, summary = _sync_apply(sheet, work, content, page_id=page_id)
    except ValueError:   # JSON 파싱 실패
        # 인식된 섹션이 0개일 때 뭘 찾았는지(헤딩/블록)까지 보여줘야 사용자가 뭘 고쳐야
        # 할지 알 수 있음(2026-07-15) — 소제목 후보로 보이는 줄만 추려 함께 안내.
        found = [ln.strip("# ").strip() for ln in content.split("\n")
                 if ln.strip().startswith("#") or (ln.strip() and len(ln.strip()) <= 20
                                                    and ln.strip().endswith((":", "："))
                                                    )][:5]
        found = [f for f in found if f]
        detail = (f" 지금 페이지엔 {', '.join(found)} 같은 블록만 보이고 "
                  "줄거리/등장인물/회차분배/개요 소제목이 안 보여요." if found
                  else " 지금 페이지엔 소제목으로 보이는 줄이 하나도 없어요.")
        msg = ("노션 내용을 구조로 못 읽었어요." + detail
               + " 예: `## 줄거리` 처럼 소제목을 달아주세요.")
        if _is_dup_last(channel, thread_ts, msg):
            msg = "(바로 위와 같은 이유로) 이번에도 반영된 항목이 없어요."
        _post_chunks(channel, thread_ts, msg, replace_ts=ph)
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
    head = f"🆕 새 작품 *{work}* 등록 + " if new_work else f"✅ *{work}* "
    msg = f"{head}{src} 동기화 — {done}개 반영.\n· " + "\n· ".join(summary)
    if new_work:
        msg += (f"\n\n📌 작품명은 *<{work}>* 이에요! 이렇게 부르면 돼요 → `[생성] <{work}> 3화`\n"
                f"이름이 길면 답글로 `[별칭] 짧은이름` 만 치면 짧게도 부를 수 있어요. 노션 수정하면 자동 반영돼요.")
        # ★2026-07-20: 신규 등록인데 노션 본문에서 장르를 못 찾았으면(works.mark_genre_required)
        # 스틸컷/영상 생성 전에 반드시 알려줘야 한다 — 나중에 스토리보드 단계에서 막히기 전에
        # 등록 완료 메시지에서 바로 요청.
        if works.genre_required(work):
            msg += (f"\n\n⚠️ *필수*: 이 작품의 장르(실사화/2D 애니메이션)를 노션 본문에서 못 찾았어요 "
                    f"— 스틸컷·영상 만들기 전에 `[스타일] <{work}> 실사화` 또는 "
                    f"`[스타일] <{work}> 2d 애니메이션`으로 꼭 지정해주세요.")
    if failed:
        msg += f"\n⚠️ {failed}개는 네트워크 문제로 실패 — 다시 `[동기화]` 하면 그 부분만 채워집니다."
    _post_chunks(channel, thread_ts, msg, replace_ts=ph)

def _normalize_gu(raw: str) -> str:
    """'1막. 지옥 같은 결혼생활' / '1막' 등 표기가 재동기화마다 달라져서 같은 막이 시트에
    중복 행으로 계속 쌓이던 문제 방지(2026-07-13, 실측: 날혐남 회차분배 6막이 12행으로 중복).
    항상 'N막' 형태로만 통일해서 upsert 키가 매번 같게 만든다."""
    # Bug7(2026-07-16): 원래 프리픽스 매칭이라 "1막합본"도 "1막"으로 뭉개져서 별개 막을 같은
    # 시트 행에 덮어썼다. "막" 뒤에 공백/구두점/문자열 끝이 와야만(단어 경계) 정규화 대상으로
    # 보고, "1막합본"처럼 "막" 바로 뒤에 다른 글자가 붙으면 매칭 안 시켜서 별개 라벨로 취급한다.
    # "1막. 지옥 같은 결혼생활"(이 함수가 원래 고치려던 케이스)은 "막" 뒤가 "."(구두점)이라
    # 그대로 "1막"으로 정규화된다.
    m = re.match(r"\s*(\d+)\s*막(?=\s|[.,!?/·\-–—]|$)", raw or "")
    return f"{m.group(1)}막" if m else (raw or "").strip()

def _sync_apply(sheet, work: str, content: str, page_id: str | None = None) -> tuple[int, int, list]:
    """동기화 소스 텍스트 → LLM 스키마 파싱 → 시트 upsert. 슬랙 무관(백그라운드 재사용).
    반환 (done, failed, summary). JSON 파싱 실패 시 ValueError.

    page_id: 노션 페이지 ID가 있으면(=content가 그 페이지 텍스트) 대본/상세콘티가 몇 화
    분량 있는지도 요약에 같이 보여준다(2026-07-20, 사용자 요청 — "요약에 대본/상세콘티
    반영 개수도 추가"). 대본/상세콘티 자체는 시트로 옮기지 않고 매번 노션에서 직접 읽으므로
    (위 주석 참고) 이건 순수 안내용 카운트일 뿐, sheet.upsert 대상이 아니다."""
    from bot.sheet_bible import CHAR_SUBS
    from bot.shared import notion_sync
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
    # 인물 이름은 재동기화마다 LLM 추출 표기가 달라질 수 있어(예: '민재'↔'김민재') 그대로 upsert
    # 키로 쓰면 회차분배 막처럼 중복 행이 쌓일 위험이 있음(2026-07-14, D2) — 기존 등록된 이름과
    # 오타/표기 수준으로만 다르면 기존 이름으로 합친다.
    try:
        _existing_names = list(((sheet.get(work) or {}).get("characters") or {}).keys())
    except Exception:
        _existing_names = []
    for r in chars:
        nm = r["이름"].strip()
        if nm not in _existing_names:
            hit = difflib.get_close_matches(nm, _existing_names, n=1, cutoff=0.6)
            if hit:
                nm = hit[0]
        for k in CHAR_SUBS:
            if r.get(k):
                _up("등장인물", nm, k, str(r[k]).strip())
    if chars:
        summary.append(f"인물 {len(chars)}명")
    plan = [r for r in (data.get("회차분배") or []) if (r.get("막") or "").strip()]
    for r in plan:
        gu = _normalize_gu(r["막"])
        for k in ("구간", "화수", "핵심사건"):
            if r.get(k):
                _up("회차분배", gu, k, str(r[k]).strip())
    if plan:
        summary.append(f"회차분배 {len(plan)}막")
    outs = [r for r in (data.get("개요") or []) if (r.get("화") or "").strip() and r.get("내용")]
    for r in outs:
        _up("개요", r["화"].strip(), "", str(r["내용"]).strip())
    if outs:
        summary.append(f"개요 {len(outs)}화")
    # 대본은 시트로 안 옮긴다 — SYNC_SYSTEM이 애초에 안 뽑지만, 혹시 모델이 넣어도 방어적으로 무시.
    # 대본은 노션에서 매번 직접 읽는다(bot/sheet_bible.py의 _notion_scripts) — 2026-07-13 결정.
    if page_id:
        try:
            n_scripts = len(notion_sync.parse_episode_scripts(content))
            if n_scripts:
                summary.append(f"대본 {n_scripts}화")
        except Exception:
            log.exception("동기화 요약: 대본 화수 집계 실패")
        try:
            n_contis = notion_sync.count_conti_episodes(page_id, token=config.NOTION_TOKEN)
            if n_contis:
                summary.append(f"상세콘티 {n_contis}화")
        except Exception:
            log.exception("동기화 요약: 상세콘티 화수 집계 실패")

    sheet.invalidate(work)
    return done, failed, summary

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

def _do_feedback(channel: str, thread_ts: str, rest: str, mode: str = "both",
                 force_generic: bool = False) -> None:
    """[피드백] 대본 평가. mode='both'(재미+개연성)/'fun'(재미만)/'logic'(개연성만).
    force_generic=True: 작품 없이 '일반 기준'으로 바로 평가(사용자가 그렇게 골랐을 때)."""
    q = rest.strip()
    work, bible = None, None
    wm = SUB_RE.match(q)
    if wm:
        work = works.resolve(wm.group(1).strip()) or wm.group(1).strip()
        q = wm.group(2).strip()
    if not work:                       # <작품> 안 쓰면 스레드(첫 댓글의 작품/노션 링크)에서 회수
        joined0 = "\n".join(m["content"] for m in _thread_messages(channel, thread_ts))
        w = _work_from_thread(joined0)
        if w:
            work = works.resolve(w) or w
    work = work or _single_registered_work()
    if not work and not force_generic:   # 작품 못 잡음 → 일반 기준으로 할지 물어봄
        _reply(channel, thread_ts,
               "작품명이 없어요 🙂 이대로면 작품 설정 대조 없이 **일반 기준**으로만 평가해요 (개연성 대조 약함).\n"
               "• 그대로 원하면 이 스레드에 `응`(또는 `일반으로`) 답글\n"
               "• 개연성까지 정확히 하려면 작품을 넣어: `[피드백] <작품> 3화`")
        return
    if work:
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
    eval_kind = "개요" if want_outline else "대본"
    src_kind = ""
    draft = q if len(q) >= 30 else ""                    # ① 직접 붙여넣은 내용 우선
    # ①.5 '로그라인 피드백'류 요청은 스레드의 마지막 메시지를 무작정 긁어오는 대신
    # 등록된 작품의 바이블/노션 로그라인을 우선 사용한다(2026-07-15, 3번 — 버튼 안내
    # 문구를 '대본'으로 오인해 평가하던 문제의 근본 대응).
    if not draft and bible and re.search(r"로그라인", q):
        logline = (bible.get("logline") or "").strip()
        if logline:
            draft = logline
            eval_kind = "로그라인"
            src_kind = "등록된 로그라인"
    if not draft and bible and ep_cmd:                   # ② 'N화 (개요/대본)' 명시 → 시트 저장본
        key = "outlines" if want_outline else "scripts"
        saved = (bible.get(key) or {}).get(f"{ep_cmd.group(1)}화", "")
        if len(saved.strip()) >= 30:
            draft = saved
            src_kind = f"시트의 {ep_cmd.group(1)}화 {eval_kind}"
    if not draft:                                        # ③ 스레드 직전 '실제 초안'(확정·오류·버튼안내 제외)
        d = _last_assistant_draft(channel, thread_ts)
        if d:
            draft = _clean_draft(d)
    min_len = 10 if eval_kind == "로그라인" else 30
    if len(draft) < min_len:
        msg = ("평가할 대본/개요를 못 찾았어요. 이렇게 해보세요:\n"
               "• 대본을 **바로 붙여넣기**: `[피드백] <작품> (여기에 대본)`\n"
               "• 시트 저장본으로: `[피드백] <작품> 3화` (개요면 `3화 개요`)\n"
               "• 생성 스레드 안에선: 그 초안 아래 답글로 `[피드백]`만")
        _reply_dedup(channel, thread_ts, msg)
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
                     prompts.fun_user(draft, lens_level=lvl, kind=eval_kind),
                     post_fn=(lambda a, L=lvl: f"*🎚️ 강도 {L}단계 관점*\n\n" + _verify_fun_score(a)))
        else:
            _run(prompts.fun_system(bible, target_episode=target),
                 prompts.fun_user(draft, kind=eval_kind), post_fn=_verify_fun_score)
        if _cancelled(channel, thread_ts, ph if first else None):
            return
    if mode in ("logic", "both"):   # 개연성 지적 (엄격도: 명령 강도 N > 시트 개연성 강도)
        strict = (lens_levels[0] if lens_levels and len(lens_levels) == 1 else None)
        if strict is None and bible:
            strict = (bible.get("intensity_map") or {}).get("개연성")
        sys_text = prompts.feedback_system(bible, target_episode=target, mode="logic", strictness=strict)
        okw = ("‼️ 이건 개요(회차 설계)다 — 대사·구체 씬이 없는 게 정상이니 사건 구성·흐름의 개연성만 보라. "
               if eval_kind == "개요" else "")
        _run(sys_text, f"{okw}‼️ 아래 [{eval_kind}]에 실제로 적힌 것만 검토하라. [작품 바이블]은 대조용 배경일 뿐, "
                       f"그 배경을 {eval_kind}으로 착각하지 마라.\n\n[평가할 {eval_kind}]\n{draft}")

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
        work = works.resolve(wm.group(1).strip()) or wm.group(1).strip()
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

_PLAN_ACCEPT_RE = re.compile(r"기획|만들어\s*줘|만들자|생성해?\s*줘|해\s*줘|응|네|좋아|ㅇㅋ|ㅇㅇ|고고|가자")

_MAKE_WORK_RE = re.compile(r"(작품|기획안?)\s*(을|를|으로|로)?\s*(만들|짜|잡아|써|생성|시작)|이걸로\s*\S*\s*(작품|기획)")

def _thread_parent_files_text(channel: str, thread_ts: str) -> str:
    """스레드에서 첨부 파일이 있는 첫 메시지의 파일 텍스트를 회수 (후속 답글엔 파일이 안 실림)."""
    try:
        resp = app.client.conversations_replies(channel=channel, ts=thread_ts,
                                                 limit=config.THREAD_HISTORY_LIMIT)
    except Exception:
        log.exception("thread parent files fetch failed")
        return ""
    for m in resp.get("messages", []):
        if m.get("files"):
            ft, _ = _files_text(m)
            if ft.strip():
                return ft
    return ""

def _do_ref(channel: str, thread_ts: str, rest: str, event: dict) -> None:
    """[참조] <작품> 인물[, 인물2] + 이미지 첨부 → data/refs/<작품>/<인물>.<ext> 저장(얼굴 고정값).
    - 이미지 1장 + 이름 1개 → 그 이름으로 저장
    - 이미지 N장 + 이름 N개(콤마/줄바꿈 구분) → 순서대로 매칭
    - 이름 생략 → 첨부 파일명(강태혁.png)을 이름으로 사용
    - 첨부 없음 → 그 작품에 등록된 참조 목록만 보여줌"""
    from bot import openrouter_image as oi

    wm = SUB_RE.match(rest or "")
    if not wm:
        _reply(channel, thread_ts,
               "작품을 `<작품>`으로 알려주세요: `[참조] <날혐남> 강태혁` + 이미지 첨부")
        return
    work = works.resolve(wm.group(1).strip()) or wm.group(1).strip()
    names = [n.strip() for n in re.split(r"[,/\n]|\s{2,}", (wm.group(2) or "").strip()) if n.strip()]

    imgs = _image_files(event)
    if not imgs:
        regs = oi.registered_refs(work)
        if regs:
            _reply(channel, thread_ts,
                   f"<{work}> 등록된 참조: {', '.join(regs)}\n"
                   "새로 등록하려면 이미지를 첨부하고 `[참조] <작품> 인물`로 보내주세요.")
        else:
            _reply(channel, thread_ts,
                   f"이미지 첨부가 없어요. `[참조] <{work}> 강태혁` 처럼 쓰고 얼굴 이미지를 함께 올려주세요.\n"
                   "(png·jpg·jpeg·webp)")
        return

    # 이미지 ↔ 이름 매핑
    if not names:
        pairs = [(stem, ext, data) for stem, ext, data in imgs]
    elif len(names) == len(imgs):
        pairs = [(names[i], imgs[i][1], imgs[i][2]) for i in range(len(imgs))]
    elif len(names) == 1:
        pairs = [(names[0], imgs[0][1], imgs[0][2])]     # 이름 하나면 첫 이미지에만
    else:
        _reply(channel, thread_ts,
               f"이미지 {len(imgs)}장과 이름 {len(names)}개가 안 맞아요. "
               "이미지 1장에 이름 1개로 보내거나, 이미지 수만큼 이름을 콤마로 나눠주세요.")
        return

    d = config.OPENROUTER_REFS_DIR / work
    d.mkdir(parents=True, exist_ok=True)
    saved, extra = [], max(0, len(imgs) - len(pairs))
    for nm, ext, data in pairs:
        nm = unicodedata.normalize("NFC", nm).strip()
        if not nm:
            continue
        for e in _REF_SAVE_EXTS:                          # 같은 이름의 기존 참조는 확장자 불문 교체
            p = d / f"{nm}{e}"
            if p.exists():
                p.unlink()
        (d / f"{nm}{ext}").write_bytes(data)
        saved.append(nm)

    if not saved:
        _reply(channel, thread_ts, "저장할 인물 이름을 못 읽었어요. `[참조] <작품> 강태혁`처럼 이름을 적어주세요.")
        return
    msg = (f"✅ <{work}> 캐릭터 참조 등록: *{', '.join(saved)}*\n"
           "이제 `[이미지]` 생성 때 이 얼굴로 고정돼요. (콘티에 이 이름이 나오면 자동 첨부)")
    if extra:
        msg += f"\n(이미지 {extra}장은 이름이 없어 건너뛰었어요 — 이름을 콤마로 나눠 다시 보내주세요.)"
    _reply(channel, thread_ts, msg)

def _do_export(channel: str, thread_ts: str, rest: str, cmd: str = "파일") -> None:
    """[파일] <md|txt|csv> [파일명] — 내보낼 내용은 (1)명령 아래 줄/첨부, 없으면 (2)스레드의 마지막 봇 답변.
    `[md]`/`[txt]`/`[csv]`처럼 형식을 명령으로 바로 줄 수도 있음. CSV는 마크다운 표를 자동 변환."""
    rest = (rest or "").strip("\n")
    head, _, inline = rest.partition("\n")
    if cmd in _EXPORT_TYPES:                            # [csv] ... 처럼 형식을 명령으로 준 경우
        ftype, name_toks = cmd, head.split()
    else:
        toks = head.split()
        if toks and toks[0].lower() in _EXPORT_TYPES:
            ftype, name_toks = toks[0].lower(), toks[1:]
        else:
            ftype, name_toks = "md", toks              # 형식 미지정 → md, head 전체를 파일명으로
    ext = _EXPORT_TYPES[ftype]

    content = inline.strip()
    if not content:                                    # 인라인 내용 없으면 스레드의 마지막 봇 답변
        for m in reversed(_thread_messages(channel, thread_ts)):
            if m["role"] == "assistant" and m["content"].strip():
                content = m["content"]
                break
    if not content:
        _reply(channel, thread_ts,
               "파일로 내보낼 내용을 못 찾았어요. 명령 아래 줄에 내용을 붙이거나, 봇 답변이 있는 스레드에서 써주세요.")
        return

    base = "_".join(name_toks).strip()
    base = re.sub(r"[^\w가-힣.\-]+", "_", base).strip("_.")
    if not base:
        base = f"cowriter_{int(time.time())}"
    if base.lower().endswith(ext):
        base = base[: -len(ext)]
    filename = base + ext

    if ext == ".csv":
        csv_text = _md_table_to_csv(content)
        if csv_text is None:                           # 표가 아니면 줄 단위 1열 CSV
            import csv as _csv
            import io
            buf = io.StringIO()
            for ln in content.splitlines():
                _csv.writer(buf).writerow([ln])
            csv_text, note = buf.getvalue(), "줄 단위 CSV로"
        else:
            note = "표를 인식해 CSV로"
        data = ("﻿" + csv_text).encode("utf-8")   # 엑셀 한글 안 깨지게 BOM
    else:
        data, note = content.encode("utf-8"), f"{ftype.upper()} 파일로"

    try:
        app.client.files_upload_v2(
            channel=channel, thread_ts=thread_ts, file=data,
            filename=filename, title=filename,
            initial_comment=f"📄 {note} 내보냈어요 — `{filename}`")
    except Exception as e:
        log.exception("export upload failed")
        _reply(channel, thread_ts,
               f"파일 업로드에서 막혔어요: {e}\n(앱에 *files:write* 권한이 필요해요)")

# NOTE (merge-time, 2026-07-16): the inflight-tracking block that used to live here
# (_INFLIGHT / _INFLIGHT_LOCK / _INFLIGHT_KEYS / _inflight_load / _inflight_save /
# _inflight_add / _inflight_done / _replay_inflight, verbatim from co-writer-bot/app.py)
# has been MOVED to bot/dispatch.py instead of staying here. Reason: _replay_inflight()'s
# body calls `_handle_dispatch(ev)` -- in the single-bot original that was co-writer's own
# 228-line elif-chain in the same file, but in the merged bot the equivalent function is the
# real merged dispatch order (storyboard _maybe_* chain + co-writer narrow chain + brackets,
# see dispatch.py), which only exists in dispatch.py. Leaving a second, dead copy here (whose
# _replay_inflight would silently NameError on `_handle_dispatch` the first time the bot
# actually needed to replay a crashed-mid-request event) was judged more dangerous than
# moving it, since dispatch_cowriter.py is not otherwise supposed to own router-level state.

def _draft_action_ctx(body: dict):
    """버튼 payload → (channel, thread_ts, message_ts, work, top, mid, level)."""
    v = json.loads(body["actions"][0]["value"])
    ch = (body.get("channel") or {}).get("id")
    msg = body.get("message") or {}
    th = msg.get("thread_ts") or msg.get("ts")
    return ch, th, msg.get("ts"), v.get("w"), v.get("t"), v.get("m", ""), v.get("l") or None

@app.action("draft_approve")
def on_draft_approve(ack, body):
    ack()
    try:
        ch, th, mts, work, top, mid, level = _draft_action_ctx(body)
        log.info("draft_approve: work=%s top=%s mid=%s level=%s", work, top, mid, level)
        path = f"<{work}> {top}" + (f" / {mid}" if mid else "") + (f" 강도 {level}로 저장" if level else "")
        try:                                   # 버튼 비활성화(중복 클릭 방지)
            app.client.chat_update(channel=ch, ts=mts,
                                   text=(f"✅ 강도 {level} 저장할게요." if level else "✅ 통과 — 저장할게요."), blocks=[])
        except Exception:
            pass
        _do_input(ch, th, path, mode="save")   # 스레드 직전 초안 캡처 → 시트+노션 저장
    except Exception:
        log.exception("draft_approve 실패")

@app.action("draft_level_select")
def on_draft_level_select(ack, body):
    """강도 1~5 비교 흐름의 저장 드롭다운 선택 → on_draft_approve와 같은 저장 경로(_do_input)
    재사용 (2026-07-16, 온보딩 C). 버튼 대신 드롭다운이라 payload 모양만 다르고 저장 로직은 동일."""
    ack()
    try:
        action = body["actions"][0]
        v = json.loads(action["selected_option"]["value"])
        ch = (body.get("channel") or {}).get("id")
        msg = body.get("message") or {}
        th = msg.get("thread_ts") or msg.get("ts")
        work, top, mid, level = v.get("w"), v.get("t"), v.get("m", ""), v.get("l") or None
        log.info("draft_level_select: work=%s top=%s mid=%s level=%s", work, top, mid, level)
        path = f"<{work}> {top}" + (f" / {mid}" if mid else "") + (f" 강도 {level}로 저장" if level else "")
        try:                                   # 드롭다운 비활성화(중복 선택 방지)
            app.client.chat_update(channel=ch, ts=msg.get("ts"),
                                   text=f"✅ 강도 {level} 저장할게요.", blocks=[])
        except Exception:
            pass
        _do_input(ch, th, path, mode="save")   # 스레드 직전 초안 캡처 → 시트+노션 저장
    except Exception:
        log.exception("draft_level_select 실패")

@app.action("draft_regen")
def on_draft_regen(ack, body):
    ack()
    # ★2026-07-20 "재생성 누르니깐 의견 안 물어보고 그냥 재생성함" — 예전엔 곧바로 _do_generate로
    # 같은 걸 다시 뽑았다. 이제 "어떻게 다시 만들까요?"를 먼저 묻고, 다음 답글(수정 방향)을
    # 받아서 반영한다. 그냥 똑같이 다시 뽑고 싶으면 답글로 '그냥'이라고 하면 된다.
    try:
        ch, th, mts, work, top, mid, level = _draft_action_ctx(body)
        _DRAFT_REGEN_PENDING[th] = {"work": work, "top": top, "mid": mid,
                                    "level": level, "ts": time.time()}
        _save_draft_caches()
        try:
            app.client.chat_update(channel=ch, ts=mts, text="🔄 재생성 — 어떤 방향으로 고칠지 알려주세요.", blocks=[])
        except Exception:
            pass
        ep_l = f"{mid} " if mid else ""
        _reply(ch, th,
               f"🔄 <{work}> {ep_l}{top} 어떻게 다시 만들까요? 고칠 방향을 이 스레드에 답글로 적어주세요.\n"
               "예: `엔딩 훅을 더 세게` / `인물 관계를 더 부각` / `사건을 하나 줄여서 간결하게`\n"
               "_그냥 똑같이 다시 뽑으려면 `그냥`이라고 답해주세요._")
    except Exception:
        log.exception("draft_regen 실패")

def _revise_action_ctx(body: dict):
    """revise 버튼 payload → (channel, thread_ts, message_ts, work, kind, episode)."""
    v = json.loads(body["actions"][0]["value"])
    ch = (body.get("channel") or {}).get("id")
    msg = body.get("message") or {}
    th = msg.get("thread_ts") or msg.get("ts")
    ep = v.get("e")
    return ch, th, msg.get("ts"), v.get("w"), v.get("k") or "개요", (int(ep) if ep else None)

@app.action("revise_generate")
def on_revise_generate(ack, body):
    ack()
    job = None
    try:
        ch, th, mts, work, kind, ep = _revise_action_ctx(body)
        log.info("revise_generate: work=%s kind=%s ep=%s", work, kind, ep)
        path = f"<{work}> {kind}" + (f" / {ep}화" if ep else "")
        try:                                   # 버튼 비활성화(중복 클릭 방지)
            app.client.chat_update(channel=ch, ts=mts, text=f"🆕 {kind} 생성할게요…", blocks=[])
        except Exception:
            pass
        job = job_ledger.start_job("revise_generate", ch, th, path)
        _do_generate(ch, th, path)             # 위 대화(제안) 맥락 그대로 반영해 새로 생성
    except Exception:
        log.exception("revise_generate 실패")
    finally:
        job_ledger.finish_job(job)

@app.action("revise_specify")
def on_revise_specify(ack, body):
    ack()
    try:
        ch, th, mts, work, kind, ep = _revise_action_ctx(body)
        ep_l = f"{ep}화 " if ep else ""
        try:
            app.client.chat_update(channel=ch, ts=mts, text="✏️ 수정 방향을 알려주세요.", blocks=[])
        except Exception:
            pass
        _reply(ch, th,
               f"✏️ 원하는 {ep_l}{kind} 수정 방향을 이 스레드에 답글로 적어주세요.\n"
               "예: `사건 1을 벽치기 해소로 시작하게 고쳐줘` / `엔딩 훅을 더 세게`")
    except Exception:
        log.exception("revise_specify 실패")

def _char_action_ctx(body: dict):
    """캐릭터 카드 버튼 payload → (channel, thread_ts, message_ts, work, name, feedback, data, context,
    existing). existing이 있으면 '기존 인물 자연어 수정' 흐름(신규 추가가 아님).
    실제 값은 버튼 value(짧은 id)로 _CHAR_DRAFT_CACHE에서 회수 — Slack 버튼 value 2000자 제한 회피용
    (2026-07-13: context까지 통째로 넣었다가 invalid_blocks로 버튼 자체가 안 붙던 버그 수정)."""
    v = json.loads(body["actions"][0]["value"])
    ch = (body.get("channel") or {}).get("id")
    msg = body.get("message") or {}
    th = msg.get("thread_ts") or msg.get("ts")
    cached = _CHAR_DRAFT_CACHE.get(v.get("id"), {}) if "id" in v else v
    return (ch, th, msg.get("ts"), cached.get("w"), cached.get("n"), cached.get("fb", ""),
            cached.get("d") or {}, cached.get("ctx", ""), cached.get("ex"))

@app.action("char_save")
def on_char_save(ack, body):
    ack()
    try:
        ch, th, mts, work, name, feedback, data, _ctx, existing = _char_action_ctx(body)
        if not name:
            _reply(ch, th, "이 카드 정보가 만료됐어요(봇 재시작 등) — 다시 요청해 주세요.")
            return
        from bot.sheet_bible import CHAR_SUBS
        log.info("char_save: work=%s name=%s edit=%s", work, name, bool(existing))
        try:
            app.client.chat_update(channel=ch, ts=mts, text="✅ 저장할게요…", blocks=[])
        except Exception:
            pass
        sheet = reference.sheet()
        if not sheet:
            _reply(ch, th, "시트가 아직 연결 안 됐어요 (SHEET_WEBAPP_URL 미설정).")
            return
        # 시트엔 '외형' 컬럼이 따로 없어서 '설정' 필드 안에 라벨 붙여 합쳐서 저장한다.
        save_data = dict(data)
        appearance = save_data.pop("외형", "")
        if appearance:
            save_data["설정"] = (f"외형: {appearance}\n\n{save_data.get('설정', '')}").strip()
        lines = []
        for k in CHAR_SUBS:
            v = save_data.get(k)
            if not v:
                continue
            r = sheet.upsert(work, "등장인물", name, k, v)
            if isinstance(r, dict) and r.get("error"):
                _reply(ch, th, f"⚠️ {k} 저장 실패: {r['error']} (일부만 저장됐을 수 있어요)")
                return
            lines.append(f"- **{k}**: {v}")
        sheet.invalidate(work)
        # 기존 인물 수정도 이제 노션 카드 블록을 in-place 교체(2026-07-14, C3 — _push_character_to_notion이
        # 이름으로 기존 카드를 찾아 바꿔치기하므로 중복 카드 걱정 없이 새 카드와 동일하게 처리 가능).
        verb = "수정해서" if existing else "새 등장인물로"
        note = " · 노션에도 반영" if _push_character_to_notion(work, name, data) else ""
        _reply(ch, th, f"✅ *{name}* {verb} 시트에 저장했어요.{note}\n" + "\n".join(lines))
    except Exception:
        log.exception("char_save 실패")

@app.action("char_regen")
def on_char_regen(ack, body):
    ack()
    job = None
    try:
        ch, th, mts, work, name, feedback, _data, ctx, existing = _char_action_ctx(body)
        if not name:
            _reply(ch, th, "이 카드 정보가 만료됐어요(봇 재시작 등) — 다시 요청해 주세요.")
            return
        try:
            app.client.chat_update(channel=ch, ts=mts, text="🔄 재생성할게요…", blocks=[])
        except Exception:
            pass
        job = job_ledger.start_job("char_regen", ch, th, name)
        bible = None
        sheet = reference.sheet()
        if sheet and work:
            try:
                bible = sheet.get(work)
            except Exception:
                log.exception("char regen bible load failed")
        ph = _thinking(ch, th, f"{name} 카드 다시 만드는 중이에요…")
        if existing:
            data, err = _generate_char_edit(work, name, existing, feedback, bible, ctx)
        else:
            data, err = _generate_char_card(work, name, feedback, bible, ctx)
        if err:
            _post_chunks(ch, th, err, replace_ts=ph)
            return
        _post_char_draft(ch, th, work, name, feedback, data, ph=ph, context=ctx, existing=existing)
    except Exception:
        log.exception("char_regen 실패")
    finally:
        job_ledger.finish_job(job)

@app.action("char_edit")
def on_char_edit(ack, body):
    ack()
    try:
        ch, th, mts, work, name, feedback, _data, ctx, existing = _char_action_ctx(body)
        if not name:
            _reply(ch, th, "이 카드 정보가 만료됐어요(봇 재시작 등) — 다시 요청해 주세요.")
            return
        try:
            app.client.chat_update(channel=ch, ts=mts, text="✏️ 어떻게 바꿀지 답글로 알려주세요.", blocks=[])
        except Exception:
            pass
        _CHAR_EDIT_PENDING[th] = {"work": work, "name": name, "feedback": feedback, "context": ctx,
                                  "existing": existing, "ts": time.time()}
        _save_draft_caches()
        _reply(ch, th,
               f"✏️ *{name}* 카드를 어떻게 바꿀지 이 스레드에 답글로 알려주세요.\n"
               "예: `나이를 더 어리게` / `포지션을 리더로` / `더 위협적인 느낌으로`")
    except Exception:
        log.exception("char_edit 실패")

def _field_action_ctx(body: dict):
    """필드 수정안 버튼 payload → (channel, thread_ts, message_ts, work, field_name, triple, feedback, value)."""
    v = json.loads(body["actions"][0]["value"])
    ch = (body.get("channel") or {}).get("id")
    msg = body.get("message") or {}
    th = msg.get("thread_ts") or msg.get("ts")
    cached = _FIELD_DRAFT_CACHE.get(v.get("id"), {})
    t = cached.get("t")
    return (ch, th, msg.get("ts"), cached.get("w"), cached.get("f"),
            tuple(t) if t else None, cached.get("fb", ""), cached.get("v", ""))

@app.action("field_save")
def on_field_save(ack, body):
    ack()
    try:
        ch, th, mts, work, field_name, triple, feedback, new_val = _field_action_ctx(body)
        if not field_name:
            _reply(ch, th, "이 수정안 정보가 만료됐어요(봇 재시작 등) — 다시 요청해 주세요.")
            return
        log.info("field_save: work=%s field=%s", work, field_name)
        try:
            app.client.chat_update(channel=ch, ts=mts, text="✅ 저장할게요…", blocks=[])
        except Exception:
            pass
        sheet = reference.sheet()
        if not sheet:
            _reply(ch, th, "시트가 아직 연결 안 됐어요 (SHEET_WEBAPP_URL 미설정).")
            return
        top, mid, sub = triple[0], triple[1], triple[2]
        if triple[3] == "episode_plan":
            try:
                rows = _json_loads_array(new_val)
            except Exception:
                _reply(ch, th, "⚠️ 회차분배 저장 실패: 모델 결과를 표로 못 읽었어요. 다시 시도해 주세요.")
                return
            # Bug9(2026-07-16): row가 "막" 없이 오면 여기서 그냥 continue돼서 조용히 드롭됐는데
            # 전체가 다 그런 경우(rows==[] 포함) 아래 성공 메시지는 무조건 나가서 "0막 저장"
            # 같은 가짜 성공이 뜬 적이 있었다 — 실제로 저장된 막 개수(saved)를 따로 세서 0이면
            # 실패로 알린다.
            failed = 0
            saved = 0
            for row in rows:
                gu = _normalize_gu(row.get("막", ""))
                if not gu:
                    continue
                row_saved = False
                for k in ("구간", "화수", "핵심사건"):
                    if row.get(k):
                        rr = sheet.upsert(work, "회차분배", gu, k, str(row[k]).strip())
                        if isinstance(rr, dict) and rr.get("error"):
                            failed += 1
                        else:
                            row_saved = True
                if row_saved:
                    saved += 1
            sheet.invalidate(work)
            if saved == 0:
                _reply(ch, th, "⚠️ 회차분배 초안에서 저장할 내용을 못 찾았어요 (막 정보가 비어있어요). "
                               "다시 생성해 주세요.")
                return
            note = f" (실패 {failed}건)" if failed else ""
            # Bug8(2026-07-16): 이 저장은 새 배열에 있는 막만 upsert하는 방식이라(기존에 있던
            # 다른 막은 그대로 남음) "저장했어요"라고만 하면 전체 교체로 오해할 수 있어 문구를
            # 명확히 함. 시트 API에 안전한 삭제(全교체) primitive가 따로 없어서(sheet.upsert만
            # 확인됨) 더 위험한 "지우고 다시 쓰기"보다 메시지를 정확히 하는 쪽을 선택.
            _reply(ch, th, f"✅ *회차분배* {saved}막 갱신했어요 (기존에 없던 막은 그대로 유지돼요).{note}")
            return
        r = sheet.upsert(work, top, mid, sub, new_val)
        if isinstance(r, dict) and r.get("error"):
            _reply(ch, th, f"⚠️ 저장 실패: {r['error']}")
            return
        sheet.invalidate(work)
        note = " · 노션에도 반영" if _push_section_to_notion(work, top, mid, new_val) else ""
        _reply(ch, th, f"✅ *{field_name}* 시트에 저장했어요.{note}")
    except Exception:
        log.exception("field_save 실패")

@app.action("field_regen")
def on_field_regen(ack, body):
    ack()
    job = None
    try:
        ch, th, mts, work, field_name, triple, feedback, _old_val = _field_action_ctx(body)
        if not field_name:
            _reply(ch, th, "이 수정안 정보가 만료됐어요(봇 재시작 등) — 다시 요청해 주세요.")
            return
        try:
            app.client.chat_update(channel=ch, ts=mts, text="🔄 재생성할게요…", blocks=[])
        except Exception:
            pass
        job = job_ledger.start_job("field_regen", ch, th, field_name)
        bible = None
        sheet = reference.sheet()
        if sheet and work:
            try:
                bible = sheet.get(work)
            except Exception:
                log.exception("field regen bible load failed")
        bkey = triple[3] if triple else None
        is_plan = bkey == "episode_plan"
        ph = _thinking(ch, th, f"{field_name} 다시 만드는 중이에요…")
        try:
            if is_plan:
                current = (bible or {}).get(bkey, {}) if bkey else {}
                new_val = generator.complete(prompts.episode_plan_edit_system(),
                                             prompts.episode_plan_edit_user(current, feedback),
                                             timeout=90).strip()
            else:
                current = (bible or {}).get(bkey, "") if bkey else ""
                new_val = generator.complete(prompts.field_edit_system(field_name),
                                             prompts.field_edit_user(field_name, current, feedback),
                                             timeout=90).strip()
        except Exception:
            log.exception("field regen generation failed")
            _post_chunks(ch, th, "재생성 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", replace_ts=ph)
            return
        _post_field_draft(ch, th, work, field_name, triple, feedback, new_val, ph=ph)
    except Exception:
        log.exception("field_regen 실패")
    finally:
        job_ledger.finish_job(job)

@app.action("field_edit")
def on_field_edit(ack, body):
    ack()
    try:
        ch, th, mts, work, field_name, triple, feedback, _val = _field_action_ctx(body)
        if not field_name:
            _reply(ch, th, "이 수정안 정보가 만료됐어요(봇 재시작 등) — 다시 요청해 주세요.")
            return
        try:
            app.client.chat_update(channel=ch, ts=mts, text="✏️ 어떻게 바꿀지 답글로 알려주세요.", blocks=[])
        except Exception:
            pass
        _FIELD_EDIT_PENDING[th] = {"work": work, "field": field_name, "triple": triple, "feedback": feedback,
                                   "ts": time.time()}
        _save_draft_caches()
        _reply(ch, th, f"✏️ *{field_name}*를 어떻게 바꿀지 이 스레드에 답글로 알려주세요.")
    except Exception:
        log.exception("field_edit 실패")

_NOTION_POLL_SEC = int(os.environ.get("COWRITER_NOTION_POLL_SEC", "60"))  # 노션 변경 확인 주기(초)

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
    변경 없으면 아무것도 안 함(LLM 미사용). 봇에 링크가 등록된 작품만 대상."""
    from bot.shared import notion_sync
    time.sleep(20)   # 기동 직후 소켓 안정될 때까지 대기
    failed_le: dict[str, str] = {}   # {작품: 실패한 last_edited} — 같은 내용 무한 재시도 방지
    while True:
        try:
            _reference_pull()                     # 레퍼런스 DB 최신화 (사본 없이 직접)
        except Exception:
            log.exception("레퍼런스 pull 오류")
        try:
            from bot.shared import works
            sheet = reference.sheet()
            registry = works.all_works() if config.NOTION_TOKEN else {}   # 봇에 등록된 링크만
            if sheet and registry:
                st = _load_notion_state()
                for work, entry in registry.items():
                    page_id = entry.get("page")
                    if not page_id:
                        continue
                    try:
                        le = notion_sync.page_last_edited(page_id)
                    except Exception:
                        log.warning("노션 수정시각 조회 실패: %s", work)
                        continue
                    # 이미 실패한 것과 동일한 편집본이면 재시도 안 함(페이지가 또 수정되면 le가 바뀌어 재시도됨)
                    if le and le != st.get(work) and le != failed_le.get(work):
                        log.info("노션 변경 감지 → 자동 동기화: %s", work)
                        try:
                            content = notion_sync.page_text(page_id)
                            done, failed, _ = _sync_apply(sheet, work, content)
                            for hwa, script_text in notion_sync.parse_episode_scripts(content).items():
                                _summarize_script(work, hwa, script_text)   # 실무자가 노션에 직접
                                # 쓴 화도 연속성 요약 캐시가 쌓이게(해시 동일하면 내부에서 스킵)
                            st[work] = le
                            _save_notion_state(st)
                            failed_le.pop(work, None)
                            log.info("자동 동기화 완료: %s (%d 반영, %d 실패)", work, done, failed)
                        except Exception:
                            failed_le[work] = le   # 이 편집본은 다시 수정될 때까지 건너뜀
                            log.warning("자동 동기화 실패(재시도 보류): %s — 노션 수정 시 재시도됨", work)
        except Exception:
            log.exception("autosync 루프 오류")
        time.sleep(_NOTION_POLL_SEC)

