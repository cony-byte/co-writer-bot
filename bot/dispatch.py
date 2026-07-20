"""dispatch.py -- the real merged Slack event router (Phase 3 final assembly).

This is the single place that:
  1. registers the ONE pair of Bolt event handlers (`on_mention`/`on_message`) on the
     shared `app` instance (bot.shared.slack_io.app) -- neither dispatch_cowriter.py nor
     dispatch_storyboard.py register their own (their own copies were excluded at
     extraction time specifically so this file would be the sole owner);
  2. implements `_handle_dispatch(event)`, which is the CONFIRMED merged dispatch order
     from `_dispatch_order_check/simulate_dispatch.py` (87/87 test-corpus cases passing),
     translated from that simulator's abstract predicate functions into calls against the
     REAL handler functions now living in dispatch_cowriter.py (`cw`) and
     dispatch_storyboard.py (`sb`);
  3. implements `_handle(event)`, co-writer's inflight-tracking wrapper (moved here from
     dispatch_cowriter.py -- see the NOTE left in that file at the old location: its
     `_replay_inflight` needs to call the MERGED `_handle_dispatch`, which only exists
     here, not co-writer's old 228-line elif chain).

Confirmed merged order (see simulate_dispatch.py module docstring + HANDOFF_봇병합.md
§2-1 / §3-5 risk #1 for the full history of why this exact order, not a simpler one):

  0. dedup guard (storyboard's _is_duplicate_event)
  1. storyboard _STOP_RE           (universal bare-word cancel, e.g. "그만")
  1.5 storyboard _FULL_HELP_RE     (explicit "도움말"/"help" escape hatch, unconditional)
  2. storyboard _RETRY_INTERRUPTED_RE (only acts if interrupted_state has a record)
  3. bracket-command parse:
       3a. co-writer bracket chain
       3b. storyboard bracket chain
       3c. unknown-bracket fallback (co-writer's richer version: notion-link / known-work-
           tag / generation-verb / typo-suggestion)
  4. storyboard _maybe_* chain (23 functions, exact call order from the real _handle)
  5. co-writer narrow inline chain (6 checks), with:
       FIX-2/FIX-2b: _is_confirm_save is suppressed inside an active storyboard thread
       (sb_stage>=1 or a stillcut/conti thread-marker present) or while a
       "노션에 최종 콘티로 저장하기 전 확인" card is pending -- otherwise a bare "확정"
       reply meant for storyboard would get hijacked into co-writer's sheet-save flow.
  6. storyboard catch-all (_do_storyboard_auto_chain), gated by:
       FIX-1: suppressed when the message has a co-writer-domain word (피드백/기획/트렌드/...)
       and no strong storyboard keyword -- prevents "3화 피드백 어때" from launching the
       (expensive) storyboard pipeline.
       FIX-3: a bare "콘티" mention alone no longer counts as an implicit-start signal --
       needs pairing with an action verb, unlike storyboard's own (unguarded) start-hint
       regex. Structural signals (episode number / explicit product noun) still count alone.
  7. co-writer fallback (_do_revise / _do_freeform / bare-mention guide) -- true last resort.

IMPORTANT: steps 6 and 7 are NOT independent alternatives that both get a chance -- per the
simulator's confirmed logic, storyboard's catch-all is tried first and, if it does not fire,
control ALWAYS falls to co-writer's fallback (never to storyboard's own "no signal" greeting
reply, which would otherwise show storyboard's help text as the universal fallback -- see
test corpus case `fallback-empty-mention`).

GAP flagged for later phases (see final report, not something this file can fix on its own):
there is no new top-level entry point yet that (a) imports this module so its `@app.event`
registrations take effect, (b) runs both backends' healthcheck(), (c) calls
`start_background_jobs()` below, and (d) starts `SocketModeHandler`. The existing top-level
`app.py` in this worktree is still co-writer-bot's original, unmodified monolith and is NOT
wired to this file at all -- that wiring is Phase 6 (cutover) work.
"""
import re
import threading
import time

from bot import config
from bot import dispatch_cowriter as cw
from bot import dispatch_storyboard as sb
from bot.shared import works
from bot.shared.files import _files_text
from bot.shared.slack_io import app, log, _reply, _thread_messages, _clean, BOT_USER_ID

MENTION_RE = re.compile(rf"<@{BOT_USER_ID}>\s*")

# co-writer-bot/app.py's original `_WORK_CMDS` (app.py L3975) was a LOCAL variable inside
# the now-excluded `_handle_dispatch`, not a module-level constant -- so it does not exist
# as `cw._WORK_CMDS`. Recomputed here, verbatim same member sets.
_WORK_CMDS = cw.CMD_GEN | cw.CMD_FEEDBACK | cw.CMD_FB_FUN | cw.CMD_FB_LOGIC | cw.CMD_IDEA | cw.CMD_CONVERT

# ★2026-07-20 "[명령]"을 문장 끝/중간에 써도(예: "<저연프> 대본 1화 [피드백]") 인식하려고,
# 알려진 명령어 키워드 전체를 모아둔다. CMD_RE(맨 앞 [...]만)로 안 잡힐 때 이 목록에 있는
# 브래킷을 앞으로 끌어와 재인식한다(_promote_bracket_command).
_KNOWN_BRACKET_CMDS = {c.lower() for c in (
    cw.CMD_INPUT | cw.CMD_EDIT | cw.CMD_GEN | cw.CMD_PLAN | cw.CMD_CONVERT | cw.CMD_TREND
    | cw.CMD_IDEA | cw.CMD_SYNC | cw.CMD_CHECK | cw.CMD_ALIAS | cw.CMD_FEEDBACK | cw.CMD_FB_FUN
    | cw.CMD_FB_LOGIC | cw.CMD_FILE | cw.CMD_REF | cw.CMD_REFRESH | cw.CMD_RELOAD | cw.CMD_HELP
    | sb.CMD_STORYBOARD_ALL | sb.CMD_IMG | sb.CMD_STILL | sb.CMD_CONTI_FINAL | sb.CMD_COMPILE
    | sb.CMD_RESET_EPISODE | sb.CMD_AUTOPILOT | sb.CMD_EPISODE_STATUS | sb.CMD_STYLE)}

