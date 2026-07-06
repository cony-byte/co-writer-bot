# -*- coding: utf-8 -*-
"""유사 사례 선별 — 요청 텍스트에서 태그 신호를 뽑아 통합 DB(v5)에서 2~3편 고른다.

패턴 레이어 원칙(patterns/INDEX.md): 생성 프롬프트에는 원본 76편이 아니라
'패턴 요약 + 유사 사례 2~3편'만 들어간다.
v5 통합 DB: v3 정제축(영어 trope·hook_type…) + v4 생성축(catharsis_type·hook_beat·cliffhanger_type).
"""
from __future__ import annotations

from .tag_vocab import ALIASES

# 화자 라벨 표시용 — ML/FL 채워지면 한글화, 화자중립(화자1/2)이면 그대로 (재크롤링에 graceful)
_ROLE_KO = {"ML": "남주", "FL": "여주", "SUP": "조연", "NAR": "나레이션", "UNK": "미상"}


def extract_tags(text: str) -> set[str]:
    tags: set[str] = set()
    for kw, mapped in ALIASES.items():
        if kw in text:
            tags.update(mapped)
    return tags


def _entry_tags(e: dict) -> set[str]:
    t = e["tags"]
    return {
        *t.get("trope_tags", []), *t.get("dialogue_tags", []),
        *t.get("male_lead", []), *t.get("hook_beat", []),
        t.get("hook_type", ""), t.get("story_type", ""), t.get("setting", ""),
        t.get("catharsis_type", ""), t.get("cliffhanger_type", ""),
    } - {""}


def select_examples(query: str, db: list[dict], k: int = 3) -> list[dict]:
    """태그 겹침 점수 → v4 생성축 보너스 → 정제 대본 유무 → ER 순."""
    want = extract_tags(query)
    # 생성축(catharsis/hook_beat/cliffhanger) 쿼리 신호가 있으면 v4_tagged 편에 가점
    v4_axes = {"regret_grovel", "revenge_payback", "status_reversal", "devotion_thrill",
               "salvation", "forbidden_tension", "humor_flutter"}
    want_v4 = bool(want & v4_axes)

    def score(e: dict) -> tuple:
        overlap = len(want & _entry_tags(e)) if want else 0
        v4_bonus = 1 if (want_v4 and e.get("v4_tagged")) else 0
        has_script = 1 if e.get("script") else 0
        er = e["metrics"].get("er") or 0
        return (overlap, v4_bonus, has_script, er)

    return sorted(db, key=score, reverse=True)[:k]


def _fmt_line(line: dict) -> str:
    rh = line.get("role_hint") or line.get("speaker") or "?"
    return f"  {_ROLE_KO.get(rh, rh)}: {line['line']}"


def format_example(e: dict) -> str:
    t = e["tags"]
    tropes = t.get("trope_tags_ko") or t.get("trope_tags") or []  # 한글 우선
    lines = [
        f"### 사례 {e['id']} (ER {e['metrics'].get('er')}, {e['metrics'].get('dur')}초)",
        f"- 훅(첫 3초): {e.get('hook_desc') or '(불명)'}",
        f"- 트로프: {', '.join(tropes) or '-'}",
        f"- 남주: {', '.join(t.get('male_lead') or []) or '-'} / 배경: {t.get('setting') or '-'}",
    ]
    # v4 생성축 (있는 편만 — 미태깅편은 빈값이라 자동 생략)
    gen = []
    if t.get("catharsis_type"):
        gen.append(f"정서={t['catharsis_type']}")
    if t.get("hook_beat"):
        gen.append(f"훅비트={'·'.join(t['hook_beat'])}")
    if t.get("cliffhanger_type"):
        gen.append(f"절단점={t['cliffhanger_type']}")
    cs = t.get("character_setup") or {}
    if cs.get("villain") and cs["villain"] != "없음":
        gen.append(f"악역={cs['villain']}")
    if gen:
        lines.append("- 생성축: " + " / ".join(gen))
    if e.get("script"):
        lines.append("- 정제 대본:")
        lines += [_fmt_line(s) for s in e["script"]]
    return "\n".join(lines)
