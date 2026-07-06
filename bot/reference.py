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


@lru_cache(maxsize=1)
def load_db() -> list[dict]:
    path = Path(config.REFERENCE_DIR) / "reference_db.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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
    return TrendSearch(str(path))


def reload() -> None:
    """레퍼런스/템플릿 갱신 후 캐시 무효화 (프로세스 재시작 없이)."""
    load_db.cache_clear()
    load_patterns.cache_clear()
    load_templates.cache_clear()
    load_trend.cache_clear()
