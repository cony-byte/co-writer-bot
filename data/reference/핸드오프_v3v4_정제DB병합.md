# 핸드오프 — v3·v4 정제 DB 병합 (실행 지시서)

> 이 문서 하나로 작업을 완결할 수 있게 쓰였다. 새 에이전트 세션은 이 파일을 먼저 읽고, 아래 "실행 순서"대로 진행하라.
> 대상 repo 2개: **story-v1-scripts**(정제 DB 홈·주 무대) + **cony-byte/co-writer-bot**(소비자). 로컬은 `~/Desktop/Github/` 아래 형제로 클론돼 있다고 가정.
> 작성: 2026-07-06 세션 (조사·설계 완료, 미실행). 상위 맥락은 crawler `HANDOFF.md` §6 참조.

---

## 0. 목표 (왜)

정제 레퍼런스 DB가 **v3·v4로 갈라져** 있고, 그중 v4가 고아다. 봇이 한 안에서 두 스키마를 쓴다(사례 선별=v3, 트렌드=v4). **레코드당 하나로 병합**해 단일 정제 DB(SSOT)로 만들고, 로우데이터에서 재현 가능한 태깅 프롬프트를 확립해 고아를 없앤다.

---

## 1. 현황 (조사로 확인된 사실 — 신뢰하고 시작해도 됨)

**두 파일, 같은 76편(drama_clip), 같은 id:**

| | v3 (base·자산 SSOT) | v4 (태그 소스) |
|---|---|---|
| 경로 | `story-v1-scripts/reference/reference_db.json` | `co-writer-bot/data/reference_db_v4.json` |
| tag_version | 76편 전부 `v3.0` | 76편 중 **38편만 `v4.0`**, 나머지 38편은 빈 placeholder |
| tags 축 | hook_type·story_type·dialogue_tags·trope_tags(**영어**: `love_triangle_or_rival`)·male_lead·setting·hook_modality·visual_hook·narration_form | catharsis_type·hook_beat·cliffhanger_type·character_setup·trope_candidates·trope_tags(**한글**: `집착남`,`능력·판타지`)·male_lead·setting·hook_modality·narration_form |
| 정제자산 | `script`(화자중립 화자1/화자2/나레이션 + `role_hint`=ML/FL/SUP/NAR/UNK)·`scenes`(컷 타임라인)·`publish_dt`·`hook_desc`·`transcript_raw`·`metrics` **다 있음** | **없음** (script/scenes/publish_dt 없음) |
| 확인된 수치 | publish_dt 76/76, script 38/76, scenes 45/76 | v4.0 38편, 그중 tag_confidence≥0.6은 **21편** |

**소비자:**
- co-writer-bot `bot/retrieval.py` — v3 태그로 사례 선별. `extract_tags/_entry_tags/select_examples/format_example` + 한글→영어태그 `ALIASES` dict.
- co-writer-bot `bot/trend_search.py` — v4 태그로 트렌드 집계. `tag_confidence>=0.6` 게이트(`MIN_CONF`). 한글→한글태그 `FILTER_ALIASES` dict.
- co-writer-bot `bot/reference.py`(`load_db`=v3, `load_trend`=v4), `bot/config.py`(`REFERENCE_DB_V4`), `scripts/sync_reference.py`(scripts `reference/`→co-writer-bot `data/reference/` 통째 복사).
- story-v1-scripts `enrich.py`(publish_dt·화자중립·scenes), `build_viewer.py`+`filters.json`(내부 뷰어), `patterns/*.md`. **전부 v3축 사용, 76편 다 채워짐.**

**주의로 확인된 것:**
- `build.py`(공개뷰어 CSV→library.json)는 reference DB를 **안 건드림 → 범위 밖**.
- v4 파일의 38개 v3.0 레코드는 **빈 placeholder**(실제 v3 태그 아님) — 이 파일에서 쓸 건 v4.0 38편의 v4축뿐.
- **버그**: `trend_search.FILTER_ALIASES`에 `정략결혼`이 있는데 실제 v4 vocab엔 없음(실제값 **`선결혼후연애`**). 병합 시 수정.
- v4 태그는 로우데이터 CSV에 **없다**(catharsis_type 등은 crawler 스키마 v2.1에 미존재) — LLM 정제 산출물.

---

## 2. 병합 설계 (확정된 결정 — 이대로 구현)

### 2-1. 스키마: superset (v3축 버리지 않음)
한 레코드 `tags{}`에 v3·v4 축을 **모두** 둔다. 뷰어·patterns·retrieval이 v3축을 쓰고 76편 다 채워져 있으므로 유지. v4축은 생성 신호(38편).