def _promote_bracket_command(q: str) -> "str | None":
    """맨 앞이 아닌 곳에 있는 알려진 명령 브래킷([피드백] 등)을 맨 앞으로 옮긴 새 문자열 반환
    (없으면 None). 예: "<저연프> 대본 1화 [피드백]" → "[피드백] <저연프> 대본 1화"."""
    for mm in re.finditer(r"\[\s*([^\]]+?)\s*\]", q or ""):
        inner = mm.group(1).strip()
        if inner.lower() in _KNOWN_BRACKET_CMDS:
            rest = (q[:mm.start()] + q[mm.end():]).strip()
            return f"[{inner}] {rest}".strip()
    return None

# (2026-07-16, Phase 4) combined typo-suggestion vocabulary -- cw._ALL_CMD_NAMES only ever
# listed co-writer's own command names (a typo'd "[스토리보드]" would get no suggestion, or
# worse, the single closest co-writer command regardless of relevance). Defined here rather
# than folded into cw._ALL_CMD_NAMES itself, since dispatch_cowriter.py stays co-writer-only
# and dispatch.py is where both bots' vocabularies are already in scope together.
_ALL_CMD_NAMES = sorted(set(cw._ALL_CMD_NAMES) | sb.CMD_STORYBOARD_ALL | sb.CMD_IMG | sb.CMD_STILL
                        | sb.CMD_FILE | sb.CMD_REF | sb.CMD_CONTI_FINAL | sb.CMD_COMPILE
                        | sb.CMD_RESET_EPISODE | sb.CMD_AUTOPILOT | sb.CMD_EPISODE_STATUS
                        | sb.CMD_STYLE)


# ============================================================================
# combined help text (2026-07-16, Phase 4) -- replaces the TODO(Phase 4) placeholders that
# showed sb._HELP alone for both the `[도움말]` bracket and the new unconditional
# _FULL_HELP_RE escape hatch. Each half is kept verbatim (both are already well-written,
# action-oriented guides for their own domain) and joined under one identity, since post-
# merge there is only one bot and a co-writer-only thread asking "도움말" should still learn
# the storyboard pipeline exists (and vice versa) rather than seeing only half the picture.
# ============================================================================
_COMBINED_HELP = (
    "🖋️🎬 *이 봇 하나로 대본 작업 + 스토리보드/영상 제작까지 다 돼요.*\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "*🖋️ 대본/기획 (co-writer)*\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    + cw._HELP +
    "\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    + sb._HELP +
    "\n━━━━━━━━━━━━━━━━━━━━"
)

# combined short guide -- used for a bare mention with no text (dispatch.py step 7's true
# last resort). cw._GUIDE alone would only ever mention writing/planning commands; a merged
# bot's bare-mention greeting should surface both halves briefly, same principle as
# _COMBINED_HELP above but condensed (full detail is one "도움말" away either way).
_COMBINED_GUIDE = (
    cw._GUIDE.rsplit("_스레드 안에선", 1)[0].rstrip() + "\n"
    "• *스토리보드/영상*: \"<작품> 3화\"처럼 작품+화만 말해도 씬 설계부터 시작돼요 "
    "(콘티→이미지→영상화→합본까지 자연어로 이어서)\n"
    "_스레드 안에선 작품 이름 없이 자연어로 이어 말해도 돼요. 전체 명령은 `도움말`(또는 `[도움말]`)._"
)


# ============================================================================
# FIX-1 / FIX-3 guard regexes -- these do NOT exist in either original bot's source.
# They were derived and validated against an 87-case adversarial corpus in
# _dispatch_order_check/simulate_dispatch.py + test_corpus.py (see that module's
# docstring for the full reasoning); copied here verbatim since this file is the
# real implementation of that confirmed logic.
# ============================================================================
# ★2026-07-20: "1화 사건 흐름까지 잡아서 생성해줘"(개요/기획 생성 요청, 스토리보드 의도 전혀
# 없음)가 그냥 "1화"라는 화 번호 언급만으로 storyboard 캐치올(_do_storyboard_auto_chain)로
# 잘못 새서 "씬 설계 중이에요…"가 튀어나온 실사용 사고 — 기존 vocabulary(피드백/기획/트렌드/
# 동기화/아이디어/별칭/재미평가/개연성)에 세계관·사건 흐름·로그라인 같은 co-writer 기획
# 도메인 어휘가 빠져있었다. FIX-1 가드가 이미 "강한 storyboard 키워드(스토리보드/씬설계/콘티/
# 스틸컷/이미지)가 같이 있으면 취소 안 함"으로 안전장치를 두고 있어서, 이 목록을 넓혀도
# 진짜 storyboard 시작 의도(예: "3화 스토리보드 진행해줘")는 그대로 보호된다.
# ★2026-07-20b: 같은 계열의 재발 — "1화 개요 다시 쓰고 싶어 인물이랑 상황이 시청자들에게
# 충분히 설명히 잘되게"(명백한 co-writer 개요 재작성 요청)가 "1화"에 걸려 storyboard 씬설계로
# 샜다. 어이없게도 "개요"/"대본"이라는, co-writer의 핵심 산출물 이름 자체가 이 목록에 없었다
# — 둘 다 storyboard의 강한 키워드(스토리보드/씬설계/콘티/스틸컷/이미지)와 안 겹쳐서 추가해도
# "3화 대본으로 스토리보드 만들어줘"처럼 진짜 storyboard 의도가 있으면 그 강한 키워드가 그대로
# 우선한다.
_COWRITER_INTENT_HINT_RE = re.compile(
    r"피드백|기획|트렌드|동기화|아이디어|별칭|재미\s*평가|개연성|세계관|사건\s*흐름|로그라인|개요|대본")
