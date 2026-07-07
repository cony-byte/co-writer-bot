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
}
# PATHED: 대분류 + 경로(중/소는 동적)
PATHED = {
    "인물": "등장인물", "등장인물": "등장인물",
    "개요": "개요", "대본": "대본",
    "회차분배": "회차분배", "분배": "회차분배",  # 중분류=구간(막), 소분류=화수·핵심사건
}

# 등장인물 소분류 통제어휘 (참고·표시 순서)
CHAR_SUBS = ["성별", "나이", "포지션", "설정", "핵심대사", "설명"]


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
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---------------- 쓰기 ----------------
    def upsert(self, work: str, top: str, mid: str = "", sub: str = "", content: str = "") -> dict:
        return self._post({"work": work, "top": top, "mid": mid, "sub": sub, "content": content})

    # ---------------- 읽기·조립 ----------------
    def list_works(self) -> list[str]:
        return self._get().get("works", [])

    def _assemble(self, work: str, rows: list[dict]) -> dict:
        """탭 행(대/중/소/내용) → 계층 bible dict. 인물은 소분류를 카드로 조립."""
        b = {
            "title": work,
            "status_raw": "", "current_episode": None,  # 진행상태 — 시점 판단 근거
            "forbidden": "",     # 금지사항 (줄글)
            "logline": "", "keyword": "",
            "target": "", "emotion": "",
            "plot": "",
            "episode_plan": {},  # {구간(막): {소분류(화수·핵심사건): 내용}}
            "characters": {},    # {이름: {소분류: 내용}}
            "outlines": {},      # {회차: 내용}
            "scripts": {},       # {회차: 내용}
        }
        for r in rows:
            top, mid, sub, content = r.get("top", ""), r.get("mid", ""), r.get("sub", ""), r.get("content", "")
            if top == "로그라인/키워드":
                if mid == "로그라인":
                    b["logline"] = content
                elif mid == "키워드":
                    b["keyword"] = content
            elif top == "타겟층/핵심정서":
                if mid == "타겟층":
                    b["target"] = content
                elif mid == "핵심정서":
                    b["emotion"] = content
            elif top == "진행상태":
                b["status_raw"] = content
                m = re.search(r"\d+", content or "")
                b["current_episode"] = int(m.group()) if m else None
            elif top == "금지사항":
                b["forbidden"] = content
            elif top == "줄거리":
                b["plot"] = content
            elif top == "회차분배":
                if mid:
                    b["episode_plan"].setdefault(mid, {})[sub or "내용"] = content
            elif top == "등장인물":
                if mid:
                    b["characters"].setdefault(mid, {})[sub or "설명"] = content
            elif top == "개요":
                if mid:
                    b["outlines"][mid] = content
            elif top == "대본":
                if mid:
                    b["scripts"][mid] = content
        b["last_synced"] = datetime.now(timezone.utc).isoformat()
        return b

    # ---------------- 캐싱 ----------------
    def get(self, work: str, force: bool = False) -> dict:
        now = time.time()
        cached = self._cache.get(work)
        if not force and cached and (now - cached[0]) < self._ttl:
            return cached[1]
        try:
            rows = self._get(work=work).get("rows", [])
            bible = self._assemble(work, rows)
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
