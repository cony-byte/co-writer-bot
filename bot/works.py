# -*- coding: utf-8 -*-
"""작품 등록소 — 작품명·별칭 → 노션 페이지 매핑. data/notion_pages.json에 저장(재시작 무관).

실무자는 노션 페이지만 만들고 `[동기화] <작품> <링크>` 한 번 → 여기 등록됨.
이후 자동 폴러가 등록된 작품의 노션을 읽어 시트(빠른 캐시)에 반영. 시트는 실무자가 안 건드림.

저장 형식: { "<정식작품명>": {"page": "<32자리 page_id>", "aliases": ["별칭1", ...]} }
env NOTION_PAGES({이름:page_id})도 기본값으로 병합(파일이 우선).
"""
from __future__ import annotations

import json

from . import config

_PATH = config.BASE_DIR / "data" / "notion_pages.json"


def _load() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def all_works() -> dict:
    """{정식작품명: {page, aliases}} — 파일 + env NOTION_PAGES 병합(파일 우선)."""
    d = _load()
    known = set(d) | {a for v in d.values() for a in (v.get("aliases") or [])}
    for name, page in (config.NOTION_PAGES or {}).items():
        if name not in known:
            d[name] = {"page": page, "aliases": []}
    return d


def resolve(name: str) -> str | None:
    """이름/별칭 → 정식 작품명. 없으면 None."""
    name = (name or "").strip()
    d = all_works()
    if name in d:
        return name
    for w, v in d.items():
        if name in (v.get("aliases") or []):
            return w
    return None


def work_by_page(page_id: str) -> str | None:
    """page_id로 이미 등록된 정식 작품명 찾기. 없으면 None. (제목 바뀌어도 id로 식별)"""
    page_id = (page_id or "").replace("-", "")
    for w, v in all_works().items():
        if (v.get("page") or "").replace("-", "") == page_id:
            return w
    return None


def sanitize(name: str) -> str:
    """구글 시트 탭명으로 못 쓰는 문자 제거 → 노션 제목을 안전한 작품명으로."""
    import re
    return re.sub(r"[\[\]:*?/\\]", " ", name or "").strip()


def page_of(name: str) -> str | None:
    w = resolve(name)
    return all_works().get(w, {}).get("page") if w else None


def register(work: str, page_id: str, aliases: list | None = None) -> None:
    """작품 등록/갱신 (페이지 매핑 + 선택 별칭)."""
    d = _load()
    entry = d.get(work) or {"page": "", "aliases": []}
    entry["page"] = page_id
    if aliases:
        entry["aliases"] = sorted(set((entry.get("aliases") or []) + list(aliases)))
    d[work] = entry
    _save(d)


def add_aliases(work: str, aliases: list) -> str | None:
    """기존 작품에 별칭 추가. 반환: 정식 작품명(없으면 None)."""
    w = resolve(work)
    if not w:
        return None
    d = _load()
    entry = d.get(w) or {"page": page_of(w) or "", "aliases": []}
    entry["aliases"] = sorted(set((entry.get("aliases") or []) + list(aliases)))
    d[w] = entry
    _save(d)
    return w