_STRONG_SB_KEYWORDS_RE = re.compile(r"스토리보드|씬\s*설계|콘티|스틸\s*컷|이미지|storyboard")
_SB_STRUCTURAL_HINT_RE = re.compile(r"\d+\s*[화회]|스토리보드|씬\s*설계|storyboard")
_SB_BARE_CONTI_RE = re.compile(r"콘티")
_SB_ACTION_VERB_RE = re.compile(r"만들|생성|바꿔|고쳐|써줘|해줘|줄래|주세요|보여줘|시작|진행")


def _looks_like_storyboard_start_guarded(channel: str, thread_ts: str, query: str) -> bool:
    """FIX-3 version of sb._looks_like_storyboard_start: a bare '콘티' mention alone no
    longer counts as an implicit-start signal (common in ordinary conversation ABOUT the
    pipeline, e.g. "이번 콘티 별로였고... 얘기 좀 하자") -- needs an action verb alongside it.
    Structural signals (episode number, or an explicit product noun like
    스토리보드/씬설계/storyboard) still count on their own, exactly as before. The
    thread-inferred-work-name branch is unchanged from sb._looks_like_storyboard_start."""
    q = query or ""
    if _SB_STRUCTURAL_HINT_RE.search(q):
        return True
    if _SB_BARE_CONTI_RE.search(q) and _SB_ACTION_VERB_RE.search(q):
        return True
    joined = "\n".join(m["content"] for m in _thread_messages(channel, thread_ts))
    return bool(sb._work_from_thread(joined, thread_ts))


# ★2026-07-20: 스토리보드 스레드 마커 substring — sb._thread_last_marker와 동일 기준.
_SB_MARKER_SUBSTRINGS = ("[1단계]", "[2단계]", "씬 설계안", "visual-pipeline 프로젝트에 저장돼요", "스틸컷")


def _last_output_is_cowriter_draft(msgs) -> bool:
    """가장 최근 봇 출력이 co-writer 초안(개요/대본/줄거리, 통과/재생성 대기)인지 — 그게
    스토리보드 마커보다 더 최근이면 True. ★2026-07-20 실사용 사고: 대본을 방금 뽑은 스레드에
    이전 스토리보드 작업이 남아있으면(sb_stage>=1) 대본에 대한 자유 피드백("줄거리 수정해줘,
    장면 연출 중심으로")이 storyboard 캐치올로 새서 상세 콘티를 만들어버렸다. 대본 초안이 가장
    최근 산출물이면 그 피드백은 '대본 수정'이지 '스토리보드 시작'이 아니다 — 이 경우 캐치올을
    막고 co-writer 수정(step 7)으로 흘려보낸다.
    reverse 스캔에서 스토리보드 마커를 먼저 만나면 storyboard가 더 최근이므로 False,
    co-writer 초안 신호(_BUTTON_PROMPT_RE=통과/재생성 버튼 안내, _DRAFT_FOOT_RE=초안 꼬리말)를
    먼저 만나면 True."""
    for m in msgs[::-1]:
        if m.get("role") != "assistant":
            continue
        c = m.get("content") or ""
        if any(s in c for s in _SB_MARKER_SUBSTRINGS):
            return False   # 스토리보드 출력이 더 최근
        if cw._BUTTON_PROMPT_RE.search(c) or cw._DRAFT_FOOT_RE.search(c):
            return True    # co-writer 초안이 더 최근
    return False


def _storyboard_wants_to_start(channel: str, thread_ts: str, query: str) -> bool:
    """Real analog of simulate_dispatch.py's sb_catchall()'s 'fires' computation
    (FIX-1 + FIX-3 applied). True iff the storyboard auto-chain (_do_storyboard_auto_chain)
    should run for this bracketless message."""
    q = query or ""
    if not q.strip():
        return False
    msgs = _thread_messages(channel, thread_ts)
    tracked_ctx = sb.conti_state.get_episode(thread_ts) or {}
    stage = sb.sb_stage(msgs, work=tracked_ctx.get("work"), episode=tracked_ctx.get("episode"))
    looks_like_start = _looks_like_storyboard_start_guarded(channel, thread_ts, q)
    fires = stage >= 1 or looks_like_start
    if fires and _COWRITER_INTENT_HINT_RE.search(q) and not _STRONG_SB_KEYWORDS_RE.search(q):
        fires = False   # FIX-1
    # ★2026-07-20 FIX-4: 가장 최근 산출물이 co-writer 초안(대본/개요/줄거리)이면, 명시적 강한
    # 스토리보드 키워드가 없는 한 이 자유 답글은 그 초안에 대한 수정 지시로 본다(스토리보드
    # 시작 아님) — 대본 뽑은 직후 "줄거리 수정/장면 연출 중심" 피드백이 상세 콘티로 새던 사고.
    if fires and not _STRONG_SB_KEYWORDS_RE.search(q) and _last_output_is_cowriter_draft(msgs):
        fires = False
    return fires


def _in_active_storyboard_thread(channel: str, thread_ts: str) -> bool:
    """FIX-2 guard signal: is this thread showing ANY storyboard pipeline activity
    (scene-design/conti/stillcut stage markers)? Real analog of the simulator's
    `sb_stage>=1 or thread_contains & _STORYBOARD_THREAD_MARKERS` state flags, built from
    the two real marker-scanning functions storyboard already has (sb.sb_stage only
    recognizes the conti-stage markers; sb._thread_last_marker additionally recognizes the
    stillcut-stage markers) rather than re-deriving a third marker set here."""
    msgs = _thread_messages(channel, thread_ts)
    if sb.sb_stage(msgs) >= 1:
        return True
    return sb._thread_last_marker(msgs) is not None


