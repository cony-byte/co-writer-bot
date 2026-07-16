"""shared/slack_io.py -- common Slack I/O plumbing, unified from co-writer-bot
and storyboard-bot's near-duplicate copies (see extraction report for the
per-function cowriter-vs-storyboard diff and why each canonical body was
picked). Mechanically extracted verbatim from the originals for every
function; storyboard's copy was picked as canonical wherever it was a safe
superset (_thread_messages, _thinking, _update_note, _post_chunks,
_looks_like_mention). Only _thinking and _work_from_thread additionally got a
small, documented HAND PATCH on top of the verbatim slice (not a pure
mechanical copy) -- see the comments directly above each for what and why.

NOTE (needs follow-up from the router-merge effort): this module is made the
single owner of the Slack Bolt `app` instance / bot identity (BOT_USER_ID,
BOT_BOT_ID) so dispatch_cowriter.py and dispatch_storyboard.py don't each
instantiate their own `App(...)` (that would create two live Bolt clients in
one process). dispatch.py's router should import `app`/`log` from here rather
than creating its own.
"""
import logging
import re

from slack_bolt import App

from bot import config, conti_state
from bot.shared import works

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

app = App(token=config.SLACK_BOT_TOKEN)
_AUTH = app.client.auth_test()
BOT_USER_ID = _AUTH["user_id"]
BOT_BOT_ID = _AUTH.get("bot_id")   # distinguishes our own bot messages from any other bot's

MENTION_RE = re.compile(rf"<@{BOT_USER_ID}>\s*")
_PLACEHOLDER_TOKENS = {"작품", "이름", "인물", "element_id", "heading", "name", "꺾쇠"}

# --- _reply --- canonical: cowriter (cosmetic-only (type hints))
def _reply(channel: str, thread_ts: str, text: str) -> None:
    app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)

# --- _post_chunks --- canonical: storyboard (REAL DIFF (discovered after a live-file re-snapshot mid-session, see top-of-file note): storyboard's version passes `blocks=[]` when overwriting a placeholder, clearing the new [stop] button (see _thinking) once the final result lands -- without this, a completed job would keep showing a live stop button. Adopted as a set together with _thinking/_update_note.)
def _post_chunks(channel, thread_ts, text, replace_ts=None):
    chunk, chunks = "", []
    for para in _mrkdwn(text or "(빈 응답)").split("\n\n"):
        if len(chunk) + len(para) + 2 > 3800:
            chunks.append(chunk); chunk = para
        else:
            chunk = f"{chunk}\n\n{para}" if chunk else para
    if chunk:
        chunks.append(chunk)
    for i, c in enumerate(chunks):
        if i == 0 and replace_ts:
            try:
                # blocks=[] — replace_ts 자리가 _thinking(stop_button=True)로 만든 placeholder면
                # 여기서 최종 결과로 덮어쓰는 시점에 [🛑 중단] 버튼도 같이 지운다(끝난 작업을
                # 다시 취소하려는 헛클릭 방지). placeholder에 버튼이 없었으면 그냥 no-op.
                app.client.chat_update(channel=channel, ts=replace_ts, text=c, blocks=[]); continue
            except Exception:
                pass
        _reply(channel, thread_ts, c)

# --- _thread_messages --- canonical: storyboard (REAL DIFF: storyboard adds BOT_BOT_ID-based foreign-bot-message exclusion (superset, safe to adopt))
def _thread_messages(channel, thread_ts):
    resp = app.client.conversations_replies(channel=channel, ts=thread_ts,
                                            limit=config.THREAD_HISTORY_LIMIT)
    out = []
    for m in resp.get("messages", []):
        t = _clean(m.get("text", ""))
        if not t:
            continue
        is_ours = m.get("user") == BOT_USER_ID or (BOT_BOT_ID and m.get("bot_id") == BOT_BOT_ID)
        if m.get("bot_id") and not is_ours:
            continue   # 다른 봇(co-writer 등)의 메시지는 우리 상태 판단에 안 섞이게 무시
        role = "assistant" if is_ours else "user"
        out.append({"role": role, "content": t})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out

