"""dispatch_storyboard.py -- storyboard-only command handlers + all _maybe_* natural-language triggers, mechanically extracted verbatim from storyboard-bot/app.py via ast line-span slicing. See extraction report for the rename/collision map and gap report."""
import concurrent.futures as cf
import itertools
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.request
import uuid
from pathlib import Path
from bot import config
# NOTE (merge-time fix, not part of the mechanical extraction): co-writer-bot and
# storyboard-bot each have their OWN bot/generator.py and bot/prompts.py with the SAME
# module names but INCOMPATIBLE content (different function signatures -- e.g. storyboard's
# generator.complete() takes a job_key= param and exposes cancel()/cancel_prefix()/CANCEL_MSG
# for the "멈춰" stop-button feature that co-writer's generator.py has no equivalent of;
# storyboard's prompts.storyboard_system()/storyboard_plan_system() take a cut_target= kwarg
# and prompts.element_extract_system()/element_extract_user() don't exist at all in
# co-writer's prompts.py). Since only ONE bot/generator.py and ONE bot/prompts.py can exist
# in the merged bot/ package, and co-writer's copies already occupy those names (used by
# dispatch_cowriter.py), storyboard's copies were brought in under distinct names
# (bot/sb_generator.py, bot/sb_prompts.py, bot/sb_video_guide.py) and aliased back to the
# names this file's body already uses everywhere (`generator.`/`prompts.`) so no call site
# below needed to change. See HANDOFF risk #3: the two LLM-call backends must never be
# collapsed into one module -- this is that principle applied one level deeper than HANDOFF
# had already anticipated (it flagged generator.py itself, but not that prompts.py/
# video_guide.py have the exact same shared-name-different-body problem).
from bot import sb_generator as generator
from bot import sb_prompts as prompts
from bot.shared import works
from bot import openrouter_image as oi
from bot import higgsfield_image as hf
from bot import higgsfield_video as hf_video_higgsfield  # 폐기 안 함 — 문제 생기면 되돌릴 백업
from bot import openrouter_video as hf_video  # 2026-07-13부터 실제 영상화 백엔드(seedance 2.0 fast)
from bot import storyboard_grid as grid
from bot import vp_store
from bot import video_index
from bot import edit_plan
from bot import episode_compile
from bot import openrouter_music as music
from bot import figma_bridge
from bot.shared import job_ledger
from bot import still_state
from bot import pending_element_state
from bot import pending_element_pick_state
from bot import conti_state
from bot import interrupted_state
from bot.shared import notion_sync

from bot.shared.files import (
    _files_text, _image_files, _decode_text, _hwpx_text, _parse_json_array,
    _repair_json_quotes,
)
from bot.shared.slack_io import (
    app, log, _reply, _post_chunks, _thread_messages, _mrkdwn, _thinking, _update_note,
    _clean, _looks_like_mention, _convo_text, _last_assistant_with, _md_table_to_csv,
    _work_from_thread, BOT_USER_ID, BOT_BOT_ID, _CANCEL,
)


MENTION_RE = re.compile(rf"<@{BOT_USER_ID}>\s*")

CMD_RE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*(.*)$", re.S)   # [명령] 나머지

SUB_RE = re.compile(r"^\s*<\s*([^>]+?)\s*>\s*(.*)$", re.S)      # <작품> 나머지

CMD_STORYBOARD_ALL = {"스토리보드", "스토리보드1", "스토리보드2", "스보", "스보1", "스보2",
                      "씬설계", "콘티", "상세콘티", "storyboard", "storyboard1", "storyboard2"}

CMD_IMG = {"이미지", "스토리보드3", "스보3", "그리드", "image", "storyboard3"}

CMD_STILL = {"스틸컷", "스틸", "스틸샷", "still", "stillcut"}

CMD_FILE = {"파일", "내보내기", "다운로드", "export", "file", "md", "markdown", "txt", "csv"}

_EXPORT_TYPES = {
    "md": ".md", "markdown": ".md", "마크다운": ".md",
    "txt": ".txt", "text": ".txt", "텍스트": ".txt",
    "csv": ".csv", "시트": ".csv",
}

CMD_REF = {"참조", "레퍼런스", "캐릭터", "얼굴", "인물참조", "ref", "reference"}

CMD_CONTI_FINAL = {"콘티확정", "콘티반영", "최종콘티", "콘티최종"}

CMD_COMPILE = {"합본", "합본만들기", "compile"}

CMD_RESET_EPISODE = {"화초기화", "출력초기화", "아웃풋초기화", "output초기화", "reset"}

CMD_AUTOPILOT = {"자동주행", "자율주행", "autopilot"}

# (2026-07-16) 진행 상황 리포트 — 그 화에서 스틸컷/영상 중 뭐가 아직 안 만들어졌는지 씬별로
# 알려준다. 읽기 전용(생성/삭제/job_ledger 아무것도 건드리지 않음) — CMD_RESET_EPISODE와
# 이름 결이 비슷하지만 파괴적 동작이 없어 danger 버튼 확인 절차가 필요 없다.
CMD_EPISODE_STATUS = {"진행상황", "미완성확인", "남은작업", "status"}

# ★2026-07-20 "작품마다 그림체를 다르게 쓰고 싶다" — [스타일] <작품> <스타일명> 명령으로
# works.py에 등록된 style_key(STYLE_PRESETS 참고)를 바꾼다.
CMD_STYLE = {"스타일", "그림체", "장르", "style", "genre"}

_REF_SAVE_EXTS = (".png", ".jpg", ".jpeg", ".webp")     # openrouter_image._REF_EXTS와 동일해야 함

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".tiff")

SB_GEN_RE = re.compile(r"^\s*(생성|생성해|생성해줘|콘티|상세\s*콘티|만들어|만들어줘|ㄱㄱ|고고)\s*$")

IMG_RE = re.compile(r"^\s*(이미지|그리드|이미지\s*생성|image)\s*$")

SB_BADGE_PLAN = "🎬 *[1단계] 씬 설계* — 씬 나누기·시간 고칠 것 있으면 말해주세요. 좋으면 「생성」\n\n"

SB_BADGE_BOARD = "🎬 *[2단계] 상세 콘티* — 좋으면 「이미지」로 그리드까지. 고칠 것 있으면 말해주세요.\n\n"

# _CANCEL now imported from bot.shared.slack_io (2026-07-16, Phase 4 collision fix --
# see that module's comment: was a separate, disconnected set here before, so a "그만"/
# "중단" said during a co-writer generation never reached co-writer's own cancel checks).

_HELP = (
    "🎬 *스토리보드 봇* — 대본을 컷 스토리보드/이미지/영상으로.\n"
    "```\n"
    "대본 → 씬 설계 → 상세 콘티 → 스틸컷/이미지 → 영상화 → 합본\n"
    "```\n"
    "*지금 할 수 있는 것*\n"
    "```\n"
    "대본이 있으면 → 시작하기: \"<작품> 3화\"처럼 작품+화만 말해도 됨([스토리보드] 안 붙여도 됨).\n"
    "씬 설계 없으면 1단계부터, 있으면 2단계(상세 콘티)로 자동 진행. '25컷'으로 컷 수 지정 가능.\n"
    "2단계에서 [✅ 통과]→씬 선택 (노션 저장은 \"노션에 저장해줘\"라고 말하면 됨)\n"
    "다시 씬 설계부터: \"씬설계부터 다시\"/\"처음부터\"라고 말하면 강제로 1단계부터\n"
    "\n"
    "콘티가 있으면 → 이미지로 만들기: [이미지] <작품> 또는 그냥 \"이미지 만들어줘\" — 컷별 이미지+그리드 1장\n"
    "한 씬만 스틸컷으로: [스틸컷] <작품> 씬2 또는 \"씬2 스틸컷\" — 그 씬만 인물 고정이미지로 컷 생성. "
    "`씬2,3,4`/`씬2-4`처럼 여러 씬을 한 번에 지정하면 씬마다 순서대로 만들어줌. "
    "[✅확정저장]→visual-pipeline에 저장 / [🔄재생성]\n"
    "스틸컷/이미지가 있으면 → 영상으로 만들기: \"영상으로 만들어줘\"라고 그 씬에서 말하면 됨\n"
    "화 전체를 합본으로: [합본] <작품> 또는 \"합본해줘\" — 콘티+생성된 컷 영상을 LLM이 편집 전략 짜서 "
    "이어붙이고 나레이션 TTS(캐릭터별 고정 목소리)까지 믹싱한 mp4. `씬2,3`/`씬2-4`처럼 뒤에 붙이면 그 씬들만 테스트로 돌릴 수 있음\n"
    "한 번에 전 단계 자동으로: [자동주행] <작품> 3화 — 개요/대본(없으면 자동 생성)→등록확인→씬설계→상세콘티→샷분해/스틸컷→영상화까지 자동으로 "
    "이어서 돌리고, 합본만 확인 요청(기본 켜져있음). *화 번호 필수* — 없으면 실행 안 함(단, 이전에 "
    "진행하던 화가 있으면 화 번호 없이 그대로 이어받음)\n"
    "\n"
    "다듬은 콘티를 최종본으로: [콘티확정] <작품> — 콘티 txt 첨부 → 최종본으로 반영(또는 파일+\"확정\"/\"최종\"만 말해도 됨)\n"
    "결과를 파일로 받기: [파일] csv 회차분배 — 스레드 마지막 답변을 md·txt·csv로 내보내기 ([md]/[txt]/[csv]도 가능)\n"
    "그 화 아웃풋을 통째로 지우기: [화초기화] <작품> 3화 — ⚠️ 영상화/합본 아웃풋 삭제(재확인 버튼 뜸, "
    "확정본 포함). 되돌릴 수 없음. *화 번호 필수* — 없으면 실행 안 함\n"
    "그 화 진행 상황(뭐가 안 만들어졌는지): [진행상황] <작품> 3화 또는 \"3화 뭐 안 만들어졌어?\" — "
    "씬별 스틸컷/영상 완성 컷수·빠진 컷 번호, 합본 여부까지 리포트(읽기 전용, 아무것도 안 만들거나 지우지 않음)\n"
    "```\n"
    "• 대본은 md·txt·csv·hwpx 파일로 첨부해도 읽어요 (또는 명령 뒤에 붙여넣기).\n"
    "• 수정: 같은 명령 뒤에 지시 (예 `[스토리보드1] <작품> 씬3 8초로`) 또는 스레드에서 그냥 자연어로 고쳐달라고 해도 돼요.\n"
    "• *인물/장소/의상/소품 등록(캐릭터 얼굴 일관성)*: 사진을 슬랙에 올리고 \"강태혁 이걸로 해줘\" / \"신혼집 이걸로 해줘\"처럼 "
    "말하면 확정 카드(✅확정/✏️다르게)가 뜨고 눌러서 등록돼요 — 등록되면 이후 이미지 생성에 자동 적용. "
    "`[참조] <작품> 강태혁`처럼 명령으로 먼저 이름을 잡아두고 이미지를 첨부해도 됨. 장소/소품/의상도 같은 방식: "
    "`장소 왕좌의방` / `소품 목걸이` / `의상 잠옷A`\n"
    "• 스틸컷 결과에 \"장소/배경 마음에 안 들어\"라고 답장하면, 그 씬 장소가 미확정일 때 [장소 생성]/[재생성] 카드가 떠요."
)

_GREETING_HELP = (
    "무엇을 도와드릴까요? 지금 도와드릴 수 있는 건:\n"
    "• *스토리보드 만들기* — \"<작품명> 3화\"처럼 작품명+화 번호만 말해도 시작해요 (씬 설계→상세 콘티 자동 진행)\n"
    "• *이미지/스틸컷 생성* — 콘티가 있는 스레드에서 \"이미지 만들어줘\"/\"씬2 스틸컷\"\n"
    "• *영상화/합본* — 스틸컷이 있는 씬에서 \"영상으로 만들어줘\", 화 전체는 \"합본해줘\"\n"
    "• *등록* — 인물/장소/의상/소품 이미지 첨부하고 \"이걸로 해줘\"\n"
    "• 자세한 명령어 목록이 필요하면 \"도움말\"이라고 말해주세요.\n"
)

_FULL_HELP_RE = re.compile(r"^\s*(도움말|명령어\s*목록?|사용법|help)\s*[?!.]*\s*$", re.I)

_WORK_NOT_FOUND_MSG = (
    "작품을 못 찾았어요. `<작품>`을 붙이거나 작품이 잡힌 스레드에서 다시 보내주세요.\n"
    "아직 등록 안 된 작품이면, co-writer 봇에서 `[동기화] <노션링크>`로 먼저 등록해야 해요"
    "(이 봇은 새 작품을 직접 등록하지 않고 co-writer의 등록소를 같이 씀)."
)

def _sheet():
    if not (config.SHEET_WEBAPP_URL and config.SHEET_SECRET):
        return None
    from bot.sheet_bible import SheetBible
    return SheetBible()

def _with_heartbeat(channel, ph, base_note, fn):
    """(D2, 2026-07-13) fn() 실행 중(씬설계/상세콘티처럼 스트리밍 없이 한 번에 끝나는 LLM
    호출) 30초마다 경과 시간을 ph 메시지에 덧붙여 갱신 — 죽었는지 오래 걸리는 건지 실무자가
    매번 물어보지 않아도 알 수 있게. fn의 반환값을 그대로 돌려준다."""
    stop = threading.Event()
    start = time.monotonic()

    def _tick():
        while not stop.wait(30):
            elapsed = int(time.monotonic() - start)
            _update_note(channel, ph, f"{base_note} (경과 {elapsed}초)")

    t = threading.Thread(target=_tick, daemon=True)
    t.start()
    try:
        return fn()
    finally:
        stop.set()

# renamed from _progress_episode (name collision with the other bot's function of the same name, different behavior)
def sb_progress_episode(bible, prefer):
    if not bible:
        return None
    prog = bible.get("progress") or {}
    for t in prefer:
        if prog.get(t):
            return prog[t]
    return next(iter(prog.values()), None) or bible.get("current_episode")

# renamed from _sb_script_from_bible (name collision with the other bot's function of the same name, different behavior)
def sb_script_from_bible(bible, episode):
    if not bible or not episode:
        return ""
    return ((bible.get("scripts") or {}).get(f"{episode}화") or "").strip()

_PLACEHOLDER_TOKENS = {"작품", "이름", "인물", "element_id", "heading", "name", "꺾쇠"}

def _notion_episode_script(full_text, episode):
    """노션 페이지 전체 텍스트에서 그 화의 대본만 추출. 두 포맷 지원(못 찾으면 ''):
    ① '## 대본 N화' 평범한 헤딩(다음 '대본 M화' 전까지) — 코니 스타일(co-writer가 자동 push).
    ② '### N화:' 헤딩 ~ **다음 화(N+1화:) 헤딩 전까지** — 날혐남처럼 사람이 손으로 정리한
       페이지. 그 구간 안엔 연출 레퍼런스·콘티/스토리보드 토글 같은 참고자료 소제목이 실제
       대본보다 먼저 나오는 경우가 많아서, **그 구간의 마지막 소제목 줄 다음부터**를 대본
       본문으로 본다(소제목이 하나도 없으면 구간 전체를 그대로 씀). notion_sync._render()는
       코드블록도 펜스(```) 없이 평문으로 풀어버려서 코드펜스 유무는 신경 쓰지 않는다."""
    if not full_text or not episode:
        return ""
    pat = re.compile(rf"^\s*#*\s*대본\s*{episode}\s*화\b.*$", re.M)
    m = pat.search(full_text)
    if m:
        nxt = re.search(r"^\s*#*\s*대본\s*\d+\s*화\b", full_text[m.end():], re.M)
        end = m.end() + nxt.start() if nxt else len(full_text)
        return full_text[m.start():end].strip()
    pat2 = re.compile(rf"^\s*#{{1,3}}\s*.*?\b{episode}\s*화\s*:.*$", re.M)
    m2 = pat2.search(full_text)
    if not m2:
        return ""
    nxt_ep = re.search(r"^\s*#{1,3}\s*.*?\b\d+\s*화\s*:.*$", full_text[m2.end():], re.M)
    window = full_text[m2.end():(m2.end() + nxt_ep.start() if nxt_ep else len(full_text))]
    inner = list(re.finditer(r"^\s*#{1,3}\s.*$", window, re.M))
    if inner:
        window = window[inner[-1].end():]
    return window.strip()

def _notion_episode_outline(full_text, episode):
    """노션 페이지에서 'N화: 제목' 개요 섹션 추출(다음 'M화:' 또는 헤딩 전까지).
    notion_sync._render()가 볼드(**)를 평문으로 풀어버려서 'N화:'는 헤딩이 아니라 그냥
    평문 줄로 남는다(대본 섹션의 '### N화:'와 달리 개요 섹션은 헤딩 마커가 없음).
    참고용 — 대본 대체로 쓰지 않는다(개요만으로 씬을 만들면 '대본 내용 불변' 원칙에 어긋남)."""
    if not full_text or not episode:
        return ""
    pat = re.compile(rf"^\s*{episode}\s*화\s*:.*$", re.M)
    m = pat.search(full_text)
    if not m:
        return ""
    nxt = re.search(r"^\s*\d+\s*화\s*:|^\s*#{1,3}\s", full_text[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(full_text)
    return full_text[m.start():end].strip()

def _notion_section(full_text, heading):
    """노션 페이지 전체 텍스트에서 '## <heading>' 섹션 본문만 추출(다음 헤딩 전까지, 헤딩줄 제외).
    notion_sync.upsert_section이 쓰는 정확한 제목과 매칭. 못 찾으면 ''."""
    if not full_text or not heading:
        return ""
    pat = re.compile(rf"^\s*#*\s*{re.escape(heading)}\s*$", re.M)
    m = pat.search(full_text)
    if not m:
        return ""
    nxt = re.search(r"^\s*#{1,3}\s+\S", full_text[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(full_text)
    return full_text[m.end():end].strip()

def _fetch_external_conti(work, episode=None):
    """콘티를 노션에서만 회수한다(2026-07-15, 사용자 요청 — 로컬 자동저장 파일은 참고하지
    않는다. 로컬 우선/최신성 비교/노션-실패시-로컬폴백 방식 전부 다 문제였다: 로컬 자동저장
    파일은 화 번호 구분 없이 "작품당 1개"라 마지막으로 생성된 콘티가 뭐든 덮어써버리므로,
    이걸 폴백으로 쓰면 다른 화를 요청했는데도 엉뚱한 화의(그리고 "봇이 방금 만든") 콘티가
    조용히 반환되는 사고가 난다 — 노션 조회가 실패하면 왜 실패했는지 정직하게 알리는 게 맞다).
    노션은: 화 번호를 알면 그 화 "상세 콘티 (N화)" 헤딩 바로 다음 토글(정확한 위치 매칭,
    현재 저장 방식) → 텍스트 섹션(옛 방식) 순으로 시도. 반환: (내용, 출처라벨) 또는 (None, None)."""
    if config.NOTION_TOKEN:
        pid = works.page_of(work)
        if pid:
            from bot.shared import notion_sync
            if episode:
                try:
                    t = notion_sync.find_conti_toggle_for_episode(pid, episode, token=config.NOTION_TOKEN)
                    if t:
                        return t.strip(), f"노션({episode}화 토글)"
                except Exception:
                    log.exception("노션 콘티(화별 토글) 회수 실패")
                try:
                    # 봇이 만든 정확한 형식이 없어도 — 실무자가 직접 써서 붙인 콘티(제목에
                    # "콘티"/"스토리보드" 들어간 토글/헤딩, 씬 헤더 있음)도 인식
                    t = notion_sync.find_authored_conti_for_episode(pid, episode, token=config.NOTION_TOKEN)
                    if t:
                        return t.strip(), f"노션({episode}화, 실무자 작성)"
                except Exception:
                    log.exception("노션 콘티(실무자 작성) 회수 실패")
            try:
                full = notion_sync.page_text(pid)
                sec = _notion_section(full, _NOTION_CONTI_HEADING)
                if sec:
                    return sec, "노션"
            except Exception:
                log.exception("노션 콘티 회수 실패")
    return None, None

def _notion_attached_script(page_id, episode):
    """노션 페이지에 대본이 텍스트 블록이 아니라 **파일**(txt/md/hwpx)로 첨부돼 있을 때 그걸 읽는다.
    (기존엔 notion_sync._render가 file/pdf 블록을 텍스트 없다고 건너뛰어서 이런 페이지는
    대본을 아예 인식 못 했음 — 2026-07-13) 파일명에 화 번호가 있으면 그 화 우선,
    없으면 이름에 '대본'/'콘티'가 들어간 첫 파일을 쓴다.
    반환: (text, error) — _files_text의 (text, blocked_count) 패턴과 같은 모양.
    error가 None이면 "그 화 첨부 파일이 원래 없음"(정상), 문자열이면 "파일은 있는데 못 읽음"
    (다운로드/목록조회 실패) — 이 둘을 구분해야 _script_for가 실패를 페이지 전체 폴백으로
    조용히 덮어버리지 않을 수 있다(2026-07-16, 다른 화 내용이 섞여 들어가는 사고 방지)."""
    from bot.shared import notion_sync
    try:
        files = notion_sync.list_files(page_id)
    except Exception:
        log.exception("노션 첨부파일 목록조회 실패: page_id=%s", page_id)
        return "", "첨부 파일 목록을 못 불러왔어요"
    cands = [f for f in files if re.search(r"대본|콘티|script", f["name"], re.I)]
    if not cands:
        return "", None   # 이 화(또는 이 페이지)엔 원래 첨부된 대본 파일이 없음 — 정상
    if episode:
        exact = [f for f in cands
                 if (m := re.search(r"(\d+)\s*화", f["name"])) and int(m.group(1)) == episode]
        if exact:
            cands = exact
    f = cands[0]
    try:
        raw = notion_sync.download_file(f["url"])
    except Exception:
        log.exception("노션 첨부파일 다운로드 실패: %s", f["name"])
        return "", f"'{f['name']}' 파일을 다운로드하지 못했어요"
    text = _hwpx_text(raw) if f["name"].lower().endswith(".hwpx") else _decode_text(raw).strip()
    return text, None

def _script_for(work, episode, bible):
    """대본 소스: ①시트 바이블(빠른 캐시) → ②없으면 노션 페이지에서 '대본 N화' 섹션만 추출
    (NOTION_TOKEN 있을 때). 페이지에 여러 화가 같이 있으면 섹션을 안 자르면 LLM이 다른 화
    내용을 섞어 쓰는 문제가 있어 반드시 그 화 섹션만 잘라 넘긴다.
    반환: (script, error) — error가 있으면 "그 화 첨부 스크립트가 있는데 못 읽었다"는 뜻이라
    호출자는 페이지 전체로 조용히 대체하면 안 되고(다른 화 내용 혼입 위험), 사용자에게 재시도를
    안내해야 한다(2026-07-16). error가 없으면 script가 빈 문자열이어도 정상(대본을 못 찾음뿐)."""
    s = sb_script_from_bible(bible, episode)
    if s:
        return s, None
    if work and config.NOTION_TOKEN:
        pid = works.page_of(work)
        if pid:
            try:
                from bot.shared import notion_sync
                full = notion_sync.page_text(pid).strip()
                if episode:
                    sec = _notion_episode_script(full, episode)
                    if sec:
                        return sec, None
                att, att_err = _notion_attached_script(pid, episode)
                if att:
                    return att, None
                if att_err:
                    # 첨부 파일이 있는 건 확인했는데 못 읽은 것 — 페이지 전체(다른 화 내용 포함
                    # 가능)로 조용히 대체하면 잘못된 화 내용이 섞여 들어갈 수 있으니, 폴백하지
                    # 않고 실패를 그대로 알린다.
                    log.warning("노션 %s화 첨부 대본 다운로드 실패 → 폴백 안 함(work=%s, pid=%s): %s",
                               episode, work, pid, att_err)
                    return "", att_err
                if episode:
                    log.warning("노션 %s화 섹션 추출 실패 → 페이지 전체를 대본으로 폴백(work=%s, pid=%s) "
                               "— 다른 화 내용이 섞일 수 있음, 헤딩 포맷 확인 필요", episode, work, pid)
                return full, None   # 화 지정 없거나 그 화 섹션을 못 찾으면 폴백으로 전체
            except Exception:
                log.exception("notion 대본 로드 실패")
    return "", None

def _conti_exists_in_notion(work, episode) -> bool:
    """work/episode의 상세 콘티(2단계)가 실제로 노션에 존재하는지 직접 확인.
    ★2026-07-16, 사용자 지시: 스레드 마커/conti_state 캐시를 신뢰하지 말고 2단계 완료 여부는
    무조건 노션 기준으로 판단한다 — 저장이 조용히 실패하거나 다른 화의 마커가 스레드에 남아있어도
    (과거 실사례: 'cony 테스트 작품' 5화) 더 이상 오판하지 않도록."""
    if not (config.NOTION_TOKEN and work and episode):
        return False
    pid = works.page_of(work)
    if not pid:
        return False
    try:
        text = (notion_sync.find_conti_toggle_for_episode(pid, episode, token=config.NOTION_TOKEN)
                or notion_sync.find_authored_conti_for_episode(pid, episode, token=config.NOTION_TOKEN))
        return bool(text and text.strip())
    except Exception:
        log.exception("노션 콘티 존재 확인 실패 — work=%s ep=%s", work, episode)
        return False

# renamed from _sb_stage (name collision with the other bot's function of the same name, different behavior)
def sb_stage(messages, work=None, episode=None):
    """스레드 현재 단계: 2(상세콘티) / 1(씬설계) / 0(아직 없음).

    ★2026-07-16: "[2단계]" 텍스트 마커만 믿으면 노션 저장이 조용히 실패했거나 다른 화의
    마커가 스레드에 남아있는 경우 실제로는 상세 콘티가 없는데도 2단계 완료로 오판한다
    (실사례: 'cony 테스트 작품' 5화). work/episode를 아는 호출부라면 마커를 찾아도 노션에
    실제 콘티가 있는지 직접 확인하고, 없으면 2단계로 확정하지 않고 계속 거슬러 올라가
    1단계 마커를 찾는다. work/episode를 모르는(넘겨줄 수 없는) 호출부는 기존처럼 마커만
    믿는다 — 씬설계(1단계)는 노션에 저장되지 않으므로 이 검증은 2단계에만 적용."""
    have_ctx = bool(work and episode)
    for m in reversed(messages):
        if m["role"] != "assistant":
            continue
        c = m["content"]
        if "[2단계]" in c:
            if not have_ctx or _conti_exists_in_notion(work, episode):
                return 2
            continue  # 마커는 있지만 노션에 실제 콘티가 없음 — 1단계 마커를 계속 찾는다
        if "[1단계]" in c or "씬 설계안" in c:
            return 1
    return 0

_FORCE_STAGE1_RE = re.compile(
    r"씬\s*설계\s*(부터|다시)|1\s*단계\s*(부터|다시)|처음부터|첨부터|새로\s*(씬\s*설계|시작)|"
    r"리셋|갈아엎|다\s*지우고")

_PROCEED_RE = re.compile(r"^(통과|좋아|좋아요|좋습니다|좋네|굿|good|ok|오케이|okay|진행|생성|콘티|콘티\s*만들어\s*줘?|다음|넘어가|고고)\s*[!.~]*$", re.I)

_PROCEED_CORE_RE = re.compile(r"통과|진행|다음\s*단계|\d\s*단계|넘어가|고고|^(좋아|좋아요|좋은데|좋습니다|좋네|굿|good|ok|오케이|okay)\b", re.I)

_PROCEED_BLOCK_RE = re.compile(r"씬\s*\d|바꿔|늘려|줄여|고쳐|수정|빼줘|추가해|다르게")

_PROCEED_NEGATIVE_RE = re.compile(r"문제|안\s*돼|왜|이상해|에러|오류|실패")

_BARE_COSTUME_LABEL_RE = re.compile(r"^\s*(?P<label>[가-힣]{1,10}-?[A-Z])\s*,\s*(?P<desc>.+)$")

def _looks_like_bare_costume_label(q: str) -> bool:
    """work 태그(<...>)를 뗀 나머지가 "{라벨}, {설명}" 형태의 맨몸 의상 등록 시도로 보이는지.
    씬 번호·수정 동사(_PROCEED_BLOCK_RE)가 섞이면 진짜 콘티 수정 지시일 수 있으니 아니라고 본다."""
    q = re.sub(r"<\s*[^>]+?\s*>", "", q or "").strip()
    if _PROCEED_BLOCK_RE.search(q):
        return False
    return bool(_BARE_COSTUME_LABEL_RE.match(q))

def _looks_like_proceed(q: str) -> bool:
    q = (q or "").strip()
    if not q:
        return True
    if _PROCEED_RE.match(q):
        return True
    if _PROCEED_BLOCK_RE.search(q):
        return False
    if "단계" in q and _PROCEED_NEGATIVE_RE.search(q):
        return False
    return bool(_PROCEED_CORE_RE.search(q))

def _do_storyboard_auto(channel, thread_ts, rest):
    """[스토리보드1]/[스토리보드2] 구분 없이: 이 스레드에 씬 설계(1단계)가 없으면 1단계부터,
    있으면(1단계든 2단계든 이미 진행됐으면) 2단계(상세 콘티)로 진행한다.

    단, "씬설계부터 다시"/"1단계부터 다시"/"처음부터" 같은 문구가 있으면 sb_stage 판단을
    무시하고 강제로 1단계부터 새로 한다 — 스레드에 오염된/원치 않는 1단계 기록이 남아있어도
    (예: 다른 봇 메시지를 대본으로 오인했던 과거 오류 등) 사용자가 명시적으로 다시 시작할 수
    있게. 이때 트리거 문구는 떼어내고 나머지(작품·화 번호 등)만 넘겨 '수정 지시'로 오인되지
    않고 깨끗한 재생성(기존 prior_plan 무시, 대본에서 새로)이 되게 한다.

    또한 이 스레드가 이미 다른 화(예: 3화)로 끝까지 진행된 상태에서 사용자가 그냥 "4화
    스토리보드 만들어줘"처럼 새 화 번호를 말하면, 자연어에 "다시"/"처음부터" 같은 트리거가
    없어도 화 번호가 바뀐 걸 감지해 자동으로 1단계부터 다시 시작한다(2026-07-13) — 안 그러면
    이 스레드에 남은 이전 화의 '씬 설계 완료' 기록 때문에 곧장 2단계로 가버려서 "먼저 씬
    설계부터 해주세요"라는 엉뚱한 안내가 나가는 버그가 있었음."""
    q = rest or ""
    force1 = bool(_FORCE_STAGE1_RE.search(q))
    if force1:
        q = _FORCE_STAGE1_RE.sub("", q)
        q = re.sub(r"\b(다시|해줘|새로|부터)\b", "", q)   # 트리거 잔여 필러 정리(instr 오인 방지)
        q = re.sub(r"\s{2,}", " ", q).strip()
    epm = re.search(r"(\d+)\s*[화회]", q)
    if epm and not force1:
        tracked = conti_state.get_episode(thread_ts) or {}
        if tracked.get("episode") and int(epm.group(1)) != tracked["episode"]:
            force1 = True
    _tracked_ctx = conti_state.get_episode(thread_ts) or {}
    # ★2026-07-16, 알려진 한계(conti_state.set_episode 참고): epm이 없으면(이번 메시지에 화
    # 번호가 없으면) _tracked_ctx["episode"]에 그냥 의존한다 — 근데 conti_state는 스레드당
    # "가장 마지막에 추적된 화" 한 쌍만 갖고 있어서, 스레드가 화 번호 명시 없이 다른 화로
    # 조용히 넘어간 뒤라면 이 값이 사용자가 지금 말하려는 화가 아닐 수 있다.
    # sb_stage/_conti_exists_in_notion의 노션 검증은 "그 화의 콘티가 실제로 있는가"만
    # 확인하지, "그 화가 맞는 화인가"는 확인 못 하므로 이 한계를 안전하게 만들지 않는다.
    # 스레드당 화 히스토리 저장 재설계 없이는 구조적으로 못 고치는 부분 — 화 번호를 매번
    # 명시하는 게 유일한 회피책.
    _ep_ctx = epm and int(epm.group(1)) or _tracked_ctx.get("episode")
    cur = sb_stage(_thread_messages(channel, thread_ts), work=_tracked_ctx.get("work"), episode=_ep_ctx)
    if force1 or cur == 0:
        stage = 1
    elif cur == 1:
        # 씬 설계까지만 끝난 상태 — "통과"류 진행 신호나 빈 텍스트가 아니면, 이 답글은 그
        # 씬 설계에 대한 수정 지시로 보고 1단계에 머문다(2단계로 바로 안 넘어감). 안 그러면
        # 답글로 씬 수정을 요청해도 곧장 상세 콘티로 넘어가버려 수정이 반영 안 되는 문제가 있었음.
        stage = 2 if _looks_like_proceed(q) else 1
    else:
        # ★2026-07-15: 2단계(상세 콘티)까지 끝난 스레드는 그 뒤 자유 답글을 전부 "콘티 수정
        # 지시"로 보고 무조건 재생성했다(cur==1과 달리 필터가 아예 없었음) — 의도상 "씬2 대사
        # 늘려줘"류 자연어 수정 지시를 그대로 받으려는 설계지만, "연습복-A, 편하고 활동성
        # 있는 반팔, 반바지"처럼 스토리보드와 무관한 맨몸 의상 등록 시도까지 휩쓸려 상세 콘티를
        # 통째로(1~10분) 재생성하는 사고가 실제로 있었다. _maybe_bare_costume_label_request가
        # 먼저 가로채는 게 1차 방어(디스패치 순서상 여기 오기 전), 이건 2차 방어 — 혹시라도
        # 그 인식기를 못 타고 여기까지 왔다면 재생성 없이 조용히 반환한다.
        if _looks_like_bare_costume_label(q):
            return
        stage = 2
    sb_do_storyboard(channel, thread_ts, q, stage=stage)

def _do_storyboard_auto_chain(channel, thread_ts, query):
    """(C2, 2026-07-13) "3화 스틸컷 만들어줘"처럼 최종 목표(스틸컷/이미지)까지 이미 말했으면,
    씬설계(1단계) 하나만 하고 멈추지 않고 상세콘티(2단계)→그 목표까지 필요한 단계를 자동으로
    이어서 만든다. 도중에 에러/대본없음 등으로 못 넘어갔으면(_sb_stage가 안 올라갔으면) 거기서
    멈춘다 — 실패했는데 다음 단계를 억지로 진행하지 않기 위해."""
    want_still = bool(_GEN_STILL_RE.search(query))
    want_image = bool(_GEN_IMAGE_RE.search(query)) and not want_still
    _do_storyboard_auto(channel, thread_ts, query)
    if not (want_still or want_image):
        return
    _tracked_ctx = conti_state.get_episode(thread_ts) or {}
    stage_now = sb_stage(_thread_messages(channel, thread_ts),
                           work=_tracked_ctx.get("work"), episode=_tracked_ctx.get("episode"))
    if stage_now == 1:
        sb_do_storyboard(channel, thread_ts, "", stage=2)
        _tracked_ctx = conti_state.get_episode(thread_ts) or {}
        stage_now = sb_stage(_thread_messages(channel, thread_ts),
                               work=_tracked_ctx.get("work"), episode=_tracked_ctx.get("episode"))
    if stage_now == 2:
        (_do_stills if want_still else _do_images)(channel, thread_ts, query)

_SB_START_HINT_RE = re.compile(r"\d+\s*[화회]|스토리보드|씬\s*설계|콘티|storyboard")

def _looks_like_storyboard_start(channel, thread_ts, query) -> bool:
    """[스토리보드] 없이, 완전히 새 스레드(씬 설계 이력 0)에서도 시작할 만한 낌새가 있는지.
    화/스토리보드 관련 낱말이 있거나, 등록된 작품명이 문장/스레드에 있으면 시작으로 본다.
    (아무 낌새도 없는 잡담까지 LLM 호출로 새는 걸 막기 위한 최소 안전장치)"""
    if _SB_START_HINT_RE.search(query or ""):
        return True
    joined = "\n".join(m["content"] for m in _thread_messages(channel, thread_ts))
    return bool(_work_from_thread(joined, thread_ts))

_NOTION_CONTI_HEADING = "상세 콘티"   # [💾 노션에 저장]이 쓰는 섹션 제목(upsert — 되읽기용)

def _work_safe_name(work: str) -> str:
    return re.sub(r"[^\w가-힣.\-]+", "_", (work or "ep")).strip("_.") or "ep"

def _thread_conti(channel, thread_ts, msgs, episode=None):
    """스레드의 상세 콘티 회수: 스레드 텍스트([2단계] 마커가 붙은 메시지)에서 찾는다.

    ★2026-07-15: _upload_conti가 노션 저장 성공 시엔 이제 전체 콘티가 아니라 "[2단계] 상세 콘티
    완성 — 노션에서 확인해주세요" 짧은 안내만 스레드에 남긴다(_sb_stage가 이 마커로 단계를
    판단하므로 마커 자체는 계속 붙여야 함) — 근데 그 안내문에도 마커가 있어서, 그냥
    _last_assistant_with(["[2단계]"])만 하면 "콘티 본문"으로 그 짧은 안내 문장을 반환해버려
    _split_scenes 등 후속 파싱이 전부 깨진다("씬을 못 나눴어요" 같은 오류로 이어짐). 실제 콘티
    본문은 항상 "■ 씬N" 헤더를 포함하므로, 마커 있는 메시지라도 그 헤더가 없으면 "본문이 아니라
    안내문"으로 보고 건너뛴다 — 그러면 _thread_conti가 ""(못 찾음)을 반환해 호출부
    (_thread_or_saved_conti)가 노션에서 실제 본문을 다시 읽어오는 폴백으로 자연히 넘어간다.

    ★2026-07-16, 사용자 지시: 이 함수는 원래 episode를 아예 안 받고 그냥 "스레드에서 가장 최근에
    매치되는 [2단계] 메시지"를 무조건 반환했다 — 그런데 그 메시지가 지금 요청받은 화가 아니라
    스레드에 남아있는 *다른* 화의 콘티일 수도 있다는 걸 전혀 검증하지 않았다. 그러면
    _thread_or_saved_conti가 "스레드에 있으니 됐다"고 보고 노션에서 정확한 화를 다시 읽어오는
    폴백을 타지도 않은 채 엉뚱한 화의 콘티를 그대로 써버린다(sb_stage/_conti_exists_in_notion이
    이미 고친 "스레드 마커를 맹신하지 말고 실제 상태로 검증" 문제의 같은 패턴이 콘티 "내용"
    회수 쪽에도 있었던 것). episode가 주어지면, conti_state에 기록된 이 스레드의 현재 화와
    다를 때는 스레드 텍스트를 신뢰하지 않고 ""를 반환해 호출부가 노션에서 그 화의 콘티를
    다시 가져오게 한다 — 스레드 텍스트는 거의 확실히 "다른 화가 추적되던 시점"에 쓰인 것이기
    때문. episode를 안 주는 호출부(아직 감사 안 한 곳)는 예전처럼 마커만 믿는 동작을 유지한다."""
    if episode is not None:
        recorded = conti_state.get_episode(thread_ts)
        if recorded and recorded.get("episode") and recorded["episode"] != episode:
            return ""     # 스레드에 남은 건 다른 화 추적 시점의 텍스트 — 신뢰하지 않고 노션 폴백으로
    for m in reversed(msgs):
        if m["role"] == "assistant" and "[2단계]" in m["content"] and _SCENE_HDR_RE.search(m["content"]):
            return m["content"]                              # 폴백: 옛 스레드/짧은 콘티(본문만)
    return ""

def _thread_or_saved_conti(channel, thread_ts, msgs, work, episode, announce: bool = True):
    """스레드에 콘티가 없어도, 그 작품·화의 상세 콘티가 이미 로컬/노션에 저장돼 있으면
    가져와서 스레드에 반영한다 — 새 스레드에서 [스틸컷]/[이미지]를 바로 요청했을 때 "콘티가
    없다"고 오판해 1단계(씬설계)부터 새로 돌려버리던 문제(2026-07-14, 실무자 지적:
    "3화는 이미 상세 콘티가 있는데 씬 설계부터 하고 있다"). 반환: 콘티 문자열 또는 None.

    announce: True면 전체 콘티를 [2단계] 스타일로 스레드에 다시 올린다(스틸컷/이미지처럼
    사용자가 그 콘티를 보고 수정할 수도 있는 흐름에 적합). False면 조용히 conti_state만
    갱신하고 텍스트를 반환만 한다(2026-07-14, "노션에 있으면 확인만 하면 되는데 왜 통째로
    또 올라오냐" 지적 — [합본]처럼 이미 확정된 콘티를 그냥 갖다 쓰기만 하는 흐름에 씀)."""
    conti = _thread_conti(channel, thread_ts, msgs, episode=episode)
    if conti or not work:
        return conti
    content, src = _fetch_external_conti(work, episode)
    if not content:
        return None
    if announce:
        _upload_conti(channel, thread_ts, work, content, episode=episode)
    conti_state.set_episode(thread_ts, work, episode, human_final=True)
    return content

def _upload_conti(channel, thread_ts, work, conti, replace_ts=None, episode=None):
    """상세 콘티(긴 결과물)를 스레드에 통째로 올리지 않고 노션에 저장한 뒤 짧은 안내만
    남긴다(2026-07-15, 사용자 요청 — 매번 긴 콘티가 스레드를 뒤덮는 문제 해소). NOTION_TOKEN이
    없거나 작품 페이지를 못 찾거나 저장 중 오류가 나면, 콘티가 유실되지 않도록 예전처럼
    chunked 텍스트를 스레드에 그대로 올리는 안전망으로 폴백한다.

    ★2026-07-15 "상세콘티 생성이랑 콘티 가져오기 기능상 문제가 너무 심해" — 이 짧은 안내
    메시지에 "[2단계]" 마커가 원래 안 붙어있었다. _sb_stage가 스레드에서 이 마커를 찾아
    "몇 단계까지 됐는지" 판단하는데(다른 함수도 여럿 이걸 그대로 씀 — 자동주행 stage 게이트,
    _maybe_generate_request의 sb_stage<2 체크 등), 마커가 없으면 방금 콘티를 만들었어도 계속
    "아직 2단계 안 끝남"으로 오판해서 1단계부터 다시 돌리거나 스틸컷/이미지 요청을 거부하는
    등 매우 심각하게 새는 버그였다. 마커는 이제 여기서도 붙이되, _thread_conti가 이 짧은
    안내문 자체를 "콘티 본문"으로 착각해 반환하지 않도록(그러면 _split_scenes가 다 깨짐) 그
    쪽에서 "■ 씬N" 헤더 존재 여부로 본문과 안내문을 구분하게 별도로 고쳤다(_thread_conti
    docstring 참고) — 여기서는 마커만 붙이면 된다."""
    pid = works.page_of(work) if work else None
    if config.NOTION_TOKEN and pid:
        ep = episode if episode is not None else (conti_state.get_episode(thread_ts) or {}).get("episode")
        try:
            from bot.shared import notion_sync
            if ep:
                notion_sync.upsert_conti_toggle_for_episode(pid, ep, conti, token=config.NOTION_TOKEN)
                where = f"{ep}화 대본 아래 토글"
                # ★2026-07-15 저장 직후 재조회로 검증 — 실제 사례(작품="cony 테스트 작품", 5화)에서
                # upsert가 예외 없이 끝났는데도 라이브 노션 페이지에 토글이 안 생겨 있었다. 그런데도
                # 여기서 바로 "[2단계]" 마커 붙은 성공 메시지를 보내는 바람에 sb_stage/conti_state가
                # 2단계 완료로 오판 → 이후 "먼저 씬 설계부터 해주세요" 루프로 이어졌다. 이제 실제로
                # 내용이 읽히는지 확인한 뒤에만 성공을 선언한다.
                verified = notion_sync.find_conti_toggle_for_episode(pid, ep, token=config.NOTION_TOKEN)
                if not (verified and verified.strip()):
                    log.error("노션 콘티 저장 검증 실패 — 저장 직후 재조회했으나 내용이 없음: work=%s ep=%s", work, ep)
                    raise RuntimeError("notion conti save verification failed")
            else:
                notion_sync.upsert_section(pid, _NOTION_CONTI_HEADING, conti, token=config.NOTION_TOKEN)
                where = f"「{_NOTION_CONTI_HEADING}」 섹션"
            if replace_ts:
                _update_note(channel, replace_ts, "✅ [2단계] 상세 콘티 완성 — 노션에 저장했어요.", clear=True)
            _reply(channel, thread_ts,
                   f"✅ [2단계] 상세 콘티 완성 — <{work}> 노션 페이지의 {where}에 저장했어요. 노션에서 확인해주세요.")
            return
        except Exception:
            log.exception("notion 콘티 저장 실패 — 스레드 폴백")
            # ★2026-07-16, 사용자 지시: 이 폴백은 노션 저장이 실패했을 때만 타는데, 그래도
            # "[2단계]" 마커 + "■ 씬N" 헤더가 붙은 본문 그대로를 올려버리면 사용자 눈에는
            # 완성된 콘티가 스레드에 버젓이 보인다. 근데 sb_stage/_conti_exists_in_notion은
            # (일부러) 노션 기준으로만 2단계 완료를 판단하므로 이후 요청에서 계속 "1단계부터
            # 다시 해달라"고 나온다 — 실제 사용자 혼란 사례: "완성본을 봤는데 계속 1단계로
            # 되돌림". sb_stage 판단 자체는 옳다(진짜 저장 안 됐으니까) — 그러니 판단을
            # 바꾸는 게 아니라, 이 폴백 메시지에 "이건 저장 안 된 로컬 안전 카피"라는 걸
            # 명시해서 왜 나중에 안 먹히는지 사용자가 이해하게 한다.
            if replace_ts:
                _update_note(channel, replace_ts, "⚠️ 상세 콘티 완성했지만 노션 저장 실패 (아래)", clear=True)
            _post_chunks(
                channel, thread_ts,
                "⚠️ *노션 저장에 실패했어요* — 아래 콘티는 스레드에만 남은 로컬 안전 카피입니다. "
                "노션에 실제로 저장되기 전까지는 [2단계] 완료로 인식되지 않아, 이 상태로 "
                "스틸컷/이미지를 요청하면 다시 1단계부터 하라고 나올 수 있어요. "
                "「노션에 저장해줘」로 재시도하거나, 상세 콘티를 다시 생성해주세요.\n\n"
                + SB_BADGE_BOARD + conti,
            )
            return
    if replace_ts:
        _update_note(channel, replace_ts, "✅ 상세 콘티 완성 (아래)", clear=True)
    _post_chunks(channel, thread_ts, SB_BADGE_BOARD + conti)

def _sb_buttons(stage):
    kind = "plan" if stage == 1 else "conti"
    pass_lbl = "✅ 통과 → 상세 콘티" if stage == 1 else "✅ 통과 → 이미지"
    elements = [
        {"type": "button", "text": {"type": "plain_text", "text": pass_lbl},
         "style": "primary", "action_id": f"sb_pass_{kind}"},
        {"type": "button", "text": {"type": "plain_text", "text": "🔄 재생성"},
         "style": "danger", "action_id": f"sb_regen_{kind}"},
    ]
    if stage == 2:
        # sb_save_conti 액션 핸들러는 있었는데 이 버튼을 실제로 붙이는 코드가 없어서
        # "노션에 저장해줘"라고 말로 해야만 저장됐음(2026-07-14) — 도움말·주석엔 버튼이
        # 있는 것처럼 적혀 있었지만 실제론 한 번도 게시된 적이 없었다.
        elements.append({"type": "button", "text": {"type": "plain_text", "text": "💾 노션에 저장"},
                         "action_id": "sb_save_conti"})
    return [{"type": "actions", "elements": elements}]

def _with_text_block(text, action_blocks):
    """버튼(actions)만 있는 blocks는 Slack이 본문에 text를 안 보여줄 수 있어서
    section 블록으로 명시 삽입 — 채널 화면에 항상 문구가 보이게 한다."""
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}, *action_blocks]

def _post_buttons(channel, thread_ts, stage):
    text = "다음으로 진행하거나 다시 생성할 수 있어요."
    try:
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=text, blocks=_with_text_block(text, _sb_buttons(stage)))
    except Exception:
        log.exception("액션 버튼 게시 실패")

def _action_ctx(body):
    ch = body["channel"]["id"]
    tts = ((body.get("container") or {}).get("thread_ts")
           or (body.get("message") or {}).get("thread_ts")
           or (body.get("message") or {}).get("ts"))
    return ch, tts

def _disable_buttons(body, status):
    """클릭된 버튼 메시지를 상태 문구로 바꿔 재클릭 방지."""
    try:
        app.client.chat_update(channel=body["channel"]["id"],
                               ts=body["message"]["ts"], text=status, blocks=[])
    except Exception:
        log.exception("버튼 비활성화 실패")

def _reply_with_stop_button(channel, thread_ts, text):
    """★2026-07-15 "이런식을 이어서, 작업중, [중단] 버튼 만들자"(사용자 요청, 다른 도구 UI 참고) —
    지금까지 자동주행 중단은 "취소"/"중단"이라고 텍스트로 답장해야만 했다. 자동주행 시작 메시지에
    실제 클릭 가능한 [🛑 중단] 버튼을 붙여서, 텍스트 기억 없이도 바로 멈출 수 있게 한다. 버튼
    클릭 시 동작은 기존 텍스트 "멈춰"/"취소" 핸들러(_STOP_RE 매치 지점)와 완전히 동일하다."""
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions",
         "elements": [{"type": "button", "text": {"type": "plain_text", "text": "🛑 중단"},
                      "style": "danger", "action_id": "autopilot_stop"}]},
    ]
    app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, blocks=blocks)

@app.action("autopilot_stop")
def _act_autopilot_stop(ack, body):
    """[🛑 중단] 버튼 — _STOP_RE("멈춰"/"중단"/"취소" 등) 텍스트 핸들러와 동일한 취소 절차를
    그대로 수행한다(_CANCEL 등록 + 병렬 job_key 전체 취소 + job_ledger/interrupted_state 정리)."""
    ack()
    ch, tts = _action_ctx(body)
    _CANCEL.add(tts)
    got = generator.cancel_prefix(tts)
    job_ledger.finish_by_thread(tts)
    interrupted_state.clear(tts)
    _disable_buttons(body, "🛑 중단 요청했어요…" if got else
                     "🛑 중단 요청했어요 (이미지 생성 중이면 진행 중인 컷까지만 끝내고 멈춰요).")

# ★2026-07-20: "붙여넣은 대본인지" 최소 검증 — 대사 인용부호가 있거나(따옴표 안 2글자 이상),
# 여러 줄로 구성돼 있거나(3줄 이상, 대본 특유의 줄바꿈 형식), 명시적 각본 마커(씬N/S#/각본/
# 시나리오)가 있어야 "대본처럼 생겼다"고 인정한다. 그냥 길기만 한 방향 설명 요청문(예: "세계관
# 및 1화 사건 흐름까지 잡아서 생성해줘")은 대사도 여러 줄도 없어서 여기 안 걸린다.
_LOOKS_LIKE_PASTED_SCRIPT_RE = re.compile(
    r"[\"“'‘「].{2,}[\"”'’」]|(?:\n[^\n]*){2,}\n|씬\s*\d|S#\s*\d|각본|시나리오"
)

# renamed from _do_storyboard (name collision with the other bot's function of the same name, different behavior)
def sb_do_storyboard(channel, thread_ts, rest, stage=1):
    q = rest.strip()
    work, bible = None, None
    wm = SUB_RE.match(q)
    if wm:
        # 사용자가 안내문 그대로 "<작품>"을 실제 작품명 대신 복붙한 경우까지 작품으로 오인하지
        # 않게(2026-07-13) — 이때도 태그 자체는 걷어내고, 진짜 작품명은 스레드에서 다시 찾는다.
        w0 = wm.group(1).strip()
        if not _looks_like_mention(w0):
            work = w0
        q = wm.group(2).strip()
    msgs = _thread_messages(channel, thread_ts)
    joined = "\n".join(m["content"] for m in msgs)
    if not work:
        work = _work_from_thread(joined, thread_ts)
    if work:
        work = works.resolve(work) or work          # '코니' → 정식 작품명(별칭 해석)
        sh = _sheet()
        if sh:
            try:
                bible = sh.get(work)
            except Exception:
                log.exception("bible load failed")
    epm = re.search(r"(\d+)\s*[화회]", q) or re.search(r"(\d+)\s*[화회]", joined)  # '화'/'회' 둘 다 인정
    target = int(epm.group(1)) if epm else sb_progress_episode(bible, ["대본", "개요"])
    ctm = re.search(r"(\d+)\s*컷", q)                       # 'N컷' → 목표 컷 수(수정지시 숫자와 구분)
    cut_target = int(ctm.group(1)) if ctm else None
    instr = re.sub(r"(\d+)\s*컷", "", re.sub(r"(\d+)\s*[화회]", "", q)).strip()
    script, script_err = _script_for(work, target, bible)
    if script_err:
        # 그 화 첨부 대본 파일이 있는 건 확인했는데 못 읽은 경우 — 페이지 전체로 조용히
        # 대체하면 다른 화 내용이 섞여 들어갈 수 있어(2026-07-16), 폴백하지 않고 재시도를 안내.
        _reply(channel, thread_ts,
              f"{target}화 대본 파일을 읽지 못했어요({script_err}). 잠시 후 다시 시도해주세요.")
        return
    ref_block = (f"\n\n[원본 대본 — 사건·행동·대사 하나도 바꾸지 마라]\n{script}" if script else "")
    prior_plan = _last_assistant_with(msgs, ["[1단계]", "씬 설계안"])
    _CANCEL.discard(thread_ts)

    if stage == 1:
        draft = script
        if not draft:
            # ★2026-07-20 실사용 사고 — "노션에 아무 정보도 없는데 씬 설계를 하고 있다" —
            # 대본이 없으면(script가 비었으면) 길이만 20자 넘으면 그 메시지 자체를 "붙여넣은
            # 대본"으로 오인해서 씬 설계에 그대로 태웠다. "라이트한 쪽으로 가고 싶어. 무겁지
            # 않게 디벨롭해서 세계관 및 1화 사건 흐름까지 잡아서 생성해줘." 같은, 대본이 아니라
            # 기획 방향을 설명하는 요청문(20자 넘는 건 흔함)까지 "대본"으로 잘못 인식해 등장인물
            # 이름까지 통째로 지어낸 씬 설계를 만들어버렸다 — 대본 유무와 무관하게 항상 자유
            # 요청문이 들어올 수 있는 이 길만 최소한의 "이거 진짜 대본처럼 생겼나" 검증 없이
            # 그대로 믿었던 게 근본 원인. 실제 대본(대사 인용부호나 여러 줄 구성)처럼 보일 때만
            # "붙여넣은 대본"으로 인정하고, 아니면 대본 없음 안내로 보낸다.
            if len(q) >= 20 and _LOOKS_LIKE_PASTED_SCRIPT_RE.search(q):
                draft = q
            else:
                prior = [m["content"] for m in msgs if m["role"] == "assistant"
                         and "[1단계]" not in m["content"] and "[2단계]" not in m["content"]]
                prior_candidate = prior[-1] if prior else ""
                draft = prior_candidate if _LOOKS_LIKE_PASTED_SCRIPT_RE.search(prior_candidate) else ""
        if not draft and not prior_plan:
            _reply(channel, thread_ts,
                   "대본을 못 찾았어요 — 이 요청은 실제 대본이 아니라 방향 설명처럼 보여요. "
                   "먼저 대본/개요부터 만들어주세요:\n"
                   "• `[생성] <작품> N화 대본`처럼 co-writer로 대본을 먼저 쓰거나\n"
                   "• `[스토리보드] <작품> 3화` (시트에 저장된 대본 자동 사용)\n"
                   "• 또는 `[스토리보드] <작품>` 뒤에 완성된 대본을 직접 붙여넣기")
            return
        base_note = (f"{target}화 대본으로 " if script else "") + "씬 설계 중이에요…" + (f" (목표 {cut_target}컷)" if cut_target else "")
        ph = _thinking(channel, thread_ts, base_note, stop_button=True)
        jid = job_ledger.start_job("plan", channel, thread_ts, rest)
        try:
            if prior_plan and instr:
                ans = _with_heartbeat(channel, ph, base_note, lambda: generator.complete(
                    prompts.storyboard_plan_system(bible, target_episode=target, cut_target=cut_target),
                    _convo_text(msgs) + ref_block
                    + f"\n\n(이번은 씬 설계안 수정 요청이다: '{instr}'. 바뀐 씬만, 맨 위 '바꾼 점:' 한 줄. 전체 재출력 금지.)",
                    job_key=thread_ts))
            else:
                ans = _with_heartbeat(channel, ph, base_note, lambda: generator.complete(
                    prompts.storyboard_plan_system(bible, target_episode=target, cut_target=cut_target),
                    prompts.storyboard_plan_user(draft), job_key=thread_ts))
        except Exception as e:
            log.exception("plan failed")
            _post_chunks(channel, thread_ts, "씬 설계 중 오류가 났어요. 잠시 후 다시 시도해주세요.", replace_ts=ph); return
        finally:
            job_ledger.finish_job(jid)
        if ans in (generator.CANCEL_MSG, generator.TIMEOUT_MSG):
            _post_chunks(channel, thread_ts, ans, replace_ts=ph); return
        answer = SB_BADGE_PLAN + (ans or "").strip()
        if work and target:
            conti_state.set_episode(thread_ts, work, target)   # 화 번호 바뀜 감지용으로 1단계 완료 시점부터 기록
    else:  # stage 2 — 상세 콘티
        # ★2026-07-16 "상세 콘티 1씬만 다시 만들고 싶어"가 씬 단위 수정 대신 전체 재생성으로
        # 샜던 버그 — _thread_conti만 쓰면 이 스레드에 콘티 "본문"이 직접 안 붙어있을 때(요즘
        # 기본: 노션에 저장 후 짧은 안내만 남김) prior_conti가 항상 빈 문자열이 되고, 그러면
        # 아래 "prior_conti and instr and not full_rewrite" 조건이 거짓이 돼 씬 단위 수정
        # 감지(_scene_num_from_instr 등) 자체가 통째로 건너뛰어진다. _thread_or_saved_conti로
        # 바꿔 노션에 있는 실제 본문까지 폴백으로 가져오게 한다(announce=False — 사용자에게
        # 다시 보여줄 필요 없이 수정 판단에만 쓸 텍스트가 필요할 뿐).
        prior_conti = _thread_or_saved_conti(channel, thread_ts, msgs, work, target, announce=False)
        # ★2026-07-15: 원래는 prior_plan(1단계 씬 설계) 없으면 무조건 반려했는데, 외부(노션/로컬)
        # 에서 이미 완성된 콘티를 방금 이 스레드로 불러온 경우(_maybe_conti_rewrite_request)는
        # 1단계를 거친 적이 없어도 prior_conti만으로 씬별/전체 수정이 가능하므로 둘 다 없을 때만 반려.
        if not prior_plan and not prior_conti:
            _reply(channel, thread_ts,
                  "아직 이 스레드에 콘티가 없어요. 어떤 작품·화 작업인지 알려주시면 시작할게요 — "
                  "예: `[스토리보드] <작품명> 3화`. (다른 스레드에서 이미 만든 콘티가 있다면, "
                  "그 스레드에서 이어서 말씀해주세요 — 콘티는 스레드별로 따로 관리돼요.)")
            return
        # ★2026-07-15: "그냥 아예 새로 쓰고 싶어"/"완전히 새로 작성해줘" 같은 지시는 "이 콘티를
        # 수정해줘"가 아니라 "옛 콘티는 버리고 처음부터 다시 써줘"라는 뜻인데, prior_conti+instr가
        # 둘 다 있으면 그냥 "전체 수정" 취급돼서 옛(구버전 포맷) 콘티를 "[현재 상세 콘티]"로
        # LLM에 통째로 넘기고 "이걸 수정해서 다시 출력하라"고 시켰다 — 그러면 LLM이 수정할 구체적
        # 내용이 없으니(막연한 지시) 옛 콘티를 거의 그대로(=옛 포맷 그대로, 구도헤더/등장/장소
        # 선언 등 최신 규칙 없이) 재출력해버리는 실측 버그가 있었다("3화 상세 콘티 다시 쓰고 싶어"
        # → 콘티 fetch → "그냥 아예 새로 쓰고 싶어" → 결과가 옛 포맷 그대로). "완전히 새로" 의도를
        # 감지하면 prior_conti를 수정 대상으로 삼지 않고, 그 콘티의 씬 헤더(번호·시간·제목)만
        # 뼈대로 재사용해 각 씬을 처음부터 다시 쓰게 한다(내용은 새로, 최신 출력 규칙 그대로 적용).
        _FULL_REWRITE_RE = re.compile(r"아예\s*새로|완전히\s*새로|처음부터\s*다시|새로\s*(작성|써)|통째로\s*다시")
        full_rewrite = bool(instr) and bool(_FULL_REWRITE_RE.search(instr))
        # 수정 지시가 씬 하나만 가리키면(번호 명시든, 내용으로 유추되든) 그 씬만 다시 써서
        # 통째로 교체 — 다른 씬까지 LLM이 따라 고쳐버리는(전체 재생성의) 위험을 없앤다(2026-07-13).
        # ★2026-07-20 실사용 사고 — "씬3에서 짝꿍이 되는 장면은 이전 씬이랑 연결이 안 되는 것
        # 같아. 1화에서 이 둘의 관계성을 더 보여줬으면 좋겠어."처럼 지시 안에 "씬3"이 명시돼 있어도
        # 실제 요구는 씬3 하나가 아니라 앞선 씬들(관계성 빌드업)까지 걸친 수정이었다. 그런데
        # _scene_num_from_instr는 순수 정규식이라 "씬3"이 보이는 순간 무조건 그 번호 하나로
        # 확정해버려서, 여러 씬에 걸친 애매한 경우를 가려내는 LLM 판단(_guess_scene_num, "여러
        # 씬에 걸치면 -1")이 아예 호출되지도 못했다. 지시문에 "이전 씬"/"관계성"/"연결" 같은
        # 교차-씬 신호가 있으면 정규식 지름길을 건너뛰고 곧장 LLM 판단으로 넘긴다.
        _CROSS_SCENE_HINT_RE = re.compile(
            r"이전\s*씬|다른\s*씬|앞\s*씬|여러\s*씬|씬\s*(들|간)|관계성|연결이?\s*안|일관성|전체적으로")
        scene_num = None
        if prior_conti and instr and not full_rewrite:
            if not _CROSS_SCENE_HINT_RE.search(instr):
                scene_num = _scene_num_from_instr(instr)
            if scene_num is None:
                scene_num = _guess_scene_num(prior_conti, instr)
        target_scene = next((s for s in _split_scenes(prior_conti) if s[0] == scene_num), None) if scene_num else None
        recon = ("위 대화에는 '씬 설계안' 전체본과 이후 부분 수정('바꾼 점' + 바뀐 씬)들이 섞여 있다. "
                 "이 둘을 합쳐 최종 씬 구성(씬 수·순서·시간)을 스스로 재구성하라(수정된 씬은 최신본, 안 바뀐 씬은 최초본).")
        treat_as_revision = prior_conti and instr and not full_rewrite
        # ★2026-07-15: 화 전체를 새로 쓰는 경우(수정 아님)만, 1단계 씬 설계안을 씬 단위로 쪼개
        # 병렬 호출한다 — 한 번의 호출로 화 전체(20~40컷)를 통짜로 뽑으면(특히 구도헤더 등
        # v2.0 규칙 추가 이후 비트당 써야 할 내용이 늘어) 5분 타임아웃에 자주 걸린다는 지적
        # (사용자, 2026-07-15). 씬별 수정(target_scene)·전체 수정(treat_as_revision)은 원래도
        # 다루는 범위가 한정적이라 그대로 한 번에 호출한다.
        plan_scenes = _parse_plan_scenes(prior_plan) if not target_scene and not treat_as_revision else []
        if not plan_scenes and full_rewrite and prior_conti:
            # 이 스레드에 1단계 씬 설계안이 없어도(예: 콘티를 외부에서 바로 불러온 경우), 기존
            # 콘티의 씬 헤더(번호·시간·제목)에서 뼈대만 재사용해 씬 단위 완전 재작성을 진행한다.
            def _plan_line_from_hdr(num, hdr):
                m = re.search(r"(\d+(?:\.\d+)?)\s*초", hdr)
                secs = m.group(1) if m else "?"
                title = hdr.split("·")[-1].strip() if "·" in hdr else hdr
                return f"{num}. `{secs}초 · {title}`"
            plan_scenes = [(num, _plan_line_from_hdr(num, hdr)) for num, hdr, _b in _split_scenes(prior_conti)]
        plan_text_for_prompt = prior_plan or "\n".join(line for _n, line in plan_scenes)
        sys_prompt = prompts.storyboard_system(bible, target_episode=target, cut_target=cut_target,
                                               known_places=_known_places(work) or None,
                                               known_costumes=_known_costumes(work) or None)
        if plan_scenes and len(plan_scenes) >= 2:
            n_total = len(plan_scenes)
            note = lambda done: (f"상세 콘티(GPT 이미지용) 씬 단위로 나눠 만드는 중이에요… ({done}/{n_total}씬)"
                                  + (f" (목표 {cut_target}컷)" if cut_target else ""))
            ph = _thinking(channel, thread_ts, note(0), stop_button=True)

            def _gen_scene(num, line):
                user = (f"[씬 설계안 — 화 전체 목록(참고용, 다른 씬은 이미 별도로 처리 중이니 이 씬에만 집중)]\n{plan_text_for_prompt}\n\n"
                        + ref_block
                        + f"\n\n(지금은 화 전체가 아니라 **이 씬 하나만** 상세 콘티로 써라: '{line}'. "
                        f"반드시 '■ 씬{num} · N초 · 제목' 헤더로 시작해 이 씬의 샷 콘티만 출력하고 다른 씬은 "
                        "언급하지 마라. 대본의 사건·행동·대사는 하나도 바꾸지 마라. "
                        "★의상 일관성: 위 씬 설계안 목록에서 이 씬 항목 제목 뒤에 `(장소/인물)` 표기가 "
                        "**없다면**, 그건 1단계에서 이미 '직전 씬과 장소·인물이 동일하다'고 판단했다는 "
                        "뜻이다 — 이 경우 이 씬은 직전 씬에서 곧장 이어지는 상황일 가능성이 높으니, "
                        "등장 라인의 의상은 옷을 갈아입는 사건이 없는 한 직전 씬과 같은 맥락으로 "
                        "일관되게 서술하라(예: 두 씬 다 '아침에 막 깬 상황'이면 둘 다 잠옷/헝클어진 "
                        "머리 묘사를 써라 — 한쪽만 사복이나 단정한 차림으로 바꾸지 마라). 다른 씬의 "
                        "실제 문구는 볼 수 없으니 상황 논리로 추론해 합리적으로 일관되게 써라.)")
                return generator.complete(sys_prompt, user, timeout=config.AGENT_TIMEOUT,
                                          job_key=f"{thread_ts}:씬{num}")

            jid = job_ledger.start_job("conti", channel, thread_ts, rest)
            results, done = {}, 0
            try:
                with cf.ThreadPoolExecutor(max_workers=min(config.CONTI_SCENE_WORKERS, n_total)) as ex:
                    futs = {ex.submit(_gen_scene, num, line): num for num, line in plan_scenes}
                    for fut in cf.as_completed(futs):
                        num = futs[fut]
                        try:
                            results[num] = fut.result()
                        except Exception as e:
                            log.exception(f"씬{num} 상세 콘티 생성 실패")
                            results[num] = f"⚠ 씬{num} 생성에 실패했어요. 이 씬만 다시 만들어달라고 요청해주세요."
                        done += 1
                        _update_note(channel, ph, note(done))
            except Exception as e:
                log.exception("conti(씬 병렬) failed")
                job_ledger.finish_job(jid)
                _post_chunks(channel, thread_ts, "상세 콘티 생성 중 오류가 났어요. 잠시 후 다시 시도해주세요.", replace_ts=ph); return
            job_ledger.finish_job(jid)
            cancelled = next((r for r in results.values() if r in (generator.CANCEL_MSG, generator.TIMEOUT_MSG)), None)
            if cancelled:
                _post_chunks(channel, thread_ts, cancelled, replace_ts=ph); return
            _sync_costume_across_scenes(plan_scenes, results)
            ans = "\n\n".join((results.get(num) or f"⚠ 씬{num} 없음").strip() for num, _ in plan_scenes)
        else:
            base_note2 = ((f"씬{scene_num}만 " if target_scene else "") + "상세 콘티(GPT 이미지용) 만드는 중이에요… (보통 1~5분, 길면 최대 10분)"
                           + (f" (목표 {cut_target}컷)" if cut_target else ""))
            ph = _thinking(channel, thread_ts, base_note2, stop_button=True)
            if target_scene:
                num, hdr, body = target_scene
                user = (f"[해당 씬의 현재 콘티]\n■ {hdr}\n{body}\n\n" + ref_block
                        + f"\n\n(이 씬 하나만 이 수정 지시를 반영해 다시 써라: '{instr}'. "
                        "같은 헤더 형식(■ 씬N · N초 · 제목)으로 시작해 이 씬의 샷 콘티만 출력하라. "
                        "다른 씬은 언급도 수정도 하지 마라. 대본의 사건·행동·대사는 하나도 바꾸지 마라.)")
            elif treat_as_revision:
                # 콘티는 스레드 텍스트로 전체를 다시 올리므로 수정도 전체 콘티를 다시 출력 → [이미지]가 항상 전체를 읽음
                user = (f"[현재 상세 콘티]\n{prior_conti}\n\n" + ref_block
                        + f"\n\n(위 [현재 상세 콘티]에 이 수정을 반영해 **전체 콘티를 다시** 출력하라: '{instr}'. "
                        "맨 위에 '바꾼 점: …' 한 줄 요약 후, **씬별 헤더(■ 씬N · N초 · 제목)로 나눠서** 전체 콘티. 대본의 사건·행동·대사는 하나도 바꾸지 마라.)")
            else:
                # 씬 설계안에서 씬 목록을 못 뽑았을 때(옛 형식 등)만 예전처럼 통짜로 한 번에 생성
                user = (_convo_text(msgs) + ref_block
                        + f"\n\n({recon} 그 최종 구성을 지켜, [원본 대본]을 영상문법가이드 정본 예시처럼 샷 단위 상세 콘티로 전개하라. "
                        "★반드시 **씬별로 헤더(■ 씬N · N초 · 제목)를 달아 나눠서** 출력하고, 각 씬 아래에 그 씬의 샷 콘티를 쓴다. 대본의 사건·행동·대사는 하나도 바꾸지 마라.)")
            jid = job_ledger.start_job("conti", channel, thread_ts, rest)
            try:
                ans = _with_heartbeat(channel, ph, base_note2, lambda: generator.complete(
                    sys_prompt, user, timeout=config.AGENT_TIMEOUT, job_key=thread_ts))
            except Exception as e:
                log.exception("conti failed")
                _post_chunks(channel, thread_ts, "상세 콘티 생성 중 오류가 났어요. 잠시 후 다시 시도해주세요.", replace_ts=ph); return
            finally:
                job_ledger.finish_job(jid)
            if ans in (generator.CANCEL_MSG, generator.TIMEOUT_MSG):
                _post_chunks(channel, thread_ts, ans, replace_ts=ph); return
        if target_scene:
            ans = _replace_scene_block(prior_conti, scene_num, ans)
        # 상세 콘티는 길어도 스레드 텍스트로 그대로 올림(txt 파일 생성 없음, 2026-07-15).
        # 게시물엔 "바뀐 점: ..." 같은 수정 요약 줄은 빼고 실제 씬 내용(씬1, 씬2, ...)만 남긴다.
        conti_body = _strip_conti_preamble(ans)
        _upload_conti(channel, thread_ts, work, conti_body, replace_ts=ph, episode=target)
        conti_state.set_episode(thread_ts, work, target)   # 이 스레드 콘티가 몇 화인지 기록
        # (F8) 완성 직후 메시지가 배지+경고+버튼 3개로 쪼개지지 않게 — 미등록 경고가 있으면
        # 그 메시지 하나에 [통과→이미지]/[재생성] 버튼까지 같이 붙이고, 없으면 버튼만 따로.
        if not _warn_unregistered_elements(channel, thread_ts, work, conti_body, extra_blocks=_sb_buttons(2)):
            _post_buttons(channel, thread_ts, 2)          # [통과→이미지] [재생성]
        return

    _post_chunks(channel, thread_ts, answer or "(빈 응답)", replace_ts=ph)
    _post_buttons(channel, thread_ts, 1)              # [통과→상세 콘티] [재생성]

def _render_cuts_tracked(kind, orig_rest, *args, **kwargs):
    """_render_cuts를 job_ledger로 감싼다 — 재시작으로 도중에 죽으면 다음 기동 때 자동 재개.
    orig_rest: [이미지]/[스틸컷] 원래 명령 텍스트(재개 시 그대로 재실행하는 데 씀)."""
    channel, thread_ts = args[0], args[1]
    jid = job_ledger.start_job(kind, channel, thread_ts, orig_rest)
    try:
        return _render_cuts(*args, kind=kind, orig_rest=orig_rest, **kwargs)
    finally:
        job_ledger.finish_job(jid)

_LAST_RENDER: dict[str, dict] = {}   # thread_ts -> 마지막 렌더 상태(실패 컷만 재시도용, C3 2026-07-13)

_PENDING_CUT_CONFIRM: dict[str, dict] = {}   # thread_ts -> 렌더 상태(컷 수 사전 확인용, C4 2026-07-13)

_CUT_CONFIRM_THRESHOLD = 15   # 목표 컷 수를 안 정했는데 이 이상 나오면 진행 전 확인받는다

_GRID_SPLIT_THRESHOLD = 40    # (2026-07-14) 컷이 이 이상이면 그리드 하나로 뭉치지 않고 나눠서 순서대로 올림

_GRID_SPLIT_N = 3

def _cut_confirm_blocks(n):
    text = f"⚠️ 컷 수를 안 정해주셔서 {n}컷이 나올 예정이에요(컷당 비용 발생). 진행할까요?"
    return text, [{
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": f"✅ {n}컷 생성"},
             "style": "primary", "action_id": "sb_confirm_cuts"},
            {"type": "button", "text": {"type": "plain_text", "text": "❌ 취소"},
             "action_id": "sb_cancel_cuts"},
        ],
    }]

_SAFETY_FAIL_RE = re.compile(r"safety|moderation|flagged|blocked|rejected", re.I)

def _classify_fail_reason(msg: str) -> str:
    """(F4) 컷 실패 사유를 안전필터 거부 vs 그 외 오류로 구분 — openrouter_image.generate()가
    HTTP 400과 함께 원본 응답 body를 그대로 예외 메시지에 담아 던지므로 그 안의 영문 키워드로 판별."""
    return "세이프티 필터 거부" if _SAFETY_FAIL_RE.search(msg or "") else "생성 오류"

_FAIL_DETAIL_MAX_LINES = 5

_FAIL_DETAIL_MSG_LEN = 100

def _build_fail_detail_lines(fail_reasons: dict, shots: list) -> str:
    """★2026-07-15 "여기서 실패 사유를 알고싶어" — 카테고리 그룹핑("생성 오류: 컷 4")만으로는
    그 컷에서 실제로 무슨 에러가 났는지 알 수 없다는 지적. 컷별 원본 예외 메시지를 짧게 잘라
    한 줄씩 덧붙인다. 컷이 많으면(수십 컷 동시 실패) 메시지가 벽이 되는 걸 막기 위해
    _FAIL_DETAIL_MAX_LINES개까지만 보여주고 나머지는 로그 참고 안내로 대체."""
    items = sorted(fail_reasons.items(), key=lambda kv: shots[kv[0]].get("n") or (kv[0] + 1))
    lines = []
    for i, msg in items[:_FAIL_DETAIL_MAX_LINES]:
        num = shots[i].get("n") or (i + 1)
        one_line = " ".join(str(msg).split())[:_FAIL_DETAIL_MSG_LEN]
        lines.append(f"· 컷{num}: {one_line}")
    remaining = len(items) - _FAIL_DETAIL_MAX_LINES
    if remaining > 0:
        lines.append(f"…외 {remaining}개 컷 (자세한 사유는 서버 로그 참고)")
    return "\n".join(lines)

_VIDEO_PRIVACY_FAIL_RE = re.compile(
    r"InputImageSensitiveContentDetected|PrivacyInformation|real person", re.I)

_VIDEO_TIMEOUT_FAIL_RE = re.compile(r"폴링 시간초과|timed?\s*out", re.I)

_VIDEO_SAFETY_FAIL_RE = re.compile(r"safety|moderation|sensitive|flagged|blocked|rejected", re.I)

def _classify_video_fail_reason(msg: str) -> str:
    """영상화(hf_video.generate) 실패 사유를 사람이 읽을 수 있는 문구로 분류.
    _classify_fail_reason(이미지용)과 같은 목적이나, 영상 쪽은 openrouter_video.py가 던지는
    예외 문구가 달라(실존 인물 감지·폴링 시간초과 등) 별도 정규식/분류로 둔다."""
    m = msg or ""
    if _VIDEO_PRIVACY_FAIL_RE.search(m):
        return "입력 이미지가 실존 인물처럼 보인다는 안전필터에 걸림"
    if _VIDEO_TIMEOUT_FAIL_RE.search(m):
        return "응답 시간 초과"
    if _VIDEO_SAFETY_FAIL_RE.search(m):
        return "생성된 콘텐츠(대사/음성 등)가 안전필터에 걸림"
    return "생성 오류"

_CONTI_CHUNK_PARAS = 22   # 실무자 실측(20~25문단씩 지피티에 넣음)과 맞춤

def _chunk_conti(text, size=_CONTI_CHUNK_PARAS):
    """(2026-07-14) 콘티를 문단 size개 안팎으로 묶어 나눈다 — 콘티 전체를 한 번에 LLM에 넣으면
    샷 분해 퀄리티가 떨어진다는 실무자 피드백에 따라, 구간 단위로 나눠 각각 분해한다.
    ★씬(■ 씬N ...) 단위로만 나눈다 — 문단 수로 기계적으로 자르면 같은 씬/같은 장소 안에서
    뚝 끊겨 그 구간 담당 LLM이 앞뒤 맥락(장소·상황) 없이 컷을 만들게 된다(실무자 지적).
    한 씬이 size보다 길어도 그 씬을 쪼개지 않고 통째로 한 청크에 넣는다."""
    scenes = _split_scenes(text)
    if not scenes:   # 씬 헤더 없는 옛 콘티 폴백 — 문단 단위로만 나눔
        paras = [p for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
        if len(paras) <= size:
            return [text]
        return ["\n\n".join(paras[i:i + size]) for i in range(0, len(paras), size)]
    chunks, cur, cur_n = [], [], 0
    for num, hdr, body in scenes:
        scene_text = f"■ {hdr}\n{body}"
        n = len([p for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]) or 1
        if cur and cur_n + n > size:   # 새 씬을 더하면 넘칠 때만 청크를 끊고, 씬 자체는 절대 안 쪼갠다
            chunks.append("\n\n".join(cur))
            cur, cur_n = [], 0
        cur.append(scene_text)
        cur_n += n
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks or [text]

_LAST_RENDER_FAIL_REASON: dict[str, str] = {}   # thread_ts -> 가장 최근 _render_cuts 완전 실패 사유(구체적 원문)

def _set_render_fail_reason(thread_ts, reason: str) -> None:
    """★2026-07-15 "실패하면 이유를 찾으라니깐?" — _render_cuts가 완전히 실패(컷 0개)하면
    사유가 그 시점에 Slack엔 바로 보이지만, 호출자(자동주행 등)는 (grid_png, cuts) 성공
    반환값 외엔 사유를 못 받아 나중에(최종 요약 등) "위 오류 메시지 참고"로 뭉뚱그릴 수밖에
    없었다. 실패 시점의 실제 사유 문자열을 여기 남겨 호출자가 나중에 읽어갈 수 있게 한다."""
    _LAST_RENDER_FAIL_REASON[thread_ts] = reason

def _render_cuts(channel, thread_ts, work, bible, source_text, *,
                 target=None, title="스토리보드 그리드", filename=None, cols=None,
                 aspect_ratio=None, style_suffix=None, no_text=False,
                 retry_shots=None, retry_results=None, retry_cost=0.0,
                 kind=None, orig_rest=None, skip_confirm=False, group_bounds=None,
                 cut_filter: "set[int] | None" = None, auto_cut_judgment: bool = False):
    """source_text(콘티 전체 또는 한 씬)를 컷 분해 → 인물 참조로 생성 → 그리드 업로드.
    retry_shots/retry_results가 주어지면(실패 컷만 재시도, C3): 샷 분해를 새로 안 하고 그
    shots를 그대로 쓰며, retry_results 중 실패(None)한 것만 다시 생성해 성공분과 합친다.
    target을 안 정했는데 컷이 많이 나오면(C4) 진행 전에 확인 카드를 띄운다(skip_confirm=True면
    건너뜀 — 확인 버튼을 눌러 재호출할 때 씀).
    group_bounds: [(start,end), ...] — 같은 씬(청크) 안 컷들의 인덱스 범위. 주어지면 그 범위
    안에서는 순차 생성하며 직전 컷 이미지를 다음 컷 참조에 추가로 넣어 이어지는 느낌을 준다
    (2026-07-14, 실무자 피드백: 컷이 따로 노는 느낌). 없으면(재시도 등) 컷마다 독립 생성(기존 방식).
    cut_filter: {컷번호, ...} — 주어지면 3단계 샷분해는 source_text 전체로 그대로 돌려 컷을
    1..N으로 온전히 번호매긴 뒤, 그 결과 중 cut_filter에 있는 번호만 남기고 나머지는 이미지
    생성 루프 전에 버린다(2026-07-15, "긴 씬 전체 대신 컷 몇 개만 뽑고 싶다"). None이면(기존
    모든 호출자) 완전히 기존과 동일하게 동작한다."""
    img = hf if config.IMAGE_BACKEND == "higgsfield" else oi   # 생성 백엔드(참조 색인은 oi 그대로)
    if not img.available():
        key = "HIGGSFIELD_API_KEY" if config.IMAGE_BACKEND == "higgsfield" else "OPENROUTER_API_KEY"  # 관리자용 설정 키, 사용자 메시지에는 노출하지 않음
        _reply(channel, thread_ts, "이미지 생성 기능이 아직 설정되지 않았어요. 봇 관리자에게 문의해주세요 (API 키 설정 필요)."); return
    ok, msg = grid.available()
    if not ok:
        _reply(channel, thread_ts, msg); return

    _CANCEL.discard(thread_ts)
    mood_ref_cost = 0.0   # retry 경로(샷 재분해 없음)에선 무드 참조를 새로 안 만듦 — 0 유지
    if retry_shots is not None:
        shots, results = retry_shots, list(retry_results)
        note = ("컷(샷) 리스트로 나누는 중이에요…" if all(r is None for r in results)
                else "실패한 컷만 다시 만드는 중이에요…")   # (C4) 확인-후-생성 vs (C3) 실패만 재시도 구분
        ph = _thinking(channel, thread_ts, f"{title}: {note}", stop_button=True)
    else:
        # ★2026-07-15: "씬2 9컷만 재생성하고 싶어"처럼 cut_filter로 특정 컷만 뽑을 때도, 컷 번호가
        # 전체 씬 기준으로 일관되게 매겨지도록 3단계 분해는 항상 씬 전체(target=n_beats) 기준으로
        # 돈다 — 그래서 진행 메시지가 "(목표 15컷)"으로 떠서 마치 15장을 다 만드는 것처럼 헷갈리게
        # 보였다(실사용자 지적, 실제로는 컷9 하나만 생성됨). cut_filter가 있으면 실제로 뽑을 컷과
        # 그 이유(전체 분해 기준)를 같이 보여준다.
        if cut_filter is not None and target:
            note_suffix = (f" (전체 씬 {target}컷 기준으로 분해한 뒤 컷"
                          f"{','.join(str(c) for c in sorted(cut_filter))}만 추출)")
        else:
            note_suffix = f" (목표 {target}컷)" if target else ""
        ph = _thinking(channel, thread_ts, f"{title}: 컷(샷) 리스트로 나누는 중이에요…{note_suffix}", stop_button=True)
        try:
            # 등록된 장소/소품/의상(엘리먼트) 이름 — 컷마다 그 씬에 해당할 때만 "places"/"props"에
            # 쓰게 모델에 알려줌. 의상은 별도 JSON 필드 없이(shot_refs가 caption/prompt 텍스트
            # 안의 등록 이름을 타입 무관하게 스캔) 정식 이름을 캡션/프롬프트에서 그대로 유지하도록
            # 알려주기만 하면 됨(2026-07-15, "의상도 레지스트리가 필요함").
            elems = oi.load_elements(work)
            places = [e["display"] for e in elems if e.get("type") == "place"]
            props = [e["display"] for e in elems if e.get("type") == "prop"]
            # ★2026-07-15: 참조 이미지 없는 의상은 이름만 등록해선 컷마다 옷이 달라 보이는 문제
            # (사용자 지적: "의상이 컷마다 다르게 나왔어") — description(등록 시 같이 적어둔 옷차림
            # 묘사)이 있으면 이름과 함께 넘겨서, 매 컷 prompt에 그 구체 묘사를 반복 반영하게 한다.
            # ★2026-07-15b: 단, 실제 참조 이미지가 있는 의상엔 description을 넘기면 안 된다 —
            # 모델이 "설명을 반복하라"는 지시를 따르다 정식 이름 자체를 프롬프트에서 빼버려서
            # (실측: "잠옷-A" 리터럴이 사라지고 "comfortable training wear"만 남음) shot_refs의
            # 텍스트 매칭이 실패해 실제 등록된 참조 이미지가 하나도 안 붙는 사고로 이어졌다.
            # 참조 이미지가 있으면 이름만 넘겨 그 이름이 프롬프트에 그대로 남게 한다(기존 동작).
            costumes = [
                {"name": e["display"], "description": e.get("description")}
                if not oi._element_data_url(work, e) else e["display"]
                for e in elems if e.get("type") == "costume"
            ]
            # (2026-07-14) target 없이(=컷수 미지정 전체 회차) 콘티가 길면 청크로 쪼개 분해 —
            # 실무자 피드백: 콘티 전체를 한 번에 넣으면 이미지 생성 퀄리티가 떨어짐(20~25문단씩 끊어야 함).
            chunks = _chunk_conti(source_text) if target is None else [source_text]
            shots = []
            group_bounds = []   # 청크(=씬 묶음) 하나 = 컷 체이닝 그룹 하나
            for idx, chunk_text in enumerate(chunks):
                if len(chunks) > 1:
                    _update_note(channel, ph,
                                 f"{title}: 컷(샷) 리스트로 나누는 중이에요… ({idx + 1}/{len(chunks)} 구간)")
                # 샷 분해는 OpenRouter chat(HTTP) — agent(claude CLI)의 느림·동시호출 충돌 회피
                raw = oi.chat(prompts.storyboard_shots_system(bible, target=target if len(chunks) == 1 else None,
                                                               places=places or None, props=props or None,
                                                               costumes=costumes or None,
                                                               force_merge_judgment=auto_cut_judgment),
                              prompts.storyboard_shots_user(chunk_text, chunk=len(chunks) > 1), timeout=300)
                part = [s for s in _parse_json_array(raw) if isinstance(s, dict) and s.get("prompt")]
                # ★2026-07-15: '등장:' 줄의 인물-의상 매핑을 코드로 이 청크의 모든 컷에 직접
                # 붙인다(LLM이 그 비트에서 의상 라벨을 다시 언급했는지와 무관하게) — shot_refs가
                # 이미 "elements" 필드를 타입 무관 참조 후보로 스캔하므로 그대로 재사용.
                costume_map = _scene_costume_map(chunk_text)
                if costume_map:
                    for s in part:
                        labels = {costume_map[c] for c in (s.get("characters") or []) if c in costume_map}
                        if labels:
                            s["elements"] = list(dict.fromkeys(list(s.get("elements") or []) + list(labels)))
                # ★2026-07-15: 장소도 의상과 같은 구조적 문제(씬 헤더 아래 '장소:' 줄에 한 번만
                # 선언되고 개별 비트는 안 반복) — 이 청크가 씬 하나뿐이면(장소 선언이 정확히
                # 1번) 그 장소를 이 청크의 모든 컷에 강제로 붙인다. 청크가 씬 여러 개를 걸치면
                # (장소 선언이 0/2번 이상) 어느 씬 소속인지 판별 불가라 강제하지 않고 기존
                # LLM 판단(shot["places"])에 맡긴다(안전 폴백).
                scene_place = _scene_single_line(chunk_text, _PLACE_LINE_RE)
                if scene_place:
                    for s in part:
                        if scene_place not in (s.get("places") or []):
                            s["places"] = list(s.get("places") or []) + [scene_place]
                # ★2026-07-16: 소품도 장소와 같은 구조적 문제(씬 헤더 아래 '소품:' 줄에 한 번만
                # 선언되고 개별 비트는 안 반복) — 단, 소품은 한 씬에 여러 개 선언될 수 있어(장소는
                # 씬당 값 하나) "정확히 1번"이 아니라 "이 청크가 씬 하나뿐인가"를 기준으로 판별
                # (_scene_multi_value)한다. 씬 하나짜리 청크면 선언된 소품 전부를 그 청크의 모든
                # 컷에 강제로 붙인다(장소와 동일한 안전망 취지 — LLM이 개별 비트에서 소품을 다시
                # 언급 안 해도 참조가 붙게).
                scene_props = _scene_multi_value(chunk_text, _PROP_LINE_RE)
                if scene_props:
                    for s in part:
                        merged = list(s.get("props") or [])
                        for p_name in scene_props:
                            if p_name not in merged:
                                merged.append(p_name)
                        s["props"] = merged
                # ★2026-07-15: 무드/조명은 참조 이미지가 아니라 텍스트 지시라 "elements"로 강제
                # 못 붙인다 — 대신 이 청크가 씬 하나뿐일 때, 그 씬의 '무드/조명:' 선언을 모든
                # 컷의 prompt 끝에 직접 덧붙여 LLM이 그 컷에서 깜빡했어도 최종 프롬프트에
                # 반드시 남게 한다(사용자 요청: "무드/조명 이걸 무조건 참조하게 고쳐").
                scene_mood = _scene_single_line(chunk_text, _MOOD_LINE_RE)
                if scene_mood:
                    for s in part:
                        p = (s.get("prompt") or "").rstrip()
                        if scene_mood not in p:
                            s["prompt"] = f"{p} Lighting/mood/atmosphere: {scene_mood}."
                    # ★2026-07-16: 무드/조명을 텍스트 지시 외에 참조 이미지로도 전달 — 청크(=씬 묶음)
                    # 하나당 딱 1번만 생성해(컷마다 만들면 비용이 컷 수만큼 곱해짐) 이 청크의
                    # 모든 컷에 같은 참조를 재사용한다. 실패해도 스틸컷 생성 자체를 막으면 안
                    # 되는 nice-to-have라 예외는 로그만 남기고 그냥 넘어간다.
                    try:
                        mood_png, mood_cost = oi.generate_mood_reference(
                            scene_mood, style_suffix=style_suffix, aspect_ratio=aspect_ratio)
                        mood_ref_cost += mood_cost
                        mood_ref_url = oi.png_data_url(mood_png)
                        for s in part:
                            s["_mood_ref_url"] = mood_ref_url
                    except Exception:
                        log.exception("무드 참조 이미지 생성 실패 — 텍스트 지시만 남기고 계속")
                start = len(shots)
                shots.extend(part)
                if part:
                    group_bounds.append((start, len(shots)))
            for i, s in enumerate(shots, 1):
                s["n"] = i
            if cut_filter is not None:
                # 필터는 "전체 씬을 1..N으로 온전히 번호매긴 뒤"에만 적용 — 3단계 분해 자체는
                # source_text 전체로 이미 끝난 상태(위에서). 여기서부터 선택 안 된 컷은 이미지
                # 생성(비용 발생 지점)에 아예 안 들어간다.
                valid_n = {s["n"] for s in shots}
                missing = sorted(cut_filter - valid_n)
                if missing:
                    _post_chunks(channel, thread_ts,
                                 f"이 씬엔 컷{','.join(map(str, missing))}이 없어요 — 이 씬은 컷 1~{len(shots)}"
                                 f"({len(shots)}개)까지 있어요.", replace_ts=ph)
                    return
                shots = [s for s in shots if s["n"] in cut_filter]
                # 필터로 골라낸 컷들은 원래 씬 안에서 서로 안 붙어있을 수 있어(예: 1,3) 체이닝
                # (직전 컷 이미지를 다음 참조에 넣는 것)이 의미가 없다 — 컷마다 독립 생성으로.
                group_bounds = [(i, i + 1) for i in range(len(shots))]
                target = len(shots)
            elif target and len(shots) > target:
                shots = shots[:target]
                group_bounds = [(s, min(e, len(shots))) for s, e in group_bounds if s < len(shots)]
        except Exception as e:
            log.exception("shots failed")
            _set_render_fail_reason(thread_ts, "컷 분해 중 오류가 났어요.")
            _post_chunks(channel, thread_ts, "컷 분해 중 오류가 났어요. 잠시 후 다시 시도해주세요.", replace_ts=ph); return
        if not shots:
            _set_render_fail_reason(thread_ts, "컷을 못 만들었어요(샷 리스트가 비어있음).")
            _post_chunks(channel, thread_ts, "컷을 못 만들었어요.", replace_ts=ph); return
        if not skip_confirm and target is None and kind and len(shots) >= _CUT_CONFIRM_THRESHOLD:
            _PENDING_CUT_CONFIRM[thread_ts] = {
                "kind": kind, "orig_rest": orig_rest or "", "channel": channel, "work": work,
                "bible": bible, "shots": shots, "title": title, "filename": filename, "cols": cols,
                "aspect_ratio": aspect_ratio, "style_suffix": style_suffix, "no_text": no_text,
                "source_text": source_text, "group_bounds": group_bounds,
            }
            text, blocks = _cut_confirm_blocks(len(shots))
            try:
                app.client.chat_update(channel=channel, ts=ph, text=text, blocks=blocks)
            except Exception:
                log.exception("컷 수 확인 카드 게시 실패")
            return
        results = [None] * len(shots)

    n = len(shots)
    retry_indices = [i for i in range(n) if results[i] is None]
    if retry_shots is not None and not retry_indices:
        _reply(channel, thread_ts, "다시 만들 실패한 컷이 없어요."); return
    _update_note(channel, ph, f"컷 {len(retry_indices)}개 이미지 생성 중이에요… (몇 분) 0/{len(retry_indices)}")
    total_cost, done = retry_cost + mood_ref_cost, 0
    retry_set = set(retry_indices)
    # group_bounds 없으면(재시도 등, 청킹 정보 미상) 컷마다 독립 그룹 — 기존과 동일하게 완전 병렬.
    groups = group_bounds if group_bounds else [(i, i + 1) for i in range(n)]

    fail_reasons: dict[int, str] = {}   # (F4) 컷별 실패 사유 — 세이프티 필터 거부인지 구분해 안내
    cancelled = False
    _lock = threading.Lock()

    def _gen_group(start, end):
        # 같은 그룹(=씬) 안에서는 순차 생성하며 직전 컷 이미지를 다음 컷 참조에 추가 —
        # 조명·구도·톤이 이어지는 느낌을 준다(2026-07-14, 실무자 지적: 컷이 따로 노는 느낌).
        # 그룹 경계에서는 이어주지 않고 리셋(다른 씬 사진이 새 씬 구도에 섞이지 않게).
        nonlocal total_cost, done, cancelled
        prev_png = None
        for i in range(start, end):
            if thread_ts in _CANCEL:
                with _lock:
                    cancelled = True
                return
            if i not in retry_set:
                if results[i]:
                    prev_png = results[i]
                continue
            s = shots[i]
            ref_entries = oi.shot_ref_entries(work, s)
            # ★2026-07-16: 무드/조명 참조는 등록 엘리먼트가 아니라 씬당 1번 생성한 별도 이미지라
            # shot_ref_entries(엘리먼트 레지스트리 기반)를 안 거친다 — 여기서 직접, 그리고 반드시
            # 맨 뒤에 붙인다(사용자 요구: "무드는 다른 참조보다 뒤에" — 참조 순서에 민감한 생성기
            # 특성상 costume-first처럼 앞에 놓으면 우세해져 다른 참조 역할을 침범할 위험이 있음).
            if s.get("_mood_ref_url"):
                ref_entries = list(ref_entries) + [("mood", s["_mood_ref_url"], None)]
            refs = [u for _role, u, *_ in ref_entries]
            if prev_png:
                refs = list(refs) + [oi.png_data_url(prev_png)]
            prompt = f"{s['prompt']}, {style_suffix}" if style_suffix else s["prompt"]
            # ★2026-07-15: 참조 역할 분리 지시(사용자 제공 — "인물은 얼굴만, 의상은 옷만"
            # 명시적 선언) — 순서 재배치만으론 부족했던 얼굴 참조 옷차림 오염 문제 보강.
            role_block = oi.reference_priority_block(ref_entries)
            if role_block:
                prompt = f"{prompt}\n\n{role_block}"
            gen_ar = aspect_ratio or config.OPENROUTER_PANEL_ASPECT
            try:
                png, cost = img.generate(prompt, aspect_ratio=gen_ar, refs=refs)
                with _lock:
                    results[i] = png; total_cost += cost; done += 1
                prev_png = png
            except Exception as e:
                # ★2026-07-15: "502 OpenAI returned invalid JSON" 같은 일시적 업스트림 오류는
                # 사용자가 매번 "실패한 컷 다시 만들어줘"라고 말할 필요 없이, 세이프티 필터
                # 거부가 아닌 한(그건 프롬프트 순화 없이 그대로 재시도해도 또 걸릴 뿐이라 의미
                # 없음) 같은 요청으로 즉시 1회만 자동 재시도한다.
                if _classify_fail_reason(str(e)) != "세이프티 필터 거부":
                    try:
                        png, cost = img.generate(prompt, aspect_ratio=gen_ar, refs=refs)
                        with _lock:
                            results[i] = png; total_cost += cost; done += 1
                        prev_png = png
                    except Exception as e2:
                        with _lock:
                            fail_reasons[i] = str(e2); done += 1
                        log.exception("한 컷 실패 (재시도 후에도, 컷 %s)", s.get("n") or (i + 1))
                else:
                    with _lock:
                        fail_reasons[i] = str(e); done += 1
                    log.exception("한 컷 실패 (컷 %s)", s.get("n") or (i + 1))
            with _lock:
                if done % 3 == 0 or done == len(retry_indices):
                    _update_note(channel, ph, f"컷 이미지 생성 중… {done}/{len(retry_indices)}")

    with cf.ThreadPoolExecutor(max_workers=config.OPENROUTER_IMG_WORKERS) as ex:
        futs = [ex.submit(_gen_group, start, end) for start, end in groups]
        for fut in cf.as_completed(futs):
            fut.result()   # 그룹 러너 자체의 예외(있으면 안 되지만) 전파
    if cancelled:
        _CANCEL.discard(thread_ts)
        _post_chunks(channel, thread_ts, generator.CANCEL_MSG, replace_ts=ph); return

    panels = [(results[i], shots[i].get("n") or (i + 1), shots[i].get("caption") or "")
              for i in range(n) if results[i]]
    if not panels:
        # ★2026-07-15 "스틸컷 생성할때도 왜 생성실패인지 이유를 알려줘" — 전체 실패 시에도
        # 부분 실패 때(아래 ~L1263)와 동일한 사유별 그룹 안내를 붙인다.
        if fail_reasons:
            by_reason: dict[str, list] = {}
            for i, msg in fail_reasons.items():
                num = shots[i].get("n") or (i + 1)
                by_reason.setdefault(_classify_fail_reason(msg), []).append(num)
            detail = " / ".join(
                f"{reason}: 컷 " + ", ".join(str(x) for x in sorted(nums))
                for reason, nums in by_reason.items())
            msg = f"이미지 생성이 모두 실패했어요 — {detail}\n" + _build_fail_detail_lines(fail_reasons, shots)
            if "세이프티 필터 거부" in by_reason:
                msg += "\n(세이프티 필터 거부는 그 컷의 표현·소재를 순화해서 다시 시도하면 대부분 통과돼요.)"
        else:
            msg = "이미지 생성이 모두 실패했어요. (키/모델/쿼터 확인)"
        _set_render_fail_reason(thread_ts, msg)
        _post_chunks(channel, thread_ts, msg, replace_ts=ph); return
    _LAST_RENDER_FAIL_REASON.pop(thread_ts, None)   # 이번엔 성공(부분 성공 포함) — 이전 실패 사유는 폐기
    _LAST_RENDER[thread_ts] = {
        "work": work, "bible": bible, "shots": shots, "results": results,
        "target": target, "title": title, "filename": filename, "cols": cols,
        "aspect_ratio": aspect_ratio, "style_suffix": style_suffix, "no_text": no_text,
        "total_cost": total_cost, "source_text": source_text, "fail_reasons": fail_reasons,
    }
    # 그리드 합성 전 개별 컷 PNG를 따로 보관 — [🎬 영상화]가 컷 하나만 골라 쓸 수 있게.
    # caption/prompt는 source_text(콘티 = 확정된 최종 대본)에서 그대로 나온 값이라
    # 영상화 모션 프롬프트에 그대로 재사용하면 최종 대본 내용이 자연히 반영됨.
    # scene_text(=source_text, 그 씬의 콘티 원문 전체)도 같이 들고 다녀서, 영상화 시
    # caption/prompt(짧은 요약)뿐 아니라 대사·지문 원문까지 모션 프롬프트에 반영되게 한다
    # (2026-07-13, "영상 만들 때 상세 콘티는 참고 안 한다" 이슈).
    cuts = [{"n": shots[i].get("n") or (i + 1), "png": results[i],
            "caption": shots[i].get("caption") or "", "prompt": shots[i].get("prompt") or "",
            "characters": list(shots[i].get("characters") or []),
            "places": list(shots[i].get("places") or []),
            "props": list(shots[i].get("props") or []),
            "duration": shots[i].get("duration"),   # 샷 분해가 씬 목표 길이/대사 분량 보고 배정(2026-07-14)
            "scene_text": source_text}
           for i in range(n) if results[i]]

    # (2026-07-14) 컷이 많으면(실무자: 130컷) 그리드 하나로 뭉치지 말고 _GRID_SPLIT_N개로
    # 나눠서 순서대로 하나씩 올린다 — 이미지 하나가 너무 커져 못 알아보는 문제 방지.
    n_batches = _GRID_SPLIT_N if len(panels) > _GRID_SPLIT_THRESHOLD else 1
    bsize = (len(panels) + n_batches - 1) // n_batches
    batches = [panels[i:i + bsize] for i in range(0, len(panels), bsize)] or [panels]
    total_batches = len(batches)

    miss = n - len(panels)
    grid_pngs = []
    for bi, batch in enumerate(batches, 1):
        _update_note(channel, ph, f"{len(panels)}컷 그리드로 합치는 중이에요… ({bi}/{total_batches})")
        try:
            grid_png = grid.build_grid(batch, cols=cols or config.OPENROUTER_GRID_COLS, no_text=no_text)
        except Exception as e:
            log.exception("grid failed")
            _post_chunks(channel, thread_ts, f"이미지 합성 중 오류가 났어요({bi}/{total_batches}). 이 배치는 건너뛰고 계속할게요."); continue
        grid_pngs.append(grid_png)
        lo, hi = batch[0][1], batch[-1][1]   # panels 항목 = (png, n, caption)
        part_s = f" {bi}/{total_batches} (컷 {lo}~{hi})" if total_batches > 1 else ""
        cap = f"🖼️ {title}{part_s} — {len(batch)}컷"
        if bi == total_batches:   # 실패·비용 요약은 마지막 배치에만 (F4/F8과 동일한 정리 방식)
            cap += (f" ({miss}컷 실패)" if miss else "") + (f" · 생성비 ~${total_cost:.2f}" if total_cost else "")
            if fail_reasons:
                by_reason: dict[str, list] = {}
                for i, msg in fail_reasons.items():
                    num = shots[i].get("n") or (i + 1)
                    by_reason.setdefault(_classify_fail_reason(msg), []).append(num)
                detail = " / ".join(
                    f"{reason}: 컷 " + ", ".join(str(x) for x in sorted(nums))
                    for reason, nums in by_reason.items())
                cap += f"\n⚠️ 실패 사유 — {detail}\n" + _build_fail_detail_lines(fail_reasons, shots)
                if "세이프티 필터 거부" in by_reason:
                    cap += "\n(세이프티 필터 거부는 그 컷의 표현·소재를 순화해서 다시 시도하면 대부분 통과돼요.)"
        fname = filename or f"storyboard_{work or 'ep'}.png"
        if total_batches > 1:
            stem, dot, ext = fname.rpartition(".")
            fname = f"{stem}_{bi}of{total_batches}.{ext}" if dot else f"{fname}_{bi}of{total_batches}"
        try:
            app.client.files_upload_v2(channel=channel, thread_ts=thread_ts, file=grid_png,
                                       filename=fname, title=f"{title}{part_s} {len(batch)}컷",
                                       initial_comment=cap)
        except Exception as e:
            log.exception("upload failed")
            _post_chunks(channel, thread_ts,
                        f"이미지는 만들었는데 업로드에서 막혔어요({bi}/{total_batches}). 잠시 후 다시 시도해주시고, 계속 안 되면 봇 관리자에게 알려주세요.")
    if not grid_pngs:
        return
    _update_note(channel, ph, f"✅ {title} 완성 (아래 이미지{'들' if total_batches > 1 else ''})", clear=True)
    return grid_pngs[-1], cuts

def _resolve_work_bible(channel, thread_ts, rest):
    """공통: <작품>/스레드로 작품 해석 + 시트 바이블 로드. 반환 (work, bible, tail, msgs)."""
    q = rest.strip()
    wm = SUB_RE.match(q)
    w0 = wm.group(1).strip() if wm else None
    work = w0 if (w0 and not _looks_like_mention(w0)) else None
    tail = (wm.group(2) if wm else q) or ""
    msgs = _thread_messages(channel, thread_ts)
    joined = "\n".join(m["content"] for m in msgs)
    if not work:
        work = _work_from_thread(joined, thread_ts)
    bible = None
    if work:
        work = works.resolve(work) or work          # 별칭 해석
        sh = _sheet()
        if sh:
            try:
                bible = sh.get(work)
            except Exception:
                log.exception("bible load failed")
    return work, bible, tail, msgs

_SCENE_HDR_RE = re.compile(r"(?m)^[ \t]*(?:[■*#\-]+[ \t]*)?씬\s*(\d+)\b[^\n]*$")

def _strip_conti_preamble(conti):
    """콘티 수정 응답 맨 앞에 붙는 "바뀐 점: ..." 같은 요약 줄을 걷어내고, 첫 씬 헤더부터만
    남긴다 — 저장/내보내기에는 실제 콘티 내용(씬1, 씬2, ...)만 필요하고 그 요약은 대화용."""
    m = _SCENE_HDR_RE.search(conti or "")
    return conti[m.start():].strip() if m else (conti or "").strip()

def _parse_json_object(text):
    t = (text or "").strip()
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e == -1 or e < s:
        raise ValueError("응답에서 JSON 객체({...})를 못 찾았어요.")
    body = t[s:e + 1]
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return json.loads(_repair_json_quotes(body))

def _place_categories(work: str | None) -> list[str]:
    """등록된 장소 이름에서 대분류(예: '숙소-화장실 앞' → '숙소')만 뽑아 중복 제거한 목록.
    ★2026-07-15, 장소 일관성 요청 — 새 장소 추출 시 이미 쓰는 대분류를 재사용하게 하기 위함."""
    out, seen = [], set()
    for e in oi.load_elements(work):
        if e.get("type") != "place":
            continue
        name = e.get("display") or ""
        cat = name.split("-", 1)[0].strip() if "-" in name else name
        if cat and cat not in seen:
            seen.add(cat)
            out.append(cat)
    return out

def _known_places(work: str | None) -> list[str]:
    """등록된 장소 대분류 + 전체 이름(둘 다) — 2단계 콘티의 "장소:" 선언이 이미 있는 이름을
    재사용하게 하는 힌트 목록(2026-07-15)."""
    cats = _place_categories(work)
    names = [e["display"] for e in oi.load_elements(work) if e.get("type") == "place" and e.get("display")]
    out, seen = [], set()
    for n in cats + names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out

def _known_costumes(work: str | None) -> list[str]:
    """등록된 의상 라벨 목록 — 2단계 콘티의 "등장:" 선언이 짧은 재사용 라벨(장소와 동일한
    방식)을 처음부터 재사용하게 하는 힌트(2026-07-15, "의상이 너무 세분화돼있다" 지적에 따라
    서술형 묘사 대신 라벨 방식으로 전환)."""
    return [e["display"] for e in oi.load_elements(work) if e.get("type") == "costume" and e.get("display")]

def _warn_unregistered_elements(channel, thread_ts, work, conti, extra_blocks=None):
    """콘티 완성 직후 등장인물·장소 후보를 뽑아 등록 안 된 게 있으면 바로 알림.
    씬 전환이 잦은 콘티(방/옷장 단위)나 단역 인물일수록 등록이 조용히 빠지기 쉬워서(2026-07-13),
    스틸컷/영상화 단계까지 가서야 참조 일관성이 안 잡히는 걸 미리 막는다.
    extra_blocks가 주어지면(F8) 이어서 바로 물어야 할 다음 액션(통과/재생성 버튼 등)을 이 경고
    메시지 하나에 같이 붙여서 완성 직후 메시지가 여러 개로 쪼개지지 않게 한다.
    반환값: 경고를 실제로 게시했으면 True(=extra_blocks를 여기 붙였다는 뜻), 아니면 False
    (호출부가 extra_blocks에 해당하는 내용을 따로 게시해야 함)."""
    if not work:
        return False
    try:
        raw = oi.chat(prompts.element_extract_system(_place_categories(work)),
                     prompts.element_extract_user(conti), timeout=60)
        obj = _parse_json_object(raw)
        chars = [c.strip() for c in (obj.get("characters") or []) if isinstance(c, str) and c.strip()]
        places = [c.strip() for c in (obj.get("places") or []) if isinstance(c, str) and c.strip()]
        # ★2026-07-15: 실무자 실측 "의상이 통일성 자체가 아예 안맞았음(잠옷-A, 편한 트레이닝복
        # 상하의)" — costumes가 이 추출 스키마에서 통째로 빠져 있어서 미등록 알림 자체가 안 뜨고
        # 있었다(매칭 실패가 아니라 미탐지). 인물/장소와 같은 흐름으로 costumes도 뽑는다.
        # ★2026-07-15: 추출 LLM이 형식 변형(접두어 누락·콤마 여러 개)을 잘 걸러내도록 프롬프트를
        # 손봤지만, LLM 출력이라 여전히 라벨 끝에 콤마/공백이 섞여 나올 수 있다 — 등록 name으로
        # 그대로 쓰면 "연습복-A," 같은 라벨이 통째로 저장되는 사고가 나므로 안전망으로 한 번 더 트림.
        costumes = [c.strip(" ,") for c in (obj.get("costumes") or []) if isinstance(c, str) and c.strip(" ,")]
        # ★2026-07-16: props도 costumes와 같은 이유(추출 스키마에 필드 자체가 없어 미탐지)로 빠져
        # 있었다 — 같은 흐름으로 뽑는다.
        props = [c.strip(" ,") for c in (obj.get("props") or []) if isinstance(c, str) and c.strip(" ,")]
    except Exception:
        log.exception("인물·장소·의상·소품 추출 실패 — 등록 안내는 생략")
        return False
    new_chars = [c for c in dict.fromkeys(chars) if not oi.resolve_element(work, c)]
    new_places = [c for c in dict.fromkeys(places) if not oi.resolve_element(work, c)]
    new_costumes = [c for c in dict.fromkeys(costumes) if not oi.resolve_element(work, c)]
    new_props = [c for c in dict.fromkeys(props) if not oi.resolve_element(work, c)]
    if not new_chars and not new_places and not new_costumes and not new_props:
        return False
    parts = []
    if new_chars:
        parts.append("**인물**\n" + "\n".join(f"· {c}" for c in new_chars))
    if new_places:
        parts.append("**장소**\n" + "\n".join(f"· {p}" for p in new_places))
    if new_costumes:
        parts.append("**의상**\n" + "\n".join(f"· {c}" for c in new_costumes))
    if new_props:
        parts.append("**소품**\n" + "\n".join(f"· {c}" for c in new_props))
    text = ("⚠️ 이 콘티에 아직 등록 안 된 인물/장소/의상/소품이 있어요 — 등록 안 하면 스틸컷/영상화에서 "
           "그 대상의 참조 일관성이 안 잡혀요:\n" + "\n".join(parts) + "\n\n"
           "사진이 있으면 첨부하면서 `인물 <이름>`/`장소 <이름>`/`의상 <이름>`/`소품 <이름>`으로 답장하거나 "
           "`<이름> 이 사진으로 해줘`처럼 자연어로 말해주세요. "
           "사진 여러 장 + 이름 여러 개(쉼표로 구분)를 한 메시지에 같이 보내면 한 번에 등록돼요. "
           "사진이 없으면 아래에서 골라 AI로 바로 만들 수도 있어요.\n\n"
           "등록 없이 바로 진행하려면 아래 버튼을 눌러도 돼요.")
    # ★2026-07-16: Slack static_select는 옵션 100개 제한 — 예전엔 인물/장소/의상/소품을 순서대로
    # 이어붙인 뒤 [:100]으로 잘라서, 합계가 100을 넘으면 맨 뒤에 붙는 소품이 무조건 먼저(그리고
    # 전부) 잘려나갔다(신규 항목이 카테고리별로 몰릴 때 특정 타입만 알림이 통째로 사라지는 문제).
    # 4개 타입을 라운드로빈으로 섞어서 넣으면, 100개로 잘려도 손실이 네 타입에 고르게 분산된다.
    _cand_lists = [
        [{"text": {"type": "plain_text", "text": f"인물 · {c}"[:75]}, "value": f"person|{c}"} for c in new_chars],
        [{"text": {"type": "plain_text", "text": f"장소 · {p}"[:75]}, "value": f"place|{p}"} for p in new_places],
        [{"text": {"type": "plain_text", "text": f"의상 · {c}"[:75]}, "value": f"costume|{c}"} for c in new_costumes],
        [{"text": {"type": "plain_text", "text": f"소품 · {c}"[:75]}, "value": f"prop|{c}"} for c in new_props],
    ]
    options = [opt for opt in itertools.chain.from_iterable(itertools.zip_longest(*_cand_lists))
               if opt is not None][:100]
    blocks = [{
        "type": "actions",
        "elements": [{"type": "static_select",
                     "placeholder": {"type": "plain_text", "text": "AI로 만들 대상 선택"},
                     "options": options, "action_id": "element_gen_pick"}],
    }]
    if extra_blocks:
        blocks += extra_blocks
    resp = app.client.chat_postMessage(
        channel=channel, thread_ts=thread_ts, text=text, blocks=_with_text_block(text, blocks))
    pending_element_pick_state.set(resp["ts"], work=work, context=conti[:1500])
    return True

_ELEMENT_TYPE_LABEL = {"person": "인물", "place": "장소", "prop": "소품", "costume": "의상"}

_CHAR_CARD_HEAD_RE = re.compile(r"^(?:#{1,3}\s*)?([^\n(]{1,12}?)\s*(?:\([^)\n]*,[^)\n]*\))?\s*/", re.M)

def _notion_character_visual_desc(work, name):
    """노션 캐릭터 카드에서 생김새 설명을 가져온다: '외형'이 있으면 그걸, 없으면 '설명'으로 대체
    (2026-07-13 실측 — 카드마다 '외형' 필드가 없을 수 있음). 인물 AI 생성은 반드시 이걸 기반으로
    해야 함 — 콘티 문맥만으론 생김새 정보가 없어서 호출마다 다른 얼굴이 나올 수 있음.
    반환: (field명 또는 None, 텍스트). 카드 자체를 못 찾으면 (None, None)."""
    if not (work and config.NOTION_TOKEN):
        return None, None
    pid = works.page_of(work)
    if not pid:
        return None, None
    try:
        from bot.shared import notion_sync
        full = notion_sync.page_text(pid)
    except Exception:
        log.exception("노션 캐릭터 카드 로드 실패")
        return None, None
    # ★2026-07-15: "민대표 있는데 못찾음" — 카드 제목과 요청 이름의 공백 유무가 다르면
    # (예: "민대표" vs "민 대표") NFC만으로는 매치가 안 됐다. 공백을 지우고 비교.
    name_n = re.sub(r"\s+", "", unicodedata.normalize("NFC", name))
    matches = list(_CHAR_CARD_HEAD_RE.finditer(full))
    for i, m in enumerate(matches):
        if re.sub(r"\s+", "", unicodedata.normalize("NFC", m.group(1).strip())) != name_n:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full)
        block = full[m.end():end]
        for field in ("외형", "설명"):
            fm = re.search(rf"{field}\s*:\s*(.+?)(?=\n-\s*\S+\s*:|\Z)", block, re.S)
            if fm:
                return field, fm.group(1).strip()
        return None, ""     # 카드는 찾았는데 외형/설명 둘 다 없음
    return None, None        # 카드 자체를 못 찾음

_IDEALIZED_FACE_GUIDANCE = (
    "The face must look breathtakingly, impossibly beautiful — flawless symmetrical features, "
    "luminous unreal skin, otherworldly beauty, clearly idealized and not an ordinary realistic "
    "face. This idealization applies equally to every character regardless of role, age, or "
    "personality — supporting characters, villains, and elderly characters must be idealized with "
    "the same beautiful bone structure, never given a realistic or aged face. Convey age, "
    "occupation, or personality through clothing, posture, and background only, never through "
    "facial realism or aging detail. Render at roughly 55-60% realism — painterly, not "
    "photorealistic, no visible skin pores or photographic skin texture."
    # ★2026-07-15: 스틸컷→영상 변환 시 톤 불일치가 심하고, 사실적일수록 영상화 API의 실존인물
    # 안전필터(InputImageSensitiveContentDetected.PrivacyInformation)에 더 잘 걸리는 것으로
    # 보여(사용자 실측) 기존 65-70%보다 더 낮춰(60%) 스타일화를 강화한다.
)

# ★2026-07-20b: 2D 애니메이션(재패니메이션) 작품용 인물 이상화 지침 — 위 _IDEALIZED_FACE_
# GUIDANCE는 "60% 리얼리즘/페인터리"를 명시해 실사풍 전용이다. 애니 작품에 그대로 쓰면 인물
# 참조가 반실사로 나와 나머지 애니 스틸컷과 어긋난다(콘티에서 이미 여러 번 겪은 "참조 화풍
# 불일치" 문제와 같은 종류) — 애니 특유의 이상화(큰 눈, 매끈한 셀 음영 피부, 애니 비례)로 별도 정의.
_ANIME_FACE_GUIDANCE = (
    "The face must look like an idealized Japanese anime protagonist — large, luminous, "
    "expressive anime eyes with detailed highlight reflections, flawless smooth cel-shaded skin "
    "(no pores, no realistic texture), perfectly proportioned anime facial structure. This "
    "idealization applies equally to every character regardless of role, age, or personality — "
    "supporting characters, villains, and elderly characters must be idealized with the same "
    "beautiful anime bone structure, never given a realistic or photographically aged face. "
    "Convey age, occupation, or personality through clothing, posture, hair style, and background "
    "only, never through facial realism or aging detail. Zero photorealism, zero 3D-rendered "
    "look — pure flat cel-shaded 2D anime rendering."
)

def _work_mood_hint(work: str | None) -> str:
    """작품 바이블(logline/keyword/emotion/plot)에서 짧은 톤·장르·배경 힌트 문장을 뽑는다.
    ★2026-07-15: 장소/소품 생성 프롬프트에 분위기 지시가 전혀 없어(그냥 "photorealistic...
    natural lighting") 드라마 톤과 무관한 밋밋한 부동산 매물 사진처럼 나오는 문제(사용자 실측,
    "복도 화장실" 사례) — 있으면 이 힌트를 프롬프트에 섞어 넣는다.
    ★2026-07-15: 사용자 실측 "의상, 장소를 너무 뜬금없지 않게 작품 맥락에서 파악해야하는데" —
    logline/emotion만으로는 감정 톤만 잡히고 정작 장르·설정(예: "아이돌 연습생 서바이벌물")이
    빠져 있어 장소/의상이 작품 세계관과 무관하게 뜬금없이 생성될 수 있었음. keyword(장르 태그)와
    plot(줄거리) 앞부분을 더해 구체적인 장르/설정 시그널을 함께 준다. 실패해도 생성 자체를 막으면
    안 되는 nice-to-have라 무조건 try/except로 감싸고 실패 시 빈 문자열."""
    if not work:
        return ""
    try:
        sh = _sheet()
        if not sh:
            return ""
        bible = sh.get(work)
        if not bible:
            return ""
        bits = []
        if bible.get("logline"):
            bits.append(str(bible["logline"]).strip())
        if bible.get("keyword"):
            bits.append(str(bible["keyword"]).strip())
        if bible.get("emotion"):
            bits.append(str(bible["emotion"]).strip())
        if bible.get("plot"):
            bits.append(str(bible["plot"]).strip()[:150])
        bits = [b for b in bits if b]
        if not bits:
            return ""
        return " / ".join(bits)[:300]
    except Exception:
        return ""

def _generate_element_candidate(work: str, name: str, etype: str, context: str,
                                 override_desc: str | None = None, feedback: str | None = None):
    # ★2026-07-15: 재생성 피드백("더 어둡게" 등)은 등록 당시 맥락인 context와 별개 채널로 받는다
    # — person 타입은 애초에 context를 프롬프트에 안 씀(카드 desc만 사용)이라 feedback을 context에
    # 얹으면 person에서 조용히 무시된다. 그래서 모든 분기 끝에 공통으로 덧붙인다.
    feedback_instr = (f" User feedback for regeneration — please incorporate: {feedback[:200]}."
                      if feedback else "")
    if etype == "place":
        mood = _work_mood_hint(work)
        # ★2026-07-15: mood 힌트에 keyword/plot(장르·설정)까지 섞이면서 "tone/mood"라는
        # 표현만으로는 좁아 보여 문구를 "genre/setting/tone"으로 넓혔다(사용자 실측 "의상, 장소를
        # 너무 뜬금없지 않게 작품 맥락에서 파악해야하는데").
        mood_instr = (
            f"This is a location for a short-form drama with this genre/setting/tone context: {mood}. "
            "Render the location's design, lighting, color palette, and atmosphere to clearly match "
            "that context — cinematic and moody, not a bland flat neutral-lit stock/real-estate photo. "
            if mood else
            "Give the location a clear cinematic mood and atmosphere (deliberate lighting, color "
            "palette, shadow) — avoid a bland flat neutral-lit stock/real-estate-photo look. "
        )
        context_sentence = f"Context: {context[:300]}. " if context else ""
        # ★2026-07-15: 사용자 실측 "장소도 너무 특이하게 나와 좀 평범하게 나오게" — 위 "cinematic
        # and moody, not bland" 지시가 조명/분위기뿐 아니라 건축·구조 자체까지 이상하거나 튀는
        # 방향으로 끌고 간 것으로 보임. 조명·분위기는 영화적으로 살리되, 공간 구조·건축·가구
        # 자체는 그 장소 종류의 평범하고 흔한 모습을 유지하라고 명확히 분리해서 지시한다(구체적
        # 묘사가 context에 없을 때 특히).
        ordinary_instr = (
            "The architecture, layout, and furnishings themselves should be ordinary and realistic "
            "for this type of location — not unusual, exotic, surreal, or visually striking in "
            "structure/design. Only the lighting, color grading, and atmosphere should be cinematic; "
            "the physical space itself should look like a normal, mundane real-world version of this "
            "location type. "
            if not (context or "").strip() else ""
        )
        # ★2026-07-20b: 이 장소 참조가 스타일 문구 없이 생성돼서(person도 동일 문제였음) 2d_anim
        # 작품에서도 실사풍으로 나올 수 있었다 — 스틸컷과 같은 화풍 문구를 앞에 못박는다.
        prompt = (f"{_element_ref_style_phrase(work)} — cinematic empty establishing shot of the "
                  f"location '{name}'. "
                  f"{mood_instr}"
                  f"{ordinary_instr}"
                  f"{context_sentence}"
                  "No people visible, clean reference plate reusable for later compositing.")
    elif etype == "prop":
        mood = _work_mood_hint(work)
        mood_instr = (f"Style/lighting/design should feel consistent with this drama's "
                      f"genre/setting/tone: {mood}. " if mood else "")
        context_sentence = f"Context: {context[:300]}. " if context else ""
        # ★2026-07-15: costume 분기와 동일한 스타일 불일치(포토리얼 vs STILL_STYLE의 세미리얼
        # 일러스트) — prop도 최종 스틸컷과 맞춰 그린다.
        prompt = (f"{_element_ref_style_phrase(work)} reference of the object '{name}' alone on "
                  f"a plain neutral background, stylized illustration rendering, not a photograph. "
                  f"{mood_instr}{context_sentence}".strip())
    elif etype == "costume":
        # ★2026-07-15: 의상 레지스트리 신규 — 특정 인물의 외형(얼굴)이 아니라 "이 옷 자체"가
        # 고정값이어야 하므로, 사람 없이 옷만 보이는 플랫레이/마네킹 구도로 만든다(인물 얼굴
        # 참조와 섞이면 그 옷을 다른 인물이 입은 컷에서도 얼굴이 끌려올 위험이 있어 분리).
        # ★2026-07-15: 사용자 실측 "옷 일관성이 전혀 다름 — 라벨링 안 읽는거 같음" — 원인은
        # 이 레퍼런스가 "Photorealistic"(포토리얼 제품샷)으로 생성되는데, 실제 스틸컷은
        # STILL_STYLE(semi-realistic illustration/painterly)이라 스타일이 어긋나 있었다. person
        # 분기는 이미 _IDEALIZED_FACE_GUIDANCE로 최종 스타일에 맞춰 생성하는데 costume만 빠져
        # 있었음 — gpt-image-2가 스타일이 안 맞는 참조를 약하게 반영/무시해 옷이 컷마다 달라지는
        # 것으로 보인다. 최종 스타일과 맞춰 그린다.
        # ★2026-07-15: 사용자 실측 "의상, 장소를 너무 뜬금없지 않게 작품 맥락에서 파악해야하는데"
        # — costume 분기는 place/prop과 달리 작품 바이블(장르/설정) 참조가 전혀 없이 로컬 씬
        # 발췌(context)만 썼음. 그 결과 의상 시대/스타일이 작품 세계관과 무관하게 뜬금없이
        # 나올 수 있었음 — place/prop과 동일하게 _work_mood_hint를 붙인다.
        mood = _work_mood_hint(work)
        mood_instr = (
            f"This costume is for a story with this genre/setting/tone context: {mood}. Make the "
            "costume's era, silhouette, fabric, and styling choices consistent with that context "
            "— not generic or disconnected from the story's world. "
            if mood else ""
        )
        # ★2026-07-15: 사용자 제공 레퍼런스 이미지(무지 흰 배경 + 옷만, 눈에 안 띄는 무난한
        # 디자인) 기준 — 배경을 "neutral"이 아니라 명시적으로 순백색으로 고정하고, 괄호 묘사
        # 등 구체적 지침이 없을 때(context가 비어있을 때)는 튀는 디자인 대신 무난한 기본
        # 디자인으로 기울이라는 지시를 추가한다.
        no_context_instr = (
            "No specific costume description was given for this label, so default to a plain, "
            "understated, minimal design — no bold colors, no loud patterns, no logos or graphics, "
            "no eye-catching or attention-grabbing details. "
            if not (context or "").strip() else ""
        )
        prompt = (f"{_element_ref_style_phrase(work)} reference of a clothing outfit called "
                  f"'{name}', shown as a flat lay or on an invisible mannequin (no visible face or "
                  "head), on a plain solid white background (no other colors, textures, or props "
                  "in the background), studio product-shot lighting. Stylized illustration rendering "
                  "matching this drama's cinematic look — clearly an illustration, not a "
                  "photograph. "
                  f"{no_context_instr}"
                  f"{mood_instr}"
                  f"Context: {context[:300]}.")
    else:
        if override_desc is not None:
            desc = override_desc
        else:
            _, desc = _notion_character_visual_desc(work, name)
            if desc is None:
                raise RuntimeError(
                    f"노션에서 '{name}' 캐릭터 카드를 못 찾았어요 — 먼저 노션에 "
                    f"'{name} (성별, 나이) / ...' 형식으로 카드를 만들어주세요.")
            if not desc:
                raise RuntimeError(f"'{name}' 카드에 '외형'도 '설명'도 없어요 — 노션 카드에 최소 하나는 채워주세요.")
        # ★2026-07-15: '외형'/'설명' 필드는 카드 첫 줄("이름 (성별, 나이) / ...")에서 이미
        # 성별을 밝혔다는 전제로 헤어스타일 등 생김새만 자유서술하는 경우가 많아, 여기서
        # 성별 재언급이 없으면 이미지 생성 프롬프트에 성별 정보가 아예 안 들어가는 버그가 있었다
        # (2026-07-15, "하진" 실측 — 남자 캐릭터인데 여성으로 생성됨). desc와 별개로 카드 첫 줄에서
        # 성별만 뽑아 프롬프트에 명시 지시로 주입한다. 카드/성별 파싱 실패 시 그냥 생략(에러 아님).
        gender = oi._notion_character_gender(work, name)
        gender_instr = ""
        if gender == "male":
            gender_instr = "이 인물은 남성이다. This character is male — generate a clearly male face and body, not female.\n\n"
        elif gender == "female":
            gender_instr = "이 인물은 여성이다. This character is female — generate a clearly female face and body, not male.\n\n"
        # ★마지막 줄(숏폼 드라마·고정값·상반신)이 핵심 — 실제 프로덕션에서 gpt-image-2로
        # 고정값(레퍼런스) 만들 때 쓰던 프롬프트 그대로(2026-07-13, 사용자 제공).
        # ★구도 추가(2026-07-13): 정면 응시·단순배경의 "증명사진" 스타일은 나중에 영상화
        # (seedance)할 때 실존인물 안전필터(InputImageSensitiveContentDetected.
        # PrivacyInformation)에 잘 걸리는 것으로 실측됨 — 필터 우회가 아니라 레퍼런스 자체를
        # 자연스러운 스틸컷 구도로 만들어 이 문제를 애초에 줄인다.
        # ★문구 강화(2026-07-14): "실존 인물과 닮음" 판정은 롤마다 확률적으로 걸리는 걸로
        # 보여서(같은 프롬프트·같은 구도로도 재현이 안 됨), 완화 지시를 한 줄 더 명시적인
        # 영어 문구로 보강 — 100% 방지는 안 되지만 닮을 확률 자체를 줄이는 목적.
        # ★정책 문구 반영(2026-07-14, 사용자 제공 기준): "유명 IP 및 공인 생성 제한 — 연예인·
        # 정치인 등 대중에게 식별 가능한 공인의 얼굴/이미지는 생성 자체가 차단"이 실제 판정
        # 기준이라, 그 기준 언어 그대로 명시해 특정 공인을 연상시키는 특징(예: 유명 아이돌 특유의
        # 헤어스타일·이목구비 조합)까지 피하도록 강화.
        # ★2026-07-15: 인물 고정값 구도 규칙 변경(사용자 지시) — 얼굴~어깨 크롭, 의상 노출
        # 최소화(무지 티셔츠/목선 안 보이는 중립적인 옷), 액세서리 제거, 정면/반측면,
        # 전신+의상 특징 강한 구도 금지. 이 인물 참조는 얼굴 고정이 유일한 목적이고 의상은
        # 별도 costume 레지스트리(참조 이미지)가 전담하므로, 인물 참조 자체에 특정 의상이
        # 강하게 찍혀 있으면 그 컷의 실제 costume 참조와 충돌해 옷이 뒤섞이거나 costume 참조가
        # 약하게 반영될 위험이 있다 — 인물 참조의 옷차림을 최대한 중립적/눈에 안 띄게 만들어
        # 이 충돌 가능성 자체를 줄인다. 기존 "정면 아니면 반측면" 요구(실존인물 안전필터 회피용,
        # 2026-07-13)는 이번 지시와도 호환되므로 그대로 유지.
        # ★2026-07-20b: 인물 참조가 STYLE_PRESETS를 안 거치는 별도 프롬프트라 화풍 문구가
        # 아예 없었다(콘티/스틸컷과 별개 경로) — 2d_anim 작품에서도 실사에 가깝게 나올 수
        # 있었던 근본 원인. 스틸컷과 동일한 화풍 문구를 맨 앞에 못박는다.
        style_phrase = _element_ref_style_phrase(work)
        prompt = (f"{style_phrase} character reference of {name}. {desc[:400]}\n\n"
                  f"{gender_instr}"
                  f"{name}를 숏폼 드라마에 나올 애로 생각하고 이미지 생성 고정값에 잘 나오게 "
                  "얼굴부터 어깨까지만 나오게 크롭해서 만들어줘(그 아래로는 안 보이게). "
                  "의상이 최대한 안 보이게 노출을 최소화하고, 무지 티셔츠나 목선이 안 보이는 "
                  "중립적인 옷차림으로(특정 의상의 색상·무늬·디자인이 두드러지지 않게). "
                  "귀걸이·목걸이·안경 같은 액세서리는 빼줘. 정면 또는 반측면 구도로, "
                  "전신 샷이나 의상 특징이 강하게 드러나는 구도는 쓰지 마. "
                  "특정 실존 유명인과 닮지 않게. "
                  "Crop tightly from face to shoulders only — nothing below the shoulders visible. "
                  "Minimize costume visibility: plain, neutral top with no visible collar/neckline "
                  "detail, no distinctive color, pattern, or design — this reference is for facial "
                  "identity only, not outfit. No accessories (earrings, necklace, glasses, etc). "
                  "Front-facing or three-quarter angle only — not a full-body shot, not a shot with "
                  "prominent costume detail. "
                  "Generate a completely fictional, original face — do not base it on or resemble "
                  "any real celebrity, actor, politician, or other publicly identifiable person. "
                  "Avoid any distinctive combination of features, hairstyle, or styling strongly "
                  "associated with a specific real public figure — invent a clearly original look. "
                  f"{_face_guidance_for_work(work)}")
    prompt = prompt + feedback_instr
    img_backend = hf if config.IMAGE_BACKEND == "higgsfield" else oi
    return img_backend.generate(prompt, aspect_ratio="1:1")

_PENDING_MISSING_APPEARANCE: dict[str, dict] = {}  # 확인 메시지 ts -> {channel, thread_ts, work, name, etype, context}

def _missing_appearance_confirm_blocks():
    return [{
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ 그냥 생성"},
             "action_id": "element_gen_no_appearance_force"},
            {"type": "button", "text": {"type": "plain_text", "text": "🎭 AI로 외형 채워서 생성"},
             "action_id": "element_gen_ai_infer_appearance"},
            {"type": "button", "text": {"type": "plain_text", "text": "취소 — 먼저 채우고 올게요"},
             "action_id": "element_gen_no_appearance_cancel"},
        ],
    }]

def _infer_appearance_via_ai(work: str, name: str, personality_desc: str) -> str:
    """'외형' 필드가 없을 때 이름/설명(성격·역할)만 가지고 LLM으로 그럴듯한 외형(생김새) 묘사를
    새로 지어낸다(2026-07-15, 사용자 요청 — 세 번째 선택지). 노션을 수정하지 않고, 이번 생성
    1회에만 쓸 desc를 반환한다. 실패 시 예외를 그대로 던짐(호출부에서 처리)."""
    system = (
        "너는 숏폼 드라마 캐릭터 디자이너다. 주어진 캐릭터 이름과 성격/역할 설명만 보고, "
        "그 캐릭터에 어울릴 법한 구체적인 '외형(생김새)' 묘사를 새로 지어내라. "
        "머리색/헤어스타일, 체형, 특징적인 이목구비나 분위기를 포함해 2~4문장으로 한국어로 "
        "작성하라. 서두나 부연 설명 없이 외형 묘사 본문만 출력하라.")
    user = (f"작품: {work}\n캐릭터 이름: {name}\n"
            f"카드에 적힌 성격/역할 설명: {personality_desc or '(없음)'}\n\n"
            "위 정보를 바탕으로 이 캐릭터의 외형(생김새)을 지어내줘.")
    return oi.chat(system, user, timeout=60).strip()

def _post_element_candidate(channel, thread_ts, work, name, etype, context, force: bool = False,
                             override_desc: str | None = None, feedback: str | None = None):
    label = _ELEMENT_TYPE_LABEL.get(etype, etype)
    # ★2026-07-15: 인물 카드에 '외형' 필드가 없어 '설명'(성격/역할 설명, 생김새 정보 아님)으로
    # 대체되는 경우 그대로 생성하면 프롬프트에 실제 외형 정보가 하나도 없어 품질이 크게 떨어짐
    # ("하진" 카드 실측 — 설명이 성격/포지션 서술뿐이었음). 그럴 땐 자동 생성하지 말고 사용자에게
    # 먼저 확인받는다(force=True면 우회).
    if not force and etype == "person":
        field, _desc = _notion_character_visual_desc(work, name)
        if field == "설명":
            text = (f"⚠️ '{name}' 카드에 '외형' 묘사가 없어요(성격/역할 설명만 있음) — "
                    "먼저 채워주시는 걸 권장해요. 그래도 지금 있는 설명만으로 생성할까요?")
            resp = app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text,
                blocks=_with_text_block(text, _missing_appearance_confirm_blocks()))
            _PENDING_MISSING_APPEARANCE[resp["ts"]] = {
                "channel": channel, "thread_ts": thread_ts, "work": work,
                "name": name, "etype": etype, "context": context,
            }
            return
    try:
        png, cost = _generate_element_candidate(work, name, etype, context, override_desc=override_desc,
                                                 feedback=feedback)
    except Exception as e:
        log.exception("엘리먼트 AI 생성 실패")
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="⚠️ 이미지 생성에 실패했어요. 잠시 후 다시 시도해주세요.")
        return
    cap = f"🎨 {label} 후보 — {name}" + (f" · ~${cost:.3f}" if cost else "")
    app.client.files_upload_v2(channel=channel, thread_ts=thread_ts, file=png,
                               filename=f"{name}_candidate.png", title=cap, initial_comment=cap)
    text = "이 이미지로 등록할까요?"
    resp = app.client.chat_postMessage(
        channel=channel, thread_ts=thread_ts, text=text,
        blocks=_with_text_block(text, [{
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ 이걸로 등록"},
                 "style": "primary", "action_id": "element_gen_confirm"},
                {"type": "button", "text": {"type": "plain_text", "text": "🔄 다시 생성"},
                 "action_id": "element_gen_regen"},
            ],
        }]))
    pending_element_state.set(resp["ts"], work=work, name=name, etype=etype, context=context, png=png)

@app.action("element_gen_no_appearance_force")
def _act_element_gen_no_appearance_force(ack, body):
    ack()
    msg_ts = body["message"]["ts"]
    p = _PENDING_MISSING_APPEARANCE.pop(msg_ts, None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요)."); return
    _disable_buttons(body, "🎨 있는 설명만으로 생성 중…")
    _post_element_candidate(p["channel"], p["thread_ts"], p["work"], p["name"], p["etype"],
                             p["context"], force=True)

@app.action("element_gen_ai_infer_appearance")
def _act_element_gen_ai_infer_appearance(ack, body):
    ack()
    msg_ts = body["message"]["ts"]
    p = _PENDING_MISSING_APPEARANCE.pop(msg_ts, None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요)."); return
    _disable_buttons(body, "🤖 AI가 외형 추론 중…")
    try:
        _, personality_desc = _notion_character_visual_desc(p["work"], p["name"])
        ai_desc = _infer_appearance_via_ai(p["work"], p["name"], personality_desc or "")
    except Exception as e:
        log.exception("AI 외형 추론 실패")
        app.client.chat_postMessage(channel=p["channel"], thread_ts=p["thread_ts"],
                                     text="⚠️ AI 외형 추론에 실패했어요 — 노션에 직접 채워주세요.")
        return
    _post_element_candidate(p["channel"], p["thread_ts"], p["work"], p["name"], p["etype"],
                             p["context"], force=True, override_desc=ai_desc)

@app.action("element_gen_no_appearance_cancel")
def _act_element_gen_no_appearance_cancel(ack, body):
    ack()
    msg_ts = body["message"]["ts"]
    _PENDING_MISSING_APPEARANCE.pop(msg_ts, None)
    _disable_buttons(body, "알겠어요 — 노션 카드에 '외형'을 채운 뒤 다시 시도해주세요.")

@app.action("element_gen_pick")
def _act_element_gen_pick(ack, body):
    ack()
    msg_ts = body["message"]["ts"]
    p = pending_element_pick_state.get(msg_ts)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요) — 콘티를 다시 만들어 주세요."); return
    etype, name = body["actions"][0]["selected_option"]["value"].split("|", 1)
    if etype == "costume":
        # ★2026-07-15: 의상 라벨만 안전망으로 트림(인물/장소는 동일 문제 사례가 없어 범위 안 넓힘) —
        # 추출 단계에서 이미 트림하지만, name이 여기 오기까지 Slack 옵션 value 문자열을 한 번 더
        # 거치므로 등록 직전에 한 번 더 걸러 "라벨," 같은 이름이 그대로 register_element에 들어가는
        # 걸 막는다.
        name = name.strip(" ,")
    ch, tts = _action_ctx(body)
    app.client.chat_postMessage(channel=ch, thread_ts=tts, text=f"🎨 <{p['work']}> {name} 생성 중…")
    _post_element_candidate(ch, tts, p["work"], name, etype, p["context"])

@app.action("element_gen_confirm")
def _act_element_gen_confirm(ack, body):
    ack()
    msg_ts = body["message"]["ts"]
    p = pending_element_state.pop(msg_ts)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요)."); return
    # 인물·장소·소품 전부 visual-pipeline의 fixed-images(단일 소스, schema.sql의 elements.type도
    # person/place/prop 셋 다 같은 구조)에 저장 — elements.json/data/refs로 새는 걸 방지
    # (2026-07-13, "다른 인물 사진은 다 있는데 왜 안되지" 이슈 → 인물뿐 아니라 전부 통일).
    # ★장소·소품은 elements.json 등록도 반드시 같이 해야 함 — 콘티→샷 분해 LLM에게 "등록된
    # 장소/소품" 힌트 목록으로 넘어가는 건 elements.json뿐이라, 파일만 두면 그 힌트에 안 잡혀서
    # 컷의 places/props 필드에 영영 안 실림(인물은 컷의 characters가 대사·지문에서 직접 나와서
    # 힌트 목록이 필요 없지만 장소·소품은 다름). file 필드는 비워서(register_element에 filename
    # 안 넘김) _element_data_url이 fixed-images 쪽 파일로 폴백해 단일 소스를 유지하게 한다.
    fx = oi.vp_fixed_dir(p["work"])
    if fx:
        # 폴더명은 이름이 아니라 id로(rename에 안전 — 2026-07-13) — register_element를
        # 먼저 호출해 id를 확보한 뒤 그 id 폴더에 저장한다.
        elem = oi.register_element(p["work"], p["name"], p["etype"], aliases=[p["name"]], clear_file=True)
        d = fx / elem["id"]
        d.mkdir(parents=True, exist_ok=True)
        # ★2026-07-14: 대표 이미지가 mtime 최초 파일로 고정(_first_image)돼서, 이 캐릭터에
        # 예전 참조 파일이 남아있으면 새로 확정해도 반영이 안 됐다 — 확정 시점엔 기존 파일을
        # 지우고 새 파일 하나만 남긴다(_save_ref_pairs와 동일 정책).
        for old in list(d.iterdir()):
            if old.is_file() and old.suffix.lower() in _REF_SAVE_EXTS:
                old.unlink()
        (d / f"{p['name']}.png").write_bytes(p["png"])
        _disable_buttons(body, f"✅ <{p['work']}> {p['name']} 등록 완료 (AI 생성 이미지 · fixed-images)")
        return
    d = config.OPENROUTER_REFS_DIR / oi.canon_work(p["work"])
    d.mkdir(parents=True, exist_ok=True)
    fname = f"{p['name']}.png"
    (d / fname).write_bytes(p["png"])
    oi.register_element(p["work"], p["name"], p["etype"], filename=fname, aliases=[p["name"]])
    _disable_buttons(body, f"✅ <{p['work']}> {p['name']} 등록 완료 (AI 생성 이미지)")

@app.action("element_gen_regen")
def _act_element_gen_regen(ack, body):
    """(2026-07-15) 클릭 즉시 재생성하지 않고, 어떻게 다시 만들지 먼저 물어본다 — 자유 답변을
    다음 메시지로 받아 반영(_maybe_element_regen_ask_reply). still_regen과 동일 패턴이라
    이스케이프 해치로 [🔁 그냥 재생성] 버튼도 같이 둔다."""
    ack()
    msg_ts = body["message"]["ts"]
    p = pending_element_state.pop(msg_ts)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요)."); return
    _disable_buttons(body, "🔄 재생성 전에 확인할게요 ↓")
    ch, tts = _action_ctx(body)
    _PENDING_ELEMENT_REGEN_ASK[tts] = {"work": p["work"], "name": p["name"], "etype": p["etype"],
                                        "context": p["context"]}
    text = ("어떻게 다시 만들까요? (예: '더 어둡게', '전신으로', '조명을 따뜻하게')\n"
            "자유롭게 말씀해주시면 반영해서 다시 만들게요.")
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "🔁 그냥 재생성"},
             "action_id": "element_gen_regen_plain"},
        ]},
    ]
    app.client.chat_postMessage(channel=ch, thread_ts=tts, text=text, blocks=blocks)

@app.action("element_gen_regen_plain")
def _act_element_gen_regen_plain(ack, body):
    """[🔁 그냥 재생성] — 피드백 없이 기존과 동일하게 즉시 재생성(에스케이프 해치)."""
    ack()
    ch, tts = _action_ctx(body)
    p = _PENDING_ELEMENT_REGEN_ASK.pop(tts, None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요)."); return
    _disable_buttons(body, "🔄 다시 생성 중…")
    _post_element_candidate(ch, tts, p["work"], p["name"], p["etype"], p["context"])

_PLAN_SCENE_LINE_RE = re.compile(r"(?m)^(\d+)\.\s*.+$")

def _parse_plan_scenes(plan_text: str) -> list[tuple[int, str]]:
    """1단계 씬 설계안 텍스트("N. `N초 · 제목` — 상황" 한 줄씩)에서 (씬번호, 그 줄 전문) 목록을
    뽑는다. ★2026-07-15 — 상세 콘티를 씬 단위로 쪼개 병렬 생성하기 위한 전제(아래 참고)."""
    out = []
    for m in _PLAN_SCENE_LINE_RE.finditer(plan_text or ""):
        out.append((int(m.group(1)), m.group(0).strip()))
    return out

_PLAN_ANNOT_RE = re.compile(r"`[^`]*`\s*\([^)]+\)")

_ENTRY_LINE_RE = re.compile(r"(?m)^\s*등장\s*:.*$")

def _sync_costume_across_scenes(plan_scenes, results):
    """★2026-07-15: 씬 단위 병렬 생성(각 씬이 서로의 실제 등장 라인을 못 봄)에서, 1단계 plan
    line에 `(장소/인물)` 표기가 없는(=직전 씬과 장소·인물이 동일하다고 1단계가 이미 판단한)
    연속 씬 쌍은 곧장 이어지는 상황일 가능성이 높다 — 이 경우 두 씬의 '등장:' 라인(의상 포함)이
    서로 다르면, 값비싼 재호출 없이 직전 씬의 등장 라인을 그대로 복사해 의상 불일치를 봉합한다.
    (완벽한 판별은 아니고 휴리스틱이다 — 오탐 시 직전 씬 그대로 따라가는 정도라 안전한 방향.)"""
    prev_num = None
    for num, line in plan_scenes:
        if prev_num is not None and num in results and prev_num in results:
            has_annot = bool(_PLAN_ANNOT_RE.search(line))
            if not has_annot:
                prev_m = _ENTRY_LINE_RE.search(results[prev_num] or "")
                cur_m = _ENTRY_LINE_RE.search(results[num] or "")
                if prev_m and cur_m and prev_m.group(0).strip() != cur_m.group(0).strip():
                    results[num] = (results[num][:cur_m.start()] + prev_m.group(0)
                                     + results[num][cur_m.end():])
                    log.info(f"씬{num}: 직전 씬(씬{prev_num})과 (장소/인물) 표기 없음 → 등장 라인 의상 동기화")
        prev_num = num

def _split_scenes(conti):
    """콘티를 '■ 씬N ...' 헤더 기준으로 분할 → [(num, header, body), ...]."""
    if not conti:
        return []
    ms = list(_SCENE_HDR_RE.finditer(conti))
    out = []
    for i, m in enumerate(ms):
        num = int(m.group(1))
        hdr = m.group(0).strip().strip("■*# -").strip()
        start = m.end()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(conti)
        body = conti[start:end].strip()
        out.append((num, hdr, body))
    return out

_ENTRANCE_LINE_RE = re.compile(r"^\s*등장\s*:\s*(.+)$", re.M)

_ENTRANCE_PERSON_RE = re.compile(r"([^\s()]+)\(([^,()]+)(?:,[^()]*)?\)")

def _scene_costume_map(text: str) -> dict:
    """콘티(청크/씬 일부)의 모든 '등장: {인물}({의상라벨}, {묘사})' 선언 줄을 훑어
    {인물: 의상라벨} 매핑을 만든다. ★2026-07-15: 씬 헤더 바로 아래 '등장:' 줄에 딱 한 번만
    의상이 선언되고, 그 뒤 개별 [N초] 비트들은 인물 이름만 언급하지 의상 라벨을 반복 안 하는
    구조라(실측 예시: "선우 위주(리안 어깨 걸침)" — 의상 언급 없음), 3단계가 비트 하나하나를
    독립적으로 처리할 때(특히 긴 씬/청크 분할) 그 비트의 인물이 어떤 의상인지 맥락을 놓치기
    쉽다(사용자 지적: "씬1 처음에만 등장·의상을 등장시켜서 정보를 매칭 못시킨거 같아"). LLM의
    텍스트 반복에 기대는 대신, 이 매핑을 코드로 미리 뽑아 각 컷의 characters로 직접 의상을
    붙여준다(아래 사용처 참고) — LLM이 그 비트에서 의상 라벨을 다시 안 써도 참조가 붙는다."""
    out = {}
    for line in _ENTRANCE_LINE_RE.findall(text or ""):
        for name, label in _ENTRANCE_PERSON_RE.findall(line):
            name = name.strip()
            # ★2026-07-15: "리안(의상: 잠옷-A)" 형식(2단계가 "의상:" 접두어를 붙여 쓴 경우, 씬1의
            # "리안(잠옷-A)"와 표기가 다름)에서 접두어까지 라벨에 같이 잡혀 "의상: 잠옷-A"가
            # 저장되던 문제 — resolve_element의 부분일치 폴백으로 우연히 매칭은 됐지만(부분
            # 문자열 포함), 저장되는 라벨 자체는 지저분했다. "의상:" 접두어를 벗겨 정확한
            # 라벨만 남긴다.
            label = re.sub(r"^\s*의상\s*:\s*", "", label).strip()
            if name and label:
                out[name] = label
    return out

_PLACE_LINE_RE = re.compile(r"^\s*장소\s*:\s*(.+)$", re.M)

_MOOD_LINE_RE = re.compile(r"^\s*무드\s*/\s*조명\s*:\s*(.+)$", re.M)

def _scene_single_line(text: str, pattern: "re.Pattern") -> str | None:
    """text(청크)에 그 패턴이 정확히 1번만 나오면 그 값을, 0번/2번 이상(청크가 씬 여러 개를
    걸치는 드문 경우)이면 None을 반환 — 여러 씬이 섞인 청크에서 잘못된 씬의 장소/무드를
    다른 씬 컷에 잘못 강제하는 걸 막는 안전장치(costume_map은 인물명 키라 씬이 여러 개라도
    안전하지만, 장소·무드/조명은 씬당 값이 하나뿐이라 이 안전장치가 필요하다)."""
    matches = pattern.findall(text or "")
    return matches[0].strip() if len(matches) == 1 else None

_PROP_LINE_RE = re.compile(r"^\s*소품\s*:\s*([^=\n]+?)\s*=", re.M)

def _scene_multi_value(text: str, pattern: "re.Pattern") -> list[str]:
    """text(청크)에서 그 패턴에 매칭되는 값 전부를 반환 — 다만 소품은 장소·무드/조명과 달리
    한 씬에 여러 개가 선언될 수 있어(선언 줄 개수로는 "씬 1개인지"를 판별 못 함) _scene_single_line
    처럼 "패턴이 정확히 1번"을 기준으로 삼을 수 없다. 대신 이 청크에 씬 헤더가 정확히 1개일 때만
    (=씬 하나짜리 청크) 매칭된 값 전부를 반환하고, 씬이 여러 개 걸친 청크(어느 씬 소속인지 판별
    불가)면 빈 리스트를 반환한다."""
    if len(_SCENE_HDR_RE.findall(text or "")) > 1:
        return []
    return [m.strip() for m in pattern.findall(text or "") if m.strip()]

def _replace_scene_block(conti, num, new_block):
    """콘티에서 씬 num 하나만 new_block(헤더+본문)으로 통째로 교체 — 다른 씬은 원문 그대로 유지.
    씬을 못 찾으면 원본을 그대로 반환(안전 폴백)."""
    ms = list(_SCENE_HDR_RE.finditer(conti))
    for i, m in enumerate(ms):
        if int(m.group(1)) != num:
            continue
        start, end = m.start(), (ms[i + 1].start() if i + 1 < len(ms) else len(conti))
        nb = new_block.strip()
        if not nb.startswith(("■", "*", "#")):
            nb = "■ " + nb
        return conti[:start] + nb + "\n\n" + conti[end:].lstrip("\n")
    return conti

_SCENE_NUM_RE = re.compile(r"씬\s*(\d+)\b|(\d+)\s*번째?\s*씬|(\d+)\s*번\s*씬|(\d+)\s*씬")

def _scene_num_from_instr(instr):
    """수정 지시에 씬 번호가 명시돼 있으면(예: '씬3', '3번째 씬') 그 번호를 뽑는다."""
    m = _SCENE_NUM_RE.search(instr or "")
    if not m:
        return None
    return int(next(g for g in m.groups() if g))

def _guess_scene_num(conti, instr):
    """씬 번호가 명시 안 됐을 때(예: 대사·상황 묘사로만 어느 부분인지 가리킬 때), 콘티의 씬
    전문을 LLM에 보여주고 어느 씬을 가리키는지 판단시킨다. 애매하면 None(전체 재생성 폴백).
    ★씬 본문을 짧게 자르면(예: 앞 100자) 지시가 가리키는 문장이 그 씬 뒷부분에 있을 때
    못 찾고 엉뚱한 씬으로 오판하는 버그가 있었음(2026-07-13 실측) — 그래서 전문을 다 준다."""
    scenes = _split_scenes(conti)
    if not scenes:
        return None
    listing = "\n\n".join(f"[씬{n} — {h}]\n{b}" for n, h, b in scenes)
    sys = ("다음은 상세 콘티의 씬 목록(번호·헤더·본문 전문)이다. 아래 수정 지시가 이 중 씬 "
           "하나를 가리키면(그 씬 본문에 실제로 등장하는 문장/내용을 지목한 것이면) 그 번호만 "
           "숫자로 답하라(다른 말 없이). 여러 씬에 걸치거나 애매하면 -1로 답하라.")
    user = f"[씬 목록]\n{listing}\n\n[수정 지시]\n{instr}"
    try:
        ans = generator.complete(sys, user, timeout=60)
    except Exception:
        return None
    m = re.search(r"-?\d+", ans or "")
    if not m:
        return None
    n = int(m.group(0))
    return n if any(n == s[0] for s in scenes) else None

def _do_images(channel, thread_ts, rest):
    work, bible, tail, msgs = _resolve_work_bible(channel, thread_ts, rest)
    if not _require_genre(channel, thread_ts, work):
        return
    tm = re.search(r"\b(\d{1,3})\b", tail)          # [이미지] <작품> N → 목표 컷 수
    target = int(tm.group(1)) if tm else None
    epm = re.search(r"(\d{1,3})\s*[화회]", tail)
    episode = int(epm.group(1)) if epm else (conti_state.get_episode(thread_ts) or {}).get("episode")
    # 콘티를 사용자에게 "보여주는" 요청이 아니라 이미지 렌더링을 진행하기 위해 텍스트만 가져오는
    # 내부 조회다 — announce=True로 두면 매 [이미지] 호출마다 노션 토글을 불필요하게 재기록/재아카이브함
    conti = _thread_or_saved_conti(channel, thread_ts, msgs, work, episode, announce=False)
    if not conti:
        _reply(channel, thread_ts, "먼저 `[스토리보드] <작품>`로 씬 설계·상세 콘티를 만든 뒤, 이 스레드에서 `[이미지]`를 쳐주세요.")
        return
    # ★2026-07-15, "그림체가 너무 달라졌어" 버그: 이 그리드 경로만 style_suffix 없이 호출돼서
    # STILL_STYLE(세미리얼 지정)이 하나도 안 붙었다 — [스틸컷] 경로만 스타일이 고정되고 이
    # [이미지] 그리드 경로는 모델 기본값에 맡겨져 매번 다른 화풍(사진/애니메 등)으로 나왔다.
    _render_cuts_tracked("images", rest, channel, thread_ts, work, bible, conti, target=target,
                        title="스토리보드 그리드", filename=f"storyboard_{work or 'ep'}.png",
                        style_suffix=_style_for_work(work))

STILL_CUTS_DEFAULT = 4    # 스틸컷은 기본 4컷·2x2 그리드 고정(스크린샷 레퍼런스와 동일)

STILL_ASPECT = "9:16"     # 스틸컷은 세로 9:16 고정

# ★2026-07-20: 씬(그룹)·옷·텍스트 관련 공통 규칙 — 렌더링 화풍(realistic/2d_anim 등)과
# 무관하게 모든 스타일 프리셋이 공유해야 하는 지시라 렌더링 화풍 서술과 분리해뒀다.
_STYLE_COMMON_SUFFIX = (
    # ★2026-07-14, "씬끼리 장소랑 옷 일관성이 안 맞음" 피드백 — 씬(그룹) 경계에서는
    # prev_png 체이닝이 리셋돼서(다른 씬 사진이 섞이면 안 되니까) 참조 이미지만으로
    # 옷차림·장소를 유지해야 하는데, 텍스트 프롬프트에 명시적 지시가 없으면 모델이
    # 매번 옷·배경 디테일을 임의로 새로 그려버렸다. 참조 이미지에 있는 옷차림·헤어·
    # 장소를 그대로 따르게 명시 지시를 추가.
    "Character clothing, hairstyle, and styling must exactly match the reference "
    "image(s) provided — do not invent different outfits or hairstyles. If a place "
    "reference image is provided, match that location's environment, colors, and "
    "objects closely rather than reimagining a new-looking space. "
    # ★2026-07-15: 실측 사고 — 대사가 있는 컷에서 그 대사가 화면 안에 자막/캡션
    # 글자로 렌더링됨(그리드의 no_text는 그리드 자체의 캡션바 장식만 끄는 옵션이라
    # 이 문제와 무관). 이미지 자체에 텍스트를 넣지 말라는 지시가 어디에도 없었어서
    # 명시적으로 금지한다.
    "No text, letters, captions, subtitles, speech bubbles, or written words should "
    "appear anywhere in the image — render a pure illustration with no on-screen "
    "text of any kind."
)

# ★2026-07-20 "작품마다 그림체를 다르게 쓰고 싶다" — 실사풍(기존 기본값)/2D 애니메이션 중
# 작품별로 고르게 한다. works.get_style(work)에 저장된 style_key로 STYLE_PRESETS를 찾고,
# 없거나(=미지정) 모르는 값이면 항상 "realistic"(기존 STILL_STYLE과 완전히 동일한 문구,
# 하위호환)로 폴백한다.
STYLE_PRESETS = {
    "realistic": (
        "semi-realistic illustration style, painterly rendering, cinematic still, "
        "natural relaxed facial expression, not stiff or uncanny, "
        "clearly a stylized illustration, not a photograph, "
        "not resembling any real celebrity or public figure. "
        f"{_IDEALIZED_FACE_GUIDANCE} "
        f"{_STYLE_COMMON_SUFFIX}"
    ),
    # ★2026-07-20b "2D 애니메이션이면 이미지·영상 전부 재패니메이션(일본 애니) 스타일로" —
    # 기존 문구가 그냥 "2D cartoon"이라 서구식 카툰/플랫디자인으로도 해석될 여지가 있었다.
    # 일본 애니메이션 특유의 요소(셀 음영, 굵은 라인, 크고 표현력 있는 눈, 애니 특유의 인체
    # 비례·헤어 렌더링)를 구체적으로 못박아 "그냥 2D"가 아니라 "일본 애니메"로 명확히 좁힌다.
    "2d_anim": (
        "authentic Japanese anime art style (2D anime/manga-inspired illustration), the visual "
        "style of a modern Japanese TV anime — flat cel shading with sharp shadow shapes (not "
        "soft painterly gradients), bold clean black linework/outlines, vibrant saturated flat "
        "color fills, large expressive anime-style eyes with detailed iris highlights, simplified "
        "anime facial structure (small nose/mouth, defined jawline), anime-style hair rendered as "
        "clean flowing strand clumps with glossy highlight streaks, anime body proportions. "
        "Absolutely not photorealistic, not 3D-rendered/CGI, not a western flat-vector cartoon, "
        "not painterly/watercolor, no realistic skin pores or texture, no photographic lighting or "
        "camera lens effects (no depth-of-field blur, no film grain), no live-action look. "
        "This must look like a frame taken directly from a professionally animated Japanese anime "
        "series, not a Western animation, not a photograph, "
        "not resembling any real celebrity or public figure. "
        f"{_ANIME_FACE_GUIDANCE} "
        f"{_STYLE_COMMON_SUFFIX}"
    ),
}

DEFAULT_STYLE_KEY = "realistic"


def _style_for_work(work: str | None) -> str:
    """그 작품에 등록된 스타일(works.get_style)을 찾아 STYLE_PRESETS 문구로 변환. 작품이
    없거나 스타일 미지정·모르는 키면 기본값(realistic)으로 폴백 — 항상 문자열을 반환한다."""
    key = (work and works.get_style(work)) or DEFAULT_STYLE_KEY
    return STYLE_PRESETS.get(key, STYLE_PRESETS[DEFAULT_STYLE_KEY])


STILL_STYLE = STYLE_PRESETS[DEFAULT_STYLE_KEY]   # 하위호환 — 옛 호출부가 참조해도 기존 기본값 그대로

# ★2026-07-20: 인물이 아닌 요소(소품/의상) 참조샷 프롬프트가 "Semi-realistic painterly
# illustration"을 하드코딩하고 있었다 — 2d_anim 작품은 이 참조샷만 다른 화풍으로 나와 실제
# 스틸컷과 어긋나므로(2026-07-15에 costume이 딱 이 문제로 한 번 고쳐진 전례가 있다), 요소
# 참조샷에도 작품별 화풍이 반영되게 짧은 문구를 스타일별로 분리해뒀다.
_ELEMENT_REF_STYLE_PHRASE = {
    "realistic": "Semi-realistic painterly illustration",
    # ★2026-07-20b: "Clean 2D... anime/cartoon"처럼 anime/cartoon을 병기하면 생성기가 둘 중
    # 아무거나(서구식 플랫 카툰 포함) 골라도 되는 것처럼 읽을 수 있어 "cartoon" 병기를 빼고
    # 일본 애니메 화풍임을 단정적으로 못박는다.
    "2d_anim": "Authentic Japanese anime-style, flat cel-shaded 2D illustration",
}


def _element_ref_style_phrase(work: str | None) -> str:
    key = (work and works.get_style(work)) or DEFAULT_STYLE_KEY
    return _ELEMENT_REF_STYLE_PHRASE.get(key, _ELEMENT_REF_STYLE_PHRASE[DEFAULT_STYLE_KEY])


def _face_guidance_for_work(work: str | None) -> str:
    """인물(person) 참조는 STYLE_PRESETS를 안 거치고 _IDEALIZED_FACE_GUIDANCE를 직접 참조하고
    있었다 — 작품이 2d_anim이어도 항상 "60% 리얼리즘/페인터리" 지침이 그대로 박혀 실사풍
    인물 참조가 나오는 화풍 불일치가 있었다(★2026-07-20b). 작품 스타일에 맞는 이상화 지침을
    고른다."""
    key = (work and works.get_style(work)) or DEFAULT_STYLE_KEY
    return _ANIME_FACE_GUIDANCE if key == "2d_anim" else _IDEALIZED_FACE_GUIDANCE

# ★2026-07-20: 영상화 style_lock(_generate_video_for_cut)의 강조 단정문 — STYLE_PRESETS와
# 짝을 맞춘 스타일별 버전. "제로 포토리얼/제로 3D" 같은 부정 나열이 스타일마다 달라야
# 하므로 별도 dict로 둔다(realistic 문구는 기존 그대로, 하위호환).
_VIDEO_STYLE_LOCK_EMPHASIS = {
    "realistic": (
        "The entire video must match this exact semi-realistic painterly illustration art "
        "style, color palette, and rendering technique of the input reference image from the "
        "first frame to the last — zero photorealistic rendering, zero live-action video look, "
        "zero 3D-rendered or CGI look, zero realistic skin pores/texture or realistic film "
        "lighting not present in the reference image. Every frame must show visible painterly "
        "brushwork, soft painterly color gradients, and clean illustrated line quality, "
        "consistent with a hand-painted illustration, not a filmed photograph."
    ),
    # ★2026-07-20b: 일본 애니메 화풍임을 명시적으로 못박는다 — "2D cartoon" 표현만으로는
    # 서구식 플랫 카툰(예: 심슨풍)으로 드리프트할 여지가 있어, 매 프레임 유지해야 할 구체
    # 요소(셀 음영·굵은 라인·큰 눈·애니 헤어 하이라이트)까지 명시한다.
    "2d_anim": (
        "The entire video must match this exact authentic Japanese anime art style (2D "
        "anime/manga-inspired illustration, the look of a modern Japanese TV anime), flat "
        "cel-shaded color fills with sharp shadow shapes, bold clean black linework, large "
        "expressive anime eyes with highlight reflections, and anime-style glossy hair rendering "
        "of the input reference image from the first frame to the last — zero photorealistic "
        "rendering, zero live-action video look, zero 3D-rendered or CGI look, zero western "
        "flat-vector cartoon look, zero painterly brushwork or soft photographic gradients, zero "
        "realistic skin texture or film lighting, no camera depth-of-field blur, no film grain. "
        "Every frame must look like a frame taken directly from a professionally animated "
        "Japanese anime series, not a painting, not a Western cartoon, and not a photograph."
    ),
}

_PENDING_STILL: dict[str, dict] = {}   # 버튼 메시지 ts -> {work, scene_num, title, rest, grid_png}

_PENDING_STILL_REGEN_ASK: dict[str, dict] = {}

_PENDING_ELEMENT_REGEN_ASK: dict[str, dict] = {}

_PENDING_PLAN_REGEN_ASK: dict[str, dict] = {}

def _still_buttons_blocks():
    return [{
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ 확정 저장"},
             "style": "primary", "action_id": "still_confirm"},
            {"type": "button", "text": {"type": "plain_text", "text": "🔄 재생성"},
             "style": "danger", "action_id": "still_regen"},
            # ★2026-07-20 "안전필터 안 걸린 스틸컷도 그냥 피그마로 보내고 싶다" — 실패 시에만
            # 붙던 버튼(_figma_send_blocks)과 별개로, 정상 생성된 스틸컷 배치에도 항상 붙여서
            # 실패 여부와 무관하게 아무 컷이나 손보고 싶을 때 쓸 수 있게 한다.
            {"type": "button", "text": {"type": "plain_text", "text": "🎨 피그마로 보내기"},
             "action_id": "figma_send_stillbatch"},
        ],
    }]

def _post_still_buttons(channel, thread_ts, work, scene_num, title, rest, grid_png, cuts=None,
                        scene_seconds=None):
    # ★2026-07-15: 배치 분할 시 버튼 메시지가 4개 연속으로 뒤섞여 올라와 "어느 그리드 버튼인지
    # 헷갈림" 실사용자 불만 → 버튼 카드에 title(컷 범위 포함)을 노출해 위 그리드와 시각적으로 묶는다.
    label = title[:40] if title and len(title) > 40 else title
    text = f"'{label}' — 확정하면 이 작품의 visual-pipeline 프로젝트에 저장돼요."
    resp = app.client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=text, blocks=_with_text_block(text, _still_buttons_blocks()))
    entry = {"work": work, "scene_num": scene_num, "title": title, "rest": rest, "grid_png": grid_png,
             "cuts": cuts or [], "scene_seconds": scene_seconds}
    _PENDING_STILL[resp["ts"]] = entry
    still_state.set_last(thread_ts, work, scene_num, rest)   # 재시작에도 남는 영구 기록

_PENDING_VIDEO: dict[str, dict] = {}   # 버튼 메시지 ts -> {work, scene_num, title, grid_png}

def _post_video_button(channel, thread_ts, p):
    """확정 저장된 스틸컷 아래에 '어느 컷을 영상화할지' 고르는 드롭다운을 추가로 게시.
    cuts가 없으면(옛 그리드/폴백 경로라 개별 컷 PNG가 없으면) 조용히 생략.
    seedance 1건은 크레딧이 드니 실제 선택·클릭 시에만 실행."""
    cuts = p.get("cuts") or []
    if not hf_video.available() or not cuts:
        return
    text = f"이 씬의 어느 컷을 영상으로 만들까요? ({hf_video.APPLICATION} — 크레딧 소모됩니다)"
    options = [{"text": {"type": "plain_text", "text": f"컷 {c['n']} — {(c['caption'] or '')[:40]}"},
               "value": str(c["n"])} for c in cuts][:100]
    # ★2026-07-15: "복수선택할 수 있게 해줘" 요청 — 컷을 하나씩 눌러 영상화하거나 "전체 N컷"
    # 버튼만 있던 걸, 원하는 컷 여러 개를 골라 한 번에 영상화할 수 있게 static_select를
    # multi_static_select로 교체(하나만 골라도 기존 단일 선택과 동일하게 동작하므로 완전 대체).
    # block_id는 메시지 내에서만 유일하면 되므로 고정 문자열 사용 — 이후 확인 버튼 핸들러가
    # body["state"]["values"][block_id][action_id]["selected_options"]로 현재 선택 상태를 읽음.
    # ★2026-07-15: multi_static_select 블록이 Slack API에서 "invalid_blocks: unsupported
    # element: multiselect"로 거부되는 사고가 실측됨(원인 불명 — 이 계정/앱 설정에서 이 element
    # type이 막힌 것으로 보임, 정상적인 Block Kit 스펙상 유효한 타입인데도 거부됨). 이 예외를
    # 안 잡아서 _handle 전체가 죽어 사용자에게 아무 응답도 안 가는 게 실제 문제였다("씬1 영상화"
    # 명령이 반응 없음). multi_static_select 시도 실패 시, static_select(단일 선택)로 안전
    # 폴백해서 최소한 컷 선택 기능은 계속 동작하고 사용자에게 응답이 반드시 가게 한다.
    def _post(elements):
        return app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=text,
            blocks=_with_text_block(text, [{
                "type": "actions", "block_id": "video_multi_select_block", "elements": elements,
            }]))
    multi_elements = [{"type": "multi_static_select",
                       "placeholder": {"type": "plain_text", "text": "컷 선택(복수 가능)"},
                       "options": options, "action_id": "video_pick_cuts_multi"},
                      {"type": "button", "text": {"type": "plain_text", "text": "🎬 선택 컷 영상화"},
                       "action_id": "video_pick_cuts_confirm"},
                      {"type": "button",
                       "text": {"type": "plain_text", "text": f"🎬 전체 {len(cuts)}컷 영상화"},
                       "action_id": "video_all_cuts"}]
    try:
        resp = _post(multi_elements)
    except Exception:
        log.exception("영상화 컷 선택 드롭다운(multi_static_select) 게시 실패 — static_select로 폴백")
        single_elements = [{"type": "static_select",
                            "placeholder": {"type": "plain_text", "text": "컷 선택(1개)"},
                            "options": options, "action_id": "video_pick_cuts_multi"},
                           {"type": "button", "text": {"type": "plain_text", "text": "🎬 선택 컷 영상화"},
                            "action_id": "video_pick_cuts_confirm"},
                           {"type": "button",
                            "text": {"type": "plain_text", "text": f"🎬 전체 {len(cuts)}컷 영상화"},
                            "action_id": "video_all_cuts"}]
        resp = _post(single_elements)
    _PENDING_VIDEO[resp["ts"]] = {"work": p["work"], "title": p["title"], "cuts": cuts,
                                  "scene_seconds": p.get("scene_seconds")}

_PENDING_VIDEO_CONFIRM: dict[str, dict] = {}   # 미등록 경고 메시지 ts -> {work,title,cut,num,scene_seconds}

def _unregistered_mentions(work, cut):
    mentions = (list(cut.get("characters") or []) + list(cut.get("places") or [])
               + list(cut.get("props") or []))
    seen, out = set(), []
    for m in mentions:
        if m in seen:
            continue
        seen.add(m)
        if not oi.resolve_element(work, m):
            out.append(m)
    return out

def _maybe_generate_video_for_cut(channel, thread_ts, work, title, cut, num, scene_seconds):
    """영상화 전 안전장치 — 이 컷에 등장하는 인물/장소 중 미등록이 있으면 먼저 경고하고
    확인 버튼을 눌러야 실제 생성이 진행되게 한다(2026-07-14, 실무자 요청 — 참조 일관성
    없이 크레딧부터 쓰는 걸 막기 위함)."""
    missing = _unregistered_mentions(work, cut)
    if not missing:
        _generate_video_for_cut_with_safety_retry(channel, thread_ts, work, title, cut, num, scene_seconds)
        return
    lines = "\n".join(f"· {m}" for m in missing)
    text = (f"⚠️ 이 컷에 아직 등록 안 된 인물/장소가 있어요 — 등록 안 하면 영상화에서 그 "
           f"대상의 참조 일관성이 안 잡혀요:\n{lines}\n\n그래도 만드시겠어요?")
    resp = app.client.chat_postMessage(
        channel=channel, thread_ts=thread_ts, text=text,
        blocks=_with_text_block(text, [{
            "type": "actions",
            "elements": [{"type": "button", "text": {"type": "plain_text", "text": "🎬 영상 생성"},
                         "style": "primary", "action_id": "video_confirm_unregistered"}],
        }]))
    _PENDING_VIDEO_CONFIRM[resp["ts"]] = {"work": work, "title": title, "cut": cut, "num": num,
                                          "scene_seconds": scene_seconds}

@app.action("video_confirm_unregistered")
def _act_video_confirm_unregistered(ack, body):
    ack()
    msg_ts = body["message"]["ts"]
    p = _PENDING_VIDEO_CONFIRM.pop(msg_ts, None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요)."); return
    _disable_buttons(body, f"🎬 컷 {p['num']} 영상 생성 중… (수 분 소요될 수 있어요)")
    ch, tts = _action_ctx(body)
    _generate_video_for_cut_with_safety_retry(ch, tts, p["work"], p["title"], p["cut"], p["num"], p["scene_seconds"])


def _generate_video_for_cut_with_safety_retry(channel, thread_ts, work, title, cut, num, scene_seconds):
    """★2026-07-16: 사용자 리포트 — "안전 필터 걸려서 안됨" 반복. 이 필터
    (InputImageSensitiveContentDetected.PrivacyInformation)는 모션 프롬프트 텍스트가 아니라
    입력 이미지(그 컷의 확정 스틸컷)가 실사 인물처럼 보인다고 판단해서 걸리므로, 텍스트만
    고쳐서는 그냥 똑같이 걸린다 — 스틸컷 그림체를 더 뚜렷한 일러스트/페인터리로 재생성해
    참조 이미지 자체를 바꿔야 한다. 자동주행 경로(_autopilot_videos_for_scene)에는 이미
    있던 이 재시도 로직을 수동 "영상 만들어줘" 경로에도 동일하게 적용 — 수동 경로만 안 붙어
    있어서 사용자가 계속 필터에 걸린 채 남아있었다."""
    cost_out: dict = {}
    fail_out: dict = {}
    local_path = _generate_video_for_cut(channel, thread_ts, work, title, cut, num, scene_seconds,
                                         post_result=False, cost_out=cost_out, fail_reason_out=fail_out)
    if not local_path and fail_out.get("reason") == "입력 이미지가 실존 인물처럼 보인다는 안전필터에 걸림":
        _reply(channel, thread_ts, f"⚠️ 컷 {num}: 실존 인물 안전필터에 걸렸어요 — 스틸컷을 더 "
                                   "일러스트풍으로 다시 만들어서 재시도할게요…")
        new_png = _autopilot_regen_shot_png(
            work, cut, "실제 인물 사진처럼 보이지 않게, 명확한 일러스트/페인터리 그림체로 "
                       "(사실적 피부 질감·실사 조명 최소화)")
        if new_png:
            cut["png"] = new_png
            retry_cost_out: dict = {}
            retry_fail_out: dict = {}
            local_path = _generate_video_for_cut(channel, thread_ts, work, title, cut, num, scene_seconds,
                                                 post_result=False, cost_out=retry_cost_out,
                                                 fail_reason_out=retry_fail_out)
            if local_path:
                cost_out = retry_cost_out
            else:
                fail_out = retry_fail_out  # 최신 실패 사유로 갱신(격자 폴백 판단용)
        # ★2026-07-20 재스타일화로도 여전히 실존인물 안전필터면, 마지막 자동 폴백으로 얼굴을
        # 빨간 격자로 덮어(원본은 .orig.bak 백업) 딱 한 번 더 재시도한다. 격자 스틸은
        # _generate_video_for_cut이 마커(.orig.bak)를 보고 프롬프트 앞줄 앵커 + 앞 0.1초 트림을
        # 자동 적용한다. opencv 미설치 등으로 격자 적용이 실패하면 조용히 넘어간다(기존 실패 안내로 종결).
        if not local_path and fail_out.get("reason") == "입력 이미지가 실존 인물처럼 보인다는 안전필터에 걸림":
            grid_png = None
            try:
                from bot import face_grid
                grid_png = face_grid.overlay_grid(cut["png"])
            except Exception:
                log.exception("격자 자동 적용 실패 — 기존 실패 안내로 종결")
            if grid_png:
                _gs_m = re.search(r"씬(\d+)", title)
                _gs = int(_gs_m.group(1)) if _gs_m else None
                _gep = (conti_state.get_episode(thread_ts) or {}).get("episode")
                vp_store.overwrite_still_with_backup(
                    work, scene_num=_gs, cut_num=num, episode=_gep,
                    new_png=grid_png, original_png=cut["png"])
                cut["png"] = grid_png
                _reply(channel, thread_ts, f"🔴 컷 {num}: 재스타일화로도 안전필터에 걸려서, 얼굴에 "
                                           "격자를 덮어 마지막으로 다시 영상화해볼게요…")
                grid_cost_out: dict = {}
                local_path = _generate_video_for_cut(channel, thread_ts, work, title, cut, num, scene_seconds,
                                                     post_result=False, cost_out=grid_cost_out)
                if local_path:
                    cost_out = grid_cost_out
    _post_generated_video(channel, thread_ts, work, title, num, local_path, cost_out.get("cost"))

def _post_generated_video(channel, thread_ts, work, title, num, local_path, cost):
    """영상 생성 결과 1건을 슬랙에 게시(파일 업로드 또는 실패 안내) — ★2026-07-15
    "얘는 왜 영상 두개 만든거임?" 실사용자 리포트로 발견된 이중 게시·이중 과금 버그 수정의
    일환으로 _generate_video_for_cut 본문에서 뽑아냄(아래 post_result 설명 참고)."""
    cost_s = f" · 생성비 ~${cost:.2f}" if cost else ""
    if local_path:
        app.client.files_upload_v2(
            channel=channel, thread_ts=thread_ts, file=local_path,
            filename=f"{_work_safe_name(work)}_{title.replace(' ', '')}_컷{num}.mp4",
            initial_comment=f"✅ 영상 생성 완료 — <{work}> {title} 컷{num}{cost_s}")
    else:
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"⚠️ 영상 생성은 완료됐지만 로컬 다운로드에 실패해 슬랙에 못 올렸어요 — <{work}> {title} 컷{num}. 재생성해주세요.")

def _generate_video_for_cut(channel, thread_ts, work, title, cut, num, scene_seconds,
                            post_confirm_buttons: bool = True,
                            post_result: bool = True, cost_out: dict | None = None,
                            fail_reason_out: dict | None = None) -> str | None:
    """선택된 컷 하나 → 실제 영상화 호출 + 결과 게시. 드롭다운 클릭·직접지정 경로 공용.
    반환: 로컬로 저장된 영상 경로(실패/다운로드 실패면 None).

    post_confirm_buttons: ★2026-07-15 자동주행 한정 — 자동주행은 컷마다 사람이 확인/재생성
    누를 일이 설계상 없는데(사람 확인은 합본 단계에서만), 이 버튼이 매번 달려서 누가 실수로
    [🔄 재생성]을 누르면 자동주행과 별개로 그 컷이 중복 생성되는 사고 위험이 있었다(리뷰에서
    지적된 실제 위험). 자동주행 호출부(_autopilot_videos_for_scene)만 False로 넘겨 버튼
    자체를 안 달리게 한다 — 매뉴얼 경로는 기본값 True로 기존 동작 그대로 유지.

    post_result/cost_out: ★2026-07-15 "얘는 왜 영상 두개 만든거임?" 실사용자 리포트 —
    자동주행의 일관성 재검사가 verdict=="no"일 때 이 함수를 컷당 두 번째로 다시 호출하는데,
    이 함수가 호출될 때마다 무조건 슬랙에 올리고 과금 로그도 남겨서, 재검사 결과 버려질
    "1차 시도"까지 완료 메시지+비용이 그대로 게시/청구돼 같은 컷의 영상이 두 번 나오고
    비용도 두 배로 잡혔다. post_result=False면 게시를 건너뛰고 cost_out(dict)에 실제 비용만
    담아둔다 — 호출부가 최종적으로 "이긴" 시도 하나만 골라 _post_generated_video로 한 번만
    게시하게 하기 위함."""
    # cut['prompt']/cut['caption']은 그 컷이 나온 콘티(=확정된 최종 대본) 문장에서 그대로 나온
    # 값이라, 별도 재작성 없이 그대로 모션 프롬프트에 반영하면 최종 대본 내용이 자연히 담긴다.
    # <<<element_id>>> 태깅은 실제 프로덕션 스토리보드(Downloads/_에피소드01/스토리보드.md)의
    # 표기 관례 — 등록된 인물뿐 아니라 장소·소품 엘리먼트도 같은 방식으로 참조 유지.
    mentions = (list(cut.get("characters") or []) + list(cut.get("places") or [])
               + list(cut.get("props") or []))
    seen_ids, tag_list = set(), []
    for m in mentions:
        e = oi.resolve_element(work, m)
        if e and e.get("id") and e["id"] not in seen_ids:
            seen_ids.add(e["id"])
            tag_list.append(f"<<<{e['id']}>>>")
    tags = " ".join(tag_list)
    # scene_text(콘티 원문, 대사·지문 포함)를 그대로 붙여서 caption(짧은 요약)만으론 못 담는
    # 디테일(대사 원문·타이밍·연기 뉘앙스)까지 모션 프롬프트에 반영되게 한다
    # (2026-07-13, "영상 만들 때 상세 콘티는 참고 안 한다" 이슈 수정).
    scene_text = (cut.get("scene_text") or "")[:1200]
    # 실무자 피드백(2026-07-13): ①표정 변화가 과함(부자연스러움) ②콘티상 같은 시간·장소인데
    # 배경/조명/구도가 바뀌는 것처럼 반영 안 됨 — 둘 다 모션 프롬프트에 명시적 제약으로 추가.
    # ★2026-07-14: "이 컷의 단일 순간만 애니메이션"이라는 예전 제약이, 콘티가 카메라 무빙으로
    # 서술한 진행 동작(예: 다가서다가 → 눈이 마주친다)을 못 그리고 어색한 정지 화면처럼 나오게
    # 했다 — caption에 담긴 진행을 그대로 애니메이션하되, 카메라/장소/조명은 이 컷 안에서
    # 안 바뀐다(=다른 컷으로 넘어가는 게 아니다)는 제약만 유지한다.
    # ★2026-07-15: 사용자 피드백("영상마다 그림체가 너무 달라 이것도 통일하게 조여") — 스틸컷은
    # STILL_STYLE이 매번 동일 텍스트로 붙어 스타일이 일관되는데(확인됨), 영상은 같은 모델
    # 호출인데도 결과 그림체가 컷마다 들쭉날쭉하다는 실사용 리포트. seedance API
    # (openrouter_video.py 상단 주석, /api/v1/videos/models 실측 기준)에는 스타일 보존
    # 강도를 조절하는 숫자 파라미터(strength/cfg_scale/image_strength류)가 아예 없다 —
    # allowed_passthrough_parameters는 watermark/req_key뿐(모더레이션 문맥이지만 이 모델이
    # 노출하는 파라미터 전체가 이게 다라는 뜻이기도 함). 그래서 API 파라미터로 조일 수는
    # 없고, 텍스트 프롬프트 구조로만 조정 가능 — 기존엔 스타일 지시(STILL_STYLE)가 최대
    # 1200자짜리 scene_text 뒤, 프롬프트 맨 끝에 붙어 있어서(모델이 앞쪽 토큰에 더 가중치를
    # 두는 경향이 있다면) 씬 대사·지문 분량에 묻혀 우선순위가 밀렸을 가능성이 있다. 그래서
    # 스타일 보존 지시를 tags/액션 설명 바로 뒤, scene_text보다 앞으로 옮기고, "입력 레퍼런스
    # 이미지의 그림체를 그대로 유지하고 포토리얼로 드리프트하지 말라"는 명시적 문구를 추가한다.
    # ★2026-07-15: 사용자가 공유한 Seedance 2.0 프롬프팅 가이드(higgsfield.ai 블로그)의
    # 구체 기법을 반영해 문구를 추가 강화 — (1) "preserve X" 대신 "the entire video must
    # match this exact style" 형태의 강한 단정문, (2) 막연한 "포토리얼로 가지 마라"가 아니라
    # 구체적으로 원치 않는 결과물을 나열하는 명시적 금지 목록(가이드의 "zero 3D, zero CGI"와
    # 같은 구체성), (3) STILL_STYLE의 기존 어휘(semi-realistic painterly illustration)에
    # 부합하는 구체적 렌더링 질감 키워드(붓터치·페인터리 그라데이션·일러스트 라인) 명시.
    # ★2026-07-20: 이 강조문 자체도 "semi-realistic painterly"를 못박고 있어서, 2D 애니메이션
    # 스타일 작품에 그대로 쓰면 오히려 스틸컷 스타일과 모순되는 지시가 된다 — 스타일별로 이
    # 강조 문구도 나눠서 그 작품이 실제로 쓰는 화풍과 일치하는 단정문을 넣는다.
    style_lock_emphasis = _VIDEO_STYLE_LOCK_EMPHASIS.get(
        (work and works.get_style(work)) or DEFAULT_STYLE_KEY,
        _VIDEO_STYLE_LOCK_EMPHASIS[DEFAULT_STYLE_KEY])
    style_lock = f"{_style_for_work(work)} {style_lock_emphasis}"
    # ★2026-07-15: 사용자 리포트 — 영상이 참조 스틸컷과 아예 다르게 나옴(머리색·옷·배경 전부
    # 다른 인물/장면으로 생성됨). 코드가 실제 컷 PNG를 input_references로 정확히 넘기는 건
    # 확인했지만(shot_refs·cut["png"] 매칭 로직 정상), 그 사실을 프롬프트 텍스트가 뒷받침 안
    # 해주면 모델이 텍스트 쪽으로 더 끌려갈 수 있다 — 참조 이미지를 "정확히 유지해야 할 시작
    # 프레임"이라고 못박는 지시를 프롬프트 맨 앞(다른 모든 지시보다 먼저)에 둬서 이미지 참조의
    # 가중치를 높인다(사용자 선택: "참조 이미지 설명을 프롬프트 앞으로").
    ref_lock = ("The provided reference image is the exact first frame of this video — do not "
                "change the character's face, hair color/style, clothing, or background/setting "
                "shown in that reference image. Only animate the motion described below; every "
                "visual element not explicitly described as changing must stay identical to the "
                "reference image throughout the video. ")
    # ★2026-07-15: 사용자 피드백 — "카메라가 계속 클로즈업을 하는게 어색함, 엥간하면 카메라가
    # 고정되게" — seedance가 컷 프레이밍(예: 미디엄샷)을 무시하고 자체적으로 클로즈업까지
    # push-in하는 경향이 있어 부자연스러웠다. 카메라를 기본적으로 고정(락다운)하도록 지시하고,
    # 컷 자체 지시(cut['prompt']의 구도 헤더)에 명시적 카메라 이동이 있을 때만 예외로 둔다.
    camera_lock = ("Keep the camera static/locked in place by default — do not push in, zoom, "
                   "dolly, or pan unless the shot description below explicitly calls for camera "
                   "movement. Maintain the exact framing/shot size (e.g., medium shot stays medium "
                   "shot) from the first frame throughout — do not drift into a closer shot on your "
                   "own. ")
    # ★2026-07-16: 사용자 요청 — 영상 생성이 계속 실존인물 안전필터(InputImageSensitiveContentDetected.
    # PrivacyInformation)에 걸려서 실패. "실존 인물이 아니라 완전한 가상 인물"임을 명시하는 문구를
    # 프롬프트 맨 앞(ref_lock보다도 먼저)에 추가해 재시도 — 사용자가 문구를 더 구체적으로 확장.
    fiction_lock = (
        "An entirely fictional adult character, created for an original fictional drama.\n"
        "The character is not based on, associated with, or intended to resemble any real person,\n"
        "celebrity, public figure, or private individual.\n"
        "Stylized cinematic realism, clearly fictional digital character. "
    )
    # ★2026-07-20: 얼굴이 안전필터에 걸려 face_grid로 얼굴을 빨간 격자로 덮어 승인한 컷 한정
    # (스틸 옆 .orig.bak 백업으로 판별), 그 격자 스틸이 "승인된 시작 프레임"임을 프롬프트
    # 맨 앞줄에 못박는다 — 나머지 프롬프트는 그대로 두고 이 한 줄만 앞에 추가. 이 격자 첫
    # 프레임은 생성 후 앞 0.1초를 잘라 최종 영상에는 비치지 않게 한다(저장부 참조).
    _grid_scene_m = re.search(r"씬(\d+)", title)
    _grid_scene = int(_grid_scene_m.group(1)) if _grid_scene_m else None
    _grid_ep = (conti_state.get_episode(thread_ts) or {}).get("episode")
    is_grid_cut = vp_store.still_has_grid_backup(
        work, scene_num=_grid_scene, cut_num=num, episode=_grid_ep)
    grid_anchor = (
        f"<<<cut{num}.png>>> is the clean approved start frame and must remain the exact "
        f"identity, costume, location, lighting, and screen-direction anchor.\n"
        if is_grid_cut else "")
    motion_prompt = (f"{grid_anchor}{fiction_lock}{ref_lock}{camera_lock}{tags} {cut['prompt']}. Scene action: {cut['caption']}. "
                     f"{style_lock} "
                     f"Full scene script (Korean, for context/dialogue/emotion — animate only "
                     f"this cut's action, not other cuts in the scene): {scene_text}. "
                     f"Facial expression must stay subtle and natural — very minor, gradual "
                     f"expression change only, no sudden or exaggerated emotional shifts. "
                     f"If the scene action describes a short continuous progression (e.g., "
                     f"reaching for something and then making eye contact), animate that "
                     f"progression naturally in sequence — this is one continuous shot, no camera "
                     f"cut, so keep the exact same location, lighting, camera framing and "
                     f"background throughout, no scene change or transition to a different setup."
                     ).strip()
    # ★영상화도 job_ledger에 등록 — 안 그러면 auto_pull.sh의 busy-gate(jobs.json 확인)가
    # "생성 중"을 못 알아채고, 영상 생성 도중 재시작이 그대로 끼어들어 끊길 수 있었다
    # (2026-07-14, 게이트 빈틈으로 발견 — 이미지/스틸컷만 등록되고 있었음).
    jid = job_ledger.start_job("video", channel, thread_ts, f"{work}|{title}|컷{num}")
    try:
        # ★2026-07-15(체이닝 폐지): 직전 컷 영상의 마지막 프레임을 다음 컷 시작 이미지로 쓰던
        # 체이닝(2026-07-14 도입, 컷 사이 전환을 매끄럽게 하려는 목적)을 없앤다 — 영상 생성이
        # 첫 프레임을 고정해도(a356d44) 그 뒤로는 미세한 드리프트가 생기는데, 체이닝은 그
        # 드리프트를 다음 컷, 또 그다음 컷으로 계속 누적시켜서 뒤로 갈수록 확정 스틸컷과 점점
        # 어긋나는 문제가 있었다(사용자 실측: "컷2는 맞는데 컷3부터 지맘대로임"). 사용자 선택:
        # 전환의 매끄러움보다 각 컷이 그 컷 자신의 확정 스틸컷과 정확히 일치하는 게 더 중요 —
        # 항상 그 컷 자신의 스틸을 시작 이미지로 쓴다(prev_last_frame 인자는 호출부 정리 전까지
        # 하위호환으로 받되 더 이상 쓰지 않음).
        seed_png = cut["png"]
        # ★2026-07-15: 드롭다운으로 컷 하나만 골라 영상화하는 이 경로는 scene_seconds(씬 헤더의
        # "전체" 길이, 예: 28초)를 그대로 duration에 넘겨서 seedance 허용 범위(4~15초)를 넘기면
        # "Duration 28s is not supported" 400으로 실패했다 — _generate_videos_for_cuts(전체 영상화
        # 경로)는 이미 컷 자신의 duration(또는 캡션 추정)으로 클램프해서 쓰는데 이 단일 컷 경로만
        # 안 그러고 있었다. 같은 로직으로 통일: 컷 자신의 duration 우선, 없으면 캡션 기반 추정.
        planned = cut.get("duration")
        cut_seconds = (max(4.0, min(15.0, float(planned)))
                      if isinstance(planned, (int, float)) and planned > 0
                      else _estimate_cut_seconds(cut.get("caption") or ""))
        # ★2026-07-15: 대사 없는 컷은 seedance 자체 생성 오디오(OUTPUT audio) 민감 콘텐츠
        # 오탐으로 생성 전체가 실패하는 사례가 있어(위 _cut_has_dialogue 주석 참고) 대사가
        # 있을 때만 config 기본값을 따르고, 없으면 강제로 끈다.
        want_audio = config.OPENROUTER_VIDEO_GENERATE_AUDIO if _cut_has_dialogue(cut) else False
        url, cost = hf_video.generate(seed_png, motion_prompt, duration=cut_seconds,
                                      generate_audio=want_audio)
        # ★영상을 URL로만 남기면 CapCut 드래프트(로컬 파일 경로만 지원)에 못 넣어서
        # 로컬로도 받아둔다(2026-07-14). 다운로드 실패해도 URL 결과 공유는 그대로 진행.
        scene_m = re.search(r"씬(\d+)", title)
        scene_num = int(scene_m.group(1)) if scene_m else None
        episode = (conti_state.get_episode(thread_ts) or {}).get("episode")
        local_path = vp_store.save_video(work, scene_num=scene_num, cut_num=num, url=url,
                                         episode=episode,
                                         prompt_summary=motion_prompt[:300],
                                         application=hf_video.APPLICATION, cost=cost)
        if not local_path:
            time.sleep(2)
            local_path = vp_store.save_video(work, scene_num=scene_num, cut_num=num, url=url,
                                             episode=episode,
                                             prompt_summary=motion_prompt[:300],
                                             application=hf_video.APPLICATION, cost=cost)
        # ★2026-07-20 격자 anchor 컷 한정: 승인용 격자 첫 프레임이 최종 영상에 잠깐 비치지
        # 않도록 앞 0.1초를 잘라낸다(격자 안 쓴 일반 컷은 건드리지 않음).
        if is_grid_cut and local_path:
            if vp_store.trim_head_seconds(local_path, 0.1):
                log.info("격자 anchor 컷 — 영상 앞 0.1초 트림 완료: %s", local_path)
        if cost_out is not None:
            cost_out["cost"] = cost
        if post_result:
            _post_generated_video(channel, thread_ts, work, title, num, local_path, cost)
        if post_confirm_buttons:
            _post_video_confirm_buttons(channel, thread_ts, work, title, cut, num, scene_seconds, local_path)
        return local_path
    except Exception as e:
        log.exception("영상화 실패")
        reason = _classify_video_fail_reason(str(e))
        # ★2026-07-15 "자동주행 중 실존 인물 안전필터로 영상화 실패하면 어떻게?" — 자동주행이 이
        # 사유를 보고 판단(입력 이미지를 더 스타일화해서 재시도)할 수 있게 out-param으로 노출.
        if fail_reason_out is not None:
            fail_reason_out["reason"] = reason
            fail_reason_out["detail"] = str(e)
        # ★2026-07-20 "안전필터 걸리면 스틸컷을 사용자가 직접 손보고 싶다" — 안전필터로 실패한
        # 경우에 한해, 원인이 된 스틸컷(seed_png)을 피그마로 보낼 수 있는 버튼을 붙인다. 다른
        # 실패 사유(네트워크 오류 등)는 스틸컷 문제가 아니므로 버튼을 안 붙인다.
        seed_png = cut.get("png")
        blocks = _figma_send_blocks() if ("안전필터" in reason and seed_png) else None
        resp = app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"⚠️ 영상화에 실패했어요({reason}). 잠시 후 다시 시도해주세요.",
            blocks=blocks)
        if blocks:
            scene_m = re.search(r"씬(\d+)", title)
            _PENDING_FIGMA_SEND[resp["ts"]] = {
                "png": seed_png, "work": work,
                "scene_num": int(scene_m.group(1)) if scene_m else None,
                "cut_num": num, "reason": reason, "episode": episode,
                "channel": channel, "thread_ts": thread_ts,
            }
        return None
    finally:
        job_ledger.finish_job(jid)

_PENDING_VIDEO_CONFIRM: dict[str, dict] = {}   # 버튼 메시지 ts -> {work,title,cut,num,scene_seconds,local_path}

_PENDING_FIGMA_SEND: dict[str, dict] = {}   # 버튼 메시지 ts -> {png,work,scene_num,cut_num,reason} — ★2026-07-20

def _figma_send_blocks():
    return [{
        "type": "actions",
        "elements": [{"type": "button", "text": {"type": "plain_text", "text": "🎨 피그마로 보내기"},
                     "action_id": "figma_send_still"}],
    }]

@app.action("figma_send_still")
def _act_figma_send_still(ack, body):
    """★2026-07-20 안전필터로 실패한 컷의 원인 스틸컷을 피그마 큐에 올린다 — 실제로 피그마
    캔버스에 얹는 건 그쪽에 설치된 동반 플러그인(figma-plugin/co-writer-bridge/)이
    figma_bridge의 로컬 HTTP 서버를 폴링해서 한다(REST API로는 파일에 못 씀, 모듈 docstring
    참고). 그래서 이 핸들러는 큐에 넣는 것까지만 하고, 실제로 캔버스에 올라왔는지는
    플러그인이 열려 있어야 확인된다."""
    ack()
    ch, tts = _action_ctx(body)
    msg_ts = body["message"]["ts"]
    pending = _PENDING_FIGMA_SEND.pop(msg_ts, None)
    if not pending:
        _reply(ch, tts, "이 스틸컷 정보가 만료됐어요 — 다시 영상화를 시도한 뒤 눌러주세요.")
        return
    if not config.FIGMA_BRIDGE_ENABLED:
        _disable_buttons(body, "⚠️ 피그마 브릿지가 꺼져있어요 — 봇 설정에서 SB_FIGMA_BRIDGE_ENABLED를 켜야 해요.")
        return
    try:
        # ★2026-07-20 still_path — 되돌리기 폴러(_on_figma_returned)가 나중에 이 경로를
        # 편집본으로 그대로 덮어써야, 다음에 이 컷을 다시 영상화할 때(기존 재생성 흐름 그대로)
        # 자동으로 손본 이미지를 쓰게 된다. 이 흐름의 컷은 영상화 전 이미 확정 저장됐으므로
        # (still_confirm 단계) cut{n}.png가 그 결정적 경로에 이미 있어야 정상이다.
        # channel/thread_ts는 되돌아왔을 때 어느 스레드에 알릴지 위해 필요.
        still_path = vp_store.still_cut_path(pending.get("work"), pending.get("scene_num"),
                                             pending.get("cut_num"), episode=pending.get("episode"))
        figma_bridge.enqueue(pending["png"], {
            "work": pending.get("work"), "scene_num": pending.get("scene_num"),
            "cut_num": pending.get("cut_num"), "reason": pending.get("reason"),
            "still_path": str(still_path) if still_path and still_path.exists() else None,
            "channel": pending.get("channel"),
            "thread_ts": pending.get("thread_ts"),
        })
        _disable_buttons(body, "🎨 피그마 대기열에 올렸어요 — 피그마에서 플러그인을 실행하면 캔버스에 자동으로 올라와요. "
                              "손본 뒤 플러그인에서 「봇으로 보내기」를 누르면 여기로 자동 반영돼요.")
    except Exception:
        log.exception("피그마 큐 등록 실패")
        _disable_buttons(body, "⚠️ 피그마로 보내기 실패 — 다시 시도해주세요.")

def _on_figma_returned(item: dict) -> None:
    """★2026-07-20 되돌리기 경로 — figma_bridge.start_return_poller의 콜백. 피그마에서 손본
    스틸컷이 돌아오면 그 컷이 실제로 읽는 원본 파일(still_path)을 편집본으로 덮어써서,
    다음에 이 컷을 다시 영상화할 때(기존 "이 컷 영상 만들어줘"/재생성 흐름 그대로, 새 코드
    경로 불필요) 자동으로 손본 이미지를 쓰게 만든다."""
    still_path = item.get("still_path")
    image_bytes = item.get("image_bytes")
    if not still_path or not image_bytes:
        log.warning(f"피그마에서 되돌아온 항목에 still_path/이미지가 없어 건너뜀: {item.get('id')}")
        return
    Path(still_path).write_bytes(image_bytes)
    ch, tts = item.get("channel"), item.get("thread_ts")
    if ch and tts:
        scene_num, cut_num = item.get("scene_num"), item.get("cut_num")
        label = (f"씬{scene_num} " if scene_num else "") + (f"컷{cut_num}" if cut_num else "")
        _reply(ch, tts, f"✅ 피그마에서 손본 스틸컷을 반영했어요({label.strip() or '해당 컷'}) — "
                       "이제 이 컷을 다시 영상화해보세요.")

if config.FIGMA_BRIDGE_ENABLED:
    figma_bridge.start_return_poller(_on_figma_returned)

def _video_confirm_blocks():
    return [{
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ 확정 (폴더에 저장)"},
             "style": "primary", "action_id": "video_confirm"},
            {"type": "button", "text": {"type": "plain_text", "text": "🔄 재생성"},
             "action_id": "video_regen"},
        ],
    }]

def _post_video_confirm_buttons(channel, thread_ts, work, title, cut, num, scene_seconds, local_path):
    text = "이 영상을 확정할까요?"
    resp = app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text,
                                       blocks=_with_text_block(text, _video_confirm_blocks()))
    _PENDING_VIDEO_CONFIRM[resp["ts"]] = {"work": work, "title": title, "cut": cut, "num": num,
                                          "scene_seconds": scene_seconds, "local_path": local_path}

@app.action("video_confirm")
def _act_video_confirm(ack, body):
    ack()
    msg_ts = body["message"]["ts"]
    p = _PENDING_VIDEO_CONFIRM.pop(msg_ts, None)
    if not p:
        # ★2026-07-15: 사용자 지적 — "영상 저장은 실제로 되는데 만료된 요청이라고 뜸". 실제로는
        # 오해를 부르는 문구였다: save_video()는 이 확정 버튼과 무관하게 영상 생성 시점에 이미
        # 무조건 실행돼 있다(위 _generate_video_for_cut, 확정 여부와 상관없이 항상 저장). 이
        # "확정" 버튼은 그 사실을 알려주는 화면일 뿐 별도 저장 동작이 없어서, _PENDING_VIDEO_CONFIRM이
        # 봇 재시작 등으로 날아가도 실제 저장에는 전혀 영향이 없다 — 그런데도 "만료된 요청이에요"라는
        # 문구가 마치 저장이 실패한 것처럼 오해를 줬다. 이미 저장 완료라는 걸 명확히 알린다.
        _disable_buttons(body, "✅ 이 확인 버튼은 만료됐지만, 영상은 생성 시점에 이미 저장 완료됐어요 — 다시 하실 필요 없습니다.")
        return
    path_s = f"\n`{p['local_path']}`" if p.get("local_path") else ""
    _disable_buttons(body, f"✅ <{p['work']}> {p['title']} 컷{p['num']} 확정 저장 완료{path_s}")
    ch, tts = _action_ctx(body)

@app.action("video_regen")
def _act_video_regen(ack, body):
    ack()
    msg_ts = body["message"]["ts"]
    p = _PENDING_VIDEO_CONFIRM.pop(msg_ts, None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요)."); return
    _disable_buttons(body, f"🔄 <{p['work']}> {p['title']} 컷{p['num']} 영상 재생성 중… (수 분 소요될 수 있어요)")
    ch, tts = _action_ctx(body)
    _generate_video_for_cut(ch, tts, p["work"], p["title"], p["cut"], p["num"], p["scene_seconds"])

@app.action("video_pick_cuts_multi")
def _act_video_pick_cuts_multi(ack):
    # ★2026-07-15: multi_static_select는 값이 바뀔 때마다(하나 추가/제거할 때마다) block_actions를
    # 쏘는데, 이걸 "선택 완료"로 보고 바로 생성을 트리거하면 고를 때마다 실행돼버린다 — 그래서
    # 여기선 ack만 하고 아무것도 안 하고, 실제 생성은 아래 "🎬 선택 컷 영상화" 버튼에서 그 시점의
    # 선택 상태(body["state"]["values"])를 읽어 처리한다. Slack은 인터랙티브 요소는 값 변경
    # 이벤트도 반드시 ack해야 하므로(안 하면 클라이언트에 에러 표시) 이 핸들러가 필요하다.
    ack()

@app.action("video_pick_cuts_confirm")
def _act_video_pick_cuts_confirm(ack, body):
    ack()
    msg_ts = body["message"]["ts"]
    p = _PENDING_VIDEO.get(msg_ts)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요)."); return
    # ★2026-07-15: multi_static_select가 Slack에 거부될 때 static_select로 폴백하는 경우
    # (_post_video_button 참고) — 그땐 state 모양이 "selected_options"(배열)이 아니라
    # "selected_option"(단일 객체)이라 그대로 두면 폴백 상황에서 컷 선택이 항상 무시된다.
    # 둘 다 안전하게 처리해 단일 선택으로 합친다.
    block_state = (body.get("state", {}).get("values", {})
                  .get("video_multi_select_block", {})
                  .get("video_pick_cuts_multi", {}) or {})
    selected = block_state.get("selected_options")
    if selected is None:
        single = block_state.get("selected_option")
        selected = [single] if single else []
    if not selected:
        # ★2026-07-15: 아직 컷을 안 고르고 버튼만 누른 경우 — _PENDING_VIDEO를 pop하지 않고
        # 남겨둬서, 사용자가 이어서 컷을 고르고 다시 눌러도 되게 한다(상태를 날리면 안 됨).
        ch, tts = _action_ctx(body)
        _reply(ch, tts, "먼저 컷을 선택해주세요.")
        return
    _PENDING_VIDEO.pop(msg_ts, None)
    selected_nums = [int(o["value"]) for o in selected]
    num_set = set(selected_nums)
    selected_cuts = [c for c in p["cuts"] if c["n"] in num_set]   # 원래 컷 순서 유지
    ch, tts = _action_ctx(body)
    nums_label = ",".join(str(n) for n in sorted(num_set))
    _disable_buttons(body, f"🎬 컷 {nums_label} 영상 생성 시작… (컷마다 완료되는 대로 하나씩 올라와요)")
    _generate_videos_for_cuts(ch, tts, p["work"], p["title"], selected_cuts, p.get("scene_seconds"))

@app.action("video_all_cuts")
def _act_video_all_cuts(ack, body):
    ack()
    msg_ts = body["message"]["ts"]
    p = _PENDING_VIDEO.pop(msg_ts, None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요)."); return
    _disable_buttons(body, f"🎬 전체 {len(p['cuts'])}컷 영상 생성 시작… (컷마다 완료되는 대로 하나씩 올라와요)")
    ch, tts = _action_ctx(body)
    _generate_videos_for_cuts(ch, tts, p["work"], p["title"], p["cuts"], p.get("scene_seconds"))

_DIALOGUE_QUOTE_RE = re.compile(r"['‘]([^'’]+)['’]|「([^」]+)」")

_KOREAN_CHARS_PER_SEC = 4.5   # 대략적인 한국어 TTS 자연 발화 속도(체감 기준 추정치)

def _estimate_cut_seconds(caption: str, min_s: float = 4.0, max_s: float = 15.0) -> float:
    """이 컷의 캡션(대사 인용 포함)에서 발화 분량을 추정해 필요한 영상 길이(초)를 계산.
    대사가 없으면(지문/액션만 있는 컷) 기본값 5초. seedance 허용 범위(4~15초)로 clamp.

    ★2026-07-14: "편집에서 영상 자체를 자르면 되잖아" — 씬 전체 길이를 컷 수로 균등 분할하면
    대사가 많은 컷은 실제 발화 시간보다 원본 영상이 짧아서 나레이션 TTS가 넘쳐버린다. 각 컷의
    실제 대사 분량에 맞춰 원본을 넉넉하게 생성해두면, 합본 편집(edit_plan의 start/duration)이
    그 안에서 필요한 만큼만 잘라 쓸 수 있다."""
    quotes = [a or b for a, b in _DIALOGUE_QUOTE_RE.findall(caption or "")]
    if not quotes:
        return min_s + 1.0   # 대사 없는 액션 컷 — 최소치보다 살짝 여유(5초)
    total_chars = sum(len(q) for q in quotes)
    seconds = total_chars / _KOREAN_CHARS_PER_SEC + 1.5   # +1.5초: 대사 전후 표정/동작 여유
    return max(min_s, min(max_s, seconds))

def _generate_videos_for_cuts(channel, thread_ts, work, title, cuts, scene_seconds):
    """씬의 컷 전체를 한 번에 영상화(2026-07-14, "하나씩 만드는 게 불편하다" 요청).
    컷별 미등록 인물/장소 확인은 건마다 멈춰 물으면 '한 번에'의 의미가 없어지므로, 여기선
    한 번에 모아서 경고만 남기고 진행한다(개별 흐름의 확인 스텝은 생략).

    ★2026-07-14: 컷마다 독립적으로(병렬) 영상화하면 합본에서 컷 사이 전환이 하드컷처럼
    어색했다 — 스틸컷 생성 때 이미 쓰던 "같은 씬 안 컷은 순차 생성 + 직전 컷을 참조로 체이닝"
    패턴(_gen_group)을 영상화에도 적용: 컷 번호 순으로 순차 생성하고, 직전 컷 영상의 마지막
    프레임을 다음 컷의 추가 참조로 넘긴다. 병렬성을 포기하는 대신 컷 사이 연결이 매끄러워짐."""
    missing_lines = [f"· 컷{c['n']}: {', '.join(m)}" for c in cuts
                     if (m := _unregistered_mentions(work, c))]
    if missing_lines:
        _reply(channel, thread_ts,
              "⚠️ 미등록 인물/장소가 있는 컷이 있어요(참조 일관성이 떨어질 수 있음):\n" +
              "\n".join(missing_lines))
    # ★2026-07-14: scene_seconds(씬 헤더의 "전체" 길이, 예: 15초)를 컷 수로 그냥 나눠 컷당
    # 길이로 썼더니("컷별로 안 나뉘고 씬 하나를 통짜로 만들었다" 문제의 원래 수정), 대사가
    # 많은 씬에서는 그 등분된 길이(예: 4개 컷 → 컷당 4초)가 실제 그 컷의 대사 분량보다 훨씬
    # 짧아서 나레이션 TTS가 영상 길이를 넘겨버렸다("목소리가 하나도 안 맞음").
    # ★사용자 피드백(2026-07-14, "편집에서 영상 자체를 자르면 되잖아"): 합본은 편집 단계에서
    # start/duration으로 원하는 구간만 잘라 쓰므로, 원본 컷 영상 자체가 그 컷의 대사를 담을 만큼
    # 충분히 길게 생성돼 있어야 나중에 편집(자르기)이 가능하다. 그래서 씬 전체 길이를 컷 수로
    # 균등 분할하는 대신, 각 컷 자기 자신의 캡션(대사 포함)에서 발화 분량을 추정해 컷별로
    # 다른 길이를 준다 — 대사가 많은 컷은 더 길게, 짧은 컷은 짧게(seedance 허용 범위 4~15초).
    ordered = sorted(cuts, key=lambda c: c["n"])
    total = len(ordered)
    # ★2026-07-14: 컷마다 몇 분씩 걸리는데 지금 몇 컷째인지 알 방법이 없어서 진행상황이 안
    # 보인다는 지적 — 진행 메시지 하나를 계속 갱신해 "컷 1/4 완료 → 컷 2/4 생성 중" 식으로 보여줌.
    _CANCEL.discard(thread_ts)
    ph = _thinking(channel, thread_ts, f"컷 1/{total} 생성 중…", stop_button=True)
    for idx, c in enumerate(ordered, start=1):
        # ★2026-07-16: [🛑 중단] 버튼(위 stop_button=True)을 눌러도 여긴 원래 컷 사이
        # 취소 체크포인트가 없어서(autopilot의 _autopilot_videos_for_scene에는 있음) 버튼을
        # 눌러도 남은 컷을 전부 다 만들고서야 멈췄다 — 같은 관례(thread_ts in _CANCEL)로
        # 컷 시작 전마다 확인해 실제로 여기서 멈추게 한다.
        if thread_ts in _CANCEL:
            _CANCEL.discard(thread_ts)
            _update_note(channel, ph, f"🛑 중단됨 — {idx - 1}/{total}컷까지 처리", clear=True)
            return
        # 콘티의 [N초] 비트 표기를 그대로 반영한 샷분해 duration을 그대로 쓴다(2026-07-14,
        # "[N초]에 있는대로 만들게" 요청) — seedance 하한(4초)만 보정하고 그 이상은 그대로.
        # duration이 없거나 이상하면(구버전 콘티 등) 이 컷 캡션만 보고 추정하는 폴백으로 내려간다.
        planned = c.get("duration")
        cut_seconds = (max(4.0, min(15.0, float(planned)))
                      if isinstance(planned, (int, float)) and planned > 0
                      else _estimate_cut_seconds(c.get("caption") or ""))
        try:
            local_path = _generate_video_for_cut(channel, thread_ts, work, title, c, c["n"], cut_seconds)
        except Exception:
            log.exception("전체 컷 영상화 중 한 건 실패")
            local_path = None
        _update_note(channel, ph,
                    (f"컷 {idx}/{total} 완료" if local_path else f"컷 {idx}/{total} 실패") +
                    (f" → 컷 {idx + 1}/{total} 생성 중…" if idx < total else f" — 전체 {total}컷 처리 끝"),
                    clear=(idx == total))

_PENDING_SCENE_PICK: dict[str, dict] = {}   # thread_ts -> {channel, rest} — (A7) "어느 씬을...?" 답 대기

_RECENTLY_MISSING_CUTS: dict[str, dict] = {}

def _maybe_scene_pick_reply(channel, thread_ts, query) -> bool:
    """(A7, 2026-07-13) "어느 씬을 스틸컷으로 만들까요?"에 "씬2"/"2번"/"2"처럼 씬 번호만
    답해도(스틸컷/이미지 단어 없이) 그 답으로 이어받는다 — 안 그러면 콘티 수정 지시로 오인됨."""
    p = _PENDING_SCENE_PICK.get(thread_ts)
    if not p or not (query or "").strip():
        return False
    q = query.strip()
    sm = (re.search(r"씬\s*(\d+)", q) or re.search(r"(\d+)\s*번째?\s*씬", q)
          or re.search(r"^\s*(\d{1,2})\s*(번)?\s*[!.]*$", q))
    if not sm:
        return False
    _PENDING_SCENE_PICK.pop(thread_ts, None)
    _do_stills(channel, thread_ts, f"{p['rest']} 씬{sm.group(1)}".strip())
    return True

def _split_regen_cut_override(q: str, rest: str) -> tuple[str, str]:
    """★2026-07-15: "4컷만 다시 만들고 싶어 옷에 장식 없애고 잠옷A와 똑같이"처럼 재생성
    자유 답변에 명시적 컷수/컷선택 지정이 섞여 있으면, _do_stills의 기존 파싱(_parse_cut_filter,
    "N컷" 정규식)이 그 지정을 실제로 반영하도록 rest 뒤에 이어 붙이고, feedback(LLM 프롬프트
    노트)에서는 그 구간을 지워서 뒤에 남은 진짜 내용 지시가 컷수 문구에 묻혀 흐려지지 않게 한다.
    두 스틸컷 재생성 경로(_maybe_stillcut_regen_ask_reply/_maybe_stillcut_regen_feedback)가
    동일하게 이 처리가 필요해 공용 헬퍼로 뺐다. 반환: (새 rest, LLM에 넘길 feedback 텍스트).

    ★2026-07-15: rest는 still_state.set_last로 매 렌더마다 영구 기록되고 다음 규제생 요청이
    또 그 rest를 이어받는다 — "컷5만"으로 한 번 append한 뒤 그 rest가 "저장"되면, 다음번
    "컷7만" 요청이 또 append만 해서 rest 안에 "컷5 ... 컷7"이 같이 남아 앞쪽(컷5)이 먼저
    매치돼 새 지정이 무시되는 실사용자 버그("컷7만 다시" 요청인데 컷5로 처리됨)로 이어졌다.
    새 지정을 append하기 전에 rest에 이미 있던 이전 컷수/컷선택 지정을 먼저 지운다.

    ★2026-07-16: 컷 지정만 챙기고 씬 번호는 안 봤다 — 스레드의 마지막 렌더가 "씬3"이었는데
    사용자가 이번 자유 답변에서 "씬2 컷7 재생성..."처럼 다른 씬을 말해도 rest에 저장된 옛
    "씬3"이 그대로 남아 조용히 엉뚱한 씬이 재생성되는 실사용자 버그. q에 _SCENE_NUM_RE로
    씬 번호가 있으면 rest에 있던 이전 씬 지정을 지우고 q에서 뽑은 새 씬 지정으로 교체
    append한다(컷 지정과 동일한 패턴). feedback_text에서도 그 구간을 제외한다."""
    feedback_text = q
    cfm = _CUT_FILTER_RE.search(q) if _parse_cut_filter(q) is not None else re.search(r"(\d{1,2})\s*컷", q)
    if cfm:
        rest = _CUT_FILTER_RE.sub("", rest)
        rest = re.sub(r"(\d{1,2})\s*컷", "", rest)
        rest = re.sub(r"\s+", " ", rest).strip()
        rest = f"{rest} {cfm.group(0)}".strip()
        feedback_text = (q[:cfm.start()] + q[cfm.end():]).strip()
    # ★2026-07-20: 여기서 _SCENE_NUM_RE(단일 숫자 캡처)만 쓰면 "씬2,3,4"처럼 콤마로 여러 씬을
    # 지정해도 첫 번째 숫자만 살아남고 ",3,4"는 rest에서 "씬"과 분리된 채 버려져 _parse_scene_filter가
    # 다시 못 붙인다 — 다중 씬 배치 요청이 이 경로(_maybe_generate_request 등)를 거치면 씬2로만
    # 좁혀지던 실사용자 버그. _SCENE_FILTER_RE(콤마·하이픈 리스트 전체 캡처, [합본]과 동일 문법)를
    # 먼저 시도해 리스트 전체를 보존하고, 안 걸리면("2번째 씬" 등 단일 표기) 기존 _SCENE_NUM_RE로 폴백한다.
    fm = _SCENE_FILTER_RE.search(q)
    if fm:
        scene_spec = fm.group(1)
        rest = _SCENE_FILTER_RE.sub("", rest)
        rest = re.sub(r"\s+", " ", rest).strip()
        rest = f"{rest} 씬{scene_spec}".strip()
        feedback_text = _SCENE_FILTER_RE.sub("", feedback_text)
        feedback_text = re.sub(r"\s+", " ", feedback_text).strip()
    else:
        sm = _SCENE_NUM_RE.search(q)
        if sm:
            scene_num = next(g for g in sm.groups() if g)
            rest = _SCENE_NUM_RE.sub("", rest)
            rest = re.sub(r"\s+", " ", rest).strip()
            rest = f"{rest} 씬{scene_num}".strip()
            feedback_text = _SCENE_NUM_RE.sub("", feedback_text)
            feedback_text = re.sub(r"\s+", " ", feedback_text).strip()
    return rest, feedback_text

def _maybe_stillcut_regen_ask_reply(channel, thread_ts, query) -> bool:
    """(2026-07-15) [🔄 재생성] 클릭 후 "어떻게 다시 만들까요?"에 대한 자유 답변 대기 — 다음
    메시지를 그대로 재생성 피드백으로 받아 반영한다. 명백히 다른 의도(작품목록/스레드상태/확정
    취소 조회)로 보이는 메시지까지 피드백으로 삼켜버리진 않도록 그런 것만 예외로 걸러
    뒤쪽 핸들러로 넘긴다(대기 상태는 그대로 유지돼 다음 진짜 답변을 기다린다)."""
    if thread_ts not in _PENDING_STILL_REGEN_ASK or not (query or "").strip():
        return False
    if _LIST_WORKS_RE.search(query) or _THREAD_STATUS_RE.search(query) or _UNCONFIRM_CONTI_RE.search(query):
        return False
    p = _PENDING_STILL_REGEN_ASK.pop(thread_ts)
    q = query.strip()
    _reply(channel, thread_ts, f"🔄 '{q}' 반영해서 다시 만들게요…")
    rest, feedback_text = _split_regen_cut_override(q, p["rest"])
    _do_stills(channel, thread_ts, rest, feedback=feedback_text or None)
    return True

def _maybe_element_regen_ask_reply(channel, thread_ts, query) -> bool:
    """(2026-07-15) 엘리먼트(인물/장소/소품/의상) 후보 [🔄 다시 생성] 클릭 후 "어떻게 다시
    만들까요?"에 대한 자유 답변 대기 — _maybe_stillcut_regen_ask_reply와 동일 패턴."""
    if thread_ts not in _PENDING_ELEMENT_REGEN_ASK or not (query or "").strip():
        return False
    if _LIST_WORKS_RE.search(query) or _THREAD_STATUS_RE.search(query) or _UNCONFIRM_CONTI_RE.search(query):
        return False
    p = _PENDING_ELEMENT_REGEN_ASK.pop(thread_ts)
    q = query.strip()
    _reply(channel, thread_ts, f"🔄 '{q}' 반영해서 다시 만들게요…")
    _post_element_candidate(channel, thread_ts, p["work"], p["name"], p["etype"], p["context"], feedback=q)
    return True

def _maybe_planregen_ask_reply(channel, thread_ts, query) -> bool:
    """★2026-07-16: STAGE-1(씬 설계) [🔄 재생성] 클릭 후 "어떻게 다시 만들까요?"에 대한 자유
    답변 대기 — _maybe_stillcut_regen_ask_reply와 동일 패턴."""
    if thread_ts not in _PENDING_PLAN_REGEN_ASK or not (query or "").strip():
        return False
    if _LIST_WORKS_RE.search(query) or _THREAD_STATUS_RE.search(query) or _UNCONFIRM_CONTI_RE.search(query):
        return False
    _PENDING_PLAN_REGEN_ASK.pop(thread_ts)
    q = query.strip()
    _reply(channel, thread_ts, f"🔄 '{q}' 반영해서 다시 만들게요…")
    sb_do_storyboard(channel, thread_ts, q, stage=1)
    return True

_BEAT_TAG_RE = re.compile(r"\[\d+(?:\.\d+)?\s*초\]")

_AUTO_CUT_RE = re.compile(r"적당히|알아서|자동으로|자동\s*컷")

_DIALOGUE_HAS_QUOTE_RE = re.compile(r"'[^']{2,}'")

def _cut_has_dialogue(cut: dict) -> bool:
    return bool(_DIALOGUE_HAS_QUOTE_RE.search(cut.get("caption") or ""))

def _do_stills(channel, thread_ts, rest, feedback=None):
    """[스틸컷] <작품> 씬N — 한 씬만 스틸컷 생성. ★2026-07-16: "씬2,3,4"/"씬2-4"처럼
    콤마·하이픈으로 여러 씬을 지정하면(_parse_scene_filter로 감지, [합본]과 동일 문법) 씬마다
    순서대로(레이트리밋/비용 때문에 씬 단위 병렬화는 하지 않음) _do_stills_render_one을 반복
    호출해 배치로 만들어준다 — 씬이 정확히 1개면 기존 단일 씬 흐름 그대로.
    feedback: [🔄 재생성] 후 "어떻게 다시 만들까요?"에 대한 자유 답변(2026-07-15). 주어지면
    그 씬(또는 콘티 전체) source_text 끝에 명시적 수정 지시로 덧붙여 3단계 샷분해 LLM이
    반영하게 한다 — rest/tail 파싱(씬 번호·컷수 등)에는 관여하지 않고 프롬프트 내용에만 영향."""
    work, bible, tail, msgs = _resolve_work_bible(channel, thread_ts, rest)
    if not _require_genre(channel, thread_ts, work):
        return
    # "컷1,3"/"1-3컷" 등 특정 컷만 골라 뽑는 지정(2026-07-15) — 있으면 아래 'N컷' 총개수
    # 오버라이드보다 우선한다(둘이 동시에 쓰이는 경우는 없다고 가정).
    cut_filter = _parse_cut_filter(tail)
    ctm = None if cut_filter else re.search(r"(\d{1,2})\s*컷", tail)   # 'N컷'으로 명시하면 그 수로 오버라이드
    # ★2026-07-14: "컷이 6개인데 스틸컷은 무조건 4개로 고정" 지적 — 콘티에 [N초] 비트 표기가
    # 있으면(2026-07-14부터 상세콘티가 비트마다 이 표기를 단다) 그 씬의 실제 비트 수를 컷
    # 목표로 쓴다. 사용자가 "N컷"으로 직접 지정했으면 그게 항상 최우선.
    target = int(ctm.group(1)) if ctm else None
    # ★2026-07-15: "적당히/알아서/자동으로 끊어줘" — 컷 수를 3단계 샷분해 LLM 판단(구도가
    # 비슷하면 합치는 legacy 로직)에 맡긴다. 명시적 "N컷"/"컷1,3" 지정이 함께 오면 그쪽이
    # 항상 우선한다(구체적 지정 > 막연한 "적당히") — 둘 다 있으면 auto는 무시하고 경고 없이
    # 넘어간다(사용자가 자동 지정을 먼저 쓰고 뒤에 마음 바꿔 N컷을 덧붙인 것으로 간주).
    auto_cut = bool(_AUTO_CUT_RE.search(tail)) and target is None and cut_filter is None

    # 화 번호를 지정했으면 그 화를 최우선으로 쓴다. ★2026-07-16: 예전엔 여기서 "스레드에 이미
    # 다른 화가 추적되고 있으면 새 스레드에서 시작하라"고 거부했는데, 그건 _thread_conti가
    # episode를 검증하지 않고 스레드 텍스트를 맹신하던 근본 버그를 스레드 분리로 회피한
    # 대증 처방이었다. _thread_conti/_thread_or_saved_conti가 이제 episode를 직접 검증해
    # 다른 화의 스레드 텍스트는 신뢰하지 않고 노션에서 올바른 화를 가져오므로, 같은 스레드에서
    # 화를 바꿔 요청해도 자연스럽게 맞는 화로 처리된다 — 거부하고 새 스레드를 강제할 필요 없음.
    epm = re.search(r"(\d{1,3})\s*[화회]", tail)   # '화'/'회' 둘 다 화 번호로 인정
    req_ep = int(epm.group(1)) if epm else None
    episode = req_ep if epm else (conti_state.get_episode(thread_ts) or {}).get("episode")
    # ★2026-07-14: _thread_or_saved_conti는 스레드에 콘티가 "이미 있으면" 조용히 반환하고
    # conti_state.set_episode를 안 불러서, "3화 씬2 스틸컷"처럼 화 번호를 명시해도 이 스레드에
    # 콘티가 이미 붙어있으면 화 정보가 기록 안 됐다 — 그 결과 나중에 영상 저장(vp_store.save_video)
    # 이 conti_state.get_episode(thread_ts)로 화를 못 찾아 outputs/videos/미분류/에 떨어지는
    # 문제로 이어졌다. 화가 확인되는 즉시(외부 fetch 여부와 무관하게) 여기서 바로 기록한다.
    if episode:
        conti_state.set_episode(thread_ts, work, episode)
    # 콘티를 사용자에게 "보여주는" 요청이 아니라 스틸컷 렌더링을 진행하기 위해 텍스트만 가져오는
    # 내부 조회다 — announce=True로 두면 매 [스틸컷] 호출마다 노션 토글을 불필요하게 재기록/재아카이브함
    conti = _thread_or_saved_conti(channel, thread_ts, msgs, work, episode, announce=False)
    if not conti:
        _reply(channel, thread_ts,
               "먼저 `[스토리보드] <작품>`로 씬 설계·상세 콘티를 만든 뒤, `[스틸컷] <작품> 씬2`처럼 씬을 지정해 주세요.")
        return
    fb_note = (f"\n\n[재생성 피드백 — 이 지시를 반드시 반영해 다시 그려라: '{feedback}']"
               if feedback else "")
    scenes = _split_scenes(conti)
    if not scenes:      # 씬 헤더 없는 옛 콘티 → 전체를 스틸로 (폴백, [N초] 비트 표기 없음 전제)
        # 옛 콘티([N초] 표기 없음)는 원래부터 "구도 비슷하면 합침" 판단이 기본이라 auto_cut이
        # 와도 딱히 다를 게 없지만, target을 강제하지 않는다는 점에서 동일하게 존중해준다.
        auto_title = "스틸컷 (자동 컷 수)" if auto_cut else "스틸컷"
        if auto_cut:
            t = STILL_CUTS_DEFAULT   # 그리드 열수 계산용 추정치일 뿐, target 자체는 None으로 넘김
            res = _render_cuts_tracked("stills", rest, channel, thread_ts, work, bible, conti + fb_note,
                                       target=None, cols=min(t, 2), auto_cut_judgment=True,
                                       aspect_ratio=STILL_ASPECT, style_suffix=_style_for_work(work), no_text=True,
                                       title=auto_title, filename=f"still_{work or 'ep'}.png")
        else:
            t = target or STILL_CUTS_DEFAULT
            res = _render_cuts_tracked("stills", rest, channel, thread_ts, work, bible, conti + fb_note,
                                       target=t, cols=min(t, 2),
                                       aspect_ratio=STILL_ASPECT, style_suffix=_style_for_work(work), no_text=True,
                                       title=auto_title, filename=f"still_{work or 'ep'}.png")
        grid_png, cuts = res if res else (None, None)
        if grid_png:
            _post_still_buttons(channel, thread_ts, work, None, auto_title, rest, grid_png, cuts=cuts)
        return
    # ★2026-07-16: 다중 씬 스틸컷 배치 — "씬2,3,4 스틸컷"/"씬2-4 스틸컷"처럼 콤마·하이픈으로
    # 여러 씬을 한 번에 지정하면(_parse_scene_filter — [합본]의 _do_compile이 이미 쓰는 것과
    # 동일 유틸을 재사용, 새 파싱 로직을 따로 안 만든다) 씬마다 순서대로 아래 단일 씬 로직
    # (_do_stills_render_one, 원래 이 함수 하단에 있던 로직을 그대로 떼어낸 것)을 반복
    # 호출한다. 씬을 병렬로 겹치지 않고 순차로 도는 이유: 이미지 생성 자체가 씬 하나 안에서도
    # config.OPENROUTER_IMG_WORKERS로 이미 병렬화돼 있어, 씬 단위까지 동시에 겹치면 레이트리밋·
    # 비용이 스레드풀 크기만큼 배로 뛴다 — 기존에 이 정도 규모로 이미지 API를 동시에 두들기는
    # 정책이 없어(CONTI_SCENE_WORKERS는 텍스트 LLM 호출용) 안전한 쪽인 순차 처리를 기본으로 한다.
    # scene_filter가 씬 1개짜리("씬2")면 여기서 개입하지 않고 아래 기존 단일 씬 흐름을 그대로
    # 태워 동작을 바이트 단위로 그대로 유지한다(요구사항: 단일 씬 동작 불변).
    scene_filter = _parse_scene_filter(tail)
    if scene_filter and len(scene_filter) > 1:
        _PENDING_SCENE_PICK.pop(thread_ts, None)
        requested = sorted(scene_filter)
        avail_nums = {s[0] for s in scenes}
        do_nums = [n for n in requested if n in avail_nums]
        skip_nums = [n for n in requested if n not in avail_nums]
        if not do_nums:
            avail = ", ".join(f"씬{s[0]}" for s in scenes)
            _reply(channel, thread_ts,
                   f"콘티에 그 씬 번호들이 없어요 — {', '.join(f'씬{n}' for n in requested)}. 있는 씬: {avail}")
            return
        label = ",".join(str(n) for n in requested)
        skip_txt = f" (씬{', '.join(str(n) for n in skip_nums)}은 콘티에 없어 건너뜀)" if skip_nums else ""
        ph = _thinking(channel, thread_ts,
                       f"씬{label} 스틸컷을 순서대로 만드는 중이에요…{skip_txt} (0/{len(do_nums)})", stop_button=True)
        results = []
        for i, num in enumerate(do_nums, 1):
            _update_note(channel, ph, f"씬{label} 스틸컷 생성 중… ({i}/{len(do_nums)} · 씬{num})")
            try:
                ok, detail = _do_stills_render_one(channel, thread_ts, rest, work, bible, scenes, num,
                                                   cut_filter, target, auto_cut, ctm, fb_note, episode)
            except Exception:
                log.exception(f"씬{num} 스틸컷 생성 실패(다중 씬 배치)")
                _reply(channel, thread_ts, f"⚠️ 씬{num} 스틸컷 생성 중 오류가 났어요 — 다음 씬은 계속 진행할게요.")
                ok, detail = False, "오류"
            results.append((num, ok, detail))
        ok_parts = [f"씬{n}: {d}" for n, ok, d in results if ok]
        fail_parts = [f"씬{n}({d})" for n, ok, d in results if not ok]
        summary = f"✅ 씬{label} 스틸컷 생성 완료"
        if ok_parts:
            summary += " — " + ", ".join(ok_parts)
        if fail_parts:
            summary += f" / 실패: {', '.join(fail_parts)}"
        if skip_nums:
            summary += f" / 콘티에 없어 건너뜀: {', '.join(f'씬{n}' for n in skip_nums)}"
        _update_note(channel, ph, summary, clear=True)
        return
    # ★2026-07-16 "5화 2씬 스틸컷 만들어줘"를 못 읽고 "어느 씬을 만들까요?"로 되물은 버그 —
    # "N씬"(번호가 먼저, "번" 없이) 표기가 아래 대안 어디에도 안 걸렸다("씬\s*(\d+)"는 순서가
    # 반대, "(\d+)\s*번째?\s*씬"은 "번"이 필수, 마지막 순수숫자 폴백은 한글 뒤에 오는 숫자에
    # \b 경계가 안 생겨(한글도 \w로 취급됨) 매치 자체가 안 됨) — 전용 대안 추가.
    sm = (re.search(r"씬\s*(\d+)", tail)                      # '씬2'
          or re.search(r"(\d+)\s*번째?\s*씬", tail)             # '2번 씬'/'2번째 씬'(순서 반대)
          or re.search(r"(\d+)\s*씬", tail)                    # '2씬'(순서 반대, "번" 없음)
          or re.search(r"\b(\d{1,2})\b", re.sub(r"\d+\s*[화회컷]", "", tail)))  # 순수 숫자도 허용
    if not sm:
        lines = "\n".join(f"· 씬{num} — {hdr}" for num, hdr, _ in scenes)
        _PENDING_SCENE_PICK[thread_ts] = {"channel": channel, "rest": rest}   # (A7) 다음 답글 "씬2"만 와도 이어받게
        _reply(channel, thread_ts,
               f"어느 씬을 스틸컷으로 만들까요? `[스틸컷] <{work or '작품'}> 씬N`\n{lines}")
        return
    _PENDING_SCENE_PICK.pop(thread_ts, None)
    num = int(sm.group(1))
    # ★2026-07-15: "씬1 3~13 영상화" → 컷12 없어서 건너뜀 → 곧바로 "씬1 12컷 스틸컷 생성" 실사용자
    # 사례 — ctm(="N컷"=총개수 문법)이 걸렸어도, 방금 이 스레드·이 씬에서 바로 그 번호가 "없어서
    # 건너뛴 컷"으로 안내됐다면 총개수가 아니라 그 컷 하나를 가리킨 것으로 재해석한다(cut_filter
    # 경로로 되돌림). 문맥이 안 맞으면(다른 씬/기록 없음) 기존 "N컷"=총개수 동작 그대로 유지.
    if cut_filter is None and ctm is not None:
        pending = _RECENTLY_MISSING_CUTS.get(thread_ts)
        if pending and pending.get("scene") == num and target in pending.get("cuts", set()):
            cut_filter = {target}
            target = None
            del _RECENTLY_MISSING_CUTS[thread_ts]
    _do_stills_render_one(channel, thread_ts, rest, work, bible, scenes, num,
                          cut_filter, target, auto_cut, ctm, fb_note, episode)

def _do_stills_render_one(channel, thread_ts, rest, work, bible, scenes, num,
                          cut_filter, target, auto_cut, ctm, fb_note, episode):
    """단일 씬 스틸컷 생성 — 2026-07-16 다중 씬 배치("씬2,3,4 스틸컷") 지원을 위해 _do_stills
    본문 하단에 있던 단일 씬 처리 로직을 그대로 떼어낸 것(동작 변경 없음). _do_stills가 씬
    1개일 때 직접 호출하고, 다중 씬 배치일 때는 씬마다 이 함수를 순서대로 반복 호출한다.
    cut_filter/target은 씬마다 아래에서 재해석될 수 있어 인자로 받은 뒤 로컬에서만 바뀐다
    (호출부 값에 영향 없음 — 씬마다 독립적으로 판단해야 하므로).
    반환값: (성공 여부, 요약용 라벨 문자열 — 다중 씬 배치의 완료 요약에 쓰인다)."""
    match = next((s for s in scenes if s[0] == num), None)
    if not match:
        avail = ", ".join(f"씬{s[0]}" for s in scenes)
        _reply(channel, thread_ts, f"씬{num}을 콘티에서 못 찾았어요. 있는 씬: {avail}")
        return False, "콘티에 없음"
    _, hdr, body = match
    dm = re.search(r"(\d+)\s*초", hdr)     # 콘티 헤더 "■ 씬N · 10초 · 제목"에 이미 씬 길이가 있음
    scene_seconds = int(dm.group(1)) if dm else None
    # ★2026-07-14: "컷이 6개인데 스틸컷은 무조건 4개로 고정" — 사용자가 "N컷"을 명시 안 했으면
    # 이 씬 본문의 [N초] 비트 개수를 그대로 컷 목표로 쓴다(비트=컷 하나가 원칙이므로). 옛
    # 콘티라 비트 표기가 없으면 기존 기본값(4)으로 폴백.
    n_beats = len(_BEAT_TAG_RE.findall(body))
    # ★2026-07-15: "씬1 12컷 스틸컷 생성"인데 12개를 새로 만들어버림 — 두 번째 반복. 위
    # _RECENTLY_MISSING_CUTS(방금 "컷N 없어서 건너뜀" 안내 직후에만 발동하는 좁은 문맥
    # 오버라이드)로는 못 잡은 케이스: 사용자가 그 안내 없이 처음부터 "N컷"을 컷 지정 의도로
    # 씀. 이 씬이 (1) 비트 표기가 있는 최신 콘티고(n_beats>0, 옛 콘티는 "N컷"=총개수가 유일한
    # 해석이라 제외), (2) N이 그 비트 수보다 작고(N>=n_beats면 "총 N컷으로 만들어줘"도 여전히
    # 말이 되는 요청이라 손대지 않음), (3) 이 씬에 이미 생성된 컷이 있으면(옛 still_state
    # 스레드 기록과 디스크의 확정 컷 원본 둘 다 확인 — 둘 중 하나라도 있으면 "이미 있는 씬"으로
    # 간주) — 처음부터 다시 만드는 "총 N컷" 요청보다 "그 번호 컷 하나만" 요청일 확률이 훨씬
    # 높다고 보고 cut_filter로 재해석한다. 셋 중 하나라도 안 맞으면(비트 표기 없음/N이 더 큼/
    # 아직 컷이 없는 첫 생성) 기존 "N컷"=총개수 동작 그대로 유지.
    if cut_filter is None and ctm is not None and target is not None and 0 < target < n_beats:
        has_existing_cuts = bool(
            vp_store.load_latest_cuts(work, num, episode=episode)
            or still_state.get_last(thread_ts)
            or still_state.get_confirmed(thread_ts)
        )
        if has_existing_cuts:
            cut_filter = {target}
            target = None
    # ★2026-07-15(하드캡 → 자동 배치 분할로 전환): 이전엔 4비트 초과 씬을 그냥 4컷으로 잘라
    # 나머지를 버렸는데, 사용자가 "13컷이면 4컷씩 끊어서 여러 메시지로 전부 만들어달라"고
    # 명시적으로 요청함(뒤에서 오버라이드 없이 n_beats > 4인 경우, batch 분기에서 처리하고
    # 여기서는 그냥 지나간다 — 아래 batch 분기 참고).
    if cut_filter is not None and n_beats and max(cut_filter) > n_beats:
        # 3단계 분해 전에도 미리 걸러줄 수 있는 명백한 범위 초과(비트 표기 있는 콘티 한정)는
        # 여기서 바로 안내 — 없어도 _render_cuts 안 shots 확정 후 동일한 검증이 한 번 더 있다
        # (콘티에 [N초] 표기가 없어 n_beats=0인 옛 콘티는 여기서 못 걸러 그쪽에서 걸러진다).
        _reply(channel, thread_ts, f"씬{num}은 컷이 {n_beats}개뿐이에요 — 요청하신 컷{sorted(cut_filter)} 중 "
                                    f"{sorted(c for c in cut_filter if c > n_beats)}은 없어요.")
        return False, "요청 컷 범위 초과"
    # ★2026-07-15: 오버라이드("N컷") 없이 비트가 4개 초과면, 예전처럼 4개로 잘라 버리지
    # 않고 전체를 4컷씩 순차 배치로 나눠 배치마다 별도 메시지(카드)로 올린다("13컷이면 4컷씩
    # 끊어서 여러 메시지로 다 만들어달라" 실사용자 요청). 기존 cut_filter 메커니즘을 그대로
    # 재사용 — 3단계 분해는 매 배치 동일하게 씬 전체(target=n_beats) 기준으로 돌려 컷 번호가
    # 항상 전체 씬 기준 1..n_beats로 일관되게 매겨지고, cut_filter로 그 배치분만 남긴다.
    # ★2026-07-15: "씬1 컷1-12 스틸컷 생성"처럼 명시적 cut_filter로 4개 넘는 범위를 지정해도
    # 예전엔 이 배치 분기(cut_filter is None 조건) 자체를 건너뛰어 12컷을 한 번에 만들어버렸다
    # (실사용자 리포트: "12개를 동시에 만들어버림") — 명시적 범위도 4개 넘으면 똑같이 배치로
    # 쪼갠다. "N컷"(target, 3단계가 병합 판단으로 컷 수를 직접 정하는 경우)은 고정된 컷 번호
    # 목록이 없어(병합 결과라 사전에 안 정해짐) 같은 방식으로 못 쪼개므로 그대로 둔다.
    if target is None and n_beats > 4 and (cut_filter is None or len(cut_filter) > 4):
        all_cuts = sorted(cut_filter) if cut_filter is not None else list(range(1, n_beats + 1))
        batches = [all_cuts[i:i + 4] for i in range(0, len(all_cuts), 4)]
        # ★2026-07-15: "몇 배치인지, 뭐가 모자란지 파악 안 됨" 실사용자 불만 → 시작 전 전체 계획
        # 안내 + 끝난 뒤 성공/실패 집계를 남겨 진행 상황을 한눈에 보이게 한다.
        _reply(channel, thread_ts,
               f"씬{num}은 {len(all_cuts)}컷이라 4개씩 나눠 총 {len(batches)}번에 걸쳐 만들게요: " +
               ", ".join(f"컷{b[0]}-{b[-1]}" for b in batches))
        failed_ranges = []
        for batch in batches:
            b_start, b_end = batch[0], batch[-1]
            batch_filter = set(batch)
            res = _render_cuts_tracked("stills", rest, channel, thread_ts, work, bible,
                                       f"■ 씬{num} · {hdr}\n{body}{fb_note}", target=n_beats,
                                       cols=min(len(batch_filter), 2), cut_filter=batch_filter,
                                       auto_cut_judgment=False, aspect_ratio=STILL_ASPECT,
                                       style_suffix=_style_for_work(work), no_text=True,
                                       title=f"스틸컷 씬{num} (컷{b_start}-{b_end}/{n_beats})",
                                       filename=f"still_{work or 'ep'}_s{num}_b{b_start}-{b_end}.png")
            grid_png, cuts = res if res else (None, None)
            if grid_png:
                _post_still_buttons(channel, thread_ts, work, num,
                                     f"스틸컷 씬{num} (컷{b_start}-{b_end}/{n_beats}) · {hdr}", rest, grid_png,
                                     cuts=cuts, scene_seconds=scene_seconds)
            else:
                _reply(channel, thread_ts, f"씬{num} 컷{b_start}-{b_end} 배치 생성에 실패했어요 — 다음 배치는 계속 진행할게요.")
                failed_ranges.append(f"{b_start}-{b_end}")
        if failed_ranges:
            _reply(channel, thread_ts, f"씬{num} 배치 생성 완료 — 실패한 구간: {', '.join(failed_ranges)}")
            return False, f"{len(all_cuts)}컷 중 실패 구간 {', '.join(failed_ranges)}"
        else:
            _reply(channel, thread_ts, f"✅ 씬{num} 전체 {len(all_cuts)}컷 생성 완료 (배치 {len(batches)}개 모두 성공)")
            return True, f"{len(all_cuts)}컷"
    # 3단계 샷분해는 필터 여부와 무관하게 "씬 전체" 기준 target(=원래 n_beats)으로 그대로 돌려야
    # 컷 번호 1..N이 전체 씬 기준으로 매겨진다(그래야 사용자가 말한 "컷1,3"이 실제 그 컷과 맞음).
    # 그리드 열수/표시 제목만 필터된(=실제 생성될) 개수 기준으로 바꾼다.
    # ★2026-07-15: auto_cut(적당히/알아서/자동으로)이면 비트=컷 강제 기본값을 안 쓰고 target을
    # None으로 넘겨 3단계 LLM이 구도 판단으로 직접 컷 수를 정하게 한다(_do_stills 상단에서
    # 이미 cut_filter/target 둘 다 없을 때만 auto_cut=True가 되도록 상호배타 처리해뒀다 —
    # 명시적 "N컷"/"컷1,3"이 있으면 auto_cut은 항상 False라 이 분기 자체가 안 탄다).
    shot_target = None if auto_cut else (target or (n_beats if n_beats else STILL_CUTS_DEFAULT))
    t = len(cut_filter) if cut_filter is not None else (shot_target or n_beats or STILL_CUTS_DEFAULT)
    cut_label = f" (컷{','.join(str(c) for c in sorted(cut_filter))})" if cut_filter is not None else \
                (" (자동 컷 수)" if auto_cut else "")
    res = _render_cuts_tracked("stills", rest, channel, thread_ts, work, bible,
                               f"■ 씬{num} · {hdr}\n{body}{fb_note}", target=shot_target,
                               cols=min(t, 2), cut_filter=cut_filter, auto_cut_judgment=auto_cut,
                               aspect_ratio=STILL_ASPECT, style_suffix=_style_for_work(work), no_text=True,
                               title=f"스틸컷 씬{num}{cut_label}", filename=f"still_{work or 'ep'}_s{num}.png")
    grid_png, cuts = res if res else (None, None)
    if grid_png:
        _post_still_buttons(channel, thread_ts, work, num, f"스틸컷 씬{num}{cut_label} · {hdr}", rest, grid_png,
                            cuts=cuts, scene_seconds=scene_seconds)
        return True, f"{len(cuts) if cuts else t}컷"
    return False, "생성 실패"

# renamed from _do_export (name collision with the other bot's function of the same name, different behavior)
def sb_do_export(channel, thread_ts, rest, cmd="파일"):
    """[파일] <md|txt|csv> [파일명] — 내보낼 내용은 (1)명령 아래 줄/첨부, 없으면 (2)스레드의 마지막 봇 답변.
    `[md]`/`[txt]`/`[csv]`로 형식을 바로 줄 수도 있음. CSV는 마크다운 표를 자동 변환."""
    rest = (rest or "").strip("\n")
    head, _, inline = rest.partition("\n")
    if cmd in _EXPORT_TYPES:
        ftype, name_toks = cmd, head.split()
    else:
        toks = head.split()
        if toks and toks[0].lower() in _EXPORT_TYPES:
            ftype, name_toks = toks[0].lower(), toks[1:]
        else:
            ftype, name_toks = "md", toks
    ext = _EXPORT_TYPES[ftype]

    content = inline.strip()
    if not content:
        for m in reversed(_thread_messages(channel, thread_ts)):
            if m["role"] == "assistant" and m["content"].strip():
                content = m["content"]
                break
    if not content:
        _reply(channel, thread_ts,
               "파일로 내보낼 내용을 못 찾았어요. 명령 아래 줄에 내용을 붙이거나, 봇 답변이 있는 스레드에서 써주세요.")
        return

    base = re.sub(r"[^\w가-힣.\-]+", "_", "_".join(name_toks).strip()).strip("_.")
    if not base:
        import time as _t
        base = f"storyboard_{int(_t.time())}"
    if base.lower().endswith(ext):
        base = base[: -len(ext)]
    filename = base + ext

    if ext == ".csv":
        csv_text = _md_table_to_csv(content)
        if csv_text is None:
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
        app.client.files_upload_v2(channel=channel, thread_ts=thread_ts, file=data,
                                   filename=filename, title=filename,
                                   initial_comment=f"📄 {note} 내보냈어요 — `{filename}`")
    except Exception as e:
        log.exception("export upload failed")
        _reply(channel, thread_ts, "파일을 업로드하지 못했어요. 봇 관리자에게 알려주세요 (Slack 앱 권한 설정이 필요할 수 있어요).")

_REF_TYPE_KW = {"인물": "person", "사람": "person", "얼굴": "person", "person": "person",
                "장소": "place", "배경": "place", "place": "place",
                "소품": "prop", "아이템": "prop", "prop": "prop",
                "의상": "costume", "옷": "costume", "복장": "costume", "코스튬": "costume",
                "costume": "costume"}

_REF_TLABEL = {"person": "인물", "place": "장소", "prop": "소품", "costume": "의상"}

_PENDING_REF: dict[str, dict] = {}   # 카드 메시지 ts -> {channel, thread_ts, work, etype, pairs}

_PENDING_REF_BY_THREAD: dict[str, str] = {}   # thread_ts -> 그 스레드의 최신 확정 카드 ts

def _post_ref_confirm(channel, thread_ts, work, etype, pairs):
    """확정 카드를 새로 올리고, 그 카드 자신의 메시지 ts로 _PENDING_REF에 등록한다."""
    old_ts = _PENDING_REF_BY_THREAD.get(thread_ts)
    if old_ts:
        _PENDING_REF.pop(old_ts, None)   # 새 카드가 뜨면 이전 미확정 카드는 무효화(안 섞이게)
    text, blocks = _ref_confirm_blocks(work, etype, pairs)
    resp = app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, blocks=blocks)
    card_ts = resp["ts"]
    _PENDING_REF[card_ts] = {"channel": channel, "thread_ts": thread_ts, "work": work,
                            "etype": etype, "pairs": pairs}
    _PENDING_REF_BY_THREAD[thread_ts] = card_ts
    return card_ts

def _parse_ref_command(q: str) -> tuple[str, list[str]]:
    """'[인물|장소|소품] 이름[,이름2]' 텍스트 → (etype, names). 이름이 <꺾쇠>로 감싸여 와도 벗긴다."""
    q = (q or "").strip()
    etype = "person"
    toks = q.split()
    if toks and toks[0].lower() in _REF_TYPE_KW:
        etype = _REF_TYPE_KW[toks[0].lower()]
        q = q[len(toks[0]):].strip()
    names = [n.strip() for n in re.split(r"[,/\n]|\s{2,}", q) if n.strip()]
    names = [re.sub(r"^<\s*|\s*>$", "", n).strip() for n in names]
    return etype, [n for n in names if n]

def _pair_names_images(names, imgs):
    """names(사용자 지정, 없을 수 있음) + imgs[(stem,ext,data,url)] → [(nm,ext,data,url)] 또는
    (None, 에러메시지). url은 재시작으로 확정 대기가 날아가도 버튼에서 복구하는 데 씀(2026-07-13)."""
    if not names:
        return [(stem, ext, data, url) for stem, ext, data, url in imgs], None
    if len(names) == len(imgs):
        return [(names[i], imgs[i][1], imgs[i][2], imgs[i][3]) for i in range(len(imgs))], None
    if len(names) == 1:
        return [(names[0], imgs[0][1], imgs[0][2], imgs[0][3])], None
    return None, (f"이미지 {len(imgs)}장과 이름 {len(names)}개가 안 맞아요. "
                  "이미지 1장에 이름 1개로 보내거나, 이미지 수만큼 이름을 콤마로 나눠주세요.")

def _save_ref_pairs(work, etype, pairs):
    """실제 저장(VP fixed-images 우선, 없으면 로컬 폴백). 반환: (saved 이름 리스트, via_vp)."""
    vpfx = oi.vp_fixed_dir(work)
    d = config.OPENROUTER_REFS_DIR / oi.canon_work(work)
    saved, via_vp = [], False
    for nm, ext, data, _url in pairs:
        nm = unicodedata.normalize("NFC", nm).strip()
        if not nm:
            continue
        if vpfx is not None:
            # 폴더명은 표시 이름이 아니라 엘리먼트 id로 — 나중에 이름을 바꿔도(rename)
            # fixed-images 연결이 안 끊기게(2026-07-13, 이름 기준 폴더의 rename 취약점 수정).
            elem = oi.register_element(work, nm, etype, aliases=[nm], clear_file=True)
            pdir = vpfx / elem["id"]
            pdir.mkdir(parents=True, exist_ok=True)
            # ★2026-07-14: 예전엔 기존 파일을 안 건드리고 추가만 해서, "대표 이미지"가
            # mtime 최초 파일로 고정돼(_first_image) 재확정해도 최신 사진이 안 반영됐다
            # (사용자 실측 — [참조]/자연어 첨부로 확정할 때마다 매번 최신으로 덮어쓰길 원함).
            # `[참조]`나 "인물 선우"처럼 명시적으로 확정하는 시점엔 기존 파일을 지우고
            # 새 파일 하나만 남긴다.
            for old in list(pdir.iterdir()):
                if old.is_file() and old.suffix.lower() in _REF_SAVE_EXTS:
                    old.unlink()
            (pdir / f"{uuid.uuid4().hex}{ext}").write_bytes(data)
            via_vp = True
        else:
            d.mkdir(parents=True, exist_ok=True)
            for e in _REF_SAVE_EXTS:
                p = d / f"{nm}{e}"
                if p.exists():
                    p.unlink()
            (d / f"{nm}{ext}").write_bytes(data)
            oi.register_element(work, nm, etype, filename=f"{nm}{ext}", aliases=[nm])
        saved.append(nm)
    return saved, via_vp

def _ref_confirm_blocks(work, etype, pairs):
    tlabel = _REF_TLABEL.get(etype, etype)
    names = ", ".join(nm for nm, _, _, _ in pairs)
    text = f"*{tlabel} · {names}* 이걸로 확정할까요?"
    # (2026-07-13) 재시작으로 _PENDING_REF(메모리)가 날아가도, 버튼 자체에 심어둔 이 복구
    # 정보(작품·타입·이름별 원본 파일 url)로 이미지를 다시 받아 사용자가 재첨부 없이 이어지게.
    recovery = json.dumps({"work": work, "etype": etype,
                          "pairs": [{"n": nm, "e": ext, "u": url} for nm, ext, _data, url in pairs]},
                         ensure_ascii=False)[:2900]
    actions = [{
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ 확정"},
             "style": "primary", "action_id": "ref_confirm", "value": recovery},
            {"type": "button", "text": {"type": "plain_text", "text": "✏️ 다르게"},
             "action_id": "ref_edit", "value": recovery},
        ],
    }]
    return text, _with_text_block(text, actions)

def _recover_pending_ref(body):
    """(2026-07-13) 재시작으로 _PENDING_REF가 사라졌을 때, 버튼 자체의 value(작품·타입·
    이름별 원본 Slack 파일 url)로 이미지를 다시 받아 그대로 이어간다 — 사용자가 이미지를
    다시 첨부할 필요 없게. 실패하면 None(호출부가 기존 "만료됨" 안내로 폴백)."""
    try:
        raw = (body.get("actions") or [{}])[0].get("value")
        if not raw:
            return None
        info = json.loads(raw)
        pairs = []
        for item in info["pairs"]:
            req = urllib.request.Request(item["u"], headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            pairs.append((item["n"], item["e"], data, item["u"]))
        ch, tts = _action_ctx(body)
        return {"channel": ch, "thread_ts": tts, "work": info["work"], "etype": info["etype"], "pairs": pairs}
    except Exception:
        log.exception("확정 요청 복구 실패")
        return None

_NATURAL_REF_RE = re.compile(
    r"(?P<name>[가-힣A-Za-z0-9_()]+?(?:\s[가-힣A-Za-z0-9_()]+?)??)"
    r"\s*(?:이미지|사진|얼굴|모습)?"
    r"\s*(?:는|은|가)?"
    r"\s*(?:이걸로|이거로|이\s*사진으로|이\s*이미지로)\s*"
    r"(?:해줘|해주세요|써줘|써주세요|고정해줘|고정해주세요|고정할게|고정|쓸게|해)"
)

_PLACE_NAME_SUFFIXES = ("집", "방", "실", "관", "원", "장", "점", "역", "궁", "성", "탑",
                        "카페", "바", "클럽", "호텔", "공항", "저택", "사무실")

def _guess_ref_type(work, name):
    """이미 등록된 이름이면 그 타입 유지, 처음이면 이름 어미로 인물/장소 추정(빗나가면 '다르게'로 수정).
    ★2026-07-15: "대분류-소분류" 하이픈 표기는 장소 전용 네이밍 컨벤션이므로, 등록된 정확한
    매치가 없어도 하이픈이 들어간 이름은 person 기본값 대신 place로 강하게 편향시킨다
    (앞부분이 이미 등록된 장소 대분류와 일치하면 더더욱 place)."""
    e = oi.resolve_element(work, name)
    if e:
        return e.get("type", "person")
    if "-" in name:
        return "place"   # 대분류-소분류 표기는 등록된 대분류 일치 여부와 무관하게 항상 장소
    return "place" if any(name.endswith(suf) for suf in _PLACE_NAME_SUFFIXES) else "person"

_ELEMENT_GEN_QUOTES_RE = re.compile(r"['\"'‘’“”]")

_ELEMENT_GEN_RE = re.compile(
    r"(?:다시|재)?\s*(?P<name>[가-힣A-Za-z0-9_()-]+?(?:\s[가-힣A-Za-z0-9_()-]+?)??)\s*"
    r"(?:\s*(?:캐릭터|인물))?\s*(?:의)?\s*(?:이미지|사진)\s*(?:를|도)?\s*"
    r"(?:좀|새로|다시|재|AI로|ai로|하나|한\s*장|\s)*"
    r"(?:생성|재생성|만들어|만들|그려|찍어|필요)"
)

_ELEMENT_GEN_INTENT_HINT_RE = re.compile(r"이미지|사진|생성|만들|그려|찍어")

_TYPED_GEN_TYPE_ALT = "|".join(re.escape(kw) for kw in sorted(_REF_TYPE_KW, key=len, reverse=True))

_TYPED_GEN_RE = re.compile(
    r"(?P<name>[가-힣A-Za-z0-9_()]+?(?:\s[가-힣A-Za-z0-9_()]+?)??)\s*"
    r"(?P<type>" + _TYPED_GEN_TYPE_ALT + r")\s*(?:를|을|는|은)?\s*"
    r"(?:만들고\s*싶어|만들어줘|만들어|생성해줘|생성하고\s*싶어|생성|필요해)"
)

_TYPE_FIRST_GEN_RE = re.compile(
    r"(?P<type>" + _TYPED_GEN_TYPE_ALT + r")\s*"
    r"(?P<name>[가-힣A-Za-z0-9_()-]+?(?:\s[가-힣A-Za-z0-9_()-]+?)??)\s*"
    r"(?:이미지|사진)?\s*(?:를|을|는|은)?\s*"
    r"(?:생성해줘|생성하고\s*싶어|생성|재생성해줘|재생성|만들어줘|만들어|만들고\s*싶어)"
)

def _explicit_type_from_prefix(q: str):
    """"장소 하나 만들어줘: 숙소 옥상" / "이 옷 이미지로 만들어줘: 잠옷C" / "소품 이미지 생성: 목걸이"
    처럼 문장 앞부분에 타입 키워드(_REF_TYPE_KW)가 있고 ':'로 이름이 구분돼 오면
    (etype, name)을 반환한다. 못 찾으면 (None, None) — 이 경우 호출부가 기존 _guess_ref_type로
    폴백한다. 명시적 타입 언급은 추측보다 항상 우선."""
    m = re.match(r"^(?P<pre>.{1,24}?)[:：]\s*(?P<name>.+)$", q.strip())
    if not m:
        return None, None
    pre, name = m.group("pre"), m.group("name").strip()
    # pre에 생성 의도를 암시하는 단어가 전혀 없으면("메모: 오늘 회의" 같은 무관한 콜론 문장)
    # 오작동을 막기 위해 건너뛴다.
    if not name or not _ELEMENT_GEN_INTENT_HINT_RE.search(pre):
        return None, None
    for kw in sorted(_REF_TYPE_KW, key=len, reverse=True):
        if kw in pre:
            return _REF_TYPE_KW[kw], name
    return None, None

def _maybe_element_gen_request(channel, thread_ts, query, event) -> bool:
    """이미지 첨부 없이 "선우의 이미지를 생성해줘"처럼 순수 자연어로 온 AI 생성 요청.
    첨부 이미지가 있으면 등록 의도(_maybe_natural_ref)로 먼저 처리되게 여기선 건너뛴다."""
    if _image_files(event):
        return False
    q = query
    wm = re.search(r"<\s*([^>]+?)\s*>", q)
    work = None
    if wm and not _looks_like_mention(wm.group(1)):
        work = wm.group(1).strip()
        q = (q[:wm.start()] + q[wm.end():]).strip()
    q = _ELEMENT_GEN_QUOTES_RE.sub("", q)   # "'리안'의" 처럼 이름을 따옴표로 강조해도 인식되게

    # ★2026-07-15: "장소 하나 만들어줘: 숙소 옥상"처럼 타입 키워드가 명시된 경우
    # _guess_ref_type로 추측하지 않고 명시된 타입을 그대로 쓴다(명시 > 추측).
    name = None
    etype = None
    explicit_etype, explicit_name = _explicit_type_from_prefix(q)
    if explicit_name:
        cand = unicodedata.normalize("NFC", _ELEMENT_GEN_QUOTES_RE.sub("", explicit_name)).strip()
        cand = re.sub(r"^<\s*|\s*>$", "", cand).strip()
        if cand and not re.search(r"스토리보드|콘티|장면|스틸\s*컷|씬\s*\d*", cand):
            name, etype = cand, explicit_etype

    # ★2026-07-15: "연습실 장소를 만들고 싶어"(이름→타입 순)와 "장소 '숙소-복도' 이미지 생성"
    # (타입→이름 순, 실제 버그 리포트) 둘 다 이미지/사진 단어도 콜론도 없이 오는 경우 —
    # _TYPED_GEN_RE / _TYPE_FIRST_GEN_RE로 잡는다(명시적 타입이라 추측보다 우선). 둘 다 매치
    # 그룹 구조(name/type)와 후처리(정규화·꺾쇠 제거·콘티 제외 가드)가 동일해 하나의 루프로
    # 순서대로 시도한다.
    trailing_context = ""
    if name is None:
        for pattern in (_TYPED_GEN_RE, _TYPE_FIRST_GEN_RE):
            tm = pattern.search(q)
            if not tm:
                continue
            cand = unicodedata.normalize("NFC", _ELEMENT_GEN_QUOTES_RE.sub("", tm.group("name"))).strip()
            cand = re.sub(r"^<\s*|\s*>$", "", cand).strip()
            if cand and not re.search(r"스토리보드|콘티|장면|스틸\s*컷|씬\s*\d*", cand):
                name, etype = cand, _REF_TYPE_KW[tm.group("type")]
                # ★2026-07-15: "연습실-쉬는구역 장소를 만들고싶어 근데 아이돌 춤연습하는 연습실
                # 같은 느낌이고 한글이 없었으면 해"처럼 트리거 문구 뒤에 붙인 추가 설명이 이후
                # context="" 하드코딩 때문에 통째로 무시되던 실사용자 버그 — 매치 뒤 남은 텍스트를
                # context로 넘긴다.
                trailing_context = q[tm.end():].strip()
                break

    if name is None:
        m = _ELEMENT_GEN_RE.search(q)
        if not m:
            return False
        # "노션에 있는 3화 상세 콘티를 보고 스토리보드 이미지를 만들어줘" 같은 문장에서 "이미지"
        # 바로 앞 단어("보고 스토리보드")를 캐릭터/장소/소품 이름으로 오인하던 버그(2026-07-13) —
        # 이 단어들이 이름 자리에 잡히면 그건 인물/장소 참조 생성이 아니라 콘티 기반 스틸컷/이미지
        # 생성 요청이므로 여기서 안 먹고 뒤쪽 라우팅(콘티 기반 생성)에 넘긴다.
        if re.search(r"스토리보드|콘티|장면|스틸\s*컷|씬\s*\d*", m.group("name")):
            return False
        name = unicodedata.normalize("NFC", m.group("name")).strip()
        if not name:
            return False
        trailing_context = q[m.end():].strip()

    if not work:
        joined = "\n".join(mm["content"] for mm in _thread_messages(channel, thread_ts))
        work = _work_from_thread(joined, thread_ts)
    if not work:
        _reply(channel, thread_ts,
              _WORK_NOT_FOUND_MSG)
        return True
    work = works.resolve(work) or work
    if etype is None:
        etype = _guess_ref_type(work, name)
    _reply(channel, thread_ts, f"🎨 <{work}> {name} 이미지를 AI로 생성할게요…")
    _post_element_candidate(channel, thread_ts, work, name, etype, context=trailing_context)
    return True

def _maybe_bare_costume_label_request(channel, thread_ts, query, event) -> bool:
    """★2026-07-15: "<코니> 연습복-A, 편하고 활동성 있는 반팔, 반바지"처럼 동사도 타입 키워드도
    없이 "{의상 라벨}, {설명}"만 온 등록 시도 — 기존 인식기(_TYPED_GEN_RE는 타입 키워드+생성
    동사 필수, _ELEMENT_GEN_RE는 이미지/사진+생성 동사 필수, _explicit_type_from_prefix는
    ':' 구분자 필수) 어디에도 안 걸려 디스패치 체인 맨 아래 스토리보드 자동 체인까지 떨어졌고,
    스레드가 이미 2단계면 상세 콘티를 통째로(1~10분) 재생성해버리는 실제 버그가 있었다.
    "연습복-A"/"잠옷-A"/"잠옷-B" 같은 "{한글}-{알파벳1글자}" 의상 라벨 컨벤션만 별도로 잡는다."""
    if _image_files(event):
        return False
    q = query
    wm = re.search(r"<\s*([^>]+?)\s*>", q)
    work = None
    if wm and not _looks_like_mention(wm.group(1)):
        work = wm.group(1).strip()
        q = (q[:wm.start()] + q[wm.end():]).strip()
    if _PROCEED_BLOCK_RE.search(q):
        return False
    m = _BARE_COSTUME_LABEL_RE.match(q)
    if not m:
        return False
    label = unicodedata.normalize("NFC", m.group("label")).strip()
    desc = m.group("desc").strip()
    if not work:
        joined = "\n".join(mm["content"] for mm in _thread_messages(channel, thread_ts))
        work = _work_from_thread(joined, thread_ts)
    if not work:
        _reply(channel, thread_ts, _WORK_NOT_FOUND_MSG)
        return True
    work = works.resolve(work) or work
    _reply(channel, thread_ts, f"🎨 <{work}> {label} 의상을 AI로 생성할게요…")
    _post_element_candidate(channel, thread_ts, work, label, "costume", context=desc)
    return True

def _maybe_natural_ref(channel, thread_ts, query, event) -> bool:
    """`[참조]` 없이 이미지+자연어("OO 이걸로 해줘")로 온 등록 의도를 감지해 확정 카드를 띄운다."""
    imgs = _image_files(event)
    if not imgs:
        return False
    q = query
    wm = re.search(r"<\s*([^>]+?)\s*>", q)
    work = None
    if wm and not _looks_like_mention(wm.group(1)):
        work = wm.group(1).strip()
        q = (q[:wm.start()] + q[wm.end():]).strip()
    m = _NATURAL_REF_RE.search(q)
    if not m:
        return False
    if not work:
        joined = "\n".join(mm["content"] for mm in _thread_messages(channel, thread_ts))
        work = _work_from_thread(joined, thread_ts)
    if not work:
        _reply(channel, thread_ts,
               _WORK_NOT_FOUND_MSG)
        return True
    work = works.resolve(work) or work
    name = unicodedata.normalize("NFC", m.group("name")).strip()
    if not name:
        return False
    etype = _guess_ref_type(work, name)
    pairs = [(name, imgs[0][1], imgs[0][2], imgs[0][3])]
    _post_ref_confirm(channel, thread_ts, work, etype, pairs)
    return True

def _maybe_typed_ref(channel, thread_ts, query, event) -> bool:
    """`[참조]`도 "이걸로 해줘"도 없이, '장소 <2번 방>'처럼 타입 키워드로 바로 시작하는
    이미지 첨부 메시지를 등록 시도로 인식한다."""
    imgs = _image_files(event)
    if not imgs:
        return False
    q = query
    work = None
    wm = SUB_RE.match(q)          # 맨 앞이 <작품> 태그면 채택(문자열 시작이 '<'일 때만 매치됨)
    if wm and not _looks_like_mention(wm.group(1).strip()):
        work = wm.group(1).strip()
        q = (wm.group(2) or "").strip()
    toks = q.split()
    if not toks or toks[0].lower() not in _REF_TYPE_KW:
        return False               # 타입 키워드로 시작 안 하면 이 흐름 아님
    etype, names = _parse_ref_command(q)
    if not names:
        return False
    if not work:
        joined = "\n".join(mm["content"] for mm in _thread_messages(channel, thread_ts))
        work = _work_from_thread(joined, thread_ts)
    if not work:
        _reply(channel, thread_ts,
               _WORK_NOT_FOUND_MSG)
        return True
    work = works.resolve(work) or work
    pairs, err = _pair_names_images(names, imgs)
    if err:
        _reply(channel, thread_ts, err); return True
    _post_ref_confirm(channel, thread_ts, work, etype, pairs)
    return True

_PLACE_FEEDBACK_KW_RE = re.compile(r"장소|배경")

_PLACE_FEEDBACK_NEG_RE = re.compile(
    r"마음에\s*안|맘에\s*안|맘에\s*들지|별로|이상해|이상하|안\s*어울|바꿔|다르게|안\s*맞|아쉬")

_PENDING_PLACE_FEEDBACK: dict[str, dict] = {}   # 버튼 메시지 ts -> {work, rest}

def _place_feedback_blocks():
    return [{
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "🏞️ 장소 생성"},
             "style": "primary", "action_id": "place_create"},
            {"type": "button", "text": {"type": "plain_text", "text": "🔄 스틸컷 재생성"},
             "action_id": "place_skip_regen"},
        ],
    }]

def _maybe_place_feedback(channel, thread_ts, query) -> bool:
    """스틸컷 스레드에서 '장소/배경 마음에 안 들어' 류 피드백 감지 → 그 씬에 등록된 장소가
    실제로 없으면(=미확정) 먼저 장소를 만들지, 그냥 재생성할지 물어본다."""
    info = still_state.get_last(thread_ts)
    if not info or not query.strip():
        return False
    if not (_PLACE_FEEDBACK_KW_RE.search(query) and _PLACE_FEEDBACK_NEG_RE.search(query)):
        return False

    work = info["work"]
    scene_num = info.get("scene_num")
    scene_text = ""
    if scene_num:
        # ★2026-07-16: 이 스레드에 실제로 추적 중인 화를 넘겨 다른 화의 스레드 텍스트를
        # 신뢰하지 않게 한다(_thread_conti의 episode 검증 참고).
        ep = (conti_state.get_episode(thread_ts) or {}).get("episode")
        conti = _thread_conti(channel, thread_ts, _thread_messages(channel, thread_ts), episode=ep)
        for num, hdr, body in (_split_scenes(conti) if conti else []):
            if num == scene_num:
                scene_text = f"{hdr}\n{body}"
                break
    places = [e["display"] for e in oi.load_elements(work) if e.get("type") == "place"]
    if any(p and p in scene_text for p in places):
        return False   # 이미 등록된 장소가 이 씬에 쓰이고 있음 — 이 흐름은 여기까지, 일반 처리로

    text = "⚠️ 이 씬의 장소 이미지가 아직 확정 안 됐어요. 장소 이미지를 먼저 만들까요? 아니면 일단 재생성할까요?"
    resp = app.client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=text, blocks=_with_text_block(text, _place_feedback_blocks()))
    _PENDING_PLACE_FEEDBACK[resp["ts"]] = {"work": work, "rest": info["rest"], "scene_text": scene_text}
    return True

@app.action("place_create")
def _act_place_create(ack, body):
    ack()
    msg_ts = body["message"]["ts"]
    p = _PENDING_PLACE_FEEDBACK.pop(msg_ts, None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요)."); return
    ch, tts = _action_ctx(body)
    scene_text = p.get("scene_text") or ""
    name = None
    if scene_text:
        try:
            raw = oi.chat(prompts.element_extract_system(_place_categories(p.get("work"))),
                         prompts.element_extract_user(scene_text), timeout=60)
            places = [c.strip() for c in (_parse_json_object(raw).get("places") or [])
                     if isinstance(c, str) and c.strip()]
            name = places[0] if places else None
        except Exception:
            log.exception("씬에서 장소 이름 추출 실패")
    if not name:
        _disable_buttons(body,
            f"⚠️ 이 씬에서 장소 이름을 자동으로 못 뽑았어요. 사진을 첨부하고 "
            f"`[참조] <{p['work']}> 장소 <이름>`으로 등록해주세요(또는 이미지 첨부 + \"<이름> 이걸로 해줘\"). "
            "등록되면 다시 `[스틸컷]`으로 돌려주세요.")
        return
    _disable_buttons(body, f"🎨 <{p['work']}> {name} 장소 이미지를 AI로 생성할게요…")
    _post_element_candidate(ch, tts, p["work"], name, "place", scene_text)

@app.action("place_skip_regen")
def _act_place_skip_regen(ack, body):
    ack()
    ch, tts = _action_ctx(body)
    msg_ts = body["message"]["ts"]
    p = _PENDING_PLACE_FEEDBACK.pop(msg_ts, None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요)."); return
    _disable_buttons(body, "🔄 재생성 중…")
    _do_stills(ch, tts, p["rest"])

_REGEN_FEEDBACK_RE = re.compile(
    r"별로|이상해|이상하|마음에\s*안|맘에\s*안|맘에\s*들지|다시\s*(만들|생성|해)|재생성|"
    r"안\s*어울|안\s*맞|바꿔|아쉬|불편|맘에\s*안들|어색|한\s*번\s*더|다시\s*한\s*번")

def _thread_last_marker(msgs):
    """이 스레드의 최근 봇 출력이 스틸컷이었는지 콘티 단계였는지(더 최근 걸로) 판단."""
    for m in reversed(msgs):
        if m["role"] != "assistant":
            continue
        c = m["content"]
        if "visual-pipeline 프로젝트에 저장돼요" in c or "스틸컷" in c:
            return "stillcut"
        if "[2단계]" in c or "[1단계]" in c or "씬 설계안" in c:
            return "conti"
    return None

def _maybe_generate_request(channel, thread_ts, query) -> bool:
    """콘티(2단계)가 이미 완료된 스레드에서 "씬3 스틸컷 생성"/"이미지 만들어줘"처럼 스틸컷/
    이미지를 직접 요청하면 그대로 생성으로 보낸다. 콘티가 아직 없으면(1단계까지만 진행) 그건
    아직 "스틸컷"이라는 단어가 이 스레드와 무관한 잡담일 수도 있으니 일반 라우팅에 맡긴다.
    ★단, 화 번호("3화")를 명시했으면 새 스레드라도 통과시킨다(2026-07-14) — 이미 상세 콘티가
    로컬/노션에 저장된 화인데도 "이 스레드엔 콘티가 없다"고 오판해 1단계부터 새로 돌리던
    문제(실무자 지적) — _do_stills/_do_images가 이제 그 화의 저장된 콘티를 스스로 찾아 쓴다.
    ★2026-07-16: 화 번호와 똑같은 이유로 씬 번호("씬3")도 통과시킨다(실사용자 사고 —
    "씬3 스틸컷 컷5,13,14 만들어줘"가 화 번호가 없다는 이유만으로 이 게이트를 못 넘고, 훨씬
    느슨한 catch-all(_do_storyboard_auto_chain, "씬 설계부터 새로 시작")로 새서 대본 씬설계를
    처음부터 다시 돌려버림). 씬 번호를 콕 집어 말한 것 자체가 "이미 있는 콘티의 특정 씬을
    가리키는 요청"이라는 강한 신호이므로 화 번호와 동일하게 취급한다.
    ★2026-07-15: 이 경로로 오는 자유 서술형 재생성 지시(각도/구도/자세 등)가 feedback 없이
    _do_stills로 넘어가 조용히 버려지던 실사용자 버그 — _split_regen_cut_override로
    컷 지정과 서술 지시를 분리해 feedback으로 넘긴다."""
    q = query or ""
    want_still = bool(_GEN_STILL_RE.search(q))
    want_image = bool(_GEN_IMAGE_RE.search(q)) and not want_still
    if not (want_still or want_image):
        return False
    _tracked_ctx = conti_state.get_episode(thread_ts) or {}
    if (sb_stage(_thread_messages(channel, thread_ts), work=_tracked_ctx.get("work"), episode=_tracked_ctx.get("episode")) < 2
            and not re.search(r"\d+\s*[화회]", q)
            and not re.search(r"씬\s*\d+", q)):
        return False
    if want_still:
        rest, feedback_text = _split_regen_cut_override(q, q)
        _do_stills(channel, thread_ts, rest, feedback=feedback_text or None)
    else:
        _do_images(channel, thread_ts, q)
    return True

_UNCONFIRM_CONTI_RE = re.compile(r"확정\s*(취소|해제|풀어)|확정\s*아니")

def _maybe_unconfirm_conti(channel, thread_ts, query) -> bool:
    """(C5, 2026-07-13) "확정 취소해줘"/"확정 해제해줘" — 실무자 최종본(human_final) 표시를
    되돌린다. 지금까지 이걸 되돌릴 방법이 버튼/명령 어디에도 없었음."""
    if not _UNCONFIRM_CONTI_RE.search(query or ""):
        return False
    rec = conti_state.get_episode(thread_ts)
    if not rec or not rec.get("human_final"):
        _reply(channel, thread_ts, "이 스레드는 확정된 콘티가 없어요."); return True
    conti_state.set_episode(thread_ts, rec["work"], rec.get("episode"), human_final=False)
    _reply(channel, thread_ts, "✅ 확정을 해제했어요 — 이제 [🔄 재생성]해도 경고 없이 바로 다시 만들어요.")
    return True

_LIST_WORKS_RE = re.compile(r"작품\s*목록|등록된?\s*작품\s*(뭐|보여|알려|목록)")

_THREAD_STATUS_RE = re.compile(r"몇\s*단계|무슨\s*작품|이\s*스레드.*(작품|단계)|지금\s*(단계|상태)")

def _maybe_list_works(channel, thread_ts, query) -> bool:
    """(C6, 2026-07-13) "작품 목록 보여줘" — 등록된 작품 이름을 나열.
    ★2026-07-20: 작품마다 장르(실사화/2D 애니메이션)가 등록될 수 있어서(works.get_style),
    작품 정보의 일부로 목록에 같이 보여준다 — 미지정이면 기본값(실사풍)이 괄호 없이 표시."""
    if not _LIST_WORKS_RE.search(query or ""):
        return False
    names = sorted(works.all_works().keys())
    if not names:
        _reply(channel, thread_ts, "등록된 작품이 없어요.")
        return True
    lines = []
    for n in names:
        style_key = works.get_style(n)
        genre_note = f" ({STYLE_LABELS[style_key]})" if style_key else ""
        lines.append(f"<{n}>{genre_note}")
    _reply(channel, thread_ts, "등록된 작품: " + ", ".join(lines))
    return True

def _maybe_thread_status(channel, thread_ts, query) -> bool:
    """(C6, 2026-07-13) "지금 몇 단계야?"/"이 스레드 무슨 작품이야?" — 이 스레드의 작품·화·단계·
    콘티확정 여부를 알려준다."""
    if not _THREAD_STATUS_RE.search(query or ""):
        return False
    msgs = _thread_messages(channel, thread_ts)
    joined = "\n".join(m["content"] for m in msgs)
    work = _work_from_thread(joined, thread_ts)
    rec = conti_state.get_episode(thread_ts) or {}
    stage = sb_stage(msgs, work=work, episode=rec.get("episode"))
    stage_label = {0: "아직 시작 전", 1: "1단계(씬설계) 완료", 2: "2단계(상세 콘티) 완료"}.get(stage, "알 수 없음")
    parts = [f"작품: {work or '미확인'}"]
    if rec.get("episode"):
        parts.append(f"화: {rec['episode']}화")
    parts.append(f"단계: {stage_label}")
    if rec.get("human_final"):
        parts.append("콘티 확정됨")
    # ★2026-07-20: 이 스레드가 추적 중인 작품의 장르(실사화/2D 애니메이션)도 같이 보여준다 —
    # 미지정이면 기본값(실사풍)이므로 굳이 표시하지 않는다(명시적으로 설정된 것만 표시).
    if work:
        style_key = works.get_style(work)
        if style_key:
            parts.append(f"장르: {STYLE_LABELS[style_key]}")
    _reply(channel, thread_ts, " · ".join(parts))
    return True

# (2026-07-16) "3화 뭐 안 만들어졌어?"/"3화 스틸컷 뭐 남았어?" — 자연어 진행 상황 질의.
# 화 번호(구조적 신호)가 없으면 이 정규식은 아예 안 걸리게 해서(예: 그냥 "뭐 남았어?") 다른
# 자연어 흐름과 오충돌하지 않게 한다 — episode 없이는 _do_episode_status도 어차피 되물으므로,
# 아예 "N화 ... 남았/안 만들/진행상황/뭐 없" 패턴으로 좁혀 구조적으로 명확한 질의만 받는다.
_EPISODE_STATUS_RE = re.compile(
    r"\d{1,3}\s*[화회].{0,20}(안\s*(만들|됐|끝났)|남았|뭐\s*없|진행\s*상황|미완성)"
)

def _maybe_episode_status(channel, thread_ts, query) -> bool:
    """(2026-07-16) `[진행상황]` 없이도 "3화 뭐 안 만들어졌어?"/"3화 스틸컷 뭐 남았어?"처럼
    자연어로 물으면 동일한 리포트를 보여준다. 읽기 전용(생성/삭제 없음)."""
    q = (query or "").strip()
    if not q or not _EPISODE_STATUS_RE.search(q):
        return False
    _do_episode_status(channel, thread_ts, q)
    return True

def _maybe_stillcut_regen_feedback(channel, thread_ts, query) -> bool:
    """스틸컷 결과 스레드에서 장소 얘기 없이도 "별로야/다시 만들어줘" 같은 일반 피드백이 오면
    콘티 수정 지시로 잘못 새지 않고 그 스틸컷을 재생성한다.
    ★2026-07-15: [🔄 재생성] 버튼을 거치지 않고 곧장 "4컷만 다시 만들고 싶어 옷에 장식
    없애고 잠옷A와 똑같이"처럼 자연어로 오는 이 경로가 query 내용을 통째로 버리고 info["rest"]
    그대로만 재실행해서 컷수 지정도 내용 지시도 전혀 반영 안 됐다(실사용자 리포트) —
    _maybe_stillcut_regen_ask_reply와 동일하게 _split_regen_cut_override로 처리."""
    info = still_state.get_last(thread_ts)
    if not info or not query.strip():
        return False
    if not _REGEN_FEEDBACK_RE.search(query):
        return False
    msgs = _thread_messages(channel, thread_ts)
    if _thread_last_marker(msgs) != "stillcut":
        return False   # 이 스레드에서 콘티 단계가 스틸컷보다 나중이면(=다시 콘티 얘기 중) 일반 처리로
    q = query.strip()
    _reply(channel, thread_ts, f"🔄 '{q}' 반영해서 다시 만들게요…")
    rest, feedback_text = _split_regen_cut_override(q, info["rest"])
    _do_stills(channel, thread_ts, rest, feedback=feedback_text or None)
    return True

_RETRY_FAILED_CUTS_RE = re.compile(r"실패한?\s*컷.{0,10}(다시|재시도|재생성)|(다시|재시도|재생성).{0,10}실패한?\s*컷")

def _maybe_retry_failed_cuts(channel, thread_ts, query) -> bool:
    """(C3, 2026-07-13) "실패한 컷만 다시 만들어줘" — 이번에 생성한 것 중 실패한 컷만
    재시도해 원래 성공한 컷들과 합쳐서 그리드를 다시 만든다(전체 재생성 안 함)."""
    if not _RETRY_FAILED_CUTS_RE.search(query or ""):
        return False
    st = _LAST_RENDER.get(thread_ts)
    if not st:
        _reply(channel, thread_ts, "다시 만들 대상이 없어요 — 방금 만든 이미지/스틸컷이 이 스레드에 없어요.")
        return True
    if all(r is not None for r in st["results"]):
        _reply(channel, thread_ts, "실패한 컷이 없어요 — 전부 성공했어요.")
        return True
    _reply(channel, thread_ts, "🔄 실패한 컷만 다시 만들게요…")
    _render_cuts_tracked("images", "", channel, thread_ts, st["work"], st["bible"], st["source_text"],
                         target=st["target"], title=st["title"], filename=st["filename"], cols=st["cols"],
                         aspect_ratio=st["aspect_ratio"], style_suffix=st["style_suffix"], no_text=st["no_text"],
                         retry_shots=st["shots"], retry_results=st["results"], retry_cost=st["total_cost"])
    return True

_VIDEO_FROM_STILL_RE = re.compile(
    r"(이|저)?\s*스틸컷.*영상|영상\s*(으?로|화)\s*(만들|해)|영상\s*(을|를)?\s*(만들|생성)|영상화\b"
)

_VIDEO_CUT_NUM_CUT_FIRST_RE = re.compile(r"컷\s*(\d+)")

_VIDEO_CUT_NUM_RE = re.compile(r"(\d+)\s*컷")

_VIDEO_CUT_RANGE_RE = re.compile(r"컷?\s*(\d+)\s*[~\-]\s*(\d+)\s*컷?")

_VIDEO_CUT_RANGE_FROM_TO_RE = re.compile(r"(\d+)\s*컷\s*부터\s*(\d+)\s*컷\s*까지")

def _match_video_cut_range(q: str):
    """"컷2-12"/"2~12컷"/"3컷부터 4컷까지" 표기를 모두 인식해 (lo, hi) 튜플로 반환(없으면 None).
    호출부 2곳(수동 영상화, 자동주행 컷 범위 지정)에서 표기가 갈라지지 않게 공용화."""
    m = _VIDEO_CUT_RANGE_RE.search(q) or _VIDEO_CUT_RANGE_FROM_TO_RE.search(q)
    if not m:
        return None
    return tuple(sorted((int(m.group(1)), int(m.group(2)))))

def _maybe_video_from_last_still(channel, thread_ts, query) -> bool:
    """"이 스틸컷으로 영상 만들고 싶어" 같은 요청 — 이미 확정 저장된 스틸컷의 컷별 원본을
    디스크에서 복원해 영상화 드롭다운을 다시 띄운다. 드롭다운 메시지가 만료됐거나(시간 경과)
    봇이 재시작돼 메모리 상태(_PENDING_VIDEO)가 날아간 뒤에도 동작하게(2026-07-13, "만료된
    요청이에요" 이슈 — 확정된 컷 데이터를 vp_store가 디스크에 영구 저장해두게 바꿔서 해결).
    ★"씬2 2컷으로 영상을 만들어줘"처럼 씬/컷 번호를 직접 지정하면 드롭다운 없이 바로
    그 컷으로 생성한다(2026-07-14 — "영상을 만들어줘"(을/를 조사) 인식 못 하던 것도 같이 수정,
    콘티 재생성으로 잘못 새던 문제)."""
    q = query or ""
    if not _VIDEO_FROM_STILL_RE.search(q):
        return False
    # ★확정된(=컷 원본이 실제로 저장된) 걸 우선 참조 — get_last는 확정 여부 상관없이
    # 생성할 때마다 갱신되므로, 그 뒤에 다른 씬을 확정 없이 한 번 더 생성만 해도
    # "컷별 원본을 못 찾았어요"로 잘못 새는 문제가 있었다(2026-07-14).
    info = still_state.get_confirmed(thread_ts) or still_state.get_last(thread_ts)
    # ★"<작품명> 씬2 2컷으로 영상을 만들어줘"처럼 작품명을 명시하면 그걸 우선 쓴다(2026-07-14).
    # 이 스레드에서 이미 여러 작품을 다뤘으면 저장된 last/confirmed가 다른 작품을 가리킬 수
    # 있어서, thread 상태만 믿으면 방금 만든 작품인데도 "컷별 원본을 못 찾았어요"로 잘못 샜다.
    wm = SUB_RE.match(q)
    if wm and not _looks_like_mention(wm.group(1).strip()):
        work = works.resolve(wm.group(1).strip()) or wm.group(1).strip()
    elif info:
        work = info["work"]
    else:
        # ★2026-07-15 "슬랙에서 새 메세지인데 씬1 영상화" — still_state는 thread_ts로만 찾아서,
        # 이 스레드에서 직접 스틸컷을 만든 적이 한 번도 없으면(예: 자동주행이 다른 스레드/세션
        # 에서 만들어 디스크에만 남은 경우) info가 항상 None이라 여기서 그냥 False를 반환해왔다
        # — 그러면 "씬1 영상화"가 스토리보드 자동체인으로 새서 콘티를 다시 만들어버리는 사고로
        # 이어진다("영상화"라는 명확한 단어가 있는데도). 스레드 문맥(_resolve_work_bible이 이미
        # 하는 작품명 추론)으로라도 작품을 찾아본다 — 그래도 못 찾으면 정말 단서가 없는 것이므로
        # 기존처럼 다른 핸들러로 넘긴다.
        work, _, _, _ = _resolve_work_bible(channel, thread_ts, q)
    if not work:
        return False
    # ★2026-07-14: 723273e는 [스틸컷] 명령(_do_stills)만 고쳤는데, "영상으로 만들어줘" 자연어
    # 경로(여기)는 애초에 화 번호를 전혀 안 봐서 conti_state에 기록될 일이 없었다 — 그래서
    # "3화 씬1 1컷으로 영상을 만들어줘"처럼 명시해도 나중에 vp_store.save_video가
    # conti_state.get_episode(thread_ts)로 화를 못 찾아 outputs/videos/미분류/에 계속 떨어짐.
    # _do_stills와 동일한 패턴으로 명시된 화 번호를 여기서도 바로 기록.
    epm = re.search(r"(\d{1,3})\s*[화회]", q)
    if epm:
        conti_state.set_episode(thread_ts, work, int(epm.group(1)))
    scene_m = _SCENE_NUM_RE.search(q)
    scene_num = (int(next(g for g in scene_m.groups() if g)) if scene_m
                else (info.get("scene_num") if info else None))
    # ★2026-07-15: info도 없고(=이 스레드 첫 언급) 씬 번호도 못 뽑았으면 어느 씬인지 알 길이
    # 없다 — 이 경우만 다른 핸들러로 넘긴다(work는 있어도 씬 특정이 안 되면 무의미).
    if scene_num is None and not info:
        return False
    # ★2026-07-15: save_video와 동일한 패턴으로 화 번호를 실어 outputs/stills/<화>/<씬>/ 경로를 찾는다.
    episode = (conti_state.get_episode(thread_ts) or {}).get("episode")
    cuts = vp_store.load_latest_cuts(work, scene_num, episode=episode)
    if not cuts:
        _reply(channel, thread_ts,
              "⚠️ 이 씬의 컷별 원본을 못 찾았어요 — `[스틸컷]`으로 다시 만들고 확정 저장한 뒤 시도해주세요.")
        return True
    title = f"스틸컷 씬{scene_num}" if scene_num else "스틸컷"
    # ★2026-07-15: "씬1 2~12 영상화"처럼 범위 지정 — 단일 컷 패턴보다 먼저 검사(순서 중요,
    # 안 그러면 "2~12"의 "12"가 "N컷" 패턴에 먼저 걸려 범위가 무시된다).
    range_lohi = _match_video_cut_range(q)
    if range_lohi:
        lo, hi = range_lohi
        picked = [c for c in cuts if lo <= c["n"] <= hi]
        if not picked:
            avail = ", ".join(str(c["n"]) for c in cuts)
            _reply(channel, thread_ts, f"컷{lo}~{hi} 범위에 해당하는 컷이 없어요. 있는 컷: {avail}")
            return True
        missing = sorted(set(range(lo, hi + 1)) - {c["n"] for c in picked})
        if missing:
            _reply(channel, thread_ts, f"⚠️ 컷{','.join(map(str, missing))}은 이 씬에 없어서 건너뛰어요.")
            # ★2026-07-15: 바로 다음에 "씬1 12컷 스틸컷"처럼 이 중 하나를 지목한 "N컷"이 오면
            # (_do_stills에서) 총개수(target)가 아니라 이 특정 컷(cut_filter)으로 되돌려 읽게
            # 기록해둔다 — 위 주석("실사용자 리포트") 참고.
            _RECENTLY_MISSING_CUTS[thread_ts] = {"work": work, "scene": scene_num, "cuts": set(missing)}
        _reply(channel, thread_ts, f"🎬 컷{lo}~{hi} ({len(picked)}개) 영상 순차 생성 시작… (컷마다 완료되는 대로 하나씩 올라와요)")
        _generate_videos_for_cuts(channel, thread_ts, work, title, picked, None)
        return True
    # ★2026-07-16: "컷3,5,8,9,12,14 영상 만들어줘" — 콤마로 여러 컷을 지정했는데 여기 로직이
    # 몰라서 아래 단일 컷 정규식(_VIDEO_CUT_NUM_CUT_FIRST_RE)이 맨 앞 숫자("3")만 집어먹고
    # 나머지(5,8,9,12,14)를 조용히 버리던 실사용 버그 — _do_stills(스틸컷)가 이미 쓰는
    # _parse_cut_filter("컷5,13,14"/"컷1,3-5" 형식, _do_compile의 씬 필터와 같은 유틸)를
    # 여기서도 재사용해 콤마·하이픈 혼합 리스트를 전부 인식한다. 물결(~) 범위(_match_video_cut_range,
    # "2~12")는 이미 위에서 따로 처리되므로 이 체크는 그 다음, 단일 컷 정규식보다 먼저 온다
    # (콤마 리스트가 단일 숫자 패턴에 절대 먼저 안 먹히게).
    cut_filter = _parse_cut_filter(q)
    if cut_filter and len(cut_filter) > 1:
        picked = [c for c in cuts if c["n"] in cut_filter]
        missing = sorted(cut_filter - {c["n"] for c in picked})
        if not picked:
            avail = ", ".join(str(c["n"]) for c in cuts)
            _reply(channel, thread_ts, f"컷{','.join(map(str, sorted(cut_filter)))} 중 이 씬에 있는 컷이 없어요. 있는 컷: {avail}")
            return True
        if missing:
            _reply(channel, thread_ts, f"⚠️ 컷{','.join(map(str, missing))}은 이 씬에 없어서 건너뛰어요.")
        label = ",".join(str(n) for n in sorted(c["n"] for c in picked))
        _reply(channel, thread_ts, f"🎬 컷{label} ({len(picked)}개) 영상 순차 생성 시작… (컷마다 완료되는 대로 하나씩 올라와요)")
        _generate_videos_for_cuts(channel, thread_ts, work, title, picked, None)
        return True
    # 2026-07-16: "컷 3개 만들어줘"(개수 요청)의 "3"이 컷 번호로 오인돼 "컷3을 못
    # 찾았어요"로 잘못 새던 버그 - 매치된 숫자 바로 뒤가 "개"(개/개를/개만 등 개수
    # 접미사)면 특정 컷 번호가 아니라 개수 표현이므로 매치를 취소하고, 아래로 떨어져
    # 드롭다운(_post_video_button)으로 안전하게 폴백한다(개수 전용 처리 로직은 아직 없음).
    cut_m = _VIDEO_CUT_NUM_CUT_FIRST_RE.search(q) or _VIDEO_CUT_NUM_RE.search(q)
    if cut_m and re.match(r"\s*개", q[cut_m.end():]):
        cut_m = None
    if cut_m:
        num = int(cut_m.group(1))
        cut = next((c for c in cuts if c["n"] == num), None)
        if not cut:
            avail = ", ".join(str(c["n"]) for c in cuts)
            _reply(channel, thread_ts, f"컷{num}을 못 찾았어요. 있는 컷: {avail}")
            return True
        if not _unregistered_mentions(work, cut):
            _reply(channel, thread_ts, f"🎬 컷 {num} 영상 생성 중… (수 분 소요될 수 있어요)")
        _maybe_generate_video_for_cut(channel, thread_ts, work, title, cut, num, None)
        return True
    _post_video_button(channel, thread_ts, {"work": work, "title": title, "cuts": cuts,
                                            "scene_seconds": None})
    return True

# renamed from _do_ref (name collision with the other bot's function of the same name, different behavior)
def sb_do_ref(channel, thread_ts, rest, event):
    """[참조] <작품> [인물|장소|소품] 이름[,이름2] + 이미지 첨부 → 이름/타입 확정 카드를 띄우고,
    「확정」을 눌러야 실제 저장(visual-pipeline fixed-images 우선, 없으면 data/refs 로컬).
    <작품> 생략 시 스레드에서 회수. 이름 생략 시 첨부 파일명을 이름 후보로. 첨부 없으면 등록 목록만 표시."""
    q = (rest or "").strip()
    work = None
    wm = SUB_RE.match(q)
    if wm and not _looks_like_mention(wm.group(1)):
        work = wm.group(1).strip()
        q = (wm.group(2) or "").strip()
    if not work:
        joined = "\n".join(m["content"] for m in _thread_messages(channel, thread_ts))
        work = _work_from_thread(joined, thread_ts)
    if not work:
        _reply(channel, thread_ts,
               "작품을 못 찾았어요. `[참조] <작품> 강태혁`처럼 작품을 적거나, 작품이 잡힌 스레드에서 보내주세요.")
        return
    # ★2026-07-14: 다른 자연어 경로들은 다 works.resolve()로 별칭을 정식 작품명으로 바꾸는데
    # 여기만 빠져 있어서, "[참조] <코니> ..."로 등록하면 정식명("cony 테스트 작품") 폴더가 아닌
    # 별칭("코니") 폴더에 elements.json/파일이 따로 생겨 등록 정보가 둘로 쪼개지는 문제가 있었다.
    work = works.resolve(work) or work
    etype, names = _parse_ref_command(q)

    imgs = _image_files(event)
    if not imgs:
        elems = oi.load_elements(work)
        if elems:
            by_t = {}
            for e in elems:
                by_t.setdefault(e.get("type", "person"), []).append(e.get("display", "?"))
            lines = [f"· {_REF_TLABEL.get(t, t)}: {', '.join(v)}" for t, v in by_t.items()]
            _reply(channel, thread_ts,
                   f"<{work}> 등록된 엘리먼트:\n" + "\n".join(lines)
                   + "\n새로 등록: 이미지 첨부 + `[참조] <작품> [인물|장소|소품] 이름`")
        else:
            _reply(channel, thread_ts,
                   f"이미지 첨부가 없어요. `[참조] <{work}> 강태혁`(인물) / `[참조] <{work}> 장소 왕좌의방`처럼 "
                   "이미지를 함께 올려주세요. (png·jpg·jpeg·webp)")
        return

    pairs, err = _pair_names_images(names, imgs)
    if err:
        _reply(channel, thread_ts, err); return

    _post_ref_confirm(channel, thread_ts, work, etype, pairs)

@app.action("ref_confirm")
def _act_ref_confirm(ack, body):
    ack()
    # (2026-07-14) thread_ts가 아니라 이 카드 자신의 메시지 ts로 찾는다 — 같은 스레드에 여러
    # 확정 카드가 동시에 떠 있어도 서로 안 섞이게(과거엔 thread_ts로만 찾아서, 나중 카드가
    # 앞 카드의 대기 상태를 덮어써 엉뚱한 게 저장되는 버그가 있었음).
    card_ts = body["message"]["ts"]
    p = _PENDING_REF.pop(card_ts, None) or _recover_pending_ref(body)
    if p and _PENDING_REF_BY_THREAD.get(p.get("thread_ts")) == card_ts:
        _PENDING_REF_BY_THREAD.pop(p["thread_ts"], None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 확정 요청이에요 — `[참조]`부터 다시 해주세요."); return
    saved, via_vp = _save_ref_pairs(p["work"], p["etype"], p["pairs"])
    if not saved:
        _disable_buttons(body, "저장할 이름을 못 읽었어요."); return
    tlabel = _REF_TLABEL.get(p["etype"], p["etype"])
    where = "visual-pipeline fixed-images" if via_vp else "data/refs"
    _disable_buttons(body,
        f"✅ <{p['work']}> {tlabel} 엘리먼트 등록: {', '.join(saved)} ({where}에 저장)\n"
        "이제 컷에 이게 등장하면 참조로 자동 첨부돼요(일관성 유지).")

@app.action("ref_edit")
def _act_ref_edit(ack, body):
    ack()
    card_ts = body["message"]["ts"]
    if card_ts not in _PENDING_REF:
        recovered = _recover_pending_ref(body)
        if not recovered:
            _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요) — `[참조]`부터 다시 해주세요."); return
        _PENDING_REF[card_ts] = recovered   # 복구한 상태를 다시 채워둬야 뒤이은 답장 수정이 먹힘
        _PENDING_REF_BY_THREAD[recovered["thread_ts"]] = card_ts
    _disable_buttons(body, "✏️ 이 스레드에 정확한 이름/타입으로 답장해주세요 (예: `인물 강태혁` / `장소 왕좌의방`).")

@app.action("still_confirm")
def _act_still_confirm(ack, body):
    ack()
    msg_ts = body["message"]["ts"]
    p = _PENDING_STILL.pop(msg_ts, None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요) — `[스틸컷]`부터 다시 해주세요."); return
    if not vp_store.available(p["work"]):
        _disable_buttons(body, f"⚠️ <{p['work']}>의 visual-pipeline 프로젝트를 못 찾아 별도 저장은 안 됐어요 "
                                "(위 슬랙 첨부 그리드가 결과물이에요).")
        return
    who = (body.get("user") or {}).get("id")
    ch, tts = _action_ctx(body)
    # ★2026-07-15: vp_store.save_still이 화/씬 폴더별 결정적 cut{n}.png 경로로 저장하는 구조로
    # 바뀌어(배치 간 경로 충돌 자체가 없어짐) batch_key 인자가 제거됨 — save_video와 동일한
    # 패턴으로 화 번호만 넘긴다.
    episode = (conti_state.get_episode(tts) or {}).get("episode")
    rel = vp_store.save_still(p["work"], scene_num=p["scene_num"], prompt_summary=p["title"],
                              png=p["grid_png"], requested_by=who, cuts=p.get("cuts"),
                              episode=episode)
    if rel:
        _disable_buttons(body, f"✅ 확정 저장됨 — <{p['work']}> 프로젝트의 `{rel}` 폴더에 컷별로 저장됐어요")
        still_state.set_confirmed(tts, p["work"], p["scene_num"], p["rest"])
        _post_video_button(ch, tts, p)
    else:
        _disable_buttons(body, "⚠️ 저장에 실패했어요.")

@app.action("figma_send_stillbatch")
def _act_figma_send_stillbatch(ack, body):
    """★2026-07-20 "안전필터 안 걸린 스틸컷도 그냥 피그마로 보내고 싶다" — still_confirm/
    still_regen과 달리 _PENDING_STILL을 pop하지 않고 peek만 한다(확정/재생성 버튼이 아직
    쓸 수 있어야 하므로 — 순서 무관하게 여러 번 눌러도 되게). 개별 컷 PNG(cuts)가 있으면
    컷마다 하나씩 큐에 올리고(어느 컷을 손볼지는 피그마에서 고르면 됨), 옛 콘티 폴백처럼
    개별 컷이 없으면 그리드 전체를 하나로 올린다."""
    ack()
    ch, tts = _action_ctx(body)
    msg_ts = body["message"]["ts"]
    p = _PENDING_STILL.get(msg_ts)
    if not p:
        _reply(ch, tts, "이 스틸컷 정보가 만료됐어요 — 다시 생성한 뒤 눌러주세요.")
        return
    if not config.FIGMA_BRIDGE_ENABLED:
        _reply(ch, tts, "⚠️ 피그마 브릿지가 꺼져있어요 — 봇 설정에서 SB_FIGMA_BRIDGE_ENABLED를 켜야 해요.")
        return
    cuts = [c for c in (p.get("cuts") or []) if c.get("png")]
    try:
        if cuts:
            # ★확정(still_confirm) 전에도 이 버튼을 누를 수 있어 아직 디스크에 파일이 없을 수
            # 있다 — 되돌리기(_on_figma_returned)가 덮어쓸 실제 경로가 있어야 하므로 확정과
            # 동일한 방식으로 미리 저장해둔다(cut{n}.png 파일명이 결정적이라 이후 확정 시
            # 다시 저장해도 안전하게 덮어써질 뿐이다).
            episode = (conti_state.get_episode(tts) or {}).get("episode")
            if vp_store.available(p.get("work")):
                vp_store.save_still(p["work"], scene_num=p.get("scene_num"), prompt_summary=p.get("title"),
                                    png=p["grid_png"], requested_by=(body.get("user") or {}).get("id"),
                                    cuts=p.get("cuts"), episode=episode)
            for c in cuts:
                still_path = vp_store.still_cut_path(p.get("work"), p.get("scene_num"), c["n"], episode=episode)
                figma_bridge.enqueue(c["png"], {
                    "work": p.get("work"), "scene_num": p.get("scene_num"), "cut_num": c.get("n"),
                    "reason": "사용자 요청",
                    "still_path": str(still_path) if still_path and still_path.exists() else None,
                    "channel": ch, "thread_ts": tts,
                })
            _reply(ch, tts, f"🎨 컷 {len(cuts)}개를 피그마 대기열에 올렸어요 — 플러그인을 실행하면 "
                           "캔버스에 자동으로 올라와요. 손본 뒤 「봇으로 보내기」를 누르면 여기로 반영돼요.")
        else:
            # ★그리드 합성본은 더 이상 디스크에 저장되지 않아(vp_store.save_still 참고) 되돌릴
            # 실제 파일이 없다 — still_path 없이 올려서 편집본이 돌아와도 되돌리기 반영은
            # 건너뛰게 한다(업로드 자체는 그대로 가능).
            figma_bridge.enqueue(p["grid_png"], {
                "work": p.get("work"), "scene_num": p.get("scene_num"), "cut_num": None,
                "reason": "사용자 요청", "still_path": None, "channel": ch, "thread_ts": tts,
            })
            _reply(ch, tts, "🎨 그리드 이미지를 피그마 대기열에 올렸어요 — 플러그인을 실행하면 캔버스에 자동으로 올라와요.")
    except Exception:
        log.exception("피그마 큐 등록 실패(스틸컷 배치)")
        _reply(ch, tts, "⚠️ 피그마로 보내기 실패 — 다시 시도해주세요.")

@app.action("still_regen")
def _act_still_regen(ack, body):
    """(2026-07-15) 클릭 즉시 재생성하지 않고, 어떻게 다시 만들지 먼저 물어본다 —
    자유 답변을 다음 메시지로 받아 반영(_maybe_stillcut_regen_ask_reply). 기존처럼 그냥
    똑같이 다시 만들고 싶은 사람을 위한 이스케이프 해치로 [🔁 그냥 재생성] 버튼도 같이 둔다."""
    ack()
    ch, tts = _action_ctx(body)
    msg_ts = body["message"]["ts"]
    p = _PENDING_STILL.pop(msg_ts, None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요) — `[스틸컷]`부터 다시 해주세요."); return
    _disable_buttons(body, "🔄 재생성 전에 확인할게요 ↓")
    # thread_ts로 키잉 — "다음 답글을 대기 중인 질문의 답으로 이어받는다"는 관례는
    # _PENDING_SCENE_PICK과 동일하게 맞춘다(버튼 메시지 ts가 아니라 스레드 기준으로 대기).
    _PENDING_STILL_REGEN_ASK[tts] = {"work": p["work"], "scene_num": p["scene_num"],
                                      "title": p["title"], "rest": p["rest"]}
    text = ("어떻게 다시 만들까요? (예: '인물 위치를 바꿔줘', '더 밝은 톤으로', '2번 컷만 다르게')\n"
            "자유롭게 말씀해주시면 반영해서 다시 만들게요.")
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "🔁 그냥 재생성"},
             "action_id": "still_regen_plain"},
        ]},
    ]
    app.client.chat_postMessage(channel=ch, thread_ts=tts, text=text, blocks=blocks)

@app.action("still_regen_plain")
def _act_still_regen_plain(ack, body):
    """[🔁 그냥 재생성] — 피드백 없이 기존과 동일하게 즉시 재생성(에스케이프 해치)."""
    ack()
    ch, tts = _action_ctx(body)
    p = _PENDING_STILL_REGEN_ASK.pop(tts, None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요) — `[스틸컷]`부터 다시 해주세요."); return
    _disable_buttons(body, "🔄 재생성 중…")
    _do_stills(ch, tts, p["rest"])

_REF_EDIT_EXCLUDE_RE = re.compile(
    r"생성|만들어|만들|영상|스틸\s*컷|콘티|스토리보드|싶어|해줘|해주세요|줘|주세요"
)

def _maybe_ref_edit_reply(channel, thread_ts, query) -> bool:
    """대기 중인 [참조] 확정이 있으면, 방금 온 일반 답장을 이름/타입 수정으로 보고 확정 카드를 다시 띄운다.
    ★"'리안'의 이미지를 다시 생성하고 싶어"처럼 전혀 다른 요청까지 "이름 수정"으로 잘못
    삼켜서(2026-07-14, 대기 중인 참조 카드가 있으면 그 스레드의 그 어떤 답장도 다 이걸로
    새던 버그) 통째로 이름란에 들어가버리는 문제 — 생성/영상/콘티 등 동사가 들어간 문장은
    이름 수정이 아니라 다른 의도이므로 여기서 안 먹고 뒤쪽 핸들러에 넘긴다."""
    card_ts = _PENDING_REF_BY_THREAD.get(thread_ts)
    p = _PENDING_REF.get(card_ts) if card_ts else None
    if not p or not query.strip() or _REF_EDIT_EXCLUDE_RE.search(query):
        return False
    etype, names = _parse_ref_command(query)
    pairs, err = _pair_names_images(names, p["pairs"])   # 기존 이미지 바이트(+url) 재사용
    if err:
        _reply(channel, thread_ts, err); return True
    _post_ref_confirm(channel, thread_ts, p["work"], etype, pairs)   # 새 카드로 교체(이전 카드는 무효화)
    return True

_CONTI_FINAL_NL_RE = re.compile(r"확정|최종|반영해|수정\s*완료|이걸로\s*진행")

def _resolve_work_for_conti_final(channel, thread_ts, rest):
    """`[콘티확정]`/자연어 확정 공통 — 요청 본문 또는 스레드 맥락에서 작품명을 뽑아낸다."""
    q = (rest or "").strip()
    work = None
    wm = SUB_RE.match(q)
    if wm and not _looks_like_mention(wm.group(1).strip()):
        work = wm.group(1).strip()
    if not work:
        joined = "\n".join(m["content"] for m in _thread_messages(channel, thread_ts))
        work = _work_from_thread(joined, thread_ts)
    if not work:
        return None
    return works.resolve(work) or work

def _apply_conti_final(channel, thread_ts, work, text):
    """실제 반영 로직 — `[콘티확정]` 경로와 자연어 확정(확인 버튼 클릭 후) 경로가 공유한다."""
    recorded = conti_state.get_episode(thread_ts) or {}
    _upload_conti(channel, thread_ts, work, text, episode=recorded.get("episode"))
    conti_state.set_episode(thread_ts, work, recorded.get("episode"), human_final=True)
    _reply(channel, thread_ts,
           "✅ 실무자 최종본으로 반영했어요. 이제부터 이 스레드의 `[이미지]`/`[스틸컷]`은 이 콘티를 기준으로 만들어요.")
    _post_buttons(channel, thread_ts, 2)

def _do_conti_final(channel, thread_ts, rest, event):
    """콘티 txt 첨부 + `[콘티확정]` → 그 내용을 최종 콘티로 재게시.
    이후 [이미지]/[스틸컷]은 이 파일을 쓰고, [🔄 재생성]은 경고를 한 번 거친다.
    (자연어 "확정"/"최종" 트리거는 _maybe_conti_final에서 확인 버튼을 거친 뒤 _apply_conti_final을 호출한다.)"""
    text, blocked = _files_text(event)
    if not text:
        msg = ("첨부된 콘티 파일을 못 읽었어요. txt 파일을 첨부하고 다시 보내주세요."
               if not blocked else
               "파일을 못 읽었어요. 다시 첨부해주시거나, 계속 안 되면 봇 관리자에게 알려주세요.")
        _reply(channel, thread_ts, msg)
        return
    work = _resolve_work_for_conti_final(channel, thread_ts, rest)
    if not work:
        _reply(channel, thread_ts, _WORK_NOT_FOUND_MSG)
        return
    _apply_conti_final(channel, thread_ts, work, text)

_PENDING_CONTI_FINAL_NL: dict[str, dict] = {}

def _conti_final_nl_confirm_blocks():
    return [{
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ 최종본으로 반영"},
             "style": "primary", "action_id": "conti_final_nl_confirm"},
            {"type": "button", "text": {"type": "plain_text", "text": "취소"},
             "action_id": "conti_final_nl_cancel"},
        ],
    }]

def _maybe_conti_final(channel, thread_ts, query, event) -> bool:
    """`[콘티확정]` 없이도, 콘티로 보이는 txt 첨부 + "확정"/"최종" 자연어면 최종본 반영으로 인식.
    단, 흔한 단어 매치이므로 바로 반영하지 않고 확인 버튼을 한 번 거친다(★2026-07-16)."""
    text, _blocked = _files_text(event)
    if not text or not _CONTI_FINAL_NL_RE.search(query or ""):
        return False
    work = _resolve_work_for_conti_final(channel, thread_ts, query)
    if not work:
        _reply(channel, thread_ts, _WORK_NOT_FOUND_MSG)
        return True
    _PENDING_CONTI_FINAL_NL[thread_ts] = {"work": work, "text": text}
    msg = (f"⚠️ 첨부한 파일을 <{work}> 콘티 최종본으로 반영할까요? "
           "(이후 [이미지]/[스틸컷]은 이 콘티를 기준으로 만들어요)")
    app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=msg,
                                blocks=_with_text_block(msg, _conti_final_nl_confirm_blocks()))
    return True

@app.action("conti_final_nl_confirm")
def _act_conti_final_nl_confirm(ack, body):
    ack()
    ch, tts = _action_ctx(body)
    if tts not in _PENDING_CONTI_FINAL_NL:   # {}가 아니라 실제 키를 저장하지만, 존재 여부로 일관되게 판정
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요) — 첨부 파일과 함께 다시 말씀해주세요."); return
    p = _PENDING_CONTI_FINAL_NL.pop(tts)
    _disable_buttons(body, "✅ 최종본으로 반영했어요…")
    _apply_conti_final(ch, tts, p["work"], p["text"])

@app.action("conti_final_nl_cancel")
def _act_conti_final_nl_cancel(ack, body):
    ack()
    ch, tts = _action_ctx(body)
    _PENDING_CONTI_FINAL_NL.pop(tts, None)
    _disable_buttons(body, "취소했어요 — 기존 콘티를 그대로 유지해요.")

_CONTI_FETCH_SRC_RE = re.compile(r"콘티|노션|로컬")

def _do_conti_fetch(channel, thread_ts, rest, event):
    """노션 「상세 콘티」에서 읽어와 이 스레드의 최종본으로 반영한다(읽기 전용 — 다시 노션에
    써넣지 않음). 자연어 콤보("노션에 콘티 있어, 이걸로 스틸컷 만들어줘" → _maybe_conti_use_
    then_generate)에서만 쓰인다. ★2026-07-16: 예전엔 여기서 방금 읽은 내용을 그대로
    _upload_conti로 다시 써서 노션 토글을 archive+재생성했는데, 아무것도 안 바뀐 내용을 매번
    덮어쓰는 의미 없는 처리였다("자꾸 새파일을 덮어씌워" — 실무자 지적) — 이제 읽기만 하고
    conti_state만 갱신한다. (첨부가 있으면 _do_conti_final과 동일하게 그 파일을 우선 씀)"""
    text, _blocked = _files_text(event)
    if text:
        _do_conti_final(channel, thread_ts, rest, event); return
    q = (rest or "").strip()
    work = None
    wm = SUB_RE.match(q)
    if wm and not _looks_like_mention(wm.group(1).strip()):
        work = wm.group(1).strip()
    if not work:
        joined = "\n".join(m["content"] for m in _thread_messages(channel, thread_ts))
        work = _work_from_thread(joined, thread_ts)
    if not work:
        _reply(channel, thread_ts, _WORK_NOT_FOUND_MSG)
        return
    work = works.resolve(work) or work
    # 화 번호는 이 메시지에 명시돼 있으면 그걸 우선(새 스레드라 conti_state가 비어있는 경우가
    # 많음 — 그때 conti_state만 보면 화를 몰라 노션의 화별 저장분을 못 찾는 버그가 있었음, 2026-07-13).
    epm = re.search(r"(\d{1,3})\s*[화회]", q) or re.search(r"(\d{1,3})\s*[화회]",
                     "\n".join(m["content"] for m in _thread_messages(channel, thread_ts)))
    episode = int(epm.group(1)) if epm else (conti_state.get_episode(thread_ts) or {}).get("episode")
    content, src = _fetch_external_conti(work, episode)
    if not content:
        pid = works.page_of(work)
        # ★2026-07-15: 항상 같은 안내문만 나가서 "왜 못 읽는지" 디버깅이 노션 페이지 구조를 직접
        # 뜯어봐야만 가능했다 — 페이지 연결 여부/화 번호 인식 여부는 구분해서 알려준다.
        if not config.NOTION_TOKEN or not pid:
            hint = "이 작품이 노션 페이지와 연결이 안 돼있는 것 같아요(통합 연결/등록 확인 필요)."
        elif episode:
            hint = (f"노션 페이지는 연결돼있는데 {episode}화 콘티를 못 찾았어요 — "
                    f"'{episode}화' 헤딩/토글 제목이나 '상세 콘티' 표기가 이 화 섹션 안에 있는지 확인해주세요.")
        else:
            hint = "몇 화인지 못 알아냈어요 — 메시지에 'N화'를 같이 적어주세요."
        _reply(channel, thread_ts,
               f"<{work}>의 로컬 파일이나 노션에서 콘티를 못 찾았어요. {hint} "
               "이미 노션에 쓴 상세 콘티가 없다면 `[스토리보드]`로 만들거나 \"노션에 저장해줘\"라고 말해주세요.")
        return
    conti_state.set_episode(thread_ts, work, episode, human_final=True)
    _reply(channel, thread_ts,
           f"✅ {src}에서 콘티를 가져와 최종본으로 반영했어요. 이제 이 스레드의 `[이미지]`/`[스틸컷]`은 이 버전 기준이에요.")
    _post_buttons(channel, thread_ts, 2)

_PENDING_COMPILE: dict[str, dict] = {}   # 커버리지 경고 메시지 ts -> {work,episode_title,conti,scenes}

_SCENE_FILTER_RE = re.compile(r"씬\s*(\d+(?:\s*[,\-]\s*\d+)*)")

def _parse_num_list(text: str) -> set[int]:
    """"1,3-5" 같은 콤마·하이픈 숫자열 → {1,3,4,5}. _parse_scene_filter/_parse_cut_filter 공용."""
    nums: set[int] = set()
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        rm = re.match(r"(\d+)-(\d+)$", part)
        if rm:
            a, b = int(rm.group(1)), int(rm.group(2))
            nums.update(range(min(a, b), max(a, b) + 1))
        elif part.isdigit():
            nums.add(int(part))
    return nums

def _parse_scene_filter(text: str) -> set[int] | None:
    m = _SCENE_FILTER_RE.search(text or "")
    if not m:
        return None
    return _parse_num_list(m.group(1)) or None

_CUT_FILTER_RE = re.compile(r"컷\s*(\d+(?:\s*[,\-]\s*\d+)*)|(\d+(?:\s*[,\-]\s*\d+)+)\s*컷")

def _parse_cut_filter(text: str) -> set[int] | None:
    m = _CUT_FILTER_RE.search(text or "")
    if not m:
        return None
    group = m.group(1) or m.group(2)
    return _parse_num_list(group) or None

def _do_compile(channel, thread_ts, rest):
    work, bible, tail, msgs = _resolve_work_bible(channel, thread_ts, rest)
    if not work:
        _reply(channel, thread_ts, _WORK_NOT_FOUND_MSG)
        return
    # ★2026-07-14: 화 번호는 이 메시지에 명시돼 있으면 우선(새 스레드라 conti_state가 비어있는
    # 경우가 많음 — _do_conti_fetch와 동일 패턴). _thread_conti만 쓰면 이 스레드에 콘티가 없을
    # 때(예: 스토리보드/영상화를 다른 스레드에서 이미 끝내고 새 스레드에서 바로 [합본]을 친
    # 경우) 이미 다 만들어 놨는데도 "먼저 스토리보드부터 하라"고 잘못 안내했다 —
    # _thread_or_saved_conti로 로컬/노션 저장분을 자동으로 끌어오게 한다.
    epm = re.search(r"(\d{1,3})\s*[화회]", tail)
    episode = int(epm.group(1)) if epm else (conti_state.get_episode(thread_ts) or {}).get("episode")
    # announce=False — 합본은 콘티를 그냥 갖다 쓰기만 하면 돼서, 스틸컷/이미지 흐름과 달리
    # 전체 콘티 파일을 스레드에 다시 뿌릴 필요가 없다(2026-07-14, 사용자 지적).
    # ★2026-07-16: episode를 넘겨 _thread_conti가 다른 화 추적 시점의 스레드 텍스트를
    # "이미 있다"고 오판하지 않게 한다(_thread_or_saved_conti와 동일 근거로 검증).
    had_thread_conti = bool(_thread_conti(channel, thread_ts, msgs, episode=episode))
    conti = _thread_or_saved_conti(channel, thread_ts, msgs, work, episode, announce=False)
    if conti and not had_thread_conti:
        _reply(channel, thread_ts, f"📄 <{work}> {episode}화 저장된 콘티를 불러왔어요." if episode
              else f"📄 <{work}> 저장된 콘티를 불러왔어요.")
    if not conti:
        _reply(channel, thread_ts,
              "먼저 `[스토리보드]`로 상세 콘티를 만들고, 각 씬을 `[영상화]`까지 마친 뒤 시도해주세요.")
        return
    scenes = _split_scenes(conti)
    if not scenes:
        _reply(channel, thread_ts,
              "이 콘티에서 씬을 나누지 못했어요. 콘티가 온전히 만들어지지 않은 것 같아요 — "
              "`[스토리보드]`로 상세 콘티를 다시 만들어 주시면 해결될 가능성이 높아요.")
        return
    scene_filter = _parse_scene_filter(tail)
    if scene_filter:
        scenes = [s for s in scenes if s[0] in scene_filter]
        if not scenes:
            _reply(channel, thread_ts, f"콘티에 그 씬 번호가 없어요 — {sorted(scene_filter)}")
            return
    scene_s = "_".join(str(n) for n, _h, _b in scenes) if scene_filter else None
    episode_title = f"{episode}화" + (f"_씬{scene_s}" if scene_s else "") if episode else \
        (f"합본_씬{scene_s}" if scene_s else "합본")
    videos_by_scene = video_index.list_episode_videos(work, [n for n, _h, _b in scenes], episode=episode)
    missing = [n for n, _h, _b in scenes if n not in videos_by_scene]
    if missing:
        miss_s = ", ".join(f"씬{n}" for n in missing)
        text = (f"⚠️ 아직 영상화 안 된 씬이 있어요 — {miss_s}\n"
               "이 씬들은 합본에서 빠져요. 그래도 지금 있는 컷만으로 만드시겠어요?")
        resp = app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=text,
            blocks=_with_text_block(text, [{
                "type": "actions",
                "elements": [{"type": "button", "text": {"type": "plain_text", "text": "🎬 합본 생성"},
                             "style": "primary", "action_id": "compile_confirm_missing"}],
            }]))
        _PENDING_COMPILE[resp["ts"]] = {"work": work, "episode_title": episode_title,
                                        "scenes": scenes, "videos_by_scene": videos_by_scene}
        return
    _run_compile(channel, thread_ts, work, episode_title, scenes, videos_by_scene)

@app.action("compile_confirm_missing")
def _act_compile_confirm_missing(ack, body):
    ack()
    msg_ts = body["message"]["ts"]
    p = _PENDING_COMPILE.pop(msg_ts, None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요)."); return
    _disable_buttons(body, "🎬 합본 생성 중… (수 분~수십 분 소요될 수 있어요)")
    ch, tts = _action_ctx(body)
    _run_compile(ch, tts, p["work"], p["episode_title"], p["scenes"], p["videos_by_scene"])

_PENDING_COMPILE_CONFIRM: dict[str, dict] = {}   # 확정/재생성 버튼 메시지 ts -> {work,episode_title,draft_path,scenes,videos_by_scene}

def _post_compile_confirm_buttons(channel, thread_ts, work, episode_title, draft_path, scenes, videos_by_scene):
    text = "이 합본을 최종본으로 저장할까요? (확정 안 하면 draft 상태로 남아요)"
    resp = app.client.chat_postMessage(
        channel=channel, thread_ts=thread_ts, text=text,
        blocks=_with_text_block(text, [{
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ 최종본으로 확정"},
                 "style": "primary", "action_id": "compile_confirm_final"},
                {"type": "button", "text": {"type": "plain_text", "text": "🔄 다시 만들기"},
                 "action_id": "compile_regenerate"},
            ],
        }]))
    _PENDING_COMPILE_CONFIRM[resp["ts"]] = {"work": work, "episode_title": episode_title,
                                            "draft_path": draft_path, "scenes": scenes,
                                            "videos_by_scene": videos_by_scene}

@app.action("compile_confirm_final")
def _act_compile_confirm_final(ack, body):
    ack()
    msg_ts = body["message"]["ts"]
    p = _PENDING_COMPILE_CONFIRM.pop(msg_ts, None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요)."); return
    try:
        final_path = episode_compile.confirm_final(p["draft_path"])
        _disable_buttons(body, f"✅ 최종본으로 저장했어요 — `{final_path}`")
    except Exception as e:
        log.exception("합본 확정 저장 실패")
        _disable_buttons(body, "⚠️ 확정 저장에 실패했어요. 잠시 후 다시 시도해주세요.")

@app.action("compile_regenerate")
def _act_compile_regenerate(ack, body):
    ack()
    msg_ts = body["message"]["ts"]
    p = _PENDING_COMPILE_CONFIRM.pop(msg_ts, None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요)."); return
    episode_compile.discard_draft(p["draft_path"])
    _disable_buttons(body, "🔄 다시 만드는 중…")
    ch, tts = _action_ctx(body)
    _run_compile(ch, tts, p["work"], p["episode_title"], p["scenes"], p["videos_by_scene"])

def _run_compile(channel, thread_ts, work, episode_title, scenes, videos_by_scene):
    jid = job_ledger.start_job("compile", channel, thread_ts, f"{work}|{episode_title}")
    ph = _thinking(channel, thread_ts, f"<{work}> {episode_title} 합본 편집 전략을 짜는 중…", stop_button=True)
    try:
        plan = edit_plan.build_edit_plan(work, episode_title, scenes, videos_by_scene, job_key=thread_ts)
        if not plan:
            _update_note(channel, ph, "편집 계획이 비었어요 — 실패", clear=True)
            _reply(channel, thread_ts,
                  "⚠️ 사용할 수 있는 컷을 찾지 못했어요. 콘티가 있는지, 원하시는 씬 번호가 맞는지 "
                  "다시 한번 확인해서 알려주세요 (예: '씬2 다시 만들어줘').")
            return
        # 배경음악(config.OPENROUTER_MUSIC_ENABLED, 기본 OFF)이 켜져 있을 때만 분위기 프롬프트를
        # 준비 — 꺼져 있으면 _work_mood_hint 호출도 생략해 기존 흐름을 그대로 둔다.
        mood_prompt = None
        note = f"{len(plan)}개 컷 렌더링 중…"
        if config.OPENROUTER_MUSIC_ENABLED:
            mood_prompt = music.build_mood_prompt(_work_mood_hint(work))
            note = f"{len(plan)}개 컷 렌더링 중… (배경음악 생성 포함)"
        _update_note(channel, ph, note)
        # ★2026-07-16: 위 note 한 번 찍고는 렌더링 끝날 때까지 아무 갱신이 없어(콘티/영상화의
        # "N/M 완료" 패턴과 달리) 오래 걸리는 화는 멈춘 건지 알 수 없었다 — episode_compile이
        # 세그먼트를 하나씩 이어붙일 때마다 부르는 progress_cb로 같은 ph 메시지를 갱신한다.
        def _compile_progress(done, total):
            _update_note(channel, ph, f"{note} ({done}/{total} 컷 처리)")
        path, bgm_path = episode_compile.compile_episode(work, episode_title, plan, mood_prompt=mood_prompt,
                                                         progress_cb=_compile_progress)
        _update_note(channel, ph, "✅ 합본 완성 (아래 파일 — 확정해야 최종본으로 남아요)", clear=True)
        app.client.files_upload_v2(channel=channel, thread_ts=thread_ts, file=path,
                                   filename=f"{episode_title}_합본.mp4",
                                   initial_comment=f"✅ 합본 완성 — {len(plan)}컷, 음성 없음(나레이션 타이밍 수정 중)\n`{path}`")
        # ★2026-07-20 "합본이 아직 안정되지 않았으니 배경음악을 합본에 바로 넣지 말고 따로
        # 다운 링크만" — episode_compile이 이제 배경음악을 합본 mp4에 섞지 않고 완전히 별도
        # mp3로 만들어 반환한다. 합본과 분리된 파일로만 올려서, 나레이션/편집 수정이 끝난
        # 뒤에 사용자가 직접 입힐지 말지 정하게 한다.
        if bgm_path:
            app.client.files_upload_v2(
                channel=channel, thread_ts=thread_ts, file=bgm_path,
                filename=f"{episode_title}_배경음악.mp3",
                initial_comment="🎵 배경음악(별도 파일 — 합본 영상에는 안 섞었어요). 나레이션/편집이 "
                                "안정되면 확인 후 직접 입혀주세요.")
        _post_compile_confirm_buttons(channel, thread_ts, work, episode_title, path, scenes, videos_by_scene)
    except Exception as e:
        log.exception("합본 생성 실패")
        _update_note(channel, ph, "⚠️ 실패", clear=True)
        _reply(channel, thread_ts, "⚠️ 합본 생성에 실패했어요. 잠시 후 다시 시도해주세요.")
    finally:
        job_ledger.finish_job(jid)

_QUESTION_END_RE = re.compile(r"\?\s*$")

_IMPERATIVE_RE = re.compile(r"해줘|해주세요|만들어|줘|가져와|가져다|반영해|저장해|올려줘|바꿔줘|변환해")

def _is_bare_question(q: str) -> bool:
    """(B2, 2026-07-13) "노션에 콘티 있어?"처럼 물음표로 끝나고 명령형 동사가 없는 순수 질문은
    실행하지 말고 그냥 통과시킨다(자동 실행하면 원치 않는 노션 저장/가져오기가 발생함)."""
    q = (q or "").strip()
    return bool(_QUESTION_END_RE.search(q) and not _IMPERATIVE_RE.search(q))

_CONTI_REWRITE_RE = re.compile(r"다시\s*(쓰|써|작성|만들|생성)|재작성|고치고\s*싶|수정하고\s*싶|고쳐\s*(줘|주세요)|바꾸고\s*싶|다시\s*하고\s*싶")

def _maybe_conti_rewrite_request(channel, thread_ts, query, event) -> bool:
    """"3화 상세 콘티 다시 쓰고 싶어"/"콘티 수정하고 싶어" 같은, 이미 존재하는 콘티를 고치고
    싶다는 자연어. "가져와"류 동사가 없어 _maybe_conti_fetch는 못 잡는다. 기존 콘티를 먼저
    노션/로컬에서 찾아 이 스레드로 반영한 뒤, 구체적으로 뭘 바꿀지가 메시지에 없으면 되묻고,
    있으면 곧장 2단계 수정 흐름(sb_do_storyboard stage=2)에 태운다."""
    if _is_bare_question(query):
        return False
    text, _blocked = _files_text(event)
    if text:
        return False   # 첨부 있으면 _maybe_conti_final이 처리
    q = query or ""
    if "콘티" not in q or not _CONTI_REWRITE_RE.search(q):
        return False
    work, rest_q = None, q
    wm = SUB_RE.match(q)
    if wm and not _looks_like_mention(wm.group(1).strip()):
        work, rest_q = wm.group(1).strip(), wm.group(2).strip()
    if not work:
        joined = "\n".join(m["content"] for m in _thread_messages(channel, thread_ts))
        work = _work_from_thread(joined, thread_ts)
    if not work:
        return False   # 작품을 못 찾으면 기존 흐름(대본 못 찾음 안내 등)에 맡긴다
    work = works.resolve(work) or work
    joined = "\n".join(m["content"] for m in _thread_messages(channel, thread_ts))
    epm = re.search(r"(\d{1,3})\s*[화회]", q) or re.search(r"(\d{1,3})\s*[화회]", joined)
    episode = int(epm.group(1)) if epm else (conti_state.get_episode(thread_ts) or {}).get("episode")
    content, src = _fetch_external_conti(work, episode)
    if not content:
        return False   # 고칠 기존 콘티가 아예 없으면 "다시 쓰기"가 아니라 새로 만들기 — 기존 흐름에 맡긴다
    # 트리거 표현·화 번호·조사/어미 찌꺼기를 걷어내고 남는 텍스트가 있으면 구체적 수정 지시로 본다.
    instr = re.sub(r"(\d{1,3})\s*[화회]", "", rest_q)
    instr = _CONTI_REWRITE_RE.sub("", instr)
    instr = re.sub(r"상세\s*콘티|콘티", "", instr)
    instr = re.sub(r"고\s*싶어|고\s*싶다|고\s*싶은데|싶어요?|싶다|줄래|해줘|줘|주세요|주라", "", instr)
    instr = instr.strip(" ,.!~？?！그걸을를이가는은도좀\n")
    _upload_conti(channel, thread_ts, work, content, episode=episode)
    conti_state.set_episode(thread_ts, work, episode, human_final=True)
    if len(instr) < 4:   # 구체적 지시가 없음 — 뭘 바꿀지 되묻는다
        _reply(channel, thread_ts,
               f"✅ <{work}> {(str(episode) + '화 ') if episode else ''}기존 상세 콘티를 {src}에서 "
               "불러왔어요. 어느 씬을, 어떻게 고치고 싶으세요? (예: \"씬2 대사 좀 더 짧게 해줘\")")
        return True
    sb_do_storyboard(channel, thread_ts, instr, stage=2)
    return True

_CONTI_EXISTS_TOKEN_RE = r"(?:이미(?!지)|있어|있음|있는데|(?:(?<=\s)|^)있는)"

_CONTI_EXISTS_SRC_RE = r"(?:콘티|노션|대본)"

_CONTI_EXISTS_RE = re.compile(
    _CONTI_EXISTS_SRC_RE + r".{0,15}?" + _CONTI_EXISTS_TOKEN_RE + r"|"
    + _CONTI_EXISTS_TOKEN_RE + r".{0,15}?" + _CONTI_EXISTS_SRC_RE
)

_GEN_STILL_RE = re.compile(r"스틸\s*컷")

_GEN_IMAGE_RE = re.compile(r"이미지")

_GEN_REQUEST_RE = re.compile(r"만들어|생성|줘|변환|바꿔|해줘")

def _maybe_conti_use_then_generate(channel, thread_ts, query, event) -> bool:
    """"노션에 이미 콘티 있어, 이걸로 스틸컷 만들어줘"처럼 "가져와"류 명령어 없이 존재 사실 +
    생성 요청만 있는 자연어 — 콘티 가져오기(_do_conti_fetch) + 곧장 이미지/스틸컷 생성까지
    한 번에 이어서 한다(2026-07-13)."""
    if _is_bare_question(query):
        return False
    text, _blocked = _files_text(event)
    if text:
        return False
    q = query or ""
    want_still, want_image = bool(_GEN_STILL_RE.search(q)), bool(_GEN_IMAGE_RE.search(q))
    if not (want_still or want_image):
        return False
    mentions_notion = "노션" in q   # (A4) "노션" 언급 자체로 존재("있어") 어미 요구 생략
    if not (_CONTI_FETCH_SRC_RE.search(q)
            and (mentions_notion or _CONTI_EXISTS_RE.search(q))
            and _GEN_REQUEST_RE.search(q)):
        return False
    _do_conti_fetch(channel, thread_ts, q, event)
    if not (conti_state.get_episode(thread_ts) or {}).get("human_final"):
        return True   # 콘티를 못 찾았으면 _do_conti_fetch가 이미 안내했으니 여기서 종료
    (_do_stills if want_still else _do_images)(channel, thread_ts, q)
    return True

_SKIP_TO_CONTI_RE = re.compile(r"상세\s*콘티(로|를)?\s*(바꿔|만들어|전환|해)|콘티로\s*바꿔")

_SKIP_TO_STILL_RE = re.compile(r"스틸\s*컷.{0,12}(만들어|생성)|(만들어|생성).{0,12}스틸\s*컷")

_SKIP_TO_IMAGE_RE = re.compile(r"이미지.{0,12}(만들어|생성)|(만들어|생성).{0,12}이미지")

def _maybe_script_to_conti(channel, thread_ts, query, event) -> bool:
    """대본/장면 텍스트를 채팅에 그냥 붙여넣고 "이거 상세 콘티로 바꿔봐"/"이걸로 스틸컷
    만들어줘"/"이미지로 만들어줘"처럼 단계를 건너뛰라고 하면, 그 텍스트를 대본 삼아 1단계
    (씬설계)를 조용히 한 번 돌린 뒤 2단계(상세 콘티)까지, 스틸컷/이미지까지 요청했으면
    3단계까지 곧장 이어서 만든다(2026-07-13) — [스토리보드] 없이도, 이전 단계 없이도 인식."""
    if _is_bare_question(query):
        return False
    text, _blocked = _files_text(event)
    q = (query or "") + (("\n" + text) if text else "")
    want_conti = bool(_SKIP_TO_CONTI_RE.search(q))
    want_still = bool(_SKIP_TO_STILL_RE.search(q))
    want_image = bool(_SKIP_TO_IMAGE_RE.search(q)) and not want_still
    if not (want_conti or want_still or want_image):
        return False
    # 트리거 문구가 있는 줄 전체를 버린다(그 줄 안의 "=> 이거" 같은 잔여 필러까지 대본에
    # 섞여 들어가던 버그 수정, 2026-07-13) — 문구만 부분 치환하면 "=> 이거 봐" 식으로 남았음.
    # 대사에 흔한 "만들어"/"줘" 단독으로는 안 지우고, 콘티/스틸컷/이미지 + 요청 동사가
    # 같이 있는 줄만 지워서 실제 대본 대사가 잘못 삭제되지 않게 한다.
    trigger_re = re.compile("|".join(p.pattern for p in
                            (_SKIP_TO_CONTI_RE, _SKIP_TO_STILL_RE, _SKIP_TO_IMAGE_RE)))
    lines = [ln for ln in q.split("\n") if not trigger_re.search(ln)]
    draft = "\n".join(lines).strip()
    if len(draft) < 100:   # 실제 대본 없이 문구만 온 거면 일반 라우팅에 맡김(과잉 트리거 방지)
        return False
    sb_do_storyboard(channel, thread_ts, draft, stage=1)
    sb_do_storyboard(channel, thread_ts, "", stage=2)
    if want_still:
        _do_stills(channel, thread_ts, "")
    elif want_image:
        _do_images(channel, thread_ts, "")
    return True

_SKIP_STAGE1_RE = re.compile(
    r"(1\s*단계|씬\s*설계)\s*(는)?\s*(건너뛰|스킵|생략|패스|없이|안\s*하고)|"
    r"(바로|곧장|곧바로)\s*(2\s*단계|상세\s*콘티|콘티)|"
    # (2026-07-15) "상세콘티 만들어줘"류 — "1단계 건너뛰고"처럼 명시적으로 스킵을 말하진
    # 않지만, 실무자가 가장 흔히 쓰는 평범한 표현이라 같은 스킵-후-체이닝 동작을 태운다.
    # "상세" 접두를 필수로 두어 bare "콘티"만으로는 트리거되지 않게 한다(과잉 트리거 방지) —
    # _maybe_conti_rewrite_request(다시 쓰고 싶어)/_maybe_conti_fetch(가져와/있어) 류가 더
    # 먼저 걸러가므로 여기 도달했다는 것 자체가 이미 "그냥 만들어줘" 의도임을 뜻한다.
    r"상세\s*콘티\s*(를|을)?\s*(만들|생성|써|쓰|해)")

def _maybe_skip_to_conti(channel, thread_ts, query, event) -> bool:
    """씬 설계(1단계)를 실제로 돌려 시간 배분된 씬 나누기는 그대로 얻되(=_do_storyboard의
    stage=1을 진짜로 호출), 그 결과에 대한 사용자 승인/수정 대기 없이 곧바로 stage=2(상세 콘티,
    씬 단위 병렬 생성)까지 이어서 진행한다. stage=1이 남기는 "[1단계]"/"씬 설계안" 배지가
    그대로 스레드에 남으므로, stage=2가 찾는 prior_plan(_last_assistant_with)도 정상 동작한다."""
    if _is_bare_question(query):
        return False
    text, _blocked = _files_text(event)
    q = (query or "") + (("\n" + text) if text else "")
    if not _SKIP_STAGE1_RE.search(q):
        return False
    rest = _SKIP_STAGE1_RE.sub("", q)
    rest = re.sub(r"\s{2,}", " ", rest).strip()
    sb_do_storyboard(channel, thread_ts, rest, stage=1)
    sb_do_storyboard(channel, thread_ts, "", stage=2)
    return True

_STOP_RE = re.compile(
    r"^(?:아|어|야|잠깐|잠시)?\s*(멈춰|중단|중지|그만|취소|스탑|stop)"
    r"(?:해|해줘|해주세요|해봐|봐|줘|줄래)?\s*[!.?~]*$", re.I)

_RETRY_INTERRUPTED_RE = re.compile(
    r"^(?:아|어)?\s*(?:재생성|재시도|다시|이어서|계속|한\s*번\s*더|retry)\s*"
    r"(?:해\s*(?:줘|주세요|볼래|볼까|봐)|할래|하고\s*싶어|할\s*수\s*있을까|"
    r"부탁(?:해요?|드려요|합니다)?|진행(?:해\s*줘|해\s*주세요)?|"
    r"시도(?:해\s*줘|해\s*주세요)?|생성(?:해\s*줘|해\s*주세요)?)?"
    r"\s*[!.?~]*$", re.I)

_REL_EP_RE = re.compile(r"(이번|다음|이전)\s*화")

_REL_EP_ACTION_HINT_RE = re.compile(r"스토리보드|씬\s*설계|콘티|스틸컷|이미지|영상|만들어|해줘|진행|시작")

def _normalize_episode_refs(text, thread_ts):
    """(A9, 2026-07-13) "이번 화"/"다음 화"/"이전 화"를 이 스레드에 기록된 화 번호 기준으로
    실제 숫자+화로 바꿔서 이후 모든 "\\d+화" 파싱이 그대로 먹히게 한다. 한글 숫자("사화" 등)는
    "전화"(phone call)류 흔한 단어와 충돌 위험이 커 일부러 지원 안 함 — 아라비아 숫자 권장.
    반환값은 (치환된 텍스트, 실제 치환이 일어났으면 대표로 해석된 화 번호(int) 아니면 None) —
    여러 개의 상대 화 참조가 섞여 있어도 첫 번째 매칭의 화 번호 하나만 대표로 반환한다."""
    if not text or not _REL_EP_RE.search(text):
        return text, None
    if not _REL_EP_ACTION_HINT_RE.search(text):
        return text, None
    tracked = (conti_state.get_episode(thread_ts) or {}).get("episode")
    if tracked is None:
        return text, None

    resolved = []

    def _repl(m):
        w = m.group(1)
        if w == "이번":
            ep = tracked
        elif w == "다음":
            ep = tracked + 1
        else:
            ep = tracked - 1   # 이전
        resolved.append(ep)
        return f"{ep}화"

    new_text = _REL_EP_RE.sub(_repl, text)
    return new_text, (resolved[0] if resolved else None)

_SEEN_EVENT_LOCK = threading.Lock()

_SEEN_EVENT_KEYS: dict[tuple, float] = {}   # (channel, ts) -> 처음 본 시각(monotonic)

_SEEN_EVENT_TTL_SEC = 600   # 10분 지나면 정리(무한정 안 쌓이게) — 재시도는 보통 수 초~분 내 옴

def _is_duplicate_event(event) -> bool:
    """★2026-07-15: Slack은 ack가 조금만 늦어도(네트워크 지연·재연결 등) 같은 이벤트를 한 번
    더 보낼 수 있는데("at-least-once" 전달), 이 코드베이스엔 이벤트 중복 체크가 전혀 없었다 —
    실사용자 리포트: "컷5만 다시 생성" 요청이 처리되는 동안(재생성 대기 상태 소비) 같은
    메시지가 한 번 더 들어와 그 대기 상태가 이미 없어져 완전히 다른 핸들러(스토리보드 자동
    체인)로 새서 상세 콘티를 뜬금없이 재생성해버렸다. channel+ts(Slack 메시지 고유 식별자,
    재전달돼도 동일)로 이미 처리한 이벤트인지 확인 — 처리한 적 있으면 조용히 건너뛴다(이미
    ack는 보냈으니 사용자에게 영향 없음)."""
    key = (event.get("channel"), event.get("ts"))
    if not key[1]:
        return False   # ts 없는 이벤트(버튼 액션 등은 여기 안 옴)는 판단 불가 — 그냥 처리
    now = time.monotonic()
    with _SEEN_EVENT_LOCK:
        for k in [k for k, t in _SEEN_EVENT_KEYS.items() if now - t > _SEEN_EVENT_TTL_SEC]:
            del _SEEN_EVENT_KEYS[k]
        if key in _SEEN_EVENT_KEYS:
            return True
        _SEEN_EVENT_KEYS[key] = now
        return False

def _is_active_bot_thread(channel, thread_ts) -> bool:
    """멘션 없는 채널 답글을 처리할지 판단 — 이 스레드에 봇이 이미 답변한 적 있으면(진행 중인
    스토리보드 스레드) 매번 @storyboard 안 붙여도 이어서 알아듣게 한다(2026-07-13, 사용성
    피드백: "채널에서는 매 답글마다 멘션을 붙여야만 봇이 봄"). 그 외 채널 잡담엔 안 끼어든다."""
    msgs = _thread_messages(channel, thread_ts)
    return any(m["role"] == "assistant" for m in msgs)

@app.action("sb_pass_plan")      # 씬 설계 통과 → 상세 콘티 자동 생성
def _act_pass_plan(ack, body):
    ack()
    ch, tts = _action_ctx(body)
    _disable_buttons(body, "✅ 통과 — 상세 콘티 생성 중…")
    sb_do_storyboard(ch, tts, "", stage=2)

@app.action("sb_regen_plan")     # 씬 설계 재생성
def _act_regen_plan(ack, body):
    """★2026-07-16: 클릭 즉시 재생성하지 않고, 어떻게 다시 만들지 먼저 물어본다 —
    자유 답변을 다음 메시지로 받아 반영(_maybe_planregen_ask_reply). 기존처럼 그냥 똑같이
    다시 만들고 싶은 사람을 위한 이스케이프 해치로 [🔁 그냥 재생성] 버튼도 같이 둔다."""
    ack()
    ch, tts = _action_ctx(body)
    _disable_buttons(body, "🔄 재생성 전에 확인할게요 ↓")
    _PENDING_PLAN_REGEN_ASK[tts] = {}
    text = ("어떻게 다시 만들까요? (예: '3번 씬을 낮 장면으로 바꿔줘', '전체적으로 더 긴장감 있게')\n"
            "자유롭게 말씀해주시면 반영해서 다시 만들게요.")
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "🔁 그냥 재생성"},
             "action_id": "sb_regen_plan_plain"},
        ]},
    ]
    app.client.chat_postMessage(channel=ch, thread_ts=tts, text=text, blocks=blocks)

@app.action("sb_regen_plan_plain")
def _act_regen_plan_plain(ack, body):
    """[🔁 그냥 재생성] — 피드백 없이 기존과 동일하게 즉시 재생성(에스케이프 해치)."""
    ack()
    ch, tts = _action_ctx(body)
    if tts not in _PENDING_PLAN_REGEN_ASK:   # {}를 저장하므로 `not p`가 아니라 존재 여부로 판정
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요) — `[스토리보드] <작품> N화`부터 다시 해주세요."); return
    _PENDING_PLAN_REGEN_ASK.pop(tts, None)
    _disable_buttons(body, "🔄 씬 설계 재생성 중…")
    sb_do_storyboard(ch, tts, "", stage=1)

def _scene_picker_blocks(scenes):
    options = [{"text": {"type": "plain_text", "text": "🖼️ 전체 씬 (모두)"}, "value": "all"}]
    for num, hdr, _ in scenes[:99]:   # static_select 옵션 상한(100) 여유
        label = f"씬{num} · {hdr}"[:75]   # Slack 옵션 텍스트 75자 제한
        options.append({"text": {"type": "plain_text", "text": label}, "value": str(num)})
    return [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": "어떤 씬을 이미지로 만들까요?"},
        "accessory": {
            "type": "static_select",
            "placeholder": {"type": "plain_text", "text": "씬 선택"},
            "action_id": "sb_pick_scene",
            "options": options,
        },
    }]

@app.action("sb_pass_conti")     # 상세 콘티 통과 → 어떤 씬을 만들지 선택
def _act_pass_conti(ack, body):
    ack()
    ch, tts = _action_ctx(body)
    msgs = _thread_messages(ch, tts)
    # ★2026-07-16: 추적 중인 화를 넘겨 다른 화의 스레드 텍스트를 신뢰하지 않게 한다.
    ep = (conti_state.get_episode(tts) or {}).get("episode")
    conti = _thread_conti(ch, tts, msgs, episode=ep)
    scenes = _split_scenes(conti) if conti else []
    if not scenes:   # 씬 헤더 없는 옛 콘티 → 고를 게 없으니 바로 전체 생성
        _disable_buttons(body, "✅ 통과 — 이미지 생성 중…")
        _do_images(ch, tts, "")
        return
    _disable_buttons(body, "✅ 통과 — 어느 씬을 만들지 골라주세요 ↓")
    app.client.chat_postMessage(channel=ch, thread_ts=tts,
                                text="어떤 씬을 이미지로 만들까요?",
                                blocks=_scene_picker_blocks(scenes))

@app.action("sb_pick_scene")      # 씬 선택 드롭다운 → 그 씬만(또는 전체) 생성
def _act_pick_scene(ack, body):
    ack()
    ch, tts = _action_ctx(body)
    val = body["actions"][0]["selected_option"]["value"]
    if val == "all":
        _disable_buttons(body, "✅ 전체 씬 이미지 생성 중…")
        _do_images(ch, tts, "")
    else:
        _disable_buttons(body, f"✅ 씬{val} 스틸컷 생성 중…")
        _do_stills(ch, tts, f"씬{val}")

def _regen_conti_confirm_blocks():
    return [{
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "⚠️ 그래도 새로 만들기"},
             "style": "danger", "action_id": "sb_regen_conti_force"},
            {"type": "button", "text": {"type": "plain_text", "text": "취소"},
             "action_id": "sb_regen_conti_cancel"},
        ],
    }]

@app.action("sb_regen_conti")    # 상세 콘티 재생성
def _act_regen_conti(ack, body):
    ack()
    ch, tts = _action_ctx(body)
    if conti_state.is_human_final(tts):
        _disable_buttons(body, "⚠️ 재생성 확인 필요 ↓")
        text = "⚠️ 이 스레드엔 실무자가 확정한 콘티가 있어요. 그래도 새로 만들까요? (덮어씌워집니다)"
        app.client.chat_postMessage(channel=ch, thread_ts=tts, text=text,
                                    blocks=_with_text_block(text, _regen_conti_confirm_blocks()))
        return
    _disable_buttons(body, "🔄 상세 콘티 재생성 중…")
    sb_do_storyboard(ch, tts, "", stage=2)

@app.action("sb_regen_conti_force")
def _act_regen_conti_force(ack, body):
    ack()
    ch, tts = _action_ctx(body)
    _disable_buttons(body, "🔄 상세 콘티 재생성 중…(실무자 확정본 덮어씀)")
    sb_do_storyboard(ch, tts, "", stage=2)

@app.action("sb_regen_conti_cancel")
def _act_regen_conti_cancel(ack, body):
    ack()
    _disable_buttons(body, "취소됨 — 실무자 확정 콘티를 그대로 유지해요.")

def _do_save_conti(ch, tts, rest=""):
    """상세 콘티를 그 작품 노션 페이지에 저장 — [💾 노션에 저장] 버튼과 자연어("노션에 저장해줘"/
    "동기화해") 양쪽이 공유하는 실제 로직(2026-07-13, A5 자연어 확장 시 분리).

    ★2026-07-14: "3화 상세콘티 동기화해"처럼 새 스레드(콘티가 이 스레드엔 안 붙어있음)에서
    바로 요청하면 "저장할 상세 콘티를 못 찾았어요"로 실패했다 — 스틸컷/영상화가 쓰는
    _thread_or_saved_conti로 로컬/노션 저장분을 자동 회수하도록 스틸컷과 동일하게 맞춤."""
    msgs = _thread_messages(ch, tts)
    joined = "\n".join(m["content"] for m in msgs)
    work = _work_from_thread(joined, tts)
    if not work:
        _reply(ch, tts, "⚠️ " + _WORK_NOT_FOUND_MSG); return
    work = works.resolve(work) or work
    epm = re.search(r"(\d{1,3})\s*[화회]", rest or "")
    episode = int(epm.group(1)) if epm else (conti_state.get_episode(tts) or {}).get("episode")
    conti = _strip_conti_preamble(_thread_or_saved_conti(ch, tts, msgs, work, episode) or "")
    if not conti:
        _reply(ch, tts, "⚠️ 저장할 상세 콘티를 못 찾았어요."); return
    if not config.NOTION_TOKEN:
        _reply(ch, tts, "⚠️ `NOTION_TOKEN`이 설정 안 돼 있어서 노션 저장을 못 해요."); return
    pid = works.page_of(work)
    if not pid:
        _reply(ch, tts, f"⚠️ <{work}>의 노션 페이지를 찾지 못했어요."); return
    episode = (conti_state.get_episode(tts) or {}).get("episode") or episode
    try:
        from bot.shared import notion_sync
        # "상세 콘티 (N화)" 토글 안에 코드 블록으로 — 접어두면 페이지가 안 지저분해지고,
        # 펼치면 클릭 없이 바로 읽힌다(2026-07-13). 화 번호를 알면 그 화 대본 섹션 바로 아래,
        # 모르면(과거 방식) page-level 텍스트 섹션에 저장.
        if episode:
            notion_sync.upsert_conti_toggle_for_episode(pid, episode, conti, token=config.NOTION_TOKEN)
            where = f"{episode}화 대본 아래 토글"
        else:
            notion_sync.upsert_section(pid, _NOTION_CONTI_HEADING, conti, token=config.NOTION_TOKEN)
            where = f"「{_NOTION_CONTI_HEADING}」 섹션"
        _reply(ch, tts,
               f"✅ <{work}> 노션 페이지의 {where}에 저장했어요. "
               "노션에서 직접 고쳐도 돼요 — 다음에 `[스틸컷]`/`[이미지]` 등을 요청하면 자동으로 최신 내용을 다시 가져와요.")
    except Exception as e:
        log.exception("notion 콘티 저장 실패")
        _reply(ch, tts, "⚠️ 노션 저장 중 오류가 났어요. 잠시 후 다시 시도해주시고, 계속 안 되면 봇 관리자에게 알려주세요.")

@app.action("sb_confirm_cuts")   # (C4) 컷 수 확인 후 실제 생성 진행
def _act_confirm_cuts(ack, body):
    ack()
    ch, tts = _action_ctx(body)
    p = _PENDING_CUT_CONFIRM.pop(tts, None)
    if not p:
        return
    _disable_buttons(body, f"✅ {len(p['shots'])}컷 생성 시작…")
    _render_cuts_tracked(p["kind"], p["orig_rest"], ch, tts, p["work"], p["bible"], p["source_text"],
                         title=p["title"], filename=p["filename"], cols=p["cols"],
                         aspect_ratio=p["aspect_ratio"], style_suffix=p["style_suffix"],
                         no_text=p["no_text"], retry_shots=p["shots"],
                         retry_results=[None] * len(p["shots"]), skip_confirm=True,
                         group_bounds=p.get("group_bounds"))

@app.action("sb_cancel_cuts")    # (C4) 컷 수 확인 취소
def _act_cancel_cuts(ack, body):
    ack()
    ch, tts = _action_ctx(body)
    _PENDING_CUT_CONFIRM.pop(tts, None)
    _disable_buttons(body, "❌ 취소했어요.")

@app.action("sb_save_conti")    # 상세 콘티를 그 작품의 노션 페이지 맨 밑에 저장
def _act_save_conti(ack, body):
    ack()
    ch, tts = _action_ctx(body)
    _do_save_conti(ch, tts)

_NOTION_SAVE_NL_RE = re.compile(r"노션\s*(에|에다가?)?\s*(저장|올려|기록)|동기화")

def _maybe_notion_save_request(channel, thread_ts, query) -> bool:
    """(A5, 2026-07-13) 버튼 없이 "노션에 저장해줘"/"노션에 올려줘"/"동기화해" 같은 자연어로도
    [💾 노션에 저장]과 동일하게 처리."""
    if _is_bare_question(query) or not _NOTION_SAVE_NL_RE.search(query or ""):
        return False
    _do_save_conti(channel, thread_ts, rest=query)
    return True

def _resume_pending_jobs():
    """지난 실행이 [이미지]/[스틸컷]/씬설계/상세콘티 도중 죽어서(재시작·크래시 등) 못 끝낸 작업 처리.
    [이미지]/[스틸컷]은 그대로 자동 재개. 씬설계/상세콘티(plan/conti)는 스레드 맥락(수정 지시 등)이
    복잡해 그대로 재실행하면 위험할 수 있어 자동 재개 대신 "끊겼다"고 바로 알림만(2026-07-13)."""
    jobs = job_ledger.pending_jobs()
    if not jobs:
        return
    log.info("재시작 복구: 끊긴 생성 작업 %d개", len(jobs))
    for j in jobs:
        job_ledger.finish_job(j["id"])   # 옛 기록 제거 — 재실행하면 새 기록이 새로 생김
        ch, tts, kind, rest = j["channel"], j["thread_ts"], j["kind"], j["rest"]
        if kind == "autopilot":
            # ★2026-07-15: "plan"/"conti"처럼 interrupted_state.mark로 넘기면 "재생성해줘"가
            # sb_do_storyboard(stage=2)로만 재시도돼 자동주행 자체는 다시 안 도는데, 그렇다고
            # kind 미매치로 아래 else 분기(_do_stills)에 떨어지면 rest(=원래 [자동주행] 명령
            # 문자열)를 엉뚱한 함수에 넘기게 된다 — 안전하게 안내만 하고 자동 재개는 하지 않는다.
            try:
                _reply(ch, tts, "⚠️ 봇이 재시작되면서 진행 중이던 자동주행이 끊겼어요. "
                                f"같은 명령(`{rest}`)으로 다시 시작해주세요(중간 결과는 이미 저장된 것만 남아있어요).")
            except Exception:
                log.exception("복구 알림 전송 실패")
            continue
        # ★2026-07-15: 자동주행이 씬마다 만드는 스틸컷은 kind="stills"로 별도 등록돼(rest에
        # "[자동주행] {work} {episode}화 씬{num}" 마커) 위 kind=="autopilot" 분기를 안 타고
        # 아래 일반 "stills" 자동 재개 경로로 떨어져, 죽기 직전 씬을 조용히 한 번 더(중복 비용)
        # 재생성해버리는 사고 위험이 있었다(리뷰 지적). 이 마커가 있으면 kind 무관하게 자동
        # 재개하지 않는다 — 다만 완전히 막지 않고, 실제로 그 씬이 이미 저장까지 끝났는지
        # vp_store에 기록된 상태를 확인해 로그로 남긴다("몇 씬 몇 컷까지 만들었는지 기록하면
        # 되지 않나" — 사용자 제안). 위 kind=="autopilot" 항목이 이미 사용자에게 재시작
        # 안내를 하므로 여기서 별도로 또 안내하지 않고 조용히 정리만 한다(이중 알림 방지).
        am = re.match(r"^\[자동주행\]\s+(\S+)\s+(\d+)화\s*씬(\d+)$", rest or "")
        if am:
            aw_work, aw_ep, aw_scene = am.group(1), int(am.group(2)), int(am.group(3))
            try:
                done = bool(vp_store.load_latest_cuts(aw_work, aw_scene, episode=aw_ep))
            except Exception:
                done = False
            log.info("자동주행 복구: <%s> %s화 씬%s 하위 job 폐기 (저장 완료 여부: %s)",
                     aw_work, aw_ep, aw_scene, done)
            continue
        if kind in ("plan", "conti"):
            interrupted_state.mark(tts, kind, rest)   # "재생성해줘"로 바로 이어서 재시도할 수 있게
            try:
                _reply(ch, tts, "⚠️ 봇이 재시작되면서 진행 중이던 생성이 끊겼어요(타임아웃 아님). "
                                "\"재생성해줘\"라고 답글 달면 끊긴 그 작업을 그대로 이어서 다시 할게요.")
            except Exception:
                log.exception("복구 알림 전송 실패")
            continue
        if kind == "video":
            # 영상화는 컷 원본(png)이 job_ledger에 저장 안 돼있어 자동 재실행이 불가능 —
            # (_do_images/_do_stills처럼 그대로 재실행할 rest 문자열이 아니라 참고용 라벨일
            # 뿐) 자동재개 대신 안내만 하고, "이 스틸컷으로 영상 만들어줘"로 다시 하게 한다.
            try:
                _reply(ch, tts, f"⚠️ 봇이 재시작되면서 진행 중이던 영상 생성이 끊겼어요({rest}). "
                                "\"이 스틸컷으로 영상 만들어줘\"로 다시 시도해주세요.")
            except Exception:
                log.exception("복구 알림 전송 실패")
            continue
        try:
            _reply(ch, tts, "🔁 봇이 재시작돼서 중단된 생성을 자동으로 다시 시작할게요…")
        except Exception:
            log.exception("복구 알림 전송 실패")

        def _run(kind=kind, ch=ch, tts=tts, rest=rest):
            try:
                (_do_images if kind == "images" else _do_stills)(ch, tts, rest)
            except Exception:
                log.exception("복구 재실행 실패")

        threading.Thread(target=_run, daemon=True).start()

_PENDING_RESET_EPISODE: dict[str, dict] = {}   # 확인 메시지 ts -> {work, episode}

def _reset_episode_confirm_blocks():
    return [{
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "⚠️ 그래도 전부 삭제"},
             "style": "danger", "action_id": "reset_episode_confirm"},
            {"type": "button", "text": {"type": "plain_text", "text": "취소"},
             "action_id": "reset_episode_cancel"},
        ],
    }]

def _do_reset_episode(channel, thread_ts, rest):
    """그 화의 영상화(outputs/videos/<N>화/)·합본(확정본 포함) 아웃풋을 통째로 삭제 — 이미
    확정된 영상도 지우거나 화 전체를 재생성해야 할 때(테스트용, 2026-07-15 사용자 요청).
    되돌릴 수 없으므로 반드시 danger 버튼으로 재확인한 뒤에만 실제 삭제한다."""
    work, bible, tail, msgs = _resolve_work_bible(channel, thread_ts, rest)
    if not work:
        _reply(channel, thread_ts, _WORK_NOT_FOUND_MSG)
        return
    epm = re.search(r"(\d{1,3})\s*[화회]", tail)
    if not epm:
        _reply(channel, thread_ts,
               "몇 화를 초기화할지 명시해주세요 — 예: `[화초기화] <작품> 3화` "
               "(잘못 지우는 걸 막기 위해 화 번호 없이는 실행 안 해요).")
        return
    episode = int(epm.group(1))
    preview = vp_store.preview_episode_outputs(work, episode)
    if preview is None:
        _reply(channel, thread_ts,
              f"'{work}' 프로젝트 폴더를 못 찾았어요. 작품명이 정확한가요? "
              f"이 작품으로 영상화·합본을 한 번도 만든 적이 없다면 지울 아웃풋도 없어요 — "
              f"정확한 작품명을 다시 알려주세요.")
        return
    # ★2026-07-15: 스틸컷도 outputs/stills/<화>/로 화별 폴더링되면서 다른 화를 건드릴 위험이
    # 없어져 삭제 대상에 포함(예전엔 씬 번호로만 저장돼 화 구분이 없어 제외했었음).
    n_video, n_compiled, n_still = (len(preview["video_files"]), len(preview["compiled_files"]),
                                    len(preview["still_scenes"]))
    if n_video == 0 and n_compiled == 0 and n_still == 0:
        _reply(channel, thread_ts, f"<{work}> {episode}화에 지울 영상화/합본/스틸컷 아웃풋이 없어요.")
        return
    text = (f"⚠️ <{work}> {episode}화 아웃풋을 삭제할까요? *되돌릴 수 없어요.*\n"
           f"· 영상: {n_video}개 (`{preview['video_dir']}`)\n"
           f"· 합본(확정본 포함): {n_compiled}개\n"
           f"· 스틸컷: {n_still}개 씬 (`{preview['still_dir']}`)")
    resp = app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text,
                                       blocks=_with_text_block(text, _reset_episode_confirm_blocks()))
    _PENDING_RESET_EPISODE[resp["ts"]] = {"work": work, "episode": episode}

@app.action("reset_episode_confirm")
def _act_reset_episode_confirm(ack, body):
    ack()
    ch, tts = _action_ctx(body)
    msg_ts = body["message"]["ts"]
    p = _PENDING_RESET_EPISODE.pop(msg_ts, None)
    if not p:
        _disable_buttons(body, "⚠️ 만료된 요청이에요 (봇이 재시작되면 대기 중인 요청은 사라져요)."); return
    _disable_buttons(body, "🗑 삭제 중…")
    deleted = vp_store.delete_episode_outputs(p["work"], p["episode"])
    n_video, n_compiled, n_still = (len(deleted["video_files"]), len(deleted["compiled_files"]),
                                    len(deleted["still_scenes"]))
    app.client.chat_postMessage(
        channel=ch, thread_ts=tts,
        text=(f"✅ <{p['work']}> {p['episode']}화 아웃풋 삭제 완료 — "
             f"영상 {n_video}개, 합본 {n_compiled}개, 스틸컷 {n_still}개 씬."))

@app.action("reset_episode_cancel")
def _act_reset_episode_cancel(ack, body):
    ack()
    _disable_buttons(body, "취소했어요 — 아무것도 안 지웠어요.")

def _fmt_missing_cuts(missing: set[int]) -> str:
    """{2,4,3} -> "컷2,3,4" (오름차순, 사람이 읽기 좋게)."""
    return "컷" + ",".join(str(n) for n in sorted(missing))

def _do_episode_status(channel, thread_ts, rest):
    """`[진행상황]`/`[미완성확인]` + 자연어("3화 뭐 안 만들어졌어?") 공통 — 그 작품·화의
    씬별 스틸컷/영상 완성도와 합본 여부를 읽기 전용으로 보고한다(2026-07-16).
    기대 컷 수는 _do_stills와 동일하게 그 씬 본문의 [N초] 비트 태그 개수(_BEAT_TAG_RE)로
    판단 — "실제로 몇 컷을 만들어야 하는지"를 이 명령 자체가 새로 정의하지 않고 스틸컷
    생성 로직과 동일한 기준을 그대로 재사용한다(비트 표기 없는 옛 콘티는 판단 기준이 없어
    실제 저장된 스틸컷 수를 잠정 기대치로 대신 쓰고, 그마저 없으면 "컷 수 불명"으로 표시).
    생성/삭제/job_ledger 등 상태 변경은 전혀 하지 않는다 — 순수 리포트."""
    work, bible, tail, msgs = _resolve_work_bible(channel, thread_ts, rest)
    if not work:
        _reply(channel, thread_ts, _WORK_NOT_FOUND_MSG)
        return
    epm = re.search(r"(\d{1,3})\s*[화회]", tail)
    episode = int(epm.group(1)) if epm else (conti_state.get_episode(thread_ts) or {}).get("episode")
    if not episode:
        _reply(channel, thread_ts,
               "몇 화의 진행 상황을 볼지 명시해주세요 — 예: `[진행상황] <작품> 3화`")
        return
    conti = _thread_or_saved_conti(channel, thread_ts, msgs, work, episode, announce=False)
    if not conti:
        _reply(channel, thread_ts,
               f"<{work}> {episode}화의 상세 콘티를 못 찾았어요 — 먼저 `[스토리보드] <작품> {episode}화`로 "
               f"씬 설계·상세 콘티부터 만들어주세요.")
        return
    scenes = _split_scenes(conti)
    if not scenes:
        _reply(channel, thread_ts,
               f"<{work}> {episode}화 콘티에 씬 헤더(■ 씬N)가 없어서 씬별 진행 상황을 못 나눠요 "
               f"— 옛 형식 콘티일 수 있어요.")
        return
    videos_by_scene = video_index.list_episode_videos(
        work, scene_nums=[n for n, _, _ in scenes], episode=episode)
    lines = []
    for num, hdr, body in scenes:
        n_beats = len(_BEAT_TAG_RE.findall(body))
        saved_cuts = vp_store.load_latest_cuts(work, num, episode=episode)
        still_nums = {c["n"] for c in saved_cuts} if saved_cuts else set()
        # 비트 표기 없는 옛 콘티(n_beats==0)는 기대 컷 수를 알 방법이 없어, 실제로 저장된
        # 스틸컷 수를 잠정 기대치로 대신 쓴다(그 이상 만들 계획인지는 이 리포트가 알 수 없음).
        expected = n_beats or len(still_nums)
        video_cuts = videos_by_scene.get(num, [])
        video_nums = {v["cut_num"] for v in video_cuts}

        if not expected:
            lines.append(f"씬{num} — {hdr} · 컷 수 불명(비트 표기 없는 옛 콘티, 스틸컷도 아직 없음)")
            continue

        if not still_nums:
            still_part = f"스틸컷 0/{expected} (전체 없음)"
        elif len(still_nums) >= expected:
            still_part = f"스틸컷 {len(still_nums)}/{expected} ✅"
        else:
            missing = set(range(1, expected + 1)) - still_nums
            still_part = f"스틸컷 {len(still_nums)}/{expected} ({_fmt_missing_cuts(missing)} 없음)"

        if not video_nums:
            video_part = f"영상 0/{expected}" + (" (전체 없음)" if still_nums else "")
        elif len(video_nums) >= expected:
            video_part = f"영상 {len(video_nums)}/{expected} ✅"
        else:
            missing_v = set(range(1, expected + 1)) - video_nums
            video_part = f"영상 {len(video_nums)}/{expected} ({_fmt_missing_cuts(missing_v)} 없음)"

        lines.append(f"씬{num} — {still_part} · {video_part}")

    preview = vp_store.preview_episode_outputs(work, episode)
    compiled = bool(preview and preview.get("compiled_files"))
    compile_part = f"있음 ({len(preview['compiled_files'])}개)" if compiled else "없음"

    text = (f"📋 *<{work}> {episode}화 진행 상황:*\n" + "\n".join(lines) +
           f"\n합본: {compile_part}")
    _reply(channel, thread_ts, text)

# ★2026-07-20 "작품마다 그림체를 다르게 쓰고 싶다" — [스타일] <작품> <스타일명> 명령/자연어로
# works.py에 저장된 style_key(STYLE_PRESETS 참고)를 바꾼다. 자유롭게 들어오는 표현을 최대한
# 폭넓게 인식하되, 애매하면 실패시키고 지원 목록을 안내한다(잘못된 값을 조용히 무시하지 않음).
# ★2026-07-20b: 라벨/키워드 파싱을 bot/shared/works.py로 옮겼다 — dispatch_cowriter.py의
# 노션 동기화 등록(_do_sync)도 신규 작품 장르를 노션 본문에서 파싱해야 하는데, cw/sb 두
# dispatch 모듈은 서로를 임포트하지 않는 구조라(순환 임포트 방지) 공용 shared 모듈에 둬야
# 양쪽이 같은 vocabulary를 쓸 수 있다. 여기서는 기존 호출부가 안 깨지게 그대로 재노출한다.
STYLE_LABELS = works.STYLE_LABELS
_parse_style_key = works.parse_style_key

def _do_style(channel, thread_ts, rest):
    """[스타일] <작품> <스타일명> — 그 작품의 스틸컷/영상/소품·의상 참조 그림체를 바꾼다.
    예) `[스타일] <코니> 2d 애니메이션`, `[스타일] <코니> 실사`."""
    work, bible, tail, msgs = _resolve_work_bible(channel, thread_ts, rest)
    if not work:
        _reply(channel, thread_ts, _WORK_NOT_FOUND_MSG)
        return
    style_key = _parse_style_key(tail)
    if not style_key:
        options = ", ".join(f"`{label}`" for label in STYLE_LABELS.values())
        _reply(channel, thread_ts,
              f"어떤 스타일인지 못 알아들었어요 — 예: `[스타일] <{work}> 2d 애니메이션`. "
              f"지원하는 스타일: {options}")
        return
    w = works.set_style(work, style_key)
    if not w:
        _reply(channel, thread_ts, f"'{work}' 작품을 못 찾았어요 — 먼저 노션 링크로 등록해주세요.")
        return
    _reply(channel, thread_ts,
          f"✅ <{w}> 스타일을 *{STYLE_LABELS[style_key]}*로 설정했어요 — 이제부터 스틸컷·영상·"
          "소품/의상 참조가 전부 이 화풍으로 만들어져요(이미 만든 컷은 그대로 유지).")

def _require_genre(channel, thread_ts, work: str | None) -> bool:
    """★2026-07-20 "노션에도 필수로 추가" — dispatch_cowriter._do_sync가 노션 링크로 신규
    등록하면서 페이지 본문에서 장르를 못 찾은 작품만 works.mark_genre_required로 표시해둔다
    (이미 등록돼 있던 작품은 절대 이 표시가 안 붙으므로, 기존 작품들의 생성은 전혀 영향 없다).
    스틸컷/이미지/자동주행 진입부에서 생성 직전에 이 게이트를 태워, 장르 미지정 신규 작품은
    `[스타일]`로 먼저 지정해야만 다음 단계로 진행되게 막는다. True=통과, False=막힘(이미
    안내 메시지도 보냄)."""
    if not work or not works.genre_required(work):
        return True
    _reply(channel, thread_ts,
          f"⚠️ <{work}>은 아직 장르(실사화/2D 애니메이션) 지정이 필요해요 — 스틸컷·영상 화풍이 "
          f"여기 달려있어서 먼저 정해야 진행할 수 있어요. `[스타일] <{work}> 실사화` 또는 "
          f"`[스타일] <{work}> 2d 애니메이션`으로 알려주세요.")
    return False

# ★2026-07-20: "그림체를 2d 애니메이션으로 바꿔줘"/"스타일 실사로 해줘" 같은 자연어. 화 번호
# 처리(_maybe_episode_status 등)와 동일하게, 구조적으로 명확한 트리거 문구가 있을 때만 걸리게
# 좁혀서 "스타일"이라는 단어가 들어간 다른 잡담과 오충돌하지 않게 한다.
_STYLE_CHANGE_RE = re.compile(r"(스타일|그림체|장르).{0,15}(바꿔|바꾸고|변경|설정|로\s*(해|가)|하기로)")

def _maybe_style_change_request(channel, thread_ts, query) -> bool:
    q = (query or "").strip()
    if not q or not _STYLE_CHANGE_RE.search(q):
        return False
    if not _parse_style_key(q):
        return False   # "스타일 바꾸고 싶어" 같은 막연한 말은 통과시켜 일반 대화로 처리
    _do_style(channel, thread_ts, q)
    return True

# ★2026-07-20 "노션으로 옮기지 말고 여기로 1화 콘티 적어줘. 간략하게 요약해서." — "콘티"라는
# 단어는 storyboard의 강한 트리거라 지금까지는 무조건 상세 콘티(2단계) '재생성'으로 튀었다.
# 근데 이미 만들어둔 콘티/대본이 있는 상태에서 "요약해서"/"간략히" 같은 말이 같이 오면, 사용자
# 의도는 "다시 만들어라"가 아니라 "있는 걸 짧게 요약해서 여기 보여달라"다(2026-07-20 사용자
# 확답: "이미 있는 상세 콘티/대본을 짧게 요약해서 보여줌, 새로 생성 안 함"). _STORYBOARD_MAYBE_
# CHAIN에서 다른 콘티 재생성 트리거들보다 앞자리에 둬서, 재생성 경로로 새기 전에 먼저 가로챈다.
_BRIEF_SUMMARY_MENTION_RE = re.compile(r"콘티|대본")
_BRIEF_SUMMARY_ASK_RE = re.compile(r"요약|간략|짧게|간단")

def _maybe_brief_conti_summary_request(channel, thread_ts, query) -> bool:
    """읽기 전용 — 콘티/대본을 다시 만들거나 노션에 저장하지 않고, 이미 있는 내용만 짧게
    요약해 스레드에 보여준다. 요약할 원본이 아예 없으면(진짜 처음부터 만들어야 하는 상황)
    False를 반환해 기존 흐름(대본 없음 안내 등)에 그대로 맡긴다."""
    q = (query or "").strip()
    if not q or not (_BRIEF_SUMMARY_MENTION_RE.search(q) and _BRIEF_SUMMARY_ASK_RE.search(q)):
        return False
    work, bible, tail, msgs = _resolve_work_bible(channel, thread_ts, q)
    if not work:
        return False
    epm = re.search(r"(\d{1,3})\s*[화회]", tail)
    episode = int(epm.group(1)) if epm else (conti_state.get_episode(thread_ts) or {}).get("episode")
    conti = _thread_or_saved_conti(channel, thread_ts, msgs, work, episode, announce=False)
    source, label = (conti, "상세 콘티") if conti else (None, None)
    if not source:
        script, script_err = _script_for(work, episode, bible)
        if script:
            source, label = script, "대본"
    if not source:
        return False   # 요약할 원본이 없음 — 기존 "대본/콘티 없음" 안내 흐름에 맡긴다
    try:
        summary = generator.complete(
            "너는 숏폼 드라마 자료를 짧게 요약하는 도우미다. 사건 순서와 인물 관계만 남기고 "
            "3~6줄 이내의 자연스러운 한국어 산문으로 요약해라. 원문에 없는 내용을 지어내지 마라.",
            f"[{label} 원문]\n{source[:6000]}\n\n위 내용을 간략히 요약해줘.",
            job_key=thread_ts)
    except Exception:
        log.exception("콘티/대본 간략 요약 실패")
        return False
    if summary in (generator.CANCEL_MSG, generator.TIMEOUT_MSG):
        _reply(channel, thread_ts, summary)
        return True
    _reply(channel, thread_ts, f"📝 {label} 요약(새로 만들거나 노션에 저장하지 않았어요):\n\n{summary.strip()}")
    return True

def _auto_register_element(work: str, name: str, etype: str, png: bytes) -> None:
    """AI로 만든 엘리먼트 후보를 확인 버튼 없이 바로 등록 — _act_element_gen_confirm과 동일한
    저장 로직(fixed-images 우선, 없으면 data/refs)이되 Slack 버튼 클릭 대신 자동주행이 바로 호출."""
    fx = oi.vp_fixed_dir(work)
    if fx:
        elem = oi.register_element(work, name, etype, aliases=[name], clear_file=True)
        d = fx / elem["id"]
        d.mkdir(parents=True, exist_ok=True)
        for old in list(d.iterdir()):
            if old.is_file() and old.suffix.lower() in _REF_SAVE_EXTS:
                old.unlink()
        (d / f"{name}.png").write_bytes(png)
        return
    d = config.OPENROUTER_REFS_DIR / oi.canon_work(work)
    d.mkdir(parents=True, exist_ok=True)
    fname = f"{name}.png"
    (d / fname).write_bytes(png)
    oi.register_element(work, name, etype, filename=fname, aliases=[name])

def _autopilot_registration_check(channel, thread_ts, work, conti) -> list[str]:
    """등록확인 단계 — 콘티에서 인물·장소·의상 후보를 뽑아(_warn_unregistered_elements와 같은 추출
    프롬프트) 미등록이면: 인물은 노션 카드에 '외형' 필드가 있으면(=단순 케이스) AI로 바로
    생성·등록하고, '외형'이 없거나(성격/역할 설명뿐) 카드 자체가 없으면(=복잡한 케이스, 실무자
    확인 필요) 등록하지 않고 gap으로 보고한다. 장소·의상은 항상 AI 생성으로 시도(카드 개념이 없어
    단순/복잡 구분 기준이 따로 없음). ★2026-07-15: 의상도 실무자 실측("잠옷-A, 편한 트레이닝복
    상하의" 일관성 붕괴)의 원인이라 인물/장소와 같은 자동등록 경로에 태운다. 반환: 자동으로 못
    채운 gap 설명 목록(비어있으면 통과)."""
    try:
        raw = oi.chat(prompts.element_extract_system(_place_categories(work)),
                     prompts.element_extract_user(conti), timeout=60)
        obj = _parse_json_object(raw)
        chars = [c.strip() for c in (obj.get("characters") or []) if isinstance(c, str) and c.strip()]
        places = [c.strip() for c in (obj.get("places") or []) if isinstance(c, str) and c.strip()]
        # ★2026-07-15: 안전망 — 형식 변형 콘티에서 추출된 라벨 끝에 콤마/공백이 남을 수 있어 트림.
        costumes = [c.strip(" ,") for c in (obj.get("costumes") or []) if isinstance(c, str) and c.strip(" ,")]
        # ★2026-07-16: props도 costumes와 같은 이유(추출 스키마에 필드 자체가 없어 미탐지)로 빠져
        # 있었다 — 같은 흐름으로 뽑는다.
        props = [c.strip(" ,") for c in (obj.get("props") or []) if isinstance(c, str) and c.strip(" ,")]
    except Exception as e:
        log.exception("자동주행 등록확인: 인물·장소·의상·소품 추출 실패")
        return ["인물/장소/의상/소품 추출 자체가 실패했어요. 잠시 후 다시 시도해주세요."]
    gaps = []
    for name in dict.fromkeys(chars):
        if oi.resolve_element(work, name):
            continue
        field, desc = _notion_character_visual_desc(work, name)
        if field == "설명" or not desc:
            gaps.append(f"인물 · {name} — 노션 카드에 '외형' 묘사가 없어 자동 생성을 건너뜀(직접 등록 필요)")
            continue
        try:
            png, _cost = _generate_element_candidate(work, name, "person", conti[:1500])
            _auto_register_element(work, name, "person", png)
            _reply(channel, thread_ts, f"🎨 <{work}> 인물 · {name} 자동 등록 완료(AI 생성 이미지).")
        except Exception as e:
            log.exception("자동주행: 인물 자동등록 실패")
            gaps.append(f"인물 · {name} — 자동 등록에 실패했어요(직접 등록 필요)")
    for name in dict.fromkeys(places):
        if oi.resolve_element(work, name):
            continue
        try:
            png, _cost = _generate_element_candidate(work, name, "place", conti[:1500])
            _auto_register_element(work, name, "place", png)
            _reply(channel, thread_ts, f"🎨 <{work}> 장소 · {name} 자동 등록 완료(AI 생성 이미지).")
        except Exception as e:
            log.exception("자동주행: 장소 자동등록 실패")
            gaps.append(f"장소 · {name} — 자동 등록에 실패했어요(직접 등록 필요)")
    for name in dict.fromkeys(costumes):
        if oi.resolve_element(work, name):
            continue
        try:
            png, _cost = _generate_element_candidate(work, name, "costume", conti[:1500])
            _auto_register_element(work, name, "costume", png)
            _reply(channel, thread_ts, f"🎨 <{work}> 의상 · {name} 자동 등록 완료(AI 생성 이미지).")
        except Exception as e:
            log.exception("자동주행: 의상 자동등록 실패")
            gaps.append(f"의상 · {name} — 자동 등록에 실패했어요(직접 등록 필요)")
    for name in dict.fromkeys(props):
        if oi.resolve_element(work, name):
            continue
        try:
            png, _cost = _generate_element_candidate(work, name, "prop", conti[:1500])
            _auto_register_element(work, name, "prop", png)
            _reply(channel, thread_ts, f"🎨 <{work}> 소품 · {name} 자동 등록 완료(AI 생성 이미지).")
        except Exception as e:
            log.exception("자동주행: 소품 자동등록 실패")
            gaps.append(f"소품 · {name} — 자동 등록에 실패했어요(직접 등록 필요)")
    return gaps

def _autopilot_consistency_verdict(png: bytes, ref_urls: list[str],
                                    subject: str = "인물/장소/의상",
                                    costume_notes: list[tuple[str, str]] | None = None) -> tuple[str, str]:
    """생성 결과(스틸컷 1장 또는 영상 프레임 1장) vs 그 컷에 실제로 붙은 참조 이미지들을 vision
    모델 1콜로 비교 — (verdict, reason) 튜플로 반환. verdict는 'yes'/'no'/'uncertain' 중 하나로
    정규화(호출 실패 시 uncertain, 화면을 막지 않고 pending-review로 흘려보내기 위한 fail-safe).
    ★2026-07-15: "자율주행모드에서 제일 중요한거는 이미지랑 영상이랑 스스로 검수하는거야"(사용자 지시) —
    검수 근거를 버리면 검사가 제대로 판단했는지 검증도, pending-review 사유 확인도 불가능해서
    reason도 함께 반환하도록 변경.
    ★2026-07-15b: costume_notes(참조 이미지 없이 description만 등록된 의상 — (이름, 설명) 목록)가
    있으면 질문에 그대로 박아 넣는다. 참조 이미지가 없는 의상은 ref_urls로는 검증 근거가 전혀
    없어서(이미지 대 이미지 비교 자체가 불가) 실제 사고 사례(잠옷-A/B 의상이 컷 중간에 다른 옷으로
    바뀌었는데 후검사를 그냥 통과함)의 원인이었다 — 최소한 텍스트 설명 기준으로 vision 모델이
    "이 컷의 인물이 정말 이 설명대로 입고 있는가"를 판단하게 한다."""
    question = (
        f"첫 번째 이미지는 방금 생성한 컷이고, 나머지는 그 컷에 등장해야 하는 {subject}의 "
        "참조 이미지다. 생성 이미지 속 대상이 참조 이미지와 동일 인물/장소/의상인가? ")
    if costume_notes:
        notes_text = "; ".join(f"{name}: {desc}" for name, desc in costume_notes if desc)
        question += (
            f"추가로, 이 컷에는 참조 이미지가 등록되어 있지 않은 의상이 있다 — 등록된 설명은 "
            f"다음과 같다: {notes_text}. 생성 이미지 속 인물이 실제로 이 설명과 일치하는 옷을 "
            "입고 있는지도 반드시 함께 확인해서, 설명과 다른 옷(예: 다른 컷에서 봤던 다른 의상)을 "
            "입고 있으면 'no'로 판정해라. ")
    question += (
        "반드시 'yes' / 'no' / 'uncertain' 중 하나로 시작해서 답하고, 그 뒤에 근거를 한 줄로 써라.")
    try:
        ans = oi.vision_check(png, ref_urls, question)
    except Exception as e:
        log.exception("자동주행: 일관성 검사 호출 실패")
        return "uncertain", "검사 호출 실패(일관성 확인 못함)"
    stripped = ans.strip()
    low = stripped.lower()
    reason = re.sub(r"^(yes|no|uncertain)\W*", "", stripped, count=1, flags=re.IGNORECASE).strip()
    if low.startswith("yes"):
        return "yes", reason
    if low.startswith("no"):
        return "no", reason
    return "uncertain", reason

def _autopilot_regen_shot_png(work, cut: dict, reason: str | None = None) -> bytes | None:
    """일관성 재검사 실패 후 1회 재시도 — focus_char를 그 컷의 첫 번째 인물로 지정해 oi.shot_refs의
    focus_char 격리 로직(사람이 아닌 다른 인물 참조 제외)을 그대로 태워서 프롬프트/참조를 좁힌다.
    ★2026-07-15: 실패 사유(vision 검사 reason)가 있으면 재생성 프롬프트에 짧은 가이드 문구로
    덧붙인다 — 수동 스틸컷 재생성 피드백(fb_note, ~L2511) 패턴과 동일하게 free-text를 다음
    생성 시도에 반영."""
    shot = {"prompt": cut.get("prompt"), "caption": cut.get("caption"),
           "characters": list(cut.get("characters") or []), "places": list(cut.get("places") or []),
           "props": list(cut.get("props") or [])}
    if shot["characters"]:
        shot["focus_char"] = shot["characters"][0]
    ref_entries = oi.shot_ref_entries(work, shot)
    refs = [u for _role, u, *_ in ref_entries]
    reason_note = f", (참조와 안 맞는 부분 수정: {reason})" if reason else ""
    prompt = f"{shot.get('prompt') or ''}, {_style_for_work(work)}{reason_note}"
    role_block = oi.reference_priority_block(ref_entries)
    if role_block:
        prompt = f"{prompt}\n\n{role_block}"
    try:
        png, _cost = oi.generate(prompt, aspect_ratio=STILL_ASPECT, refs=refs)
        return png
    except Exception:
        log.exception("자동주행: 일관성 재생성 실패")
        return None

def _autopilot_vision_budget_left(deadline: float | None) -> bool:
    """★2026-07-15: 후검사 예산(config.AUTOPILOT_VISION_BUDGET_SEC) 초과 여부. deadline=None이면
    예산 제한 없음(하위호환 — 직접 호출/테스트용)."""
    return deadline is None or time.monotonic() < deadline

_AUTOPILOT_CONSISTENCY_MAX_RETRIES = 2   # ★2026-07-16 "일단 2번 재시도까지로 올려" — 기존 1회에서 상향
_AUTOPILOT_SAFETY_FILTER_MAX_RETRIES = 3   # ★2026-07-20 "안전필터 걸리면 그 구간 스틸컷만 다시 생성하는 루프 3회로" — 기존 1회에서 상향

def _autopilot_check_stills(work, cuts: list[dict], deadline: float | None = None) -> list[tuple[int, str]]:
    """스틸컷 후검사 — 컷마다 실제 shot_refs() 참조와 비교해 'no'면 focus_char 격리로 최대
    _AUTOPILOT_CONSISTENCY_MAX_RETRIES회(★2026-07-16, 기존 1회 → 2회로 상향) 재생성 후 재검사,
    그래도 'no'/'uncertain'이면 pending-review로 (컷 번호, 사유)를 남긴다(그 이상은 재시도하지
    않음 — 합본 확인 단계에서 사람에게 보고). 참조가 아예 없는 컷(등록된 인물/장소가 안 나오는
    컷)은 비교 대상이 없으므로 건너뛴다. deadline(time.monotonic() 기준) 넘으면 남은 컷은
    vision_check 호출 없이 바로 pending-review로 흘려보낸다(★2026-07-15, 후검사 비용/시간 예산 —
    재시도 횟수를 늘려도 이 예산 한도 자체는 그대로라, 예산 초과 컷은 여전히 미검사로 빠진다).
    ★2026-07-15: 반환에 사유 문자열도 함께 담아 pending-review 메시지에서 "왜" 걸렸는지 보이게 한다
    (사용자 지시: "자율주행모드에서 제일 중요한거는 이미지랑 영상이랑 스스로 검수하는거야")."""
    flagged = []
    for c in cuts:
        refs = oi.shot_refs(work, c)
        # ★2026-07-15b: 참조 이미지가 전혀 없어도(등록된 인물/장소/의상이 전부 이미지 참조가
        # 없는 경우) description-only 의상 설명이 있으면 건너뛰지 않고 텍스트 기준으로라도
        # 검사한다 — 안 그러면 "잠옷-A" 처럼 참조 이미지 없는 의상은 컷마다 다른 옷으로
        # 바뀌어도 절대 걸리지 않는 사각지대가 생긴다(실측 사고). 참조 이미지도, 의상 설명도
        # 전혀 없는 컷만 기존대로 건너뛴다.
        costume_notes = oi.shot_costume_text_notes(work, c)
        if (not refs and not costume_notes) or not c.get("png"):
            continue
        if not _autopilot_vision_budget_left(deadline):
            flagged.append((c["n"], "검사 예산 초과로 미검사"))  # ★2026-07-15
            continue
        verdict, reason = _autopilot_consistency_verdict(c["png"], refs, costume_notes=costume_notes)
        for _ in range(_AUTOPILOT_CONSISTENCY_MAX_RETRIES):
            if verdict != "no":
                break
            new_png = _autopilot_regen_shot_png(work, c, reason)
            if not new_png:
                break
            c["png"] = new_png
            verdict, reason = _autopilot_consistency_verdict(
                c["png"], oi.shot_refs(work, c), costume_notes=costume_notes)
        if verdict != "yes":
            flagged.append((c["n"], reason))
    return flagged

def _autopilot_check_video(work, cut: dict, local_path: str,
                           deadline: float | None = None) -> tuple[str, str]:
    """영상 일관성 후검사 — 전체 클립이 아니라 첫/끝 프레임만 shot_refs() 참조와 비교한다.
    deadline 넘으면 vision_check를 아예 호출하지 않고 uncertain(=pending-review로 흘려보냄)을
    바로 반환한다(★2026-07-15, 후검사 비용/시간 예산 — 자세한 이유는 config.AUTOPILOT_VISION_BUDGET_SEC 참고).
    ★2026-07-15: (verdict, reason) 튜플 반환 — 첫/끝 프레임 두 검사 결과를 합쳐 사유를 만든다."""
    refs = oi.shot_refs(work, cut)
    if not refs:
        return "yes", ""
    if not _autopilot_vision_budget_left(deadline):
        return "uncertain", "검사 예산 초과로 미검사"  # ★2026-07-15
    frames = [f for f in (vp_store.extract_first_frame_png(local_path),
                          vp_store.extract_last_frame_png(local_path)) if f]
    if not frames:
        return "uncertain", "프레임 추출 실패"
    labels = ["첫프레임", "끝프레임"] if len(frames) > 1 else ["프레임"]
    results = [_autopilot_consistency_verdict(f, refs) for f in frames]
    verdicts = [v for v, _ in results]
    if all(v == "yes" for v in verdicts):
        return "yes", ""
    if any(v == "no" for v in verdicts):
        parts = [f"{labels[i]}: {r}" for i, (v, r) in enumerate(results) if v == "no"]
        return "no", " / ".join(parts)
    parts = [f"{labels[i]}: {r}" for i, (v, r) in enumerate(results) if v != "yes"]
    return "uncertain", " / ".join(parts)

_AUTOPILOT_SAFETY_SOFTEN_SUFFIX = (
    ", Avoid any content, wording, or imagery that could trigger content moderation — "
    "depict this scene in a clearly tame, non-explicit, non-violent, safe-for-work manner "
    "while preserving the core action/emotion described."
)

def _autopilot_render_still_batch(channel, thread_ts, work, bible, num, episode,
                                  batch_source_text, target, batch_index):
    """한 배치(≤4컷) 렌더 + 기존 컷 단위 세이프티/오류 1회 재시도. 반환: (grid_png, cuts, gave_up)
    또는 전멸 시 (None, None, [])."""
    res = _render_cuts_tracked(
        "stills", f"[자동주행] {work} {episode or 0}화 씬{num}", channel, thread_ts, work, bible,
        batch_source_text, target=target, cols=min(target, 2), skip_confirm=True,
        aspect_ratio=STILL_ASPECT, style_suffix=_style_for_work(work), no_text=True,
        title=f"스틸컷 씬{num}", filename=f"still_{work or 'ep'}_s{num}_b{batch_index}.png")
    if not res:
        return None, None, []
    grid_png, cuts = res

    gave_up: list[tuple[int, str]] = []
    # ★2026-07-15: 방금 그 _render_cuts_tracked 호출이 이 스레드에서 마지막으로 쓴 _LAST_RENDER를
    # 이 시점(다음 배치 렌더 시작 전)에 바로 읽으므로 이 배치의 실패 정보가 맞다(배치 루프 순차 처리).
    st = _LAST_RENDER.get(thread_ts)
    if st and st.get("fail_reasons"):
        shots = list(st["shots"])
        results = list(st["results"])
        fail_reasons = st["fail_reasons"]
        retry_shots = []
        for i, s in enumerate(shots):
            if i in fail_reasons and _classify_fail_reason(fail_reasons[i]) == "세이프티 필터 거부":
                s = dict(s)
                s["prompt"] = (s.get("prompt") or "") + _AUTOPILOT_SAFETY_SOFTEN_SUFFIX
            retry_shots.append(s)
        retry_res = _render_cuts_tracked(
            "stills", "", channel, thread_ts, work, bible, st["source_text"],
            target=st["target"], title=st["title"], filename=st["filename"], cols=st["cols"],
            aspect_ratio=st["aspect_ratio"], style_suffix=st["style_suffix"], no_text=st["no_text"],
            retry_shots=retry_shots, retry_results=results, retry_cost=st["total_cost"])
        if retry_res:
            grid_png, cuts = retry_res
            st2 = _LAST_RENDER.get(thread_ts) or {}
            still_failed = st2.get("fail_reasons") or {}
            final_shots = st2.get("shots") or shots
        else:   # 재시도 자체가 전멸(패닉 실패 등)하면 원래 실패분을 그대로 포기 처리
            still_failed = fail_reasons
            final_shots = shots
        for i, msg in still_failed.items():
            n = final_shots[i].get("n") or (i + 1)
            one_line = " ".join(str(msg).split())[:_FAIL_DETAIL_MSG_LEN]
            gave_up.append((n, f"{_classify_fail_reason(msg)} — {one_line}"))
    return grid_png, cuts, gave_up

def _autopilot_stills_for_scene(channel, thread_ts, work, bible, num, hdr, body,
                                vision_deadline=None, on_batch_ready=None):
    """자동주행 전용 스틸컷 생성 — _do_stills의 씬 단위 흐름을 재사용하되, 대화형 세부 옵션 없이
    skip_confirm=True로 컷 수 확인 카드를 건너뛴다(사람 확인은 합본 단계에서만 받으므로).
    ★2026-07-15 "그 씬 11컷을 한 번에 다 만들고 나서야 영상화가 시작되는 게 아니라, 4컷 단위로
    끊어서 그 배치 검수 끝나는 대로 바로 영상화 넘기고 다음 배치 스틸컷을 이어가게" — 씬을
    _BEAT_TAG_RE 비트 기준 ≤4개씩 배치로 쪼개 순차로(배치끼리는 절대 동시 진행 안 함, 여전히
    한 스레드 안에서 for 루프) 렌더 → 검수 → on_batch_ready(있으면 즉시 그 배치를 영상화로 넘김)
    까지 마친 뒤 다음 배치로 넘어간다. 배치 사이의 컷 체이닝/연속성은 이 지점에서 리셋되는데,
    이는 이미 씬과 씬 사이에서 일어나던 것과 같은 종류의 트레이드오프를 더 잘게 적용한 것뿐이고
    사용자가 명시적으로 받아들인 설계다.
    ★2026-07-15 "실패하면 사유를 보고 알아서 조정해서 재생성하게 해야할듯?" — 수동 흐름은
    _maybe_retry_failed_cuts로 사람이 "실패한 컷 다시 만들어줘"라고 말해야 재시도되고, 세이프티
    필터 거부는 사람이 표현을 순화해 직접 다시 요청해야 했다(기존 안내 문구: "표현·소재를
    순화해서 다시 시도하면 대부분 통과돼요"). 자동주행은 그 사람 개입이 없으므로, 실패 컷을
    _LAST_RENDER의 fail_reasons로 찾아 세이프티 필터 거부는 순화 지시를 프롬프트에 덧붙이고
    그 외(생성 오류 등 일시적 실패)는 그대로 _render_cuts_tracked의 retry_shots 메커니즘
    (_maybe_retry_failed_cuts와 동일 경로)으로 1회만 자동 재시도한다(_autopilot_render_still_batch,
    배치 단위로 적용). 그래도 실패한 컷은 포기하고 gave_up으로 반환 — _do_autopilot이
    pending_review에 접어 최종 보고에 노출한다. 배치 전체가 전멸하면(그 배치만의) diagnose→
    adjust→retry를 1회 시도하고(예전엔 이게 _do_autopilot 쪽에서 씬 전체 단위로 있었다), 그래도
    실패하면 그 배치만 건너뛰고(batch_failures에 기록) 다음 배치로 계속 진행한다 — 씬 전체를
    버리지 않는다.
    반환: (cuts, scene_seconds, gave_up, batch_failures) — cuts: 전 배치 성공분 합산(컷번호는
    배치 경계를 넘어 씬 전체에서 유일하도록 오프셋 보정됨), gave_up: [(컷번호, 사유)] 개별 컷
    단위 포기분 합산, batch_failures: 배치 전체가 재시도 후에도 실패한 경우의 문자열 목록.
    전 배치가 다 실패하면 cuts=[]."""
    episode = (conti_state.get_episode(thread_ts) or {}).get("episode")
    dm = re.search(r"(\d+)\s*초", hdr)
    scene_seconds = int(dm.group(1)) if dm else None

    beat_matches = list(_BEAT_TAG_RE.finditer(body))
    if not beat_matches:
        # 비트 태그가 아예 없으면 쪼갤 기준이 없다 — 씬 전체를 배치 하나로 취급(기존 동작과 동일).
        batches = [(body, STILL_CUTS_DEFAULT)]
    else:
        # ★2026-07-15: 첫 비트([N초]) 앞의 "등장:"/"장소:"/"무드/조명:" 헤더 줄들은 배치별로
        # 잘라내면(batch_start=그 배치의 첫 비트 위치) 1번 배치조차 이 줄들을 못 받는다 — 이
        # 줄들은 _scene_costume_map/_scene_single_line(_PLACE_LINE_RE/_MOOD_LINE_RE)이 의상·장소·
        # 무드를 모든 컷에 강제 주입하는 데 쓰는 유일한 소스라, 빠지면 자동주행 배치 스틸컷에서
        # 의상/장소/무드 일관성이 조용히 깨진다(이번 세션에서 어렵게 고친 문제들 재발). 모든
        # 배치에 이 헤더를 그대로 반복해서 붙여준다.
        preamble = body[:beat_matches[0].start()]
        batches = []
        starts = [m.start() for m in beat_matches]
        for i in range(0, len(starts), 4):
            group_starts = starts[i:i + 4]
            batch_start = group_starts[0]
            # 다음 그룹의 시작(=이 그룹 마지막 비트 뒤 텍스트의 끝) 또는 body 끝까지.
            next_i = i + 4
            batch_end = starts[next_i] if next_i < len(starts) else len(body)
            batches.append((preamble + body[batch_start:batch_end], len(group_starts)))

    all_cuts: list[dict] = []
    all_gave_up: list[tuple[int, str]] = []
    batch_failures: list[str] = []
    offset = 0
    for bi, (batch_body, batch_target) in enumerate(batches, 1):
        if thread_ts in _CANCEL:
            break
        batch_start_num = offset + 1
        batch_end_num = offset + batch_target
        batch_source_text = f"■ 씬{num} · {hdr}\n{batch_body}"
        grid_png, cuts, gave_up = _autopilot_render_still_batch(
            channel, thread_ts, work, bible, num, episode, batch_source_text, batch_target, bi)
        if not cuts:
            # ★배치 전멸 — 씬 전체를 포기하던 예전 로직을 배치 단위로 축소해 재사용.
            reason = _LAST_RENDER_FAIL_REASON.get(thread_ts) or "사유 불명(로그 확인 필요)"
            adjusted_body = None
            if "컷 분해 중 오류" in reason:
                adjusted_body = (batch_body + "\n\n(★재시도 — 직전 시도에서 JSON 파싱에 실패했어요: 반드시 "
                                 "유효한 JSON 배열만 출력하고, 문자열 안 따옴표/줄바꿈 등 JSON 문법을 "
                                 "깨뜨리지 마세요.)")
            elif "샷 리스트가 비어있음" in reason:
                adjusted_body = (batch_body + "\n\n(★재시도 — 직전 시도에서 컷이 하나도 안 나왔어요: 이 씬에는 "
                                 "분명히 내용이 있습니다. 각 [N초] 비트마다 최소 1개 컷을 반드시 만들어서 "
                                 "빈 배열을 반환하지 마세요.)")
            elif "이미지 생성이 모두 실패했어요" in reason and "세이프티 필터 거부" in reason:
                adjusted_body = batch_body + _AUTOPILOT_SAFETY_SOFTEN_SUFFIX
            if adjusted_body is not None:
                retry_source_text = f"■ 씬{num} · {hdr}\n{adjusted_body}"
                grid_png, cuts, gave_up = _autopilot_render_still_batch(
                    channel, thread_ts, work, bible, num, episode, retry_source_text, batch_target, bi)
            if not cuts:
                reason2 = _LAST_RENDER_FAIL_REASON.get(thread_ts) or "사유 불명(로그 확인 필요)"
                if adjusted_body is not None:
                    batch_failures.append(
                        f"씬{num} 배치{bi}(컷{batch_start_num}~{batch_end_num}) — 1차: {reason} / "
                        f"재시도(조정 후): {reason2}")
                else:
                    batch_failures.append(f"씬{num} 배치{bi}(컷{batch_start_num}~{batch_end_num}) — {reason}")
                offset += batch_target
                continue

        # ★컷 번호 오프셋 보정 — _render_cuts가 배치마다 1..len(shots)로 새로 매기므로, 씬 전체
        # 기준(비트 위치)으로 밀어줘야 배치 경계를 넘어 겹치지 않는다.
        for c in cuts:
            c["n"] = (c.get("n") or 1) + offset
        gave_up = [(n + offset, reason) for n, reason in gave_up]

        # ★2026-07-15: vision 일관성 검사도 배치 단위로 즉시 실행(전체 씬이 다 모일 때까지 미루지
        # 않음) — 이 배치를 on_batch_ready로 영상화에 넘기기 전에 이 배치 컷만이라도 검수를 거친다.
        flagged = _autopilot_check_stills(work, cuts, vision_deadline)

        if vp_store.available(work):
            try:
                vp_store.save_still(work, scene_num=num, prompt_summary=f"스틸컷 씬{num}", png=grid_png,
                                    cuts=cuts, episode=episode)
            except Exception:
                log.exception("자동주행: 스틸컷 visual-pipeline 저장 실패(계속 진행)")

        all_cuts.extend(cuts)
        # gave_up: (컷번호, "생성 실패(재시도 후에도): {사유}") — 개별 컷 생성 실패.
        all_gave_up.extend((n, f"생성 실패(재시도 후에도): {reason}") for n, reason in gave_up)
        # flagged: (컷번호, "{사유}") — vision 일관성 재검사 실패. 호출부의 기존 최종 문구 포맷과
        # 동일하게 맞추기 위해 all_gave_up과 같은 (번호, 최종문구) 형태로 합쳐 반환한다(4-tuple
        # 시그니처를 유지하면서 두 출처를 구분 없이 pending_review에 그대로 이어붙일 수 있게).
        all_gave_up.extend((c, " ".join(reason.split())[:100]) for c, reason in flagged)
        if on_batch_ready:
            on_batch_ready(cuts, scene_seconds)
        offset += batch_target

    return all_cuts, scene_seconds, all_gave_up, batch_failures

def _autopilot_videos_for_scene(channel, thread_ts, work, title, cuts, scene_seconds,
                                deadline: float | None = None,
                                scene_num: int | None = None,
                                episode: int | str | None = None) -> list[tuple[int, str]]:
    """자동주행 전용 영상화 — _generate_videos_for_cuts와 같은 순차 생성이되(2026-07-15부터
    직전 컷 마지막 프레임 체이닝은 폐지 — 각 컷은 항상 자기 자신의 확정 스틸컷을 시작 이미지로
    씀, 아래 _generate_video_for_cut 참고), 컷마다 첫/끝 프레임 일관성 후검사를 끼워 넣는다.
    반환: 최대 _AUTOPILOT_CONSISTENCY_MAX_RETRIES회(★2026-07-16, 기존 1회 → 2회 상향) 재시도
    후에도 통과 못한(pending-review) (컷 번호, 사유) 목록. deadline은
    _autopilot_check_video로 그대로 전달(★2026-07-15, 후검사 시간 예산). ★2026-07-15: 사유도
    함께 반환 — 사유 없는 flagged 목록은 "왜 걸렸는지" 아무도 검증할 수 없어 자율주행 자기검수
    요건에 못 미친다.

    ★2026-07-15 "단계 안에서의 재개" — 영상화 도중 취소하고 같은 씬으로 다시 돌리면, 이미 만든
    컷까지 전부 다시 만들던 문제. scene_num/episode가 있으면(=자동주행 호출) 컷마다 먼저
    vp_store.find_existing_video로 이미 저장된 영상이 있는지 보고, 있으면 그 컷은 새로 안 만들고
    건너뛴다(체이닝이 폐지돼 더 이상 직전 컷 마지막 프레임을 따로 챙길 필요가 없다). scene_num이
    없으면(구경로 호출 대비) 항상 새로 만드는 기존 동작 그대로."""
    ordered = sorted(cuts, key=lambda c: c["n"])
    flagged = []
    for c in ordered:
        if thread_ts in _CANCEL:   # ★2026-07-15: 컷 사이 취소 체크포인트 — 영상화는 컷 1개도 수십초~분 단위
            break
        if scene_num is not None and vp_store.find_existing_video(work, scene_num, c["n"], episode=episode):
            continue
        planned = c.get("duration")
        cut_seconds = (max(4.0, min(15.0, float(planned)))
                      if isinstance(planned, (int, float)) and planned > 0
                      else _estimate_cut_seconds(c.get("caption") or ""))
        # ★2026-07-15 "얘는 왜 영상 두개 만든거임?" — 재검사(verdict=="no")로 인한 재시도가
        # 1차 시도까지 슬랙에 올리고 과금해버려 같은 컷 영상이 두 번 나오던 버그. post_result=False
        # 로 게시를 미루고, 최종적으로 "이긴" 시도 하나만 아래에서 한 번만 게시한다.
        cost_out = {}
        fail_out: dict = {}
        local_path = _generate_video_for_cut(channel, thread_ts, work, title, c, c["n"],
                                            cut_seconds, post_confirm_buttons=False,
                                            post_result=False, cost_out=cost_out, fail_reason_out=fail_out)
        if not local_path:
            # ★2026-07-15 "자동주행 중 실존 인물 안전필터로 영상화 실패하면 어떻게?" — 이 필터는
            # 모션 프롬프트 텍스트가 아니라 입력 이미지(=그 컷의 확정 스틸컷) 자체가 "실사 인물
            # 사진처럼 보인다"고 판단해서 걸린다. 그래서 스틸컷 그림체를 더 뚜렷하게 일러스트/
            # 페인터리 쪽으로 밀어 재생성한 뒤(_autopilot_regen_shot_png — 일관성 재검사 실패
            # 때와 같은 함수, reason 문구만 이 상황에 맞게) 영상화를 재시도한다. 프롬프트
            # 텍스트 문제(세이프티 필터 거부 등)와 원인이 달라서 그냥 재시도해도 똑같이 걸릴 뿐 —
            # 반드시 참조 이미지 자체를 바꿔야 의미가 있다.
            # ★2026-07-20 "안전필터 걸리면 그 구간 스틸컷만 다시 생성하는 루프 3회로" — 기존엔
            # 스틸컷 재생성→영상화 재시도가 딱 1회뿐이라, 그 1번의 재생성으로도 여전히 실사
            # 인물처럼 보이면 그대로 포기했다. 일관성 재검사 재시도(위 _AUTOPILOT_CONSISTENCY_
            # MAX_RETRIES)와 동일한 방식으로 _AUTOPILOT_SAFETY_FILTER_MAX_RETRIES(3)회까지 반복한다.
            for _ in range(_AUTOPILOT_SAFETY_FILTER_MAX_RETRIES):
                if fail_out.get("reason") != "입력 이미지가 실존 인물처럼 보인다는 안전필터에 걸림":
                    break
                new_png = _autopilot_regen_shot_png(
                    work, c, "실제 인물 사진처럼 보이지 않게, 명확한 일러스트/페인터리 그림체로 "
                             "(사실적 피부 질감·실사 조명 최소화)")
                if not new_png:
                    break
                c["png"] = new_png
                retry_cost_out, retry_fail_out = {}, {}
                retry_path = _generate_video_for_cut(
                    channel, thread_ts, work, title, c, c["n"], cut_seconds,
                    post_confirm_buttons=False, post_result=False,
                    cost_out=retry_cost_out, fail_reason_out=retry_fail_out)
                if retry_path:
                    local_path = retry_path
                    cost_out = retry_cost_out
                    break
                fail_out = retry_fail_out or fail_out
            if not local_path:
                flagged.append((c["n"], fail_out.get("reason") or "영상 생성 실패"))
                continue
        verdict, reason = _autopilot_check_video(work, c, local_path, deadline)
        # ★2026-07-16 "일단 2번 재시도까지로 올려" — 스틸컷 일관성 재검사와 동일하게 기존 1회에서
        # _AUTOPILOT_CONSISTENCY_MAX_RETRIES(2)회로 상향.
        for _ in range(_AUTOPILOT_CONSISTENCY_MAX_RETRIES):
            if verdict != "no":
                break
            new_png = _autopilot_regen_shot_png(work, c, reason)
            if not new_png:
                break
            c["png"] = new_png
            retry_cost_out = {}
            retry_path = _generate_video_for_cut(channel, thread_ts, work, title, c, c["n"],
                                                 cut_seconds, post_confirm_buttons=False,
                                                 post_result=False, cost_out=retry_cost_out)
            if not retry_path:
                break
            local_path = retry_path
            cost_out = retry_cost_out
            verdict, reason = _autopilot_check_video(work, c, local_path, deadline)
        _post_generated_video(channel, thread_ts, work, title, c["n"], local_path, cost_out.get("cost", 0))
        if verdict != "yes":
            flagged.append((c["n"], reason))
    return flagged

def _autopilot_cancelled(channel, thread_ts, where: str) -> bool:
    """★2026-07-15: 스테이지 경계마다 _CANCEL 체크 — 이걸 안 하면 "취소"라고 말해도 sb_do_storyboard/
    _render_cuts_tracked 안에서만 멈추고 _do_autopilot 루프는 다음 씬/단계로 계속 진행해버린다.
    _render_cuts 쪽 관례(취소 후 _CANCEL.discard)를 그대로 따라 여기서도 처리 후 지운다."""
    if thread_ts not in _CANCEL:
        return False
    _CANCEL.discard(thread_ts)
    _reply(channel, thread_ts, f"🛑 자동주행을 취소했어요 — {where}까지 진행된 상태에서 멈췄어요.")
    return True

_PENDING_AUTOPILOT_STAGE: dict[str, dict] = {}   # thread_ts -> {"rest": rest} — ★2026-07-20 단계 선택 드롭다운 대기

_AUTOPILOT_STAGE_LABELS = {
    1: "1단계 · 씬 설계", 2: "2단계 · 상세 콘티", 3: "3단계 · 등록 확인",
    4: "4단계 · 샷분해·스틸컷", 5: "5단계 · 영상화", 6: "6단계 · 합본",
}

def _autopilot_stage_picker_blocks():
    """★2026-07-20 "4단계부터/5단계부터 텍스트 입력은 사용성이 떨어지니 드롭다운으로" —
    기존엔 `[자동주행] <작품> <화> 4단계부터`처럼 정확한 문구를 외워 쳐야 특정 단계부터
    시작할 수 있었다. 전체 실행(기존 기본 동작)은 버튼 한 번으로, 특정 단계부터 시작은
    드롭다운 선택으로 바꿔 문구를 몰라도 되게 한다."""
    options = [{"text": {"type": "plain_text", "text": label}, "value": str(n)}
              for n, label in _AUTOPILOT_STAGE_LABELS.items()]
    return [{
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "🚀 처음부터(전체 1~6단계)"},
             "style": "primary", "action_id": "autopilot_full_run"},
            {"type": "static_select",
             "placeholder": {"type": "plain_text", "text": "특정 단계부터 시작"},
             "options": options, "action_id": "autopilot_stage_pick"},
        ],
    }]

@app.action("autopilot_full_run")
def _act_autopilot_full_run(ack, body):
    ack()
    ch, tts = _action_ctx(body)
    pending = _PENDING_AUTOPILOT_STAGE.pop(tts, None)
    if not pending:
        _reply(ch, tts, "이 선택은 만료됐어요 — `[자동주행] <작품> <화번호>`를 다시 입력해주세요.")
        return
    _disable_buttons(body, "✅ 처음부터(1~6단계) 전체 실행할게요…")
    _do_autopilot(ch, tts, pending["rest"], skip_stage_picker=True)

@app.action("autopilot_stage_pick")
def _act_autopilot_stage_pick(ack, body):
    ack()
    ch, tts = _action_ctx(body)
    stage = body["actions"][0]["selected_option"]["value"]
    pending = _PENDING_AUTOPILOT_STAGE.pop(tts, None)
    if not pending:
        _reply(ch, tts, "이 선택은 만료됐어요 — `[자동주행] <작품> <화번호>`를 다시 입력해주세요.")
        return
    label = _AUTOPILOT_STAGE_LABELS.get(int(stage), f"{stage}단계")
    _disable_buttons(body, f"✅ {label}부터 시작할게요…")
    _do_autopilot(ch, tts, f"{pending['rest']} {stage}단계부터", skip_stage_picker=True)

def _autopilot_prepare_script(channel, thread_ts, work, bible, episode):
    """★2026-07-20 "[자동주행] 모드에 개요랑 대본 생성 단계도 넣자" — 기존 1~6단계(등록확인~
    합본) 번호는 그대로 두고 그 앞에 얹는 "0단계". 이미 있으면 건너뛰고, 없는 것만
    co-writer의 실제 생성+저장 함수를 그대로 재사용해 사람 확인(✅ 통과) 버튼 없이 곧바로
    만든다 — _do_generate로 초안을 스레드에 만들고, 그 직후 _do_input(mode="save")로 "방금
    만든 초안을 확정 저장"시킨다(두 함수 다 실제 버튼 핸들러가 쓰는 것과 동일한 경로, 자동주행
    전용 특수 로직이 아니라 기존 저장 흐름을 헤드리스로 그대로 태우는 것). 반환: 최신 bible
    (개요/대본이 반영된 새로고침본), 생성 자체가 실패하면 None(호출자가 자동주행을 중단해야 함).
    ★co-writer 쪽 함수를 이 storyboard 모듈에서 직접 호출하는 것은 dispatch_cowriter.py가
    dispatch_storyboard.py를 거꾸로 import하지 않아 순환 임포트가 없기 때문에 안전하다(둘 다
    dispatch.py에서만 동시에 import됨)."""
    from bot import dispatch_cowriter as cw
    sh = _sheet()
    if not sh:
        return bible   # 시트 연결 자체가 없으면(SHEET_WEBAPP_URL 미설정) 이 준비 단계는 조용히 생략
    script, script_err = _script_for(work, episode, bible)
    if script_err:
        _reply(channel, thread_ts, f"⚠️ {episode}화 대본을 확인하는 중 오류가 났어요({script_err}) — 자동주행을 멈출게요.")
        return None
    if script:
        return bible   # 이미 대본이 있으면 개요 유무와 무관하게 준비 끝(콘티는 대본만 있으면 됨)
    outline = ((bible or {}).get("outlines") or {}).get(f"{episode}화")
    if not outline:
        _reply(channel, thread_ts, f"📝 0단계 — <{work}> {episode}화 대본이 없어서 개요부터 자동으로 만들게요…")
        try:
            cw._do_generate(channel, thread_ts, f"<{work}> {episode}화 개요")
            cw._do_input(channel, thread_ts, f"<{work}> 개요/{episode}화", mode="save")
        except Exception:
            log.exception("자동주행 0단계(개요) 생성 실패")
            _reply(channel, thread_ts, "⚠️ 개요 생성 중 오류가 나서 자동주행을 멈출게요 — 대본을 직접 만들거나 다시 시도해주세요.")
            return None
        bible = sh.get(work, force=True)
        if not ((bible or {}).get("outlines") or {}).get(f"{episode}화"):
            _reply(channel, thread_ts, "⚠️ 개요가 저장되지 않은 것 같아요 — 자동주행을 멈출게요. 스레드에서 개요 초안을 확인해주세요.")
            return None
    _reply(channel, thread_ts, f"📝 0단계 — <{work}> {episode}화 대본을 자동으로 만들게요…")
    try:
        cw._do_generate(channel, thread_ts, f"<{work}> {episode}화 대본")
        cw._do_input(channel, thread_ts, f"<{work}> 대본/{episode}화", mode="save")
    except Exception:
        log.exception("자동주행 0단계(대본) 생성 실패")
        _reply(channel, thread_ts, "⚠️ 대본 생성 중 오류가 나서 자동주행을 멈출게요 — 대본을 직접 만들거나 다시 시도해주세요.")
        return None
    bible = sh.get(work, force=True)
    new_script, _err = _script_for(work, episode, bible)
    if not new_script:
        _reply(channel, thread_ts, "⚠️ 대본이 저장되지 않은 것 같아요 — 자동주행을 멈출게요. 스레드에서 대본 초안을 확인해주세요.")
        return None
    _reply(channel, thread_ts, f"✅ 0단계 완료 — <{work}> {episode}화 개요·대본 준비됨. 이어서 진행할게요…")
    return bible

def _do_autopilot(channel, thread_ts, rest, skip_stage_picker: bool = False):
    """[자동주행] <작품> <화번호> — (없으면) 개요/대본→등록확인→씬설계→상세콘티→샷분해/스틸컷→
    영상화→합본을 한 번에 이어서 돌린다. 사람 확인은 마지막 합본(_do_compile, draft+확정 버튼)
    단계에서만 받는다. 개요/대본 생성(★2026-07-20 "0단계"로 신규 — _autopilot_prepare_script
    참고)은 기존 1~6단계 번호를 그대로 유지하기 위해 그 앞에 붙는 별도 단계로 뒀다 — 이미 대본이
    있으면 조용히 건너뛰고, 개요만 없으면 개요부터, 대본까지 없으면 개요+대본 둘 다 자동 생성.
    ★2026-07-15 "실패하면 왠만해서는 중단하지 말고 실패 이유를 찾고 쭉 진행하게 해야한다" —
    씬 설계/상세 콘티는 없으면 이후 아무것도 진행할 수 없어(진짜 복구불가) 1회 자동 재시도 후에도
    실패하면 그때만 멈추고, 등록확인 gap·씬 단위 스틸컷 전멸·컷 단위 일관성 검사 실패는 전부
    "기록하고 계속 진행"으로 처리해 다른 씬/컷까지 덩달아 막히지 않게 한다. 최종 요약에서 재시도
    복구/gap/건너뛴 씬/확인 필요 컷을 모두 구분해서 보고한다.
    skip_stage_picker: ★2026-07-20 위 두 액션 핸들러가 드롭다운/버튼 선택 후 재호출할 때만
    True — 이번 호출은 이미 사용자가 범위를 골랐으니 아래 드롭다운 게이트를 다시 태우지 않는다."""
    if not config.AUTOPILOT_ENABLED:
        _reply(channel, thread_ts,
              "자동주행 기능은 아직 꺼져있어요(`SB_AUTOPILOT_ENABLED=true`로 켜야 동작해요).")
        return
    # ★2026-07-15 "왜 멈춘거지?" 실사용자 리포트 — "멈춰"는 무조건 _CANCEL.add(thread_ts)를 하는데
    # (그 시점에 실제로 돌던 게 이미지/영상 생성 루프가 아니라 _CANCEL을 아예 안 보는 sb_do_storyboard
    # 같은 단순 LLM 호출이면) 그 플래그를 아무도 소비 안 해서 스레드에 그대로 남는다. 그 뒤 완전히
    # 새로운 "[자동주행] ... 5단계부터" 요청이 오면, 이번 실행과 무관한 그 과거 플래그를 첫
    # 체크포인트에서 그대로 주워 먹고 "취소했어요"로 즉시 멈춰버렸다(실제로는 이번 실행에 대해
    # 취소한 적이 없는데도). _render_cuts가 이미 자기 시작 지점에서 하는 것(L1134)과 똑같이,
    # 새 자동주행을 시작할 때는 그 이전에 남아있을 수 있는 stale 플래그를 먼저 지운다 — 지금
    # 시작하는 이 실행은 명백히 새 사용자 지시이지, 취소 대상이 아니다.
    _CANCEL.discard(thread_ts)
    work, bible, tail, msgs = _resolve_work_bible(channel, thread_ts, rest)
    epm = re.search(r"(\d{1,3})\s*[화회]", tail)
    # ★2026-07-15 "이 스레드를 읽고 다시 [자동주행]을 치면 알아서 다음 단계로 이어지게" — 화 번호를
    # 안 붙인 맨몸 "[자동주행]"이면(=epm 없음) 이 스레드에 기록된 이전 자동주행 진행 상태를 찾아
    # work/화/씬 범위/시작 단계를 자동으로 채운다. 화 번호를 명시했으면(재시작이 아니라 새로
    # 지정한 것으로 간주) 이 자동완성은 건너뛰고 기존처럼 새 실행으로 처리한다.
    resumed_progress = None
    if not epm:
        resumed_progress = conti_state.get_autopilot_progress(thread_ts)
    # ★2026-07-15: resumed_progress가 있으면 work/episode 둘 다 거기서 채우므로, work/epm이
    # 스레드 문맥만으로 못 찾아졌어도 진짜 실패는 아니다 — resumed_progress가 없을 때만
    # work·epm 둘 다 있어야 한다는 기존 검증을 적용한다.
    if not resumed_progress and (not work or not epm):
        _reply(channel, thread_ts, "`[자동주행] <작품> <화번호>` 형식으로 보내주세요 (예: `[자동주행] 코니 3화`).")
        return
    if resumed_progress:
        work = resumed_progress["work"]
        episode = resumed_progress["episode"]
    else:
        episode = int(epm.group(1))
    if not _require_genre(channel, thread_ts, work):
        return
    # ★2026-07-20 "[자동주행] 모드에 개요랑 대본 생성 단계도 넣자" — 지금까지 자동주행은
    # 대본이 이미 있다는 전제로 등록확인부터 시작했다(대본이 없으면 sb_do_storyboard가 그때
    # 가서야 "대본을 못 찾았어요"로 막았음). 기존 1~6단계 번호는 그대로 두고(재개 상태·드롭다운
    # 호환), 그 앞에 "0단계"로 개요/대본을 필요할 때만(이미 있으면 건너뜀) 자동 생성+저장한다.
    bible = _autopilot_prepare_script(channel, thread_ts, work, bible, episode)
    if bible is None:   # 개요/대본 생성 자체가 실패 — 이후 단계를 진행할 근거가 없어 중단
        return
    # ★2026-07-15: 사용자 요청 — "1씬만 하게 해서" 테스트로 짧게 돌려보고 싶다는 요구. 자동주행은
    # 원래 화 전체 씬을 다 처리하는데, 첫 실전 테스트는 시간이 오래 걸리는 영상화가 씬 수에
    # 비례해서 순식간에 30분을 넘길 수 있어(컷당 순차로 수 분) 특정 씬 하나만으로 범위를 좁힐
    # 수 있게 한다. 씬설계/상세콘티/등록확인(1~3단계)은 화 전체 맥락이 필요해 그대로 전체를
    # 돌리고, 시간이 오래 걸리는 4~5단계(샷분해·스틸컷·영상화)만 지정된 씬으로 제한한다.
    # ★2026-07-15: "그 1씬에 3컷부터..."처럼 숫자가 "씬" 앞에 오는 표기("N씬")도 실사용에서 나옴 —
    # 기존엔 "씬N"만 인식해 이 문구에서 scene_only가 아예 안 잡히는 사고가 있었다(연쇄적으로
    # 아래 컷 범위 파싱도 scene_only 필요라 같이 무효화됨). 두 순서 다 인정.
    scene_only_m = re.search(r"씬\s*(\d+)", tail) or re.search(r"(\d+)\s*씬", tail)
    scene_only = int(scene_only_m.group(1)) if scene_only_m else None
    if resumed_progress and scene_only is None:
        scene_only = resumed_progress.get("scene_only")
    # ★2026-07-15 "그 1씬에 3컷부터 4컷까지 영상화 이것도 자율주행 모드 하고 싶음" — 씬 범위 위에
    # 컷 범위까지 좁힐 수 있게. _VIDEO_CUT_RANGE_RE(수동 영상화 경로에서 이미 쓰던 "컷3-4"/
    # "3~4컷" 패턴)를 재사용 — 별도 정규식을 새로 만들면 표현이 갈라져 사용자가 헷갈린다.
    # 씬 지정 없이 컷 범위만 있으면 어느 씬 기준인지 알 수 없어 무시(=씬 전체 그대로 진행).
    video_cut_range = _match_video_cut_range(tail) if scene_only else None
    # ★2026-07-15 "어디 단계부터 시작해서 어디 단계에서 끝날지 선택할 수 있게" — 기존 씬N/화 필터와
    # 겹치지 않게 "단계"라는 고유 키워드를 쓰는 단일 정규식으로 4가지 표현("2~4단계"/"2단계부터
    # 4단계까지"/"2단계부터"(끝은 6 기본값)/"4단계까지"(시작은 1 기본값))을 한 번에 처리한다.
    stage_range_m = re.search(
        r"(\d)\s*~\s*(\d)\s*단계"                       # "2~4단계"
        r"|(\d)\s*단계\s*부터\s*(\d)\s*단계\s*까지"          # "2단계부터 4단계까지"
        r"|(\d)\s*단계\s*부터"                            # "2단계부터" (끝=6)
        r"|(\d)\s*단계\s*까지",                           # "4단계까지" (시작=1)
        tail)
    # ★2026-07-20: 단계 지정도 없고(=텍스트로 "N단계부터"를 안 씀) 이전 진행 이어받기도 아니면
    # (=완전히 새로운 실행) 어디부터 시작할지 드롭다운으로 물어보고 여기서 멈춘다 — 위 두 액션
    # 핸들러가 사용자가 고른 선택을 반영해 skip_stage_picker=True로 이 함수를 다시 부른다.
    # resumed_progress가 있으면(맨몸 "[자동주행]" 재개) 사람이 매번 고를 필요 없이 자동으로
    # 이어가는 기존 동작을 그대로 유지한다.
    if not stage_range_m and not resumed_progress and not skip_stage_picker:
        _PENDING_AUTOPILOT_STAGE[thread_ts] = {"rest": rest}
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"<{work}> {episode}화 자동주행 — 어느 단계부터 시작할까요?",
            blocks=_autopilot_stage_picker_blocks())
        return
    start_stage, end_stage = 1, 6
    if stage_range_m:
        g = stage_range_m.groups()
        if g[0] is not None:
            start_stage, end_stage = int(g[0]), int(g[1])
        elif g[2] is not None:
            start_stage, end_stage = int(g[2]), int(g[3])
        elif g[4] is not None:
            start_stage, end_stage = int(g[4]), 6
        elif g[5] is not None:
            start_stage, end_stage = 1, int(g[5])
    elif resumed_progress:
        # ★2026-07-15: 명시적 단계 지정이 없는 맨몸 재실행이면, 기록된 last_stage 바로 다음
        # 단계부터 이어간다(그 단계 안에서 어디까지 됐는지는 이미 구현된 씬/컷 단위 재개 로직이
        # 각자 알아서 skip 처리).
        start_stage = resumed_progress["last_stage"] + 1
    if not (1 <= start_stage <= end_stage <= 6):
        _reply(channel, thread_ts,
              f"❌ 자동주행 단계 범위가 잘못됐어요 ({start_stage}~{end_stage}) — 1~6 사이의 숫자여야 하고, "
              "시작 단계가 끝 단계보다 크면 안 돼요.")
        return
    stage_scope_note = f" ({start_stage}~{end_stage}단계만 실행)" if (start_stage, end_stage) != (1, 6) else ""
    conti_state.set_episode(thread_ts, work, episode)
    resume_note = f" — 이전 진행(~{resumed_progress['last_stage']}단계) 이어서" if resumed_progress else ""
    scene_only_note = f" (테스트 모드: 씬{scene_only}만 처리, 나머지 씬은 건너뜀)" if scene_only else ""
    cut_range_note = (f" · 영상화는 컷{video_cut_range[0]}~{video_cut_range[1]}만"
                      + (" (⚠️ 6단계 합본까지 실행하면 범위 밖 컷은 영상 없이 합본돼요)" if video_cut_range and end_stage == 6 else "")
                      if video_cut_range else "")
    reg_skip_note = (" ⚠️ 3단계부터 시작해 등록확인(3단계)은 건너뛰므로 미등록 요소로 인한 일관성 문제는 "
                     "이번 실행에서 걸러지지 않아요." if start_stage >= 4 else "")
    _reply_with_stop_button(channel, thread_ts,
          f"🚀 <{work}> {episode}화 자동주행 시작{stage_scope_note}{scene_only_note}{cut_range_note}{resume_note} — "
          f"개요/대본(없으면 자동 생성)→등록확인→씬설계→상세콘티→샷분해/스틸컷→영상화까지 "
          f"자동으로 진행하고, 합본만 확인 요청할게요.{reg_skip_note} 씬 설계/상세 콘티가 완전히 실패하는 경우만 멈추고, "
          "그 외 부분 실패(미등록 요소/특정 씬·컷 문제)는 기록만 남기고 계속 진행할게요. "
          "중간에 멈추려면 아래 [🛑 중단] 버튼을 누르거나 \"취소\"/\"중단\"이라고 답해주세요.\n"
          "(0/6 개요·대본 준비 · 1/6 씬 설계 · 2/6 상세 콘티 · 3/6 등록 확인 · 4/6 샷분해·스틸컷 · 5/6 영상화 · 6/6 합본)")

    # ★2026-07-15: 개별 단계 함수들(_render_cuts_tracked/_generate_video_for_cut)은 각자 자기
    # 몫만 job_ledger에 등록해서, 그 사이(등록확인의 AI 이미지 생성 등)는 아무 job도 안 잡혀
    # auto_pull.sh의 busy-gate가 "지금 아무 작업도 안 돈다"고 오판하고 재배포/재시작을 허용할 수
    # 있었다 — 자동주행 전체를 감싸는 상위 job을 하나 더 등록해 전 구간을 커버한다.
    jid = job_ledger.start_job("autopilot", channel, thread_ts, rest)

    # ★2026-07-15 "이 스레드를 읽고 다시 [자동주행]을 치면 알아서 다음 단계로 이어지게" — 단계가
    # 완전히 끝날 때마다 여기 기록해서, 화 번호 없이 맨몸 "[자동주행]"이 다시 오면 위에서
    # last_stage+1부터 자동으로 이어가게 한다(취소/크래시로 중간에 끊겨도 마지막으로 완전히
    # 끝난 단계까지는 남아있음).
    def _mark_progress(stage: int) -> None:
        conti_state.set_autopilot_progress(thread_ts, work, episode, scene_only, stage)

    try:
        # ★2026-07-15 "실패하면 왠만해서는 중단하지 말고 실패 이유를 찾고 쭉 진행하게 해야한다" —
        # LLM 호출도 이미지 생성처럼 일시적으로 실패할 수 있으니 자동 1회 재시도. 다만 씬 설계는
        # 그 자체가 없으면 이후 아무 단계도 돌 씬이 없어(진짜 복구불가) 재시도 후에도 실패하면
        # 여기서는 중단이 맞다 — 이 두 지점만 예외적으로 하드 스톱 유지.
        stage1_retried = False
        # ★2026-07-15 "어디 단계부터 시작해서 어디 단계에서 끝날지" — start_stage>=2면 씬설계(1단계)
        # 자체는 건너뛰지만, 2단계(상세콘티)로 바로 들어가려면 최소한 씬 설계 결과(sb_stage>=1)가
        # 스레드에 이미 있어야 한다(없으면 나눌 씬 자체가 없다).
        if start_stage <= 1:
            _reply(channel, thread_ts, "1/6 씬 설계 중…")
            sb_do_storyboard(channel, thread_ts, f"<{work}> {episode}화", stage=1)
            if _autopilot_cancelled(channel, thread_ts, "1/6 씬 설계"):
                return
            if sb_stage(_thread_messages(channel, thread_ts), work=work, episode=episode) < 1:
                _reply(channel, thread_ts, "⚠️ 씬 설계 1회 실패 — 1회 자동 재시도할게요…")
                stage1_retried = True
                sb_do_storyboard(channel, thread_ts, f"<{work}> {episode}화", stage=1)
                if _autopilot_cancelled(channel, thread_ts, "1/6 씬 설계(재시도)"):
                    return
                if sb_stage(_thread_messages(channel, thread_ts), work=work, episode=episode) < 1:
                    _reply(channel, thread_ts,
                          "❌ 자동주행 중단 — 씬 설계 단계에서 재시도(1회)까지 실패했어요(위 오류 메시지 참고). "
                          "이 단계 없이는 이후 어떤 단계도 진행할 씬이 없어 여기서 멈춰요.")
                    return
            _mark_progress(1)
            if end_stage == 1:
                _reply(channel, thread_ts,
                      ("✅ 씬 설계 완료(재시도로 복구됨) — 요청하신 1단계까지만 실행하기로 해서 여기서 멈춰요."
                       if stage1_retried else "✅ 씬 설계 완료 — 요청하신 1단계까지만 실행하기로 해서 여기서 멈춰요."))
                return
            _reply(channel, thread_ts,
                  ("✅ 씬 설계 완료(재시도로 복구됨) → 2/6 상세 콘티 생성 중…" if stage1_retried
                   else "✅ 씬 설계 완료 → 2/6 상세 콘티 생성 중…"))
        else:
            _reply(channel, thread_ts, f"⏭️ {start_stage}단계부터 시작 — 씬 설계(1단계)는 건너뛰어요.")
            if start_stage == 2 and sb_stage(_thread_messages(channel, thread_ts), work=work, episode=episode) < 1:
                _reply(channel, thread_ts, "❌ 자동주행 중단 — 2단계부터 시작하려면 먼저 씬 설계가 있어야 해요.")
                return

        stage2_retried = False
        # ★2026-07-15: start_stage>=3이면 상세콘티 생성 자체도 건너뛰고, 이미 만들어진 콘티가
        # 있는지(_thread_or_saved_conti)만 확인한다 — 없으면 3단계부터는 시작할 수 없다.
        if start_stage >= 3:
            _reply(channel, thread_ts, "⏭️ 상세 콘티(2단계) 생성도 건너뛰고 기존 콘티를 찾아볼게요…")
            msgs0 = _thread_messages(channel, thread_ts)
            precheck_conti = _thread_or_saved_conti(channel, thread_ts, msgs0, work, episode, announce=False)
            if not precheck_conti:
                _reply(channel, thread_ts,
                      "❌ 자동주행 중단 — 3단계부터 시작하려면 이미 만들어진 상세 콘티가 있어야 해요 — "
                      "노션에 저장돼 있거나 이 스레드에 붙어있어야 합니다.")
                return
        else:
            sb_do_storyboard(channel, thread_ts, "", stage=2)
            if _autopilot_cancelled(channel, thread_ts, "2/6 상세 콘티"):
                return
            if sb_stage(_thread_messages(channel, thread_ts), work=work, episode=episode) < 2:
                _reply(channel, thread_ts, "⚠️ 상세 콘티 1회 실패 — 1회 자동 재시도할게요…")
                stage2_retried = True
                sb_do_storyboard(channel, thread_ts, "", stage=2)
                if _autopilot_cancelled(channel, thread_ts, "2/6 상세 콘티(재시도)"):
                    return
                if sb_stage(_thread_messages(channel, thread_ts), work=work, episode=episode) < 2:
                    _reply(channel, thread_ts,
                          "❌ 자동주행 중단 — 상세 콘티 단계에서 재시도(1회)까지 실패했어요(위 오류 메시지 참고). "
                          "이 단계 없이는 씬을 나눌 수 없어 여기서 멈춰요.")
                    return

        msgs = _thread_messages(channel, thread_ts)
        conti = _thread_or_saved_conti(channel, thread_ts, msgs, work, episode, announce=False)
        if not conti:
            _reply(channel, thread_ts, "❌ 자동주행 중단 — 방금 만든 상세 콘티를 못 불러왔어요(스레드/저장본 모두 조회 실패)."); return
        scenes = _split_scenes(conti)
        if not scenes:
            _reply(channel, thread_ts, "❌ 자동주행 중단 — 콘티에서 씬을 못 나눴어요(씬 헤더 '■ 씬N' 형식인지 확인 필요)."); return
        _mark_progress(2)
        if end_stage == 2:
            _reply(channel, thread_ts,
                  ("✅ 상세 콘티 완료(재시도로 복구됨) — 요청하신 2단계까지만 실행하기로 해서 여기서 멈춰요."
                   if stage2_retried else "✅ 상세 콘티 완료 — 요청하신 2단계까지만 실행하기로 해서 여기서 멈춰요."))
            return
        # ★2026-07-15: "1씬만 하게 해서" — 4~5단계(샷분해·스틸컷·영상화)만 지정된 씬으로 좁힌다
        # (등록확인은 화 전체 맥락이 필요해 conti 전체를 그대로 씀 — scenes가 아니라 conti를
        # 참조하므로 이 필터의 영향을 안 받는다).
        if scene_only is not None:
            filtered = [s for s in scenes if s[0] == scene_only]
            if not filtered:
                avail = ", ".join(f"씬{s[0]}" for s in scenes)
                _reply(channel, thread_ts, f"❌ 자동주행 중단 — 씬{scene_only}을 콘티에서 못 찾았어요. 있는 씬: {avail}")
                return
            scenes = filtered

        # ★2026-07-15: start_stage>=4면 등록확인(3단계)도 건너뛴다 — 이 경우 미등록 요소로 인한
        # 일관성 문제가 이번 실행에서는 전혀 걸러지지 않는다는 점을 초반 안내(reg_skip_note)에서
        # 이미 밝혔고, 여기서도 gaps=[]로만 두고 넘어간다.
        gaps: list[str] = []
        if start_stage >= 4:
            _reply(channel, thread_ts, "⏭️ 등록확인(3단계)은 건너뛰어요 → 4/6 샷분해·스틸컷으로 바로 진행할게요…")
        else:
            _reply(channel, thread_ts, "✅ 상세 콘티 완료 → 3/6 등록 확인 중(미등록 인물/장소는 단순 케이스만 자동 등록)…")
            gaps = _autopilot_registration_check(channel, thread_ts, work, conti)
            if _autopilot_cancelled(channel, thread_ts, "3/6 등록 확인"):
                return
        _mark_progress(3)
        if end_stage == 3:
            parts = [f"🎬 자동주행 부분 실행 완료 ({start_stage}~{end_stage}단계) — <{work}> {episode}화"]
            parts.append("⚠️ 자동등록 못한 요소: " + " / ".join(gaps) if gaps
                         else ("(등록확인 자체를 건너뛰어서 미등록 요소 여부는 확인 안 됨)" if start_stage >= 4
                               else "등록확인 이상 없음"))
            parts.append("요청하신 3단계까지만 실행하기로 해서 여기서 멈춰요(4단계 샷분해부터는 실행하지 않았어요).")
            _reply(channel, thread_ts, "\n".join(parts))
            return
        # ★2026-07-15 "실패하면 왠만해서는 중단하지 말고 실패 이유를 찾고 쭉 진행하게 해야한다" —
        # gaps는 콘티 전체 요소 중 일부(예: 인물 1명 '외형' 누락)일 뿐이고, 그 요소가 아예 안 나오는
        # 씬도 많다. 그 하나 때문에 전체 화를 통째로 멈추지 않고, 참조 없이 진행됨을 미리 알린 뒤
        # 4/6부터 계속 진행한다 — 해당 요소가 나오는 컷만 일관성이 떨어질 수 있고, 그건 뒤이은
        # vision 후검사(pending_review)로도 다시 한 번 걸러진다.
        if gaps:
            _reply(channel, thread_ts,
                  "⚠️ 등록확인에서 자동으로 못 채운 항목이 있어요 — 이런 미등록 요소가 있어 그 부분 "
                  "일관성이 떨어질 수 있어요, 계속 진행할게요:\n" +
                  "\n".join(f"· {g}" for g in gaps))

        # ★2026-07-15(C4): 수동 스틸컷/영상화는 _CUT_CONFIRM_THRESHOLD를 넘으면 사람 확인을 받지만,
        # 자동주행은 skip_confirm=True로 그 게이트를 의도적으로 건너뛴다(사람 확인은 합본 단계에서만).
        # 비용 감이라도 미리 알 수 있게, 실제 생성 시작 전에 규모 추정치를 한 번 안내한다.
        total_beats = sum(len(_BEAT_TAG_RE.findall(body)) or STILL_CUTS_DEFAULT for _, _, body in scenes)
        _reply(channel, thread_ts,
              f"📊 규모 추정: {len(scenes)}개 씬, 약 {total_beats}컷 예상 — 이미지+영상 생성이 이어서 "
              "진행돼요(합본 전까지 별도 확인 없음). 중간에 멈추려면 \"취소\"/\"중단\"이라고 답해주세요.")

        pending_review = []
        skipped_scenes: list[str] = []  # ★2026-07-15: 씬 전체 실패는 pending_review(컷 단위 확인)보다
        # 더 나쁜 상태(완전 누락)라 따로 모아서 최종 요약에서 구분되게 보여준다.
        scene_cuts: dict[int, tuple] = {}
        vision_deadline = time.monotonic() + config.AUTOPILOT_VISION_BUDGET_SEC
        # ★2026-07-15 "이미 스틸컷까지 다 있는 상태인데 영상만 작업하게 지시 못하나?" — start_stage
        # 를 1~4단계는 건너뛰게 처리해뒀지만, 4단계(스틸컷 생성) "자체"를 건너뛰고 이미 확정 저장된
        # 컷을 불러와 곧장 영상화로 넘어가는 경로가 빠져있었다(이대로면 5단계부터 시작해도 스틸컷을
        # 처음부터 다시 만들어버림 — 사용자가 방금 지적). start_stage>=5면 vp_store에 이미 저장된
        # 컷을 씬별로 불러와서 쓰고, 없는 씬은 건너뛴다(스틸컷이 아예 없으면 영상화할 것도 없음).
        # ★2026-07-15 "씬1이 스틸컷 다 만들어졌으면 영상 생성이랑 병행해도 되는거 아냐?" — 사용자가
        # 실제로 씬1 스틸컷은 끝났는데 씬2+ 스틸컷이 아직 도는 동안 (가장 시간이 오래 걸리는) 영상
        # 생성이 통째로 놀고 있는 걸 보고 지적. 예전엔 "전체 씬 스틸컷 완료 → 그 다음에 전체 씬
        # 영상화"로 완전히 순차 2단계였는데, 스틸컷은 씬별로 체이닝(다음 씬이 이전 씬 콘티에 의존)돼
        # 순차일 수밖에 없는 반면 영상화는 씬 단위로 독립적이다. 그래서 각 씬의 스틸컷이 준비되는
        # 즉시 그 씬의 영상화를 백그라운드 스레드풀에 던져두고, 메인 스레드는 곧바로 다음 씬
        # 스틸컷으로 넘어가게 파이프라인화한다. max_workers=1로 "영상화끼리는 여전히 순차"를
        # 유지하는 이유는 영상 API를 여러 씬이 동시에 두드리게 하려는 게 아니라 "영상화가 다음 씬
        # 스틸컷 생성과 겹치게" 하려는 것뿐이라서다(목표는 두 단계 사이의 idle time 제거이지,
        # 영상 자체의 동시성 확대가 아님). end_stage==4(스틸컷까지만 요청)면 video_pool을 아예
        # None으로 둬서 영상 생성이 절대 시작되지 않게 한다.
        video_pool = cf.ThreadPoolExecutor(max_workers=1) if end_stage >= 5 else None
        video_futures: list = []

        def _submit_video(num, cuts, scene_seconds):
            if video_pool is None:
                return

            def _run():
                if thread_ts in _CANCEL:  # 시작 전에 이미 취소된 상태면 실행하지 않음
                    return []
                video_cuts = cuts
                if video_cut_range and num == scene_only:
                    lo, hi = video_cut_range
                    video_cuts = [c for c in cuts if lo <= c["n"] <= hi]
                    if not video_cuts:
                        _reply(channel, thread_ts, f"❌ 씬{num}에 컷{lo}~{hi} 범위에 해당하는 컷이 없어요 — 영상화를 건너뛰어요.")
                        return []
                flagged = _autopilot_videos_for_scene(channel, thread_ts, work, f"씬{num}", video_cuts, scene_seconds,
                                                      vision_deadline, scene_num=num, episode=episode)
                return [(num, c, reason) for c, reason in flagged]

            video_futures.append(video_pool.submit(_run))

        if start_stage >= 5:
            _reply(channel, thread_ts,
                  "⏭️ 샷분해·스틸컷(4단계)도 건너뛰어요 → 이미 저장된 컷을 불러와서 5/6 영상화로 바로 진행할게요"
                  + ("(씬별로 스틸컷을 불러오는 즉시 그 씬 영상화를 백그라운드로 바로 시작해요)…" if end_stage >= 5 else "…"))
            for num, hdr, body in scenes:
                if thread_ts in _CANCEL:
                    if video_pool is not None:
                        video_pool.shutdown(wait=False, cancel_futures=True)
                    if _autopilot_cancelled(channel, thread_ts, f"5/6 영상화 준비(씬{num} 컷 불러오기)"):
                        return
                existing_cuts = vp_store.load_latest_cuts(work, num, episode=episode)
                if not existing_cuts:
                    skipped_scenes.append(f"씬{num} — 저장된 스틸컷을 못 찾음(5단계부터 시작하려면 그 씬의 "
                                          "스틸컷이 먼저 확정 저장돼 있어야 해요)")
                    _reply(channel, thread_ts, f"❌ 씬{num}의 저장된 스틸컷을 못 찾아 이 씬은 건너뛰어요.")
                    continue
                dm = re.search(r"(\d+)\s*초", hdr)
                scene_seconds = int(dm.group(1)) if dm else None
                scene_cuts[num] = (existing_cuts, scene_seconds)
                _submit_video(num, existing_cuts, scene_seconds)
        else:
          _reply(channel, thread_ts, "✅ 등록 확인 완료 → 4/6 샷분해·스틸컷 생성 중…"
                + ("(씬별로 스틸컷이 끝나는 즉시 그 씬 영상화를 백그라운드로 시작하고, 다음 씬 스틸컷을 이어서 진행해요)"
                   if end_stage >= 5 else ""))
          for num, hdr, body in scenes:
            if thread_ts in _CANCEL:
                if video_pool is not None:
                    video_pool.shutdown(wait=False, cancel_futures=True)
                if _autopilot_cancelled(channel, thread_ts, f"4/6 샷분해·스틸컷(씬{num} 시작 전)"):
                    return
            # ★2026-07-15 "단계 안에서의 재개" — 4단계 도중 취소하고 같은 화로 다시 돌리면 이미
            # 끝난 씬까지 처음부터 다시 만들던 문제. 이 씬이 이미 (기대 컷 수만큼) 저장돼있으면
            # 재생성 없이 그대로 재사용한다. 컷 수가 기대치보다 적으면(중간에 취소된 씬) 안전하게
            # 판단해 그냥 다시 전부 생성 — 어느 컷이 빠졌는지 골라내는 부분 재생성은 _render_cuts의
            # 샷분해 자체가 매번 새로 이뤄져(컷 번호가 재호출마다 안정적이라는 보장이 없음) 여기서는
            # 지원하지 않는다(영상화 단계는 파일명이 컷 번호로 결정적이라 부분 재개가 가능했던 것과 다름).
            n_beats = len(_BEAT_TAG_RE.findall(body))
            expect_n = n_beats if n_beats else STILL_CUTS_DEFAULT
            existing_cuts = vp_store.load_latest_cuts(work, num, episode=episode)
            if existing_cuts and len(existing_cuts) >= expect_n:
                dm = re.search(r"(\d+)\s*초", hdr)
                scene_seconds = int(dm.group(1)) if dm else None
                scene_cuts[num] = (existing_cuts, scene_seconds)
                _submit_video(num, existing_cuts, scene_seconds)
                _reply(channel, thread_ts, f"⏭️ 씬{num}은 이미 스틸컷이 저장돼있어 다시 안 만들고 이어가요.")
                continue
            # ★2026-07-15 "그 씬 전체를 다 만들고 나서야 영상화 시작하지 말고 4컷 배치마다 끝나는
            # 대로 바로 영상화 넘기게" — 스틸컷 생성을 씬 내부에서 ≤4컷 배치로 쪼개 배치가 끝날
            # 때마다(검수까지 마친 뒤) on_batch_ready로 그 배치를 곧장 _submit_video에 넘긴다.
            # 배치 전체 전멸 시의 diagnose→adjust→retry(예전엔 여기 씬 단위로 있던 로직)는 이제
            # _autopilot_stills_for_scene 내부에서 배치 단위로 수행되므로 여기서는 그 결과
            # (batch_failures)만 받아 skipped_scenes에 반영한다 — 일부 배치만 실패해도 씬 전체를
            # 버리지 않고 성공한 배치들의 컷으로 scene_cuts를 채운다.
            cuts, scene_seconds, gave_up, batch_failures = _autopilot_stills_for_scene(
                channel, thread_ts, work, bible, num, hdr, body, vision_deadline=vision_deadline,
                on_batch_ready=lambda batch_cuts, ss, n=num: _submit_video(n, batch_cuts, ss))
            if thread_ts in _CANCEL:
                if video_pool is not None:
                    video_pool.shutdown(wait=False, cancel_futures=True)
                if _autopilot_cancelled(channel, thread_ts, f"4/6 샷분해·스틸컷(씬{num})"):
                    return
            skipped_scenes.extend(batch_failures)
            if not cuts:
                # 이 씬의 모든 배치가 실패한 경우에만 씬 자체를 완전히 건너뛴다(scene_cuts 미설정,
                # 영상화도 이미 배치별로 on_batch_ready가 한 번도 안 불렸으니 자동으로 안 일어남).
                _reply(channel, thread_ts, f"❌ 씬{num} 스틸컷 생성에 실패해 이 씬은 건너뛰어요 — 다음 씬 계속 진행할게요.")
                continue
            # ★2026-07-15: 개별 컷 생성 실패(재시도 후에도)와 vision 일관성 재검사 실패(flagged) 둘
            # 다 _autopilot_stills_for_scene이 배치 단위로 이미 최종 문구까지 포맷해 gave_up 하나로
            # 합쳐 반환한다(자세한 내용은 그 함수 docstring 참고) — 여기서는 그대로 이어붙이면 된다.
            pending_review += [f"씬{num} 스틸컷 컷{c} — {reason}" for c, reason in gave_up]
            scene_cuts[num] = (cuts, scene_seconds)

        # ★2026-07-15 "어디 단계에서 끝날지" — 재시도/gap/건너뛴 씬/확인필요 컷을 나열하는 로직은
        # 원래 6단계 끝에서만 한 번 만들어졌던 것을 부분 실행에서도 재사용할 수 있게 뽑아낸다.
        def _build_summary_parts(done_desc: str) -> list[str]:
            parts = [f"🎬 자동주행 부분 실행 완료 ({start_stage}~{end_stage}단계) — <{work}> {episode}화"
                     if end_stage < 6 else f"🎬 자동주행 요약 — <{work}> {episode}화",
                     "완료: " + done_desc]
            if stage1_retried or stage2_retried:
                retried = []
                if stage1_retried:
                    retried.append("씬 설계")
                if stage2_retried:
                    retried.append("상세 콘티")
                parts.append("🔁 재시도로 복구된 단계: " + ", ".join(retried))
            if gaps:
                parts.append(
                    "⚠️ 자동등록 못한 요소(해당 요소 나오는 컷은 참조 일관성이 떨어질 수 있어요): " +
                    " / ".join(gaps))
            elif start_stage >= 4:
                parts.append("⚠️ 등록확인을 건너뛰어서 미등록 요소 여부는 확인 안 됨")
            if skipped_scenes:
                parts.append("❌ 완전히 실패해서 건너뛴 씬: " + " / ".join(skipped_scenes))
            parts.append(
                "⚠️ 확인이 필요한 컷(일관성 재검사 실패 등): " + ", ".join(pending_review)
                if pending_review else "일관성 검사 이상 없음")
            return parts

        _mark_progress(4)
        if end_stage == 4:
            parts = _build_summary_parts("샷분해·스틸컷")
            parts.append("요청하신 4단계까지만 실행하기로 해서 여기서 멈춰요(5단계 영상화부터는 실행하지 않았어요).")
            _reply(channel, thread_ts, "✅ 스틸컷 완료\n\n" + "\n".join(parts))
            return

        # ★2026-07-15 위에서 각 씬 스틸컷이 준비되는 즉시 video_pool로 그 씬 영상화를 이미 던져뒀다
        # (병행 파이프라인) — 여기서는 남은 영상화가 마무리될 때까지만 기다린다. 스틸컷 전체가
        # 끝난 시점엔 이미 앞쪽 씬들의 영상화는 상당 부분 진행됐거나 끝나있는 게 정상.
        if video_pool is not None:
            _reply(channel, thread_ts, "✅ 모든 씬 스틸컷 처리 완료 → 남은 영상화 마무리 중…")
            video_pool.shutdown(wait=True)
            for fut in video_futures:
                for num, c, reason in (fut.result() or []):
                    pending_review.append(f"씬{num} 영상 컷{c} — {' '.join(reason.split())[:100]}")

        _mark_progress(5)
        if end_stage == 5:
            parts = _build_summary_parts("샷분해·스틸컷·영상화")
            parts.append("요청하신 5단계까지만 실행하기로 해서 여기서 멈춰요(6단계 합본은 실행하지 않았고, "
                         "그래서 합본 확인 절차도 이번엔 없어요).")
            _reply(channel, thread_ts, "✅ 영상화 완료\n\n" + "\n".join(parts))
            return

        # ★2026-07-15 "실패하면 왠만해서는 중단하지 말고 실패 이유를 찾고 쭉 진행하게 해야한다" —
        # 합본 확인 게이트 전에 사용자가 완전한 그림을 보도록, 재시도로 복구된 것/자동등록 못한
        # gap/완전히 건너뛴 씬/컷 단위 확인 필요 항목을 모두 구분해서 나열한다.
        summary_parts = _build_summary_parts("등록확인·씬설계·상세콘티·샷분해·스틸컷·영상화")
        summary_parts.append("아래 합본 결과를 확인하고 확정해주세요(자동주행에서 사람 확인이 필요한 유일한 지점).")
        _reply(channel, thread_ts, "✅ 영상화 완료 → 6/6 합본 준비 중…\n\n" + "\n".join(summary_parts))
        # ★2026-07-15: 6단계(합본)는 사람 확인 버튼을 거치는 지점이라 "완료"라 부르기 애매하지만,
        # 자동주행 스스로 할 일은 여기서 다 끝났다 — 다음에 맨몸 "[자동주행]"이 다시 와도 더 이상
        # 이어갈 단계가 없으므로 진행 기록을 지운다(get_autopilot_progress도 last_stage>=6이면
        # 어차피 None을 반환하지만, 명시적으로 지워두는 쪽이 이후 읽는 사람에게 더 명확하다).
        conti_state.clear_autopilot_progress(thread_ts)
        _do_compile(channel, thread_ts, f"<{work}> {episode}화")
    finally:
        job_ledger.finish_job(jid)