# ============================================================================
# storyboard _maybe_* chain -- EXACT call order from the real storyboard-bot _handle
# (storyboard-bot/app.py L5143-5222 in the commit-9e48fdc snapshot this was extracted
# from). Each entry: (function, needs_event). All are real, side-effecting functions --
# they send their own Slack reply AND return True/False for "did this consume the
# message", exactly like the original _handle's `if _maybe_x(...): return` chain.
# ============================================================================
_STORYBOARD_MAYBE_CHAIN = (
    (sb._maybe_ref_edit_reply, False),
    (sb._maybe_scene_pick_reply, False),
    (sb._maybe_stillcut_regen_ask_reply, False),
    (sb._maybe_element_regen_ask_reply, False),
    (sb._maybe_planregen_ask_reply, False),
    (sb._maybe_list_works, False),
    (sb._maybe_thread_status, False),
    (sb._maybe_episode_status, False),
    (sb._maybe_style_change_request, False),
    (sb._maybe_brief_conti_summary_request, False),
    (sb._maybe_unconfirm_conti, False),
    (sb._maybe_ordered_ref, True),   # ★2026-07-20 "순서대로 <A>,<B>… 등록해줘" + 이미지 N장 → 순서 매칭 등록
    (sb._maybe_natural_ref, True),
    (sb._maybe_bare_costume_label_request, True),
    (sb._maybe_element_gen_request, True),
    (sb._maybe_conti_final, True),
    (sb._maybe_notion_save_request, False),
    (sb._maybe_conti_rewrite_request, True),
    (sb._maybe_conti_use_then_generate, True),
    (sb._maybe_script_to_conti, True),
    (sb._maybe_skip_to_conti, True),
    (sb._maybe_typed_ref, True),
    (sb._maybe_video_from_last_still, False),
    (sb._maybe_place_feedback, False),
    (sb._maybe_retry_failed_cuts, False),
    (sb._maybe_stillcut_regen_feedback, False),
    (sb._maybe_generate_request, False),
)


def _handle_dispatch(event: dict) -> None:
    """The merged router body. See module docstring for the confirmed order."""
    # step 0: dedup guard (storyboard's -- co-writer never had one)
    if sb._is_duplicate_event(event):
        log.info("중복 이벤트 감지 — 건너뜀: channel=%s ts=%s", event.get("channel"), event.get("ts"))
        return

    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    query = _clean(event.get("text", ""))
    in_thread = bool(event.get("thread_ts")) and event.get("thread_ts") != event.get("ts")

    # storyboard's episode-reference normalization ("지난 화"/"다음 화" -> "3화" etc.) runs
    # unconditionally before any routing decision in the real storyboard _handle -- preserved
    # here so this pre-processing isn't silently lost for storyboard-flavored messages.
    query, resolved_ep = sb._normalize_episode_refs(query, thread_ts)
    if resolved_ep is not None:
        _reply(channel, thread_ts, f"📌 {resolved_ep}화로 이해하고 진행할게요!")

    # step 1: storyboard's universal bare-word stop ("그만"/"중단"/...)
    if sb._STOP_RE.match(query):
        sb._CANCEL.add(thread_ts)
        got = sb.generator.cancel_prefix(thread_ts)
        sb.job_ledger.finish_by_thread(thread_ts)
        sb.interrupted_state.clear(thread_ts)
        _reply(channel, thread_ts, "🛑 이 스레드 작업을 중단할게요…" if got else
               "🛑 중단 요청했어요 (이미지 생성 중이면 진행 중인 컷까지만 끝내고 멈춰요).")
        return

    # step 1.5: explicit "도움말"/"help" escape hatch (unconditional, fires even in a
    # co-writer-flavored thread -- see HANDOFF's existing "_HELP must never be silently
    # merged" note. Shows the combined help text (see _COMBINED_HELP above) since post-merge
    # there's only one bot identity and a co-writer-flavored thread should still learn the
    # storyboard pipeline exists (and vice versa).
    if sb._FULL_HELP_RE.match(query):
        _reply(channel, thread_ts, _COMBINED_HELP)
        return

    # step 2: resume an interrupted storyboard job ("재생성"/"다시 해줘"/...) -- a no-op
    # (falls through, does NOT return) if there is nothing to resume, exactly like the
    # real storyboard _handle.
    if sb._RETRY_INTERRUPTED_RE.match(query):
        rec = sb.interrupted_state.get(thread_ts)
        if rec:
            sb.interrupted_state.clear(thread_ts)
            _reply(channel, thread_ts, "🔁 끊겼던 작업을 이어서 다시 시도할게요…")
            sb.sb_do_storyboard(channel, thread_ts, rec["rest"], stage=(1 if rec["kind"] == "plan" else 2))
            return

    # step 3: bracket-command parse
    m = cw.CMD_RE.match(query)
    if not m:
        # ★2026-07-20 "[피드백]"을 문장 끝에 쓴 경우("<저연프> 대본 1화 [피드백]") 등 — 맨 앞이
        # 아니라 명령으로 인식 못 하고 스토리보드 등으로 새던 문제. 알려진 명령 브래킷을 앞으로
        # 끌어와 다시 인식한다.
        _promoted = _promote_bracket_command(query)
        if _promoted:
            query = _promoted
            m = cw.CMD_RE.match(query)
    if m:
        _dispatch_bracket_command(channel, thread_ts, query, event, m, in_thread)
        return

    # step 4: storyboard _maybe_* chain (23, exact order)
    for fn, needs_event in _STORYBOARD_MAYBE_CHAIN:
        hit = fn(channel, thread_ts, query, event) if needs_event else fn(channel, thread_ts, query)
        if hit:
            return

    # step 5: co-writer narrow inline chain
    if _dispatch_cowriter_narrow_chain(channel, thread_ts, query, event, in_thread):
        return

    # step 6: storyboard catch-all (FIX-1 + FIX-3 gated)
    if _storyboard_wants_to_start(channel, thread_ts, query):
        sb._do_storyboard_auto_chain(channel, thread_ts, query)
        return

    # step 7: co-writer fallback -- true last resort (never storyboard's own "no signal"
    # greeting -- see module docstring).
    if in_thread and query.strip():
        cw._do_revise(channel, thread_ts, query)
    elif query.strip():
        cw._do_freeform(channel, thread_ts, query)
    else:
        _reply(channel, thread_ts, _COMBINED_GUIDE)


