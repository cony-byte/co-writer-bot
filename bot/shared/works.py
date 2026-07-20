# -*- coding: utf-8 -*-
"""작품 등록소 — 작품명·별칭 → 노션 페이지 매핑. data/notion_pages.json에 저장(재시작 무관).

실무자는 노션 페이지만 만들고 `[동기화] <작품> <링크>` 한 번 → 여기 등록됨.
이후 자동 폴러가 등록된 작품의 노션을 읽어 시트(빠른 캐시)에 반영. 시트는 실무자가 안 건드림.

저장 형식: { "<정식작품명>": {"page": "<32자리 page_id>", "aliases": ["별칭1", ...]} }
env NOTION_PAGES({이름:page_id})도 기본값으로 병합(파일이 우선).

(2026-07-16, 봇 합체 HANDOFF §3-2) co-writer-bot/storyboard-bot 두 버전을 병합.
storyboard 버전에는 `COWRITER_WORKS_PATH` env로 co-writer repo의 notion_pages.json을 가리키고,
`_cowriter_env_pages()`로 그 옆의 .env까지 따로 읽어 NOTION_PAGES를 병합하는 로직이 있었다 —
두 봇이 서로 다른 repo/프로세스로 떨어져 있을 때 "저쪽 봇이 등록한 작품"을 이쪽에서도 알아보기
위한 다리였음. 이제 두 봇이 이 repo 하나, `.env` 파일 하나를 공유하므로 그 다리 자체가
필요 없어졌다: `COWRITER_WORKS_PATH`는 이미 삭제됐고(.env 참고), `_cowriter_env_pages()`가
읽으려던 NOTION_PAGES는 `config.NOTION_PAGES`가 같은 .env에서 이미 읽어와 들고 있는 값과
동일하다 — 그대로 남겨뒀다면 같은 값을 파일에서 다시 파싱하는 죽은 코드가 됐을 것이므로
드롭했다. `_looks_like_bad_work()`/`_TRAILING_SUFFIXES` 가드는 두 버전 모두에 필요해 유지.
"""
from __future__ import annotations

import json
import re

from .. import config

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
            known.add(name)
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


_TRAILING_SUFFIXES = ("기획안", "기획서")


def sanitize(name: str) -> str:
    """구글 시트 탭명으로 못 쓰는 문자 제거 → 노션 제목을 안전한 작품명으로.
    (2026-07-15) 노션 페이지 제목을 그대로 정식명으로 쓰다 보니 중복 공백이나
    '…기획안/기획서' 같은 꼬리표가 그대로 등록돼 부르기 지저분했던 문제 —
    공백을 하나로 줄이고, 끝단어가 그 꼬리표면 떼어낸다."""
    s = re.sub(r"[\[\]:*?/\\]", " ", name or "").strip()
    s = re.sub(r"\s+", " ", s)
    for suf in _TRAILING_SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            stripped = s[: -len(suf)].strip()
            if stripped:
                s = stripped
            break
    return s


def page_of(name: str) -> str | None:
    w = resolve(name)
    return all_works().get(w, {}).get("page") if w else None


_BAD_WORK_TOKEN_RE = re.compile(r"^[@#!]|^[UBWC][A-Z0-9]{6,}(\||$)")   # <@U..>/<#C..|이름>/<!here> 등
_BAD_WORK_LITERALS = {"작품", "undefined", "none", "null"}


def _looks_like_bad_work(work: str) -> bool:
    """멘션 토큰(<@U..> 등)이나 '작품' 플레이스홀더를 작품명으로 등록하지 않게(2026-07-13,
    sheet_bible.py의 동명 함수와 같은 방어 — 실제로 가짜 탭/매핑이 생겼던 문제)."""
    w = (work or "").strip()
    if not w or w in _BAD_WORK_LITERALS:
        return True
    return bool(_BAD_WORK_TOKEN_RE.match(w))


def register(work: str, page_id: str, aliases: list | None = None) -> None:
    """작품 등록/갱신 (페이지 매핑 + 선택 별칭)."""
    if _looks_like_bad_work(work):
        return
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


def get_style(work: str) -> str | None:
    """★2026-07-20: 작품마다 스틸컷/영상 그림체(예: realistic/2d_anim)를 다르게 쓰고 싶다는
    요청 — 등록된 style_key를 반환(없으면 None → 호출자가 기본 스타일을 쓴다)."""
    w = resolve(work) or work
    d = _load()
    return (d.get(w) or {}).get("style")


def set_style(work: str, style_key: str) -> str | None:
    """작품에 그림체를 등록/변경. 반환: 정식 작품명(작품을 못 찾으면 None)."""
    w = resolve(work)
    if not w:
        return None
    d = _load()
    entry = d.get(w) or {"page": page_of(w) or "", "aliases": []}
    entry["style"] = style_key
    d[w] = entry
    _save(d)
    return w
