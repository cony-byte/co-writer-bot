# -*- coding: utf-8 -*-
"""구글 시트 스토리 바이블 — 저장(upsert) + 조회 + 캐싱.

입력구는 슬랙 봇, 열람은 구글 시트. 시트가 바이블 SSOT다 (노션 대체).
Apps Script 웹앱(google_sheet/Code.gs) 경유로 read/write.

시트 행 = (work, kind, content). kind:
  현재화 | 로그라인 | 타겟정서 | 인물 | 줄거리 | 회차표 | {N}화_개요 | {N}화_대본 | 기획안
→ 노션과 동일한 bible dict 스키마로 변환해 prompts.build_bible_block이 그대로 소비.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import config

# kind → bible.raw 필드 (단일값 섹션)
_KIND_TO_RAW = {
    "로그라인": "logline",
    "타겟정서": "target_emotion",
    "인물": "characters",
    "줄거리": "plot",
    "회차표": "episode_table",
}
_EP_KIND = re.compile(r"^(\d+)화_(개요|대본)$")


class SheetBible:
    def __init__(self, url: str | None = None, secret: str | None = None,
                 ttl: int | None = None):
        self._url = url or config.SHEET_WEBAPP_URL
        self._secret = secret or config.SHEET_SECRET
        self._ttl = ttl if ttl is not None else config.SHEET_CACHE_TTL
        self._cache: dict[str, tuple[float, dict]] = {}  # work → (fetched_at, bible)

    # ---------------- HTTP (Apps Script 웹앱) ----------------
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
        # Apps Script 웹앱은 302→googleusercontent 리다이렉트를 거쳐 결과 반환 (urllib이 따라감)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---------------- 쓰기 (슬랙 → 시트) ----------------
    def upsert(self, work: str, kind: str, content: str) -> dict:
        return self._post({"work": work, "kind": kind, "content": content})

    # ---------------- 읽기 ----------------
    def list_works(self) -> list[str]:
        return self._get().get("works", [])

    def _fetch_bible(self, work: str) -> dict:
        rows = self._get(work=work).get("rows", [])
        raw = {"logline": "", "target_emotion": "", "characters": "",
               "plot": "", "episode_table": "", "episodes": []}
        cur = None
        eps: dict[int, dict] = {}
        for row in rows:
            kind, content = row.get("kind", ""), row.get("content", "")
            if kind == "현재화":
                m = re.search(r"\d+", str(content))
                cur = int(m.group()) if m else None
            elif kind in _KIND_TO_RAW:
                raw[_KIND_TO_RAW[kind]] = content
            else:
                m = _EP_KIND.match(kind)
                if m:
                    num, part = int(m.group(1)), m.group(2)
                    e = eps.setdefault(num, {"number": num, "outline": "", "script": ""})
                    e["outline" if part == "개요" else "script"] = content
        raw["episodes"] = [eps[n] for n in sorted(eps)]
        return {
            "title": work, "current_episode": cur, "raw": raw,
            "last_synced": datetime.now(timezone.utc).isoformat(),
        }

    # ---------------- 캐싱·갱신 ----------------
    def get(self, work: str, force: bool = False) -> dict:
        """짧은 캐시 + 새로고침 즉시 무효화. 시트 장애 시 마지막 캐시로 폴백(stale 표시)."""
        now = time.time()
        cached = self._cache.get(work)
        if not force and cached and (now - cached[0]) < self._ttl:
            return cached[1]
        try:
            bible = self._fetch_bible(work)
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
