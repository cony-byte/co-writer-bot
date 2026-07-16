# -*- coding: utf-8 -*-
"""구글 시트 스토리 바이블 — 탭=작품, 대/중/소 계층. 저장 + 조립 + 캐싱.

입력구는 슬랙 봇, 열람은 구글 시트. Apps Script 웹앱(google_sheet/Code.gs) 경유.
스프레드시트 1개 = 바이블, 탭 1개 = 작품. 각 탭 행 = (대분류, 중분류, 소분류, 내용).

스키마(대분류 7종) → 명령 경로 파싱:
  고정 항목(이름만): 로그라인·키워드·타겟층·핵심정서
  단일 대분류:        줄거리·회차분배
  경로 대분류:        인물/<이름>/<소분류> · 개요/<N화> · 대본/<N화>
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import config

# ── 스키마: 명령 첫 조각 → (대분류, 해석 방식) ──────────────────────────────
# FIXED: 첫 조각이 곧 중분류, 대분류는 자동 복원 (소분류 없음)
FIXED = {
    "로그라인": ("로그라인/키워드", "로그라인"),
    "키워드": ("로그라인/키워드", "키워드"),
    "타겟층": ("타겟층/핵심정서", "타겟층"),
    "타겟": ("타겟층/핵심정서", "타겟층"),
    "핵심정서": ("타겟층/핵심정서", "핵심정서"),
    "정서": ("타겟층/핵심정서", "핵심정서"),
}
# SINGLE: 대분류 단일 (중/소 없음)
SINGLE = {
    "줄거리": "줄거리",
    "금지사항": "금지사항", "금지": "금지사항",
    "진행상태": "진행상태", "진행": "진행상태", "현재화": "진행상태", "현재": "진행상태",
    "강도": "강도", "수위": "강도", "강도조절": "강도",   # 1~5단계 톤/강도 다이얼
}
# PATHED: 대분류 + 경로(중/소는 동적)
PATHED = {
    "인물": "등장인물", "등장인물": "등장인물",
    "개요": "개요", "대본": "대본",
    "회차분배": "회차분배", "분배": "회차분배",  # 중분류=구간(막), 소분류=화수·핵심사건
}

# 등장인물 소분류 통제어휘 (참고·표시 순서)
CHAR_SUBS = ["성별", "나이", "포지션", "설정", "핵심대사", "설명"]
# 여러 소분류(열)를 갖는 표 대분류 → 필요한 소분류 목록. 내용을 넣으려면 소분류 필수.
TABLE_SUBS = {"등장인물": CHAR_SUBS, "회차분배": ["구간", "화수", "핵심사건"]}


def _parse_intensity(text: str) -> dict:
    """강도 필드 파싱. '개요 3, 대본 2' → {개요:3,대본:2}, 타입 없는 숫자는 일반값.
    → {intensity_map:{...}, intensity_level: 일반값|None}"""
    imap, general = {}, None
    for chunk in re.split(r"[,\n;]", text or ""):
        mi = re.search(r"[1-5]", chunk)
        if not mi:
            continue
        lvl = int(mi.group())
        typed = False
        for t in ("개요", "대본", "아이디어", "재미", "개연성", "회차분배"):
            if t in chunk:
                imap[t] = lvl
                typed = True
        if not typed and general is None:
            general = lvl
    return {"intensity_map": imap, "intensity_level": general}


def split_command(first_line: str) -> tuple[str, str]:
    """첫 줄을 (경로, 인라인 내용)으로 분리. 종류별로 경로가 차지하는 세그먼트 수가
    정해져 있으므로, 그 뒤에 붙은 텍스트(슬래시든 공백이든)는 전부 내용으로 본다.

      '로그라인/어머니를 살리기…'   → ('로그라인', '어머니를 살리기…')
      '로그라인 어머니를 살리기…'    → ('로그라인', '어머니를 살리기…')
      '인물/강태혁/성별 남'          → ('인물/강태혁/성별', '남')
      '인물/강태혁/성별/남'          → ('인물/강태혁/성별', '남')
      '개요/11화'                    → ('개요/11화', '')      (내용은 다음 줄)
    """
    parts = first_line.split("/")
    head = parts[0].strip().split()[0] if parts[0].strip() else ""
    if head in FIXED or head in SINGLE:
        n = 1
    elif head in PATHED and PATHED[head] in ("개요", "대본"):
        n = 2
    elif head in PATHED:                       # 등장인물·회차분배 (중분류+소분류)
        n = 3
    else:
        return (first_line.strip(), "")        # 모르는 종류 → 전부 경로 (parse_path가 거름)

    path_segs = [s.strip() for s in parts[:n]]
    rest = [s for s in parts[n:]]
    inline = "/".join(rest).strip()
    # 마지막 경로 세그먼트에 공백으로 붙은 내용 떼어내기 (예: '성별 남', '로그라인 …')
    if path_segs:
        bits = path_segs[-1].split(None, 1)
        if len(bits) > 1:
            path_segs[-1] = bits[0]
            inline = (bits[1] + ((" " + inline) if inline else "")).strip()
    return ("/".join(s for s in path_segs if s), inline)


def parse_path(path_str: str) -> tuple[str, str, str] | None:
    """명령 경로 → (대분류, 중분류, 소분류). 모르는 종류면 None."""
    parts = [p.strip() for p in path_str.split("/") if p.strip()]
    if not parts:
        return None
    head = parts[0]
    if head in FIXED:
        top, mid = FIXED[head]
        return (top, mid, "")
    if head in SINGLE:
        return (SINGLE[head], "", "")
    if head in PATHED:
        top = PATHED[head]
        mid = parts[1] if len(parts) > 1 else ""
        sub = parts[2] if len(parts) > 2 else ""
        return (top, mid, sub)
    return None


def _load_script_summaries(work: str) -> dict:
    """대본 확정 저장 시 app.py가 쌓아둔 흐름 요약 캐시(로컬, 시트에는 없음) 중 이 작품 것만."""
    try:
        all_st = json.loads(config.SCRIPT_SUMMARIES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return all_st.get(work, {})


def _load_notion_scripts_cache() -> dict:
    try:
        return json.loads(config.NOTION_SCRIPTS_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_notion_scripts_cache(cache: dict) -> None:
    try:
        config.NOTION_SCRIPTS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.NOTION_SCRIPTS_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


_BAD_WORK_TOKEN_RE = re.compile(r"^[@#!]|^[UBWC][A-Z0-9]{6,}(\||$)")   # <@U..>/<#C..|이름>/<!here> 등
_BAD_WORK_LITERALS = {"작품", "undefined", "none", "null"}


def _looks_like_bad_work(work: str) -> bool:
    """슬랙 멘션 토큰이나 '작품' 같은 도움말 플레이스홀더가 실수로 작품명으로 들어와 시트에
    가짜 탭이 생기는 걸 막는 마지막 방어선(2026-07-13, 실제로 '@U0BGBH3DQKG'/'작품' 탭이 생겼었음).
    호출부가 어디든(지금·앞으로) 이 upsert 관문 하나로 다 막힌다."""
    w = (work or "").strip()
    if not w or w in _BAD_WORK_LITERALS:
        return True
    return bool(_BAD_WORK_TOKEN_RE.match(w))


def _notion_scripts(work: str) -> dict:
    """실무자가 최종 대본을 노션에서 직접 관리하는 작품 대응 — 대본은 시트에 안 옮기고
    노션에서 매번 직접 읽는다(2026-07-13 결정: [동기화] LLM 요약이 긴 원문을 요약해버려
    시트 값이 오염되는 문제가 있었음). 등록 안 됐거나 실패하면 조용히 {} — 시트 값이 폴백.

    풀 페이지 재귀 페치(수 초~10초대)는 비싸므로, 먼저 page_last_edited(단일 호출, 0.2~0.3초)로
    페이지가 실제 바뀌었는지 확인 → 안 바뀌었으면 로컬 캐시를 그대로 반환하고 풀 페치를 생략."""
    if not config.NOTION_TOKEN:
        return {}
    try:
        from .shared import notion_sync, works
        pid = works.page_of(work)
        if not pid:
            return {}
        le = notion_sync.page_last_edited(pid)
        cache = _load_notion_scripts_cache()
        entry = cache.get(work)
        if entry and le and entry.get("last_edited") == le:
            return entry.get("scripts") or {}
        full = notion_sync.page_text(pid)
        scripts = notion_sync.parse_episode_scripts(full)
        cache[work] = {"last_edited": le, "scripts": scripts}
        _save_notion_scripts_cache(cache)
        return scripts
    except Exception:
        return {}


class SheetBible:
    def __init__(self, url: str | None = None, secret: str | None = None,
                 ttl: int | None = None):
        self._url = url or config.SHEET_WEBAPP_URL
        self._secret = secret or config.SHEET_SECRET
        self._ttl = ttl if ttl is not None else config.SHEET_CACHE_TTL
        self._cache: dict[str, tuple[float, dict]] = {}

    # ---------------- HTTP ----------------
    def _get(self, **params) -> dict:
        params["secret"] = self._secret
        q = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{self._url}?{q}", method="GET")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post(self, payload: dict) -> dict:
        payload["secret"] = self._secret
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        last = None
        for attempt in range(3):   # Apps Script 간헐 지연 대비 재시도 (upsert는 멱등)
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:
                last = e
                time.sleep(0.6 * (attempt + 1))
        raise last

    # ---------------- 쓰기 ----------------
    def upsert(self, work: str, top: str, mid: str = "", sub: str = "", content: str = "") -> dict:
        if _looks_like_bad_work(work):
            return {"error": f"작품명이 올바르지 않아 저장을 막았어요: {work!r}"}
        return self._post({"work": work, "top": top, "mid": mid, "sub": sub, "content": content})

    def exists(self, work: str, top: str, mid: str = "", sub: str = "") -> bool | None:
        """해당 칸에 값이 이미 있는지. 확인 불가(장애)면 None. (레이아웃은 Apps Script가 관리,
        여기선 구조화 JSON에서 값 유무만 확인)"""
        try:
            d = self._get(work=work)
        except Exception:
            return None
        single = d.get("single", {})
        if top in ("진행상태", "금지사항", "줄거리", "강도"):
            return bool(single.get(top))
        if top in ("로그라인/키워드", "타겟층/핵심정서"):
            return bool(single.get(mid))          # mid = 로그라인/키워드/타겟층/핵심정서
        if top == "등장인물":
            r = next((x for x in d.get("등장인물", []) if x.get("이름") == mid), None)
            return bool(r and r.get(sub))
        if top == "회차분배":
            r = next((x for x in d.get("회차분배", []) if x.get("막") == mid), None)
            return bool(r and r.get(sub))
        if top in ("개요", "대본"):
            r = next((x for x in d.get(top, []) if x.get("화") == mid), None)
            return bool(r and r.get("내용"))
        return None

    # ---------------- 읽기·조립 ----------------
    def list_works(self) -> list[str]:
        return self._get().get("works", [])

    def _assemble(self, work: str, data: dict, notion_scripts: dict | None = None) -> dict:
        """구조화 JSON(Apps Script가 표 레이아웃을 파싱해 반환) → bible dict.
        data = {single:{...}, 등장인물:[{이름,성별,...}], 회차분배:[{막,구간,화수,핵심사건}],
                개요:[{화,내용}], 대본:[{화,내용}]}
        notion_scripts: get()에서 시트 fetch와 병렬로 미리 받아온 노션 대본(없으면 여기서 직접 fetch —
        순차 폴백, _assemble을 단독 호출하는 다른 경로 대비)."""
        s = data.get("single", {}) or {}
        status = s.get("진행상태", "") or ""
        m = re.search(r"\d+", status)

        # 타입별 진행 화 파싱: "3화 개요 작업 중, 2화 대본" → {개요:3, 대본:2, 회차분배:...}
        progress = {}
        for chunk in re.split(r"[,\n;·]", status):
            me = re.search(r"(\d+)\s*화", chunk)
            if not me:
                continue
            ep = int(me.group(1))
            if "개요" in chunk:
                progress["개요"] = ep
            if "대본" in chunk:
                progress["대본"] = ep
            if "회차" in chunk or "분배" in chunk:
                progress["회차분배"] = ep

        def _rows_to_map(rows, key, keep, normalize=None):
            """[{key:.., col:..}] → {키: {col: 값}} (빈 값·키 없는 행 제외).
            normalize가 있으면 키를 통일해서(예: '1막'/'1막. 제목' → '1막') 예전에 재동기화마다
            다르게 뽑혀 쌓인 중복 행이 하나로 합쳐지게 한다(2026-07-13)."""
            out = {}
            for r in rows or []:
                name = (r.get(key) or "").strip()
                if not name:
                    continue
                if normalize:
                    name = normalize(name)
                out[name] = {k: r[k] for k in keep if r.get(k)}
            return out

        def _normalize_gu(raw):
            m = re.match(r"\s*(\d+)\s*막", raw or "")
            return f"{m.group(1)}막" if m else raw

        b = {
            "title": work,
            "status_raw": status,
            "current_episode": int(m.group()) if m else None,
            "progress": progress,   # {개요:3, 대본:2, 회차분배:..} 타입별 진행 화
            "intensity_raw": (s.get("강도", "") or "").strip(),
            **_parse_intensity(s.get("강도", "") or ""),
            "forbidden": s.get("금지사항", ""),
            "logline": s.get("로그라인", ""),
            "keyword": s.get("키워드", ""),
            "target": s.get("타겟층", ""),
            "emotion": s.get("핵심정서", ""),
            "plot": s.get("줄거리", ""),
            "characters": _rows_to_map(data.get("등장인물"), "이름", CHAR_SUBS),
            "episode_plan": _rows_to_map(data.get("회차분배"), "막", ["구간", "화수", "핵심사건"],
                                        normalize=_normalize_gu),
            # 내용이 빈 개요/대본 행은 '없는 것'으로 취급 (빈칸저장 잔재·환각 방지)
            "outlines": {(r.get("화") or "").strip(): r.get("내용", "")
                         for r in (data.get("개요") or [])
                         if (r.get("화") or "").strip() and (r.get("내용") or "").strip()},
            # 대본은 시트 값(봇이 직접 생성·확정한 화) 위에 노션 원문(실무자가 직접 관리하는 화)을
            # 덮어써서 합친다 — 노션이 있으면 그게 최종본, 없는 화만 시트 값이 남는다.
            "scripts": {
                **{(r.get("화") or "").strip(): r.get("내용", "")
                   for r in (data.get("대본") or [])
                   if (r.get("화") or "").strip() and (r.get("내용") or "").strip()},
                **(notion_scripts if notion_scripts is not None else _notion_scripts(work)),
            },
            "script_summaries": _load_script_summaries(work),
            "last_synced": datetime.now(timezone.utc).isoformat(),
        }
        return b

    # ---------------- 캐싱 ----------------
    def get(self, work: str, force: bool = False) -> dict:
        now = time.time()
        cached = self._cache.get(work)
        if not force and cached and (now - cached[0]) < self._ttl:
            return cached[1]
        try:
            # 시트(Apps Script)와 노션 풀 페이지 fetch는 서로 무관한 HTTP 호출이라 병렬 실행 —
            # 순차면 둘 합산 시간(최대 11초 실측)이 들지만, 병렬이면 둘 중 느린 쪽 시간만 든다.
            import threading
            notion_scripts: dict = {}

            def _fetch_notion():
                nonlocal notion_scripts
                notion_scripts = _notion_scripts(work)

            t = threading.Thread(target=_fetch_notion, daemon=True)
            t.start()
            data = self._get(work=work)
            t.join(timeout=20)   # 노션이 20초 넘게 걸리면 포기 — 시트 값만으로 진행(늦게 끝나도 무해)
            bible = self._assemble(work, data, notion_scripts=notion_scripts)
            self._cache[work] = (now, bible)
            return bible
        except Exception as e:
            if cached:
                fb = dict(cached[1])
                fb["stale"] = True
                fb["stale_reason"] = str(e)
                return fb
            raise

    def invalidate(self, work: str | None = None) -> None:
        if work:
            self._cache.pop(work, None)
        else:
            self._cache.clear()
