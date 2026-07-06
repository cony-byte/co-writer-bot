# -*- coding: utf-8 -*-
"""한글 키워드 → 태그 공유 테이블 (retrieval·trend_search 공용 SSOT).

두 소비자가 값체계가 다르다:
- retrieval(사례 선별): 통합 DB의 **영어 머신키**(trope_tags·hook_type…)와 catharsis_type(영어)로 매칭 → ALIASES
- trend_search(집계): **한글** trope_tags_ko + catharsis_type로 집계 → FILTER_ALIASES

한 파일에서 관리해 키워드·버그 수정을 한 곳에서. (구 retrieval.ALIASES + trend.FILTER_ALIASES 통합)
"""

# ── retrieval용: 한글 키워드 → canonical 태그(영어/enum) 리스트 ──────────────
# 통합 DB의 tags(영어 trope_tags·hook_type·story_type·setting·male_lead·catharsis_type 영어값)와 매칭.
ALIASES: dict[str, list[str]] = {
    # trope (영어 머신키)
    "삼각관계": ["love_triangle_or_rival"], "연적": ["love_triangle_or_rival"],
    "계약": ["contract_or_fake_relationship"], "위장": ["contract_or_fake_relationship"],
    "신데렐라": ["class_gap_cinderella"], "신분": ["class_gap_cinderella"],
    "복수": ["revenge_betrayal_or_payback"], "배신": ["revenge_betrayal_or_payback"],
    "정체": ["secret_identity_or_hidden_truth"], "비밀": ["secret_identity_or_hidden_truth"],
    "보스": ["boss_employee_or_power_romance"], "사장": ["boss_employee_or_power_romance"],
    "회장": ["boss_employee_or_power_romance"], "상사": ["boss_employee_or_power_romance"],
    "앙숙": ["enemies_to_lovers"], "원수": ["enemies_to_lovers"],
    "재회": ["second_chance_or_regret"], "첫사랑": ["second_chance_or_regret"],
    "후회": ["second_chance_or_regret", "regret_grovel"],
    "집착": ["obsessive_devotion", "devotion_thrill"], "독점": ["obsessive_devotion"],
    "구원": ["danger_rescue_romance", "protective_male_or_partner", "salvation"],
    "금지": ["forbidden_love"], "불륜": ["forbidden_love"],
    "결혼": ["marriage_contract_or_family_pressure"], "이혼": ["marriage_contract_or_family_pressure"],
    "정략결혼": ["marriage_contract_or_family_pressure"], "선결혼": ["marriage_contract_or_family_pressure"],
    "이별": ["breakup_sacrifice_or_noble_idiot"], "희생": ["breakup_sacrifice_or_noble_idiot"],
    "오해": ["misunderstanding_to_reconciliation"],
    "힐링": ["healing_or_comfort"], "위로": ["healing_or_comfort"], "치유": ["healing_or_comfort", "salvation"],
    # setting
    "학교": ["school_campus"], "캠퍼스": ["school_campus"],
    "회사": ["office_workplace"], "오피스": ["office_workplace"], "직장": ["office_workplace"], "사내": ["office_workplace"],
    "재벌": ["chaebol_highsociety"], "상류": ["chaebol_highsociety"],
    "병원": ["medical"], "의사": ["medical"],
    "사극": ["historical_palace"], "궁": ["historical_palace"],
    "아이돌": ["entertainment_idol"], "연예": ["entertainment_idol"],
    "판타지": ["fantasy_supernatural"], "늑대인간": ["fantasy_supernatural"],
    "회귀": ["fantasy_supernatural"], "빙의": ["fantasy_supernatural"],
    # story_type / catharsis (영어)
    "질투": ["jealousy_rival_drama", "jealousy_possession_or_rival"],
    "폭로": ["secret_reveal_betrayal_drama"],
    "말싸움": ["dialogue_conflict_driven"], "밀당": ["dialogue_conflict_driven"],
    "코믹": ["humor_flutter"], "로코": ["humor_flutter"],
    "금단": ["forbidden_tension"],
    # male_lead
    "츤데레": ["cold_to_warm"], "다정": ["devoted_straightforward"],
    "직진": ["devoted_straightforward"], "보호": ["protective_rescuer"],
    "위험한": ["dangerous_forbidden"], "마피아": ["dangerous_forbidden"],
}

# ── trend_search용: 한글 키워드 → (kind, value) ─────────────────────────────
# kind="catharsis" → catharsis_type(영어값)에 매칭 / kind="trope" → trope_tags_ko(한글값)에 매칭.
FILTER_ALIASES: dict[str, tuple[str, str]] = {
    "후회남": ("catharsis", "regret_grovel"), "후회": ("catharsis", "regret_grovel"),
    "복수": ("catharsis", "revenge_payback"),
    "신분상승": ("catharsis", "status_reversal"),
    "집착": ("catharsis", "devotion_thrill"), "집착남": ("catharsis", "devotion_thrill"),
    "구원": ("catharsis", "salvation"), "치유": ("catharsis", "salvation"),
    "금단": ("catharsis", "forbidden_tension"),
    "로코": ("catharsis", "humor_flutter"), "코믹": ("catharsis", "humor_flutter"),
    # trope (한글 trope_tags_ko 값)
    "신데렐라": ("trope", "신데렐라(신분격차)"),
    "오피스": ("trope", "오피스로맨스"), "사내": ("trope", "오피스로맨스"),
    "정략결혼": ("trope", "선결혼후연애"),  # 버그 수정: 실제 vocab은 '선결혼후연애' (구 '정략결혼'은 미존재)
    "선결혼": ("trope", "선결혼후연애"),
    "계약": ("trope", "계약연애"),
    "혐관": ("trope", "혐관"), "삼각": ("trope", "삼각관계"),
    "재회": ("trope", "재회물"), "판타지": ("trope", "능력·판타지"),
    "늑대인간": ("trope", "능력·판타지"), "임신": ("trope", "임신·출산"),
    "회귀": ("trope", "회귀·환생"), "마피아": ("trope", "신분숨김(히든재벌·정체은닉)"),
    "배신": ("trope", "배신"), "복수극": ("trope", "복수극"),
}
