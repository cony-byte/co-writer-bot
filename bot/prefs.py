# -*- coding: utf-8 -*-
"""작가 선호 피드백 저장소 (좋아/별로) — 태그 기반 검색으로 다음 생성에 주입.

흐름: 생성 → [좋아]/[별로] → 여기 저장 → 다음 생성 때 관련 피드백 검색 → 프롬프트 주입.
임베딩 없이 기존 retrieval(태그 겹침)과 같은 방식. 작품별 로컬 JSON.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config, retrieval

PREF_DIR = Path(config.BASE_DIR) / "data" / "prefs"


def _path(work: str) -> Path:
    safe = re.sub(r"[^\w가-힣]", "_", work.strip()) or "work"
    return PREF_DIR / f"{safe}.json"


def load(work: str) -> list[dict]:
    p = _path(work)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def add(work: str, sign: str, top: str, episode: int | None,
        reason: str, excerpt: str, level: int | None = None) -> None:
    """sign: '+'(좋아) / '-'(별로). level=그 생성물의 강도 단계(있으면 강도별 반영)."""
    PREF_DIR.mkdir(parents=True, exist_ok=True)
    items = load(work)
    items.append({
        "sign": sign,
        "top": top or "",
        "episode": episode,
        "level": level,
        "reason": (reason or "").strip(),
        "excerpt": (excerpt or "").strip()[:600],
        "tags": sorted(retrieval.extract_tags(f"{reason} {excerpt}")),
    })
    _path(work).write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def retrieve(work: str, top: str, query: str, level: int | None = None,
             k_pos: int = 2, k_neg: int = 3) -> tuple[list[dict], list[dict]]:
    """현재 요청(query)·타입(top)·강도(level)와 관련된 좋아/별로 피드백을 뽑는다.
    점수 = 강도 일치 + 태그 겹침 + 같은 타입 + 최근순."""
    items = load(work)
    if not items:
        return [], []
    want = retrieval.extract_tags(query)
    n = len(items)

    def score(i_it):
        i, it = i_it
        same_level = 1 if (level and it.get("level") == level) else 0
        overlap = len(want & set(it.get("tags") or [])) if want else 0
        same_top = 1 if it.get("top") == top else 0
        recency = i / n            # 뒤(최근)일수록 ↑
        return (same_level, overlap, same_top, recency)

    pos = [it for _, it in sorted(enumerate(items), key=score, reverse=True) if it["sign"] == "+"]
    neg = [it for _, it in sorted(enumerate(items), key=score, reverse=True) if it["sign"] == "-"]
    return pos[:k_pos], neg[:k_neg]


def format_block(positives: list[dict], negatives: list[dict]) -> str:
    """검색된 선호 피드백 → 프롬프트 주입용 블록."""
    if not positives and not negatives:
        return ""
    def _lv(it):
        return f"[강도 {it['level']}] " if it.get("level") else ""
    lines = ["## 작가 선호 피드백 (지난 생성에 대한 반응 — 반드시 반영)"]
    for it in positives:
        r = f" — {it['reason']}" if it.get("reason") else ""
        lines.append(f"- 👍 {_lv(it)}이런 방향 좋았음{r}\n  예: {it['excerpt'][:200]}")
    for it in negatives:
        r = f" — {it['reason']}" if it.get("reason") else ""
        lines.append(f"- 👎 {_lv(it)}이건 별로였음(피하라){r}\n  예: {it['excerpt'][:200]}")
    lines.append("→ 👍 방향은 살리고 👎 방향은 반복하지 마라.")
    return "\n".join(lines)