# --- _mrkdwn --- canonical: cowriter (cosmetic-only (docstring))
def _mrkdwn(text: str) -> str:
    """표준 마크다운 → 슬랙 mrkdwn. **볼드**→*볼드*, ## 헤더 → 굵은 줄."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text or "")          # **x** → *x*
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*(.+?)\s*$", r"*\1*", text)  # # 헤더 → *굵은 줄*
    return text

# --- _thinking --- canonical: storyboard+patch (SIGNATURE DIFF + REAL FEATURE DIFF: storyboard's current body (re-verified after the live-file snapshot, see top-of-file note) adds a `stop_button=False` param that attaches a Slack [stop] button to the placeholder message -- a real feature add, paired with _update_note(clear=...) and _post_chunks' blocks=[] cleanup. cowriter has neither, but has a default value for `note` that storyboard's requires positionally; verified every call site in both files always passes `note` explicitly, so restoring the default is a safe superset patch on top of storyboard's body.)
def _thinking(channel, thread_ts, note: str = "생성 중이에요… (몇 초~1분)", stop_button=False):
    """★2026-07-16 "잠깐만 멈춰줄래?" 같은 자연어 변형이 _STOP_RE에 안 걸려서 못 멈춰지는
    문제 — 텍스트 매칭을 더 넓히는 대신(사용자 요청), 이 placeholder 자체에 autopilot의
    [🛑 중단] 버튼(_reply_with_stop_button/_act_autopilot_stop과 동일한 action_id
    "autopilot_stop", 스레드 범위 범용 취소)을 옵션으로 붙여 텍스트 없이도 바로 멈출 수 있게
    한다. stop_button=True로 붙인 메시지는 이후 _update_note(..., clear=True)나
    _post_chunks(..., replace_ts=...)가 최종 결과로 덮어쓸 때 blocks=[]로 버튼을 지운다."""
    try:
        kwargs = {}
        if stop_button:
            kwargs["blocks"] = [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"⏳ {note}"}},
                {"type": "actions",
                 "elements": [{"type": "button", "text": {"type": "plain_text", "text": "🛑 중단"},
                              "style": "danger", "action_id": "autopilot_stop"}]},
            ]
        return app.client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                           text=f"⏳ {note}", **kwargs).get("ts")
    except Exception:
        return None

# --- _update_note --- canonical: storyboard (REAL DIFF (same live-file re-check as above): storyboard adds `clear=False` to blank out the stop button when the final result overwrites the placeholder. Adopted as part of the same _thinking/_post_chunks/_update_note feature set.)
def _update_note(channel, ts, note, clear=False):
    """clear=True — _thinking(stop_button=True)로 붙인 [🛑 중단] 버튼을 최종 결과/실패
    메시지로 덮어쓸 때 같이 지운다(blocks=[]). 중간 진행 갱신(하트비트 등)은 기본값(False)으로
    버튼을 그대로 유지해 생성 중에도 계속 누를 수 있게 둔다."""
    if not ts:
        return
    try:
        kwargs = {"blocks": []} if clear else {}
        app.client.chat_update(channel=channel, ts=ts, text=f"⏳ {note}", **kwargs)
    except Exception:
        pass

# --- _clean --- canonical: cowriter (cosmetic-only (comments removed in storyboard, logic identical))
def _clean(text: str) -> str:
    # 슬랙은 사용자가 친 < > & 를 HTML 엔티티로 보냄 → 되돌려야 <작품> 패턴이 잡힘
    text = MENTION_RE.sub("", text or "")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    text = text.strip()
    # 슬랙이 자동으로 붙인 목록 기호(1. / 1) / - / • 등)를 앞에서 제거 → 명령이 '['로 시작하게
    text = re.sub(r"^\s*(?:\d+[.)]|[-*•·▪◦])\s+", "", text)
    return text.strip()

# --- _looks_like_mention --- canonical: storyboard (REAL DIFF: storyboard's regex checks are IDENTICAL logic to cowriter's _MENTION_TOKEN_RE (just split into 2 re.match calls instead of 1 compiled alternation) PLUS an extra `w in _PLACEHOLDER_TOKENS` check -- strict superset, safe to adopt. Needs _PLACEHOLDER_TOKENS constant carried over.)
def _looks_like_mention(w: str) -> bool:
    """Slack 멘션/채널 토큰(<@U...>, <#C...|이름>, <!here> 등)이나 봇 자신의 안내 문구에
    쓰는 <작품>/<이름> 같은 플레이스홀더를 진짜 <작품> 태그로 오인하지 않게.
    다른 봇/사람을 멘션한 스레드, 혹은 봇의 사용법 안내 텍스트 자체("[스토리보드] <작품> ...")가
    스레드에 남아있을 때 그걸 작품명으로 잘못 인식하던 버그들 방지."""
    w = (w or "").strip()
    if not w:
        return False
    return bool(re.match(r"^[@#!]", w) or re.match(r"^[UBWC][A-Z0-9]{6,}(\||$)", w)
                or w in _PLACEHOLDER_TOKENS)

# --- _convo_text --- canonical: cowriter (cosmetic-only (type hint))
def _convo_text(messages: list[dict]) -> str:
    lines = [f"[{'작가' if m['role'] == 'user' else '봇'}] {m['content']}" for m in messages]
    return "\n".join(lines) + "\n\n(위 대화 흐름을 그대로 이어서 답하라.)"

# --- _last_assistant_with --- canonical: cowriter (cosmetic-only (type hints/docstring))
def _last_assistant_with(messages: list[dict], markers: list[str]) -> str:
    """스레드에서 markers 중 하나를 포함하는 '가장 최근 봇 메시지' 본문. 없으면 ''."""
    for m in reversed(messages):
        if m["role"] == "assistant" and any(k in m["content"] for k in markers):
            return m["content"]
    return ""

# --- _md_table_to_csv --- canonical: cowriter (cosmetic-only (comment))
def _md_table_to_csv(text: str) -> str | None:
    """마크다운 표(| a | b |) → CSV 문자열. 표 행이 2줄 미만이면 None(표 아님)."""
    rows = []
    for ln in text.splitlines():
        s = ln.strip()
        if not (s.startswith("|") and s.endswith("|") and len(s) > 1):
            continue
        cells = [c.strip() for c in s[1:-1].split("|")]
        if cells and all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells):
            continue                                   # |---|:--| 구분선 행 스킵
        rows.append(cells)
    if len(rows) < 2:
        return None
    import csv
    import io
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue()

# --- _work_from_thread --- HAND-MERGED (not a pure slice): storyboard's superset signature (adds thread_ts=None + conti_state fallback) + config.NOTION_TOKEN guard, kept, but reverted the substring-match line back to cowriter's stricter word-boundary regex (storyboard's plain `nm in text` is a real precision regression -- could false-positive on a work name that is a substring of another word). Needs human sign-off before this is treated as final.
def _work_from_thread(joined, thread_ts=None):
    """스레드에서 작품 회수: ①노션 링크 → 등록 작품 ②<작품> 토큰(URL·멘션 제외) ③꺾쇠 없이도
    등록 작품명이 텍스트에 그대로 있으면 ④(thread_ts 주어지면) conti_state에 기록된 이 스레드의
    작품 — 스레드가 길어 THREAD_HISTORY_LIMIT(40개)에 원래 작품 태그가 잘려나가도, 씬설계/콘티가
    이미 한 번 성공한 스레드라면 그때 기록해둔 작품으로 여전히 찾을 수 있다(2026-07-13)."""
    lm = re.search(r"https?://\S*notion\.\S+", joined or "")
    if lm and config.NOTION_TOKEN:
        from bot.shared import notion_sync
        pid = notion_sync.extract_page_id(lm.group(0))
        if pid:
            w = works.work_by_page(pid)
            if w:
                return w
    for w in re.findall(r"<\s*([^>]+?)\s*>", joined or ""):
        w = w.strip()
        if w.startswith("http") or "notion." in w or _looks_like_mention(w):
            continue
        return w
    # 꺾쇠 없이도 등록된 작품명/별칭이 문장에 그대로 있으면 인식(가장 긴 매칭 우선)
    text = joined or ""
    cands = []
    for w, v in (works.all_works() or {}).items():
        for nm in ({w} | set(v.get("aliases") or [])):
            if nm and len(nm) >= 2 and re.search(
                    rf"(?<![\w가-힣]){re.escape(nm)}(?![\w가-힣])", text):
                cands.append((len(nm), w))
    if cands:
        cands.sort(reverse=True)
        return cands[0][1]
    if thread_ts:
        rec = conti_state.get_episode(thread_ts)
        if rec and rec.get("work"):
            return rec["work"]
    return None

