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

    def _assemble(self, work: str, data: dict) -> dict:
        """구조화 JSON(Apps Script가 표 레이아웃을 파싱해 반환) → bible dict.
        data = {single:{...}, 등장인물:[{이름,성별,...}], 회차분배:[{막,구간,화수,핵심사건}],
                개요:[{화,내용}], 대본:[{화,내용}]}"""
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

        def _rows_to_map(rows, key, keep):
            """[{key:.., col:..}] → {키: {col: 값}} (빈 값·키 없는 행 제외)"""
            out = {}
            for r in rows or []:
                name = (r.get(key) or "").strip()
                if not name:
                    continue
                out[name] = {k: r[k] for k in keep if r.get(k)}
            return out

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
            "episode_plan": _rows_to_map(data.get("회차분배"), "막", ["구간", "화수", "핵심사건"]),
            "outlines": {(r.get("화") or "").strip(): r.get("내용", "")
                         for r in (data.get("개요") or []) if (r.get("화") or "").strip()},
            "scripts": {(r.get("화") or "").strip(): r.get("내용", "")
                        for r in (data.get("대본") or []) if (r.get("화") or "").strip()},
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
            data = self._get(work=work)
            bible = self._assemble(work, data)
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
