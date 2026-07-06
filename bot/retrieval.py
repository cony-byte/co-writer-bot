# -*- coding: utf-8 -*-
"""유사 사례 선별 — 요청 텍스트에서 태그 신호를 뽑아 레퍼런스 DB에서 2~3편 고른다.

패턴 레이어 원칙(patterns/INDEX.md): 생성 프롬프트에는 원본 76편이 아니라
'패턴 요약 + 유사 사례 2~3편'만 들어간다.
"""
from __future__ import annotations

# 한국어 키워드 → v3 태그 별칭 사전 (가벼운 규칙 기반 1차 매칭)
ALIASES: dict[str, list[str]] = {
    # trope_tags
    "삼각관계": ["love_triangle_or_rival"], "연적": ["love_triangle_or_rival"],
    "계약": ["contract_or_fake_relationship"], "위장": ["contract_or_fake_relationship"],
    "신데렐라": ["class_gap_cinderella"], "신분": ["class_gap_cinderella"],
    "복수": ["revenge_betrayal_or_payback"], "배신": ["revenge_betrayal_or_payback"],
    "정체": ["secret_identity_or_hidden_truth"], "비밀": ["secret_identity_or_hidden_truth"],
    "보스": ["boss_employee_or_power_romance"], "사장": ["boss_employee_or_power_romance"],
    "회장": ["boss_employee_or_power_romance"], "상사": ["boss_employee_or_power_romance"],
    "앙숙": ["enemies_to_lovers"], "원수": ["enemies_to_lovers"],
    "재회": ["second_chance_or_regret"], "첫사랑": ["second_chance_or_regret"],
    "후회": ["second_chance_or_regret"],
    "집착": ["obsessive_devotion"], "독점": ["obsessive_devotion"],
    "구원": ["danger_rescue_romance", "protective_male_or_partner"],
    "금지": ["forbidden_love"], "불륜": ["forbidden_love"],
    "결혼": ["marriage_contract_or_family_pressure"], "이혼": ["marriage_contract_or_family_pressure"],
    "이별": ["breakup_sacrifice_or_noble_idiot"], "희생": ["breakup_sacrifice_or_noble_idiot"],
    "오해": ["misunderstanding_to_reconciliation"],
    "힐링": ["healing_or_comfort"], "위로": ["healing_or_comfort"],
    # setting
    "학교": ["school_campus"], "캠퍼스": ["school_campus"],
    "회사": ["office_workplace"], "오피스": ["office_workplace"], "직장": ["office_workplace"],
    "재벌": ["chaebol_highsociety"], "상류": ["chaebol_highsociety"],
    "병원": ["medical"], "의사": ["medical"],
    "사극": ["historical_palace"], "궁": ["historical_palace"],
    "아이돌": ["entertainment_idol"], "연예": ["entertainment_idol"],
    "판타지": ["fantasy_supernatural"], "늑대인간": ["fantasy_supernatural"],
    "회귀": ["fantasy_supernatural"], "빙의": ["fantasy_supernatural"],
    # story_type
    "질투": ["jealousy_rival_drama", "jealousy_possession_or_rival"],
    "폭로": ["secret_reveal_betrayal_drama"],
    "말싸움": ["dialogue_conflict_driven"], "밀당": ["dialogue_conflict_driven"],
    # male_lead
    "츤데레": ["cold_to_warm"], "다정": ["devoted_straightforward"],
    "직진": ["devoted_straightforward"], "보호": ["protective_rescuer"],
    "위험한": ["dangerous_forbidden"], "마피아": ["dangerous_forbidden"],
}


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
        *t.get("male_lead", []),
        t.get("hook_type", ""), t.get("story_type", ""), t.get("setting", ""),
    } - {""}


def select_examples(query: str, db: list[dict], k: int = 3) -> list[dict]:
    """태그 겹침 점수 → 동점이면 ER 순. 정제 대본(script) 있는 편 우선."""
    want = extract_tags(query)

    def score(e: dict) -> tuple:
        overlap = len(want & _entry_tags(e)) if want else 0
        has_script = 1 if e.get("script") else 0
        er = e["metrics"].get("er") or 0
        return (overlap, has_script, er)

    ranked = sorted(db, key=score, reverse=True)
    return ranked[:k]


def format_example(e: dict) -> str:
    lines = [
        f"### 사례 {e['id']} (ER {e['metrics'].get('er')}, {e['metrics'].get('dur')}초)",
        f"- 훅(첫 3초): {e.get('hook_desc') or '(불명)'}",
        f"- story_type: {e['tags'].get('story_type') or '-'} / hook_type: {e['tags'].get('hook_type') or '-'}",
        f"- 트로프: {', '.join(e['tags'].get('trope_tags') or []) or '-'}",
        f"- 남주: {', '.join(e['tags'].get('male_lead') or []) or '-'} / 배경: {e['tags'].get('setting') or '-'}",
    ]
    if e.get("script"):
        lines.append("- 정제 대본:")
        lines += [f"  {s['speaker']}: {s['line']}" for s in e["script"]]
    return "\n".join(lines)