| 축 | 출처 | 비고 |
|---|---|---|
| hook_type, story_type, dialogue_tags, visual_hook | v3 | 유지 |
| trope_tags (영어 enum) | v3 | **canonical 머신키** (76편) |
| trope_tags_ko (한글) | v4 (+영어에서 backfill) | 표시·트렌드 집계 |
| male_lead, setting, hook_modality, narration_form | v3∪v4 | 공통 |
| catharsis_type, hook_beat, cliffhanger_type, character_setup, trope_candidates | v4 | 생성 신호 (38편) |

프로비넌스: `tag_version="v5.0"`, 기존 `tag_confidence` 유지 + **`v4_tagged`(bool)·`v4_tag_confidence`(float\|null) 신설** (트렌드는 이걸로 게이트 — v3 정제신뢰도와 v4 생성신뢰도는 다른 축). 정제자산(script/scenes/publish_dt/hook_desc/transcript_raw/metrics)은 v3 그대로.

### 2-2. 단일 DB 위치
**`story-v1-scripts/reference/reference_db.json` 제자리 업그레이드.** enrich.py·뷰어·프롬프트 SSOT가 여기 있음. co-writer-bot은 `sync_reference.py`로 받고, **`data/reference_db_v4.json`은 삭제.**

### 2-3. 미태깅 38편
재태깅으로 병합을 막지 말 것. v4축 없는 38편: `v4_tagged=false`·`v4_tag_confidence=null`·v4축 빈값·`needs_review=true`·`tag_notes`에 "v4축 미태깅 — 재태깅 대상". 트렌드는 게이트로 자동 제외(현행과 동일). **재태깅(그리고 0.6 미만 17편 재점수)은 별도 후속 배치** — v5 프롬프트로 돌려 같은 병합 스크립트로 흘려보냄(idempotent).

### 2-4. trope 값체계 통일
영어(머신키)+`trope_tags_ko`(한글) 둘 다 보존. 두 표현을 잇는 **`trope_map`(영↔한) 표**를 v5 프롬프트에 넣어 향후 배치가 둘 다 산출하게 → 충돌 원천 제거. 병합 스크립트는 v4 한글이 없는 편에 영어→한글 backfill.

---

## 3. 파일별 작업

### story-v1-scripts
- **`reference/merge_v4_tags.py`** (신규) — 일회성·idempotent 병합. v3(base 76편) + v4 파일의 **v4.0 레코드만** id로 join. v4축 복사, `trope_tags_ko`=v4 한글(없으면 trope_map으로 영어에서 파생), `v4_tagged`/`v4_tag_confidence` 세팅, `tag_version="v5.0"`. 재실행 시 태깅편을 미태깅으로 되돌리지 않음. 출력: `reference_db.json` 제자리(`ensure_ascii=False, indent=1` — enrich.py 형식과 동일하게).
- **`reference/태깅프롬프트_v3.md` → `태깅프롬프트_v5.md`** — v3 task1~3(정제·transcript_form·hook_desc) 유지 + task4 사전을 superset으로(v3 축 전부 + catharsis_type/hook_beat/cliffhanger_type/character_setup/trope_candidates 통제어휘). **trope_map 표** 포함해 영어·한글 trope 동시 산출. 출력 JSON에 v4축+trope_tags_ko+`v4_tag_confidence`. `needs_review = tag_confidence<0.7 or hook_desc_confidence<0.6 or v4_tag_confidence<0.6`. v3 파일은 이력 보존.
- **`reference/filters.json`** (선택) — catharsis_type·cliffhanger_type auto-chip 추가, trope 칩을 `trope_tags_ko`로.
- `enrich.py`·`build_viewer.py` — 코드 무변경(둘 다 tags를 통째로 통과시킴). 병합 후 재실행해 자산 안 깨지는지 확인만.