def _dispatch_bracket_command(channel: str, thread_ts: str, query: str, event: dict,
                               m: "re.Match", in_thread: bool) -> None:
    """Step 3: bracket-command parse. Verbatim port of co-writer's real
    `_handle_dispatch` bracket branch (co-writer-bot/app.py L3963-4074) followed by
    storyboard's real `_handle` bracket branch (storyboard-bot/app.py L5236-5273),
    with storyboard's CMD_STORYBOARD/CMD_STORYBOARD2/CMD_STORYBOARD_IMG stub branch
    (the "use the other bot" message) removed -- that stub is exactly what this whole
    merge project replaces with real routing to dispatch_storyboard.py's handlers."""
    cmd, rest = m.group(1).strip(), m.group(2)

    # storyboard's space-in-bracket CMD_FILE normalization (2026-07-16, e.g.
    # "[파일 csv]" -> cmd="파일", rest="csv ..."). Only ever matches storyboard's own
    # CMD_FILE token, so it cannot newly collide with any co-writer bracket alias.
    if " " in cmd:
        head, _, tail = cmd.partition(" ")
        if head in sb.CMD_FILE:
            cmd, rest = head, (tail + " " + rest if rest else tail)

    # snippet/file attachment -> append its text after the command (long script/notion doc)
    ft, blocked = _files_text(event)
    if blocked and not ft:
        _reply(channel, thread_ts,
               "⚠️ 첨부 파일을 못 읽었어요 — Slack 앱에 *files:read* 권한이 필요해요.\n"
               "설정(OAuth & Permissions)에서 권한 추가 후 재설치하거나, 스니펫 대신 **채팅에 직접 붙여넣어** 주세요.")
        return
    rest_f = (rest + "\n" + ft) if ft else rest

    # co-writer: notion-link auto-registration for bible-consuming commands (excludes
    # sync/plan, which handle links themselves)
    if cmd in _WORK_CMDS and config.NOTION_TOKEN:
        rest = re.sub(r"<(https?://[^>|]+)(?:\|[^>]*)?>", r"\1", rest)
        lm = cw._NOTION_LINK.search(rest)
        if lm:
            no_link = cw._NOTION_LINK.sub("", rest).strip()
            sm = cw.SUB_RE.match(no_link)
            exp = (works.resolve(sm.group(1).strip()) or sm.group(1).strip()) if sm else None
            w = cw._autosync_link(channel, thread_ts, lm.group(0), explicit=exp)
            rest = no_link
            rest_f = cw._NOTION_LINK.sub("", rest_f).strip()
            if w and not cw.SUB_RE.match(rest):
                rest = f"<{w}> {rest}".strip()
                rest_f = f"<{w}> {rest_f}".strip()

    # 3a. co-writer bracket chain
    if cmd in cw.CMD_RELOAD:
        pulled = cw._reference_pull()
        cw.reference.reload()
        _reply(channel, thread_ts,
               ("최신 레퍼런스를 받아 " if pulled else "") + "레퍼런스 DB·템플릿을 다시 불러왔어요.")
    elif cmd in cw.CMD_REFRESH:
        sm = cw.SUB_RE.match(rest_f.strip())
        rf_work = (works.resolve(sm.group(1).strip()) or sm.group(1).strip()) if sm else None
        rf_work = rf_work or cw._work_from_thread(
            "\n".join(mm["content"] for mm in _thread_messages(channel, thread_ts)))
        if rf_work and works.page_of(rf_work) and config.NOTION_TOKEN:
            cw._do_sync(channel, thread_ts, f"<{rf_work}>")
        else:
            sheet = cw.reference.sheet()
            if sheet:
                sheet.invalidate()
            _reply(channel, thread_ts, "시트 바이블 캐시를 비웠어요. 다음 요청부터 최신으로 읽어옵니다.")
    elif cmd in cw.CMD_CONVERT:
        cw._do_convert(channel, thread_ts, rest_f)
    elif cmd in cw.CMD_TREND:
        cw._do_trend(channel, thread_ts, rest)
    elif cmd in cw.CMD_SYNC:
        cw._do_sync(channel, thread_ts, rest_f)
    elif cmd in cw.CMD_CHECK:
        cw._do_check(channel, thread_ts, rest)
    elif cmd in cw.CMD_ALIAS:
        cw._do_alias(channel, thread_ts, rest)
    elif cmd in cw.CMD_IDEA:
        cw._do_idea(channel, thread_ts, rest)
    elif cmd in cw.CMD_PLAN:
        cw._do_plan(channel, thread_ts, rest, files_text=ft, in_thread=in_thread)
    elif cmd in cw.CMD_FEEDBACK:
        cw._do_feedback(channel, thread_ts, rest_f, mode="both")
    elif cmd in cw.CMD_FB_FUN:
        cw._do_feedback(channel, thread_ts, rest_f, mode="fun")
    elif cmd in cw.CMD_FB_LOGIC:
        cw._do_feedback(channel, thread_ts, rest_f, mode="logic")
    elif cmd in cw.CMD_STOP:
        cw._do_stop(channel, thread_ts)
    elif cmd in cw.CMD_LIKE:
        cw._do_pref(channel, thread_ts, rest, "+")
    elif cmd in cw.CMD_DISLIKE:
        cw._do_pref(channel, thread_ts, rest, "-")
    elif cmd in cw.CMD_INPUT:
        cw._do_input(channel, thread_ts, rest, mode="create")
    elif cmd in cw.CMD_EDIT:
        cw._do_input(channel, thread_ts, rest, mode="update")
    elif cmd in cw.CMD_GEN:
        cw._do_generate(channel, thread_ts, rest, files_text=ft)
    elif cmd in cw.CMD_HELP:
        _reply(channel, thread_ts, _COMBINED_HELP)

    # 3b. storyboard bracket chain
    elif cmd in sb.CMD_STORYBOARD_ALL:
        sb._do_storyboard_auto(channel, thread_ts, rest_f)
    elif cmd in sb.CMD_IMG:
        sb._do_images(channel, thread_ts, rest_f)
    elif cmd in sb.CMD_STILL:
        sb._do_stills(channel, thread_ts, rest_f)
    elif cmd in sb.CMD_FILE:
        sb.sb_do_export(channel, thread_ts, rest_f, cmd=cmd)
    elif cmd in sb.CMD_REF:
        sb.sb_do_ref(channel, thread_ts, rest, event)
    elif cmd in sb.CMD_CONTI_FINAL:
        sb._do_conti_final(channel, thread_ts, rest, event)
    elif cmd in sb.CMD_COMPILE:
        sb._do_compile(channel, thread_ts, rest_f)
    elif cmd in sb.CMD_RESET_EPISODE:
        sb._do_reset_episode(channel, thread_ts, rest_f)
    elif cmd in sb.CMD_AUTOPILOT:
        sb._do_autopilot(channel, thread_ts, rest_f)
    elif cmd in sb.CMD_EPISODE_STATUS:
        sb._do_episode_status(channel, thread_ts, rest_f)
    elif cmd in sb.CMD_STYLE:
        sb._do_style(channel, thread_ts, rest_f)
    elif cmd.strip().lower() in sb._REF_TYPE_KW:
        # ★2026-07-20 "[장소] 대기실 이미지 다시 만들어줘" / "[의상] 정장-A 이미지 다시 만들어줘" —
        # 등록된 요소(장소/의상/소품/인물)의 참조 이미지를 AI로 다시 생성. 예전엔 이 브래킷이 어느
        # 커맨드 셋에도 없어 아래 생성동사 rescue로 co-writer LLM에 새서 "이미지 생성 못 함"으로 거절됐다.
        sb._do_typed_element_regen(channel, thread_ts, cmd, rest_f)

    # 3c. unknown-bracket fallback -- co-writer's richer version (notion-link / known-
    # work-tag / generation-verb rescue / typo suggestion). storyboard's own bracket
    # branch had only a flat "unknown command" reply; this is a strict superset.
    else:
        has_link = bool(cw._NOTION_LINK.search(rest_f) or cw._NOTION_LINK.search(cmd))
        resolved_tag = works.resolve(cmd)
        if has_link:
            cw._do_sync(channel, thread_ts, f"{cmd} {rest_f}".strip())
        elif resolved_tag:
            cw._do_freeform(channel, thread_ts, f"<{resolved_tag}> {rest_f}".strip())
        elif re.search(r"(만들|작성|생성|뽑|그려|써|쓰|짜)(?:어|아)?\s*(?:봐|줘|줄래|주세요)?", rest_f):
            cw._do_freeform(channel, thread_ts, f"{cmd} {rest_f}".strip())
        else:
            suggestion = ""
            close = cw.difflib.get_close_matches(cmd, _ALL_CMD_NAMES, n=1, cutoff=0.6)
            if close:
                suggestion = f"혹시 `[{close[0]}]`를 말씀하신 거예요?\n\n"
            cw._reply_dedup(channel, thread_ts, f"`[{cmd}]` 는 모르는 명령이에요.\n\n" + suggestion + _COMBINED_GUIDE)


