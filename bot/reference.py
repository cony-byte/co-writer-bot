# -*- coding: utf-8 -*-
"""레퍼런스 DB 로드 — story-v1-scripts/reference/ 스키마 v3 산출물.

- reference_db.json : drama_clip 정제본 (정제 대본 script[], hook_desc, v3 태그)
- patterns/*.md     : story_type별 패턴 요약 (프롬프트 주입용 SSOT)
- templates/*.md    : 사내 작가 기획안/대본 템플릿 (있으면 주입)
"""
import json
from functools import lru_cache
from pathlib import Path

from . import config


def _joined_db() -> list[dict]:
    """reference_db.json + (있으면) bl_cats.json 조인. 비파괴 — 원본 파일은 안 건드림.
    bl_cats: 유튜브 video id → 한국 BL 수작업 태그. 매칭 항목에 genre='BL' + bl_tags 부착."""
    path = Path(config.REFERENCE_DIR) / "reference_db.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    blp = Path(config.REFERENCE_DIR) / "bl_cats.json"
    if blp.exists():
        try:
            bl = json.loads(blp.read_text(encoding="utf-8"))
        except Exception:
            bl = {}
        for it in data:
            tags = bl.get(str(it.get("id")))
            if tags:
                it["genre"] = "BL"
                it["bl_tags"] = tags
    return data


@lru_cache(maxsize=1)
def load_db() -> list[dict]:
    return _joined_db()


@lru_cache(maxsize=1)
def load_patterns() -> str:
    """patterns/ 전체를 하나의 문서로 병합 (INDEX 먼저). 총 수 KB — 통째로 주입 + 캐싱."""
    pdir = Path(config.REFERENCE_DIR) / "patterns"
    if not pdir.is_dir():
        return ""
    files = sorted(pdir.glob("*.md"), key=lambda p: (p.name != "INDEX.md", p.name))
    return "\n\n---\n\n".join(p.read_text(encoding="utf-8") for p in files)


@lru_cache(maxsize=1)
def load_templates() -> str:
    """사내 템플릿 — templates/*.md 병합. 아직 없으면 빈 문자열 (봇은 기본 양식으로 동작)."""
    tdir = Path(config.TEMPLATES_DIR)
    files = sorted(p for p in tdir.glob("*.md") if p.name != "README.md")
    return "\n\n---\n\n".join(p.read_text(encoding="utf-8") for p in files)


@lru_cache(maxsize=1)
def load_trend():
    """트렌드서치 인스턴스 (통합 DB v5 — v4_tagged 편으로 게이트).
    파일 없으면 None — 봇은 트렌드 기능만 비활성."""
    path = Path(config.REFERENCE_DIR) / "reference_db.json"
    if not path.exists():
        return None
    from .trend_search import TrendSearch
    return TrendSearch(items=load_db())   # bl_cats 조인된 items 사용 (BL 트렌드 가능)


@lru_cache(maxsize=1)
def sheet():
    """구글 시트 바이블 핸들 (URL·SECRET 설정 시). 미설정이면 None — 바이블 기능 비활성."""
    if not (config.SHEET_WEBAPP_URL and config.SHEET_SECRET):
        return None
    from .sheet_bible import SheetBible
    return SheetBible()


def reload() -> None:
    """레퍼런스/템플릿 갱신 후 캐시 무효화 (프로세스 재시작 없이). 시트 바이블은 별도(TTL/새로고침)."""
    load_db.cache_clear()
    load_patterns.cache_clear()
    load_templates.cache_clear()
    load_trend.cache_clear()
