# 패턴 요약 레이어 — 사용법과 공통 패턴

> 근거: reference_db.json v3.0 — drama_clip 76편 (그중 정제 대본 38편, 2026-07-01 크롤링).
> **생성 파이프라인 프롬프트에는 원본 76편이 아니라 이 요약 + 유사 사례 2~3편을 주입한다.**
> 사례 검색: reference_db.json에서 id로 조회 → `script`(정제 대본)·`hook_desc` 사용.

## story_type별 문서

| 문서 | 표본 | ER 중앙값 |
|---|---|---|
| [emotion_reaction_driven.md](emotion_reaction_driven.md) | 12편 | 0.47 |
| [power_status_romance.md](power_status_romance.md) | 8편 | 0.42 |
| [dialogue_conflict_driven.md](dialogue_conflict_driven.md) | 8편 | 0.32 |
| [secret_reveal_betrayal.md](secret_reveal_betrayal.md) | 7편 | 0.38 |
| [minor_types.md](minor_types.md) — jealousy/marriage/danger/fast_cut | 8편 | — |

미분류 33편(transcript 없음, 태그 저신뢰)은 패턴 근거에서 제외 — 단 트로프 분포(hidden_truth 7, enemies 5, second_chance 5)는 시장 신호로 참고.

## 공통 훅 패턴 (첫 3초)

빈도순 (대사 표본 38편):
1. **대면 추궁형** (emotional_question 12편, 32%) — 여주(또는 남주)가 관계·행동을 정면으로 따지는 한마디로 오픈. "뭐 하는 거야?" / "우리 사이에 뭔가 있다고 생각했어" / "너 훔쳐보고 있었잖아". **질문이 곧 관계의 실체를 건드림** — 답을 듣기 위해 계속 본다.
2. **고백/호감형** (confession_or_desire 5편) — 직진 호감 표현으로 오픈. "이상할 만큼 너무 예뻐요" / "이 춤을 함께해도 될까요?"
3. **현상 파괴 선언형** (status_quo_break 3편) — 균형을 깨는 정보 투척. "다 봤어, 카메라에 다 찍혔어" / "헤일리를 찾았습니다".
4. **권력 명령형** (power/threat 계열) — 지배적 명령 한마디. "펜 내려놔." / "말할 때 나를 봐." 초단편(13~15초) 마이크로드라마에서 특히 효과적.
5. **정체 단서형** (identity_reveal 2편) — 사소한 디테일로 정체 암시. "타코, 양파 빼고요"(남의 식성을 아는 여자).

## 절단점(클립 종료) 유형론 — 전 유형 공통

관측된 절단 패턴, 강한 순:
1. **킬러 라인 정점 컷** — 가장 흔하고 강력. 위트/선언의 정점에서 즉시 컷. "위험할 정도로." / "우리는 못 해." / "그럼 내가 떠날게."
2. **무응답 질문 컷** — 정체·진심을 묻는 질문에 답하지 않고 컷. "너 도대체 누구야?" / "맞아, 아니야?"
3. **말 끊기(mid-sentence) 컷** — 문장이 잘리며 끝. "왜냐하면 넌 한 번도…"
4. **비밀/위험 예고 컷** — 다음 화 떡밥 투척 직후. "레오는 내 아들이 아니야" / "모두 널 원할 거야" / 폭로 직전 "그녀는… / 안 돼."
5. **선언·축출 컷** — 관계 단절 선언으로 종결감+후폭풍 기대. "그냥 나가." / "난 아직 그 여자와 함께 있어."

## 전 표본 트로프 상위 (76편)

secret_identity_or_hidden_truth(15) > boss_employee_or_power_romance(10) > second_chance_or_regret(9) > love_triangle_or_rival(8) > enemies_to_lovers(7) = forbidden_love(7) > revenge_betrayal_or_payback(6) > obsessive_devotion(5). 남주 축은 powerful_status·dominant_possessive·dangerous_forbidden이 지배적 — 시장이 '권력+위험+집착' 남주에 몰려 있음.

## 주의

- ER 최상위권(2.28, 1.40, 1.07)은 전부 **transcript 없는 편** — 비주얼/편집 훅 영향 가능성. 대사 패턴만으로 ER을 설명하지 말 것.
- 규칙 수치화(가중치)는 story-v1-system engine.py 소관 — 이 문서는 질적 패턴 요약.