def _dispatch_cowriter_narrow_chain(channel: str, thread_ts: str, query: str, event: dict,
                                     in_thread: bool) -> bool:
    """Step 5: co-writer's narrow no-bracket inline chain (co-writer-bot/app.py
    L3856-3954, minus the trailing revise/freeform/guide fallback which is step 7).
    Returns True iff one of the 6 checks consumed the message (each already sent its own
    reply before returning True, exactly like the original inline code)."""
    # 5.1 bare notion link -> auto-register/sync (co-writer's job in both single-bot and
    # merged setups; storyboard's own _WORK_NOT_FOUND_MSG explicitly points users at
    # co-writer's [동기화] for this).
    if config.NOTION_TOKEN and re.search(r"https?://\S*notion\.\S+", query):
        cw._do_sync(channel, thread_ts, query)
        if event.get("files"):
            ftx, _blocked = _files_text(event)
            if ftx.strip():
                _reply(channel, thread_ts,
                       "📎 첨부 파일도 있네요. 방금 링크는 **동기화**했고, 이 파일들로 "
                       "**새 작품 기획안**을 만들 수도 있어요.\n"
                       "만들려면 이 스레드에 `기획안 만들어줘`(또는 `기획`) 라고 답글 주세요.")
        return True

    # 5.2 brand-new team, zero registered works, first-ever message -> onboarding
    if not in_thread and not works.all_works():
        _reply(channel, thread_ts, cw._ONBOARD_FIRST_CONTACT)
        return True

    # 5.3 accepting a just-made "새 작품 기획안" file-based plan offer
    if (in_thread and cw._PLAN_ACCEPT_RE.search(query)
            and (len(query.strip()) <= 20 or re.search(r"기획|만들", query))):
        offered = any(mm["role"] == "assistant" and "새 작품 기획안" in mm["content"]
                      for mm in _thread_messages(channel, thread_ts))
        if offered:
            ftx = cw._thread_parent_files_text(channel, thread_ts)
            if ftx.strip():
                cw._do_plan(channel, thread_ts, "", files_text=ftx)
            else:
                _reply(channel, thread_ts,
                       "첨부 파일을 다시 못 읽었어요. 파일을 이 스레드에 다시 올리고 `[기획]` 해주세요.")
            return True

    # 5.4 "이걸로 확정/입력/저장" -- FIX-2 + FIX-2b: suppressed inside an active storyboard
    # thread, or while a pending conti-final-confirm card is outstanding, so a bare "확정"
    # meant for storyboard doesn't get hijacked into co-writer's sheet-save flow.
    if in_thread and cw._is_confirm(query):
        guarded = (_in_active_storyboard_thread(channel, thread_ts)
                   or thread_ts in sb._PENDING_CONTI_FINAL_NL)
        if not guarded:
            savecmd = cw._draft_save_cmd(channel, thread_ts)
            if not savecmd:
                tmsgs = _thread_messages(channel, thread_ts)
                twk = cw._work_from_thread("\n".join(mm["content"] for mm in tmsgs))
                twk = (works.resolve(twk) or twk) if twk else None
                if twk:
                    ctx = cw._thread_gen_context(tmsgs)
                    tkind = ("대본" if "대본" in query else ("개요" if "개요" in query else None)) or ctx[1]
                    tem = (re.search(r"(\d+)\s*화", query)
                           or re.search(r"(\d+)\s*화", "\n".join(mm["content"] for mm in tmsgs)))
                    tep = tem.group(1) if tem else None
                    if tkind and tep:
                        savecmd = f"<{twk}> {tkind} / {tep}화"
            if savecmd:
                qep = re.search(r"(\d+)\s*화", query)
                qkind = "대본" if "대본" in query else ("개요" if "개요" in query else None)
                if qep or qkind:
                    wtok = re.match(r"\s*(<[^>]+>)", savecmd)
                    wtok = wtok.group(1) if wtok else ""
                    kind = qkind or ("대본" if "대본" in savecmd else "개요")
                    m2 = re.search(r"(\d+)\s*화", savecmd)
                    ep = qep.group(1) if qep else (m2.group(1) if m2 else None)
                    if wtok and ep:
                        savecmd = f"{wtok} {kind} / {ep}화"
                cw._do_input(channel, thread_ts, savecmd, mode="save")
            else:
                _reply(channel, thread_ts,
                       "무엇을 확정할지 못 찾았어요. `[입력] <작품> 개요 / 1화` 처럼 경로를 알려주세요.")
            return True
        # guarded: fall through to steps 6/7 instead of returning

    # 5.5 "응/네/일반/그냥 해줘" accepting a "작품명이 없어요 — 일반으로 드릴까요?" offer
    if (in_thread and re.search(r"응|네|일반|그냥|좋아|해줘|ㅇㅋ|ㅇㅇ|ok", query, re.I)
            and len(query.strip()) <= 20):
        amsgs = _thread_messages(channel, thread_ts)
        asked_generic = any(mm["role"] == "assistant" and "작품명이 없어요" in mm["content"] for mm in amsgs)
        if asked_generic:
            for mm0 in reversed(amsgs):
                if mm0["role"] != "user":
                    continue
                cm0 = cw.CMD_RE.match(mm0["content"])
                if not cm0:
                    continue
                c0, body0 = cm0.group(1).strip(), cm0.group(2).strip()
                if c0 in cw.CMD_IDEA:
                    cw._do_idea(channel, thread_ts, body0, force_generic=True); return True
                if c0 in cw.CMD_GEN:
                    cw._do_generate(channel, thread_ts, body0, force_generic=True); return True
                if c0 in cw.CMD_FEEDBACK:
                    cw._do_feedback(channel, thread_ts, body0, mode="both", force_generic=True); return True
                if c0 in cw.CMD_FB_FUN:
                    cw._do_feedback(channel, thread_ts, body0, mode="fun", force_generic=True); return True
                if c0 in cw.CMD_FB_LOGIC:
                    cw._do_feedback(channel, thread_ts, body0, mode="logic", force_generic=True); return True

    # 5.6 "이걸로 작품/기획 만들어줘" (trend/idea thread) -> offer, or accept-and-run
    if in_thread:
        asked = any(mm["role"] == "assistant" and "기획 초안을 생성할까요" in mm["content"]
                    for mm in _thread_messages(channel, thread_ts))
        has_link = bool(re.search(r"https?://\S*notion\.\S+", query))
        if asked and (has_link or cw._PLAN_ACCEPT_RE.search(query)) and len(query.strip()) <= 60:
            cw._do_plan(channel, thread_ts, query if has_link else "")
            return True
        if cw._MAKE_WORK_RE.search(query):
            _reply(channel, thread_ts,
                   "이걸로 **기획 초안을 생성할까요**? 이 스레드에 `응`(또는 `만들어줘`)라고 답해주세요.\n"
                   "📎 **노션 링크를 함께 주면** 그 페이지에 **새 작품으로 기록**해드려요 "
                   "(예: `<노션링크> 만들어줘`).")
            return True

    return False