### co-writer-bot
- **`data/reference_db_v4.json`** — **삭제**.
- **`bot/tag_vocab.py`** (신규) — 한글 키워드→태그 공유 테이블. retrieval의 `ALIASES` + trend의 `FILTER_ALIASES`를 여기로 통합. **`정략결혼`→`선결혼후연애` 버그 수정.** catharsis alias(후회→regret_grovel, 복수→revenge_payback, 집착→devotion_thrill, 구원/치유→salvation, 금단→forbidden_tension, 코믹/로코→humor_flutter, 신분상승→status_reversal) 포함.
- **`bot/retrieval.py`** — `ALIASES`를 tag_vocab에서 import + catharsis alias 추가. `_entry_tags()`에 catharsis_type/hook_beat/cliffhanger_type 추가(미태깅편 빈값은 자동 무기여). `select_examples()` 점수에 `v4_bonus`(생성축 쿼리 & v4_tagged) 추가 → `(overlap, v4_bonus, has_script, er)`. `format_example()`에 생성축(catharsis·hook_beat·cliffhanger·character_setup) 출력 + **script는 role_hint로 ML/FL 표시**(화자중립 대비) + trope는 trope_tags_ko 우선.
- **`bot/trend_search.py`** — 풀 게이트 `tag_version=="v4.0"` → **`v4_tagged and v4_tag_confidence>=MIN_CONF`**. trope 읽기를 `trope_tags_ko`로(`_apply_filter`·`combos`·`top_clips`의 `trope_tags` 참조 교체). `FILTER_ALIASES`는 tag_vocab로 이전. docstring·`__main__`의 `reference_db_v4.json` 참조를 통합 파일명으로.
- **`bot/reference.py`** — `load_trend()`가 `config.REFERENCE_DIR/"reference_db.json"` 가리키게(별도 v4 경로 제거).
- **`bot/config.py`** — `REFERENCE_DB_V4` 제거, `REFERENCE_DIR` 주석에서 "v3" 삭제.
- `scripts/sync_reference.py` — 코드 무변경(디렉터리 통째 복사라 통합 DB·v5 프롬프트 자동 동반). 확인만.
- `bot/prompts.py` — 시그니처 무변경. ROLE 텍스트 "레퍼런스 DB v3" 문구만 v5로.

---

## 4. 실행 순서 (원자적 — 중간상태가 깨지므로 한 번에)
1. `태깅프롬프트_v5.md` 작성 (스키마 진실원).
2. `merge_v4_tags.py` 작성 → 1회 실행 → scripts `reference_db.json` v5.0 통합본 생성.
3. `enrich.py --csv <system 로우데이터 CSV 또는 crawler data/out/*.csv>` 재실행 → 통합본이 정제 후에도 안 깨지는지 확인.
4. scripts 커밋·푸시. (선택) `build_viewer.py`로 뷰어 재빌드 + filters.json 갱신.
5. co-writer-bot: `tag_vocab.py` 신설 → retrieval·trend·reference·config 수정 → `sync_reference.py` 실행(통합 DB 반영) → `reference_db_v4.json` 삭제.
6. co-writer-bot 커밋·푸시.

---

## 5. 검증 (end-to-end)
1. **병합 무결성**(python): 76편, 전부 v5.0, `v4_tagged=true` 38편, publish_dt 76/76·script 38/76·scenes 45/76 유지, 모든 영어 trope_tags에 trope_tags_ko 대응 존재.
2. **뷰어**: `python3 reference/build_viewer.py` → viewer.html 76편 렌더, 칩 populate.
3. **sync**: `python3 scripts/sync_reference.py`(co-writer-bot) → `data/reference/reference_db.json`이 통합본, `data/reference_db_v4.json` 없음.
4. **retrieval**: `select_examples("집착남 오피스 후회남 대본", reference.load_db())` → 3편, catharsis/hook_beat 겹침이 랭킹 반영, format_example이 생성축 출력·미태깅편 graceful.
5. **trend**: `TrendSearch(통합DB).answer(...)` 데모 → 풀 ~21편, 한글 trope 집계, `정략결혼`→`선결혼후연애` 수정 반영.
6. **봇 스모크**: `app.py`로 "기획안"·"대본"·트렌드 각 1건 → 단일 DB 응답, `reference_db_v4.json` 참조 없음.

---

## 6. 함정 (놓치면 깨짐)
- **부모 무시 gitignore**와 무관하지만, 병합 스크립트 출력 형식은 `indent=1, ensure_ascii=False`로 — 안 그러면 enrich.py 재실행 시 diff 폭발.
- `format_example`의 script는 **화자중립(화자1/나레이션)** 이라 그대로 쓰면 ML/FL이 안 보임 → `role_hint`로 남주/여주 표시할 것.
- trend 게이트를 `tag_version` 대신 `v4_tagged`로 바꾸는 걸 잊으면, 병합 후 전부 v5.0이라 v4 미태깅편까지 트렌드 풀에 들어와 오염.
- co-writer-bot은 sync 사본을 읽으므로, scripts 통합본을 **먼저** 커밋/생성한 뒤 sync해야 함.