# ============================================================================
# inflight-tracking wrapper (moved here from dispatch_cowriter.py -- see the NOTE left at
# its old location there). Same logic/file format as co-writer's original
# (co-writer-bot/app.py L3730-3845), except _replay_inflight now calls the MERGED
# _handle_dispatch above instead of co-writer's old one.
# ============================================================================
_INFLIGHT = config.BASE_DIR / "data" / "inflight.json"
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_KEYS = ("channel", "ts", "thread_ts", "text", "user", "channel_type")


def _inflight_load() -> dict:
    import json
    try:
        return json.loads(_INFLIGHT.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _inflight_save(d: dict) -> None:
    import json
    try:
        _INFLIGHT.parent.mkdir(parents=True, exist_ok=True)
        _INFLIGHT.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.warning("inflight 저장 실패")


def _inflight_add(event: dict) -> str | None:
    key = event.get("ts") or event.get("event_ts")
    if not key:
        return None
    with _INFLIGHT_LOCK:
        d = _inflight_load()
        prev = d.get(key) or {}
        d[key] = {"event": {k: event.get(k) for k in _INFLIGHT_KEYS},
                  "attempts": prev.get("attempts", 0),
                  "created": prev.get("created", time.time())}
        _inflight_save(d)
    return key


def _inflight_done(key: str | None) -> None:
    if not key:
        return
    with _INFLIGHT_LOCK:
        d = _inflight_load()
        if d.pop(key, None) is not None:
            _inflight_save(d)


def _replay_inflight() -> None:
    """기동 시: 이전 실행에서 완료 못 하고 남은(=처리 중 죽은) 요청을 다시 실행.
    반복 크래시 방지 위해 재시도는 1회까지만."""
    time.sleep(8)   # 소켓·시트 준비 대기
    with _INFLIGHT_LOCK:
        d = _inflight_load()
    if not d:
        return
    _RESUME_MAX_AGE = 20 * 60
    replay, keep = [], {}
    now = time.time()
    for key, rec in d.items():
        if rec.get("attempts", 0) >= 1:
            log.warning("중단 요청 재시도 포기(반복 실패 가능): %s", key)
            continue
        if now - rec.get("created", now) > _RESUME_MAX_AGE:
            log.warning("중단 요청 재시도 포기(너무 오래됨): %s", key)
            continue
        replay.append((key, rec))
        rec["attempts"] = rec.get("attempts", 0) + 1
        keep[key] = rec
    with _INFLIGHT_LOCK:
        _inflight_save(keep)
    for key, rec in replay:
        ev = rec.get("event") or {}
        ch = ev.get("channel")
        th = ev.get("thread_ts") or ev.get("ts")
        own_ts = ev.get("ts")
        if not ch:
            _inflight_done(key)
            continue
        try:
            resp = app.client.conversations_replies(
                channel=ch, ts=th, limit=config.THREAD_HISTORY_LIMIT)
            newer_user = any(
                mm.get("user") and mm.get("user") != BOT_USER_ID and not mm.get("bot_id")
                and float(mm.get("ts", 0)) > float(own_ts or 0)
                for mm in resp.get("messages", []))
        except Exception:
            newer_user = False
        if newer_user:
            log.info("중단 요청 재개 건너뜀(이후 새 메시지 있음): %s", key)
            _inflight_done(key)
            continue
        log.info("중단 요청 재실행: ch=%s text=%r", ch, (ev.get("text") or "")[:60])
        try:
            app.client.chat_postMessage(
                channel=ch, thread_ts=th,
                text="🔄 봇이 재시작되며 이전 요청이 중단됐어요. 다시 실행할게요…")
        except Exception:
            pass
        try:
            _handle_dispatch(ev)
            _inflight_done(key)
        except Exception:
            log.exception("중단 요청 재실행 실패: %s", key)


def _handle(event: dict) -> None:
    """디스패치 래퍼: 처리 시작을 파일에 기록, 완료(또는 예외)되면 제거.
    처리 도중 프로세스가 죽으면 기록이 남아 기동 시 _replay_inflight가 재실행."""
    key = _inflight_add(event)
    try:
        _handle_dispatch(event)
    finally:
        _inflight_done(key)


# ============================================================================
# the ONE pair of Bolt event handlers for the merged bot.
# ============================================================================
@app.event("app_mention")
def on_mention(event, ack):
    ack()
    log.info("app_mention 수신: ch=%s text=%r", event.get("channel"), (event.get("text") or "")[:60])
    _handle(event)


@app.event("message")
def on_message(event, ack):
    ack()
    if event.get("bot_id") or event.get("subtype"):
        return
    if event.get("channel_type") == "im":
        _handle(event)
        return
    # channel/group message, no explicit mention: app_mention already covers the
    # mentioned case (handling it again here would double-reply) -- but storyboard's
    # existing UX lets a user keep talking in an ALREADY-ACTIVE bot thread without
    # re-mentioning every reply. Extended here to any thread the merged bot has replied
    # in (not storyboard-specific), since post-merge there is only one bot identity.
    if MENTION_RE.search(event.get("text", "")):
        return
    thread_ts = event.get("thread_ts")
    if thread_ts and sb._is_active_bot_thread(event["channel"], thread_ts):
        _handle(event)


def start_background_jobs() -> None:
    """Bundles both bots' former startup-recovery background threads. NOT called
    automatically by importing this module (importing only registers the Bolt event
    handlers above) -- whoever writes the new top-level entry point (see module
    docstring's GAP note) must call this once at startup, alongside both backends'
    healthcheck() and before/after starting SocketModeHandler. Mirrors:
      - co-writer-bot/app.py's `threading.Thread(target=_replay_inflight, daemon=True).start()`
      - storyboard-bot/app.py's `_resume_pending_jobs()` (storyboard ran this synchronously
        at startup, before starting the socket handler; kept synchronous here too since it
        posts "resuming" messages before returning -- call this from the same place in the
        new entry point that storyboard's __main__ block called it from)
    """
    threading.Thread(target=_replay_inflight, daemon=True).start()
    sb._resume_pending_jobs()
