# reference/ — 생성 파이프라인용 레퍼런스 DB (스키마 v3)

> `data/library.json`(뷰어용)과 **별도**의 사이드카. build.py 리빌드가 library.json 레코드를
> CSV 필드셋으로 통째로 교체하므로 정제 필드를 library.json에 넣으면 유실된다 — 여기가 정제 레이어의 SSOT.

## 파일

| 파일 | 내용 |
|---|---|
| `reference_db.json` | **drama_clip 76편만.** 정제 대본 + v3 재태깅 + hook_desc |
| `excluded.json` | 비본편 24편 (trailer_recap 15 / fan_edit 4 / bts 3 / movie_clip 1 / other 1) — 태그 통계 오염 방지용 격리. 정제 안 함 |
| `태깅프롬프트_v3.md` | transcript 정제·재태깅·hook_desc 프롬프트 SSOT (다음 배치 재실행용) |
| `patterns/` | story_type별 패턴 요약 — **생성 파이프라인 프롬프트에 들어가는 건 원본 76편이 아니라 이 요약 + 유사 사례 2~3편** |

## 스키마 v3 — v2.1 대비 변경

1. **general_* 도피 카테고리 폐지** (hook_type의 `general_hook`, story_type의 `general_romance_drama` 등).
   분류가 안 되면 해당 축을 빈 값(`""`)으로 두고 `tag_confidence`를 낮추고 `tag_notes`에 사유를 적는다.
2. **`script` 신설 — 정제 대본.** STT 원문(`transcript_raw`)은 보존하고, LLM이
   (a) STT 오류 정리 (b) 화자 추정 분리 (c) 대사/나레이션 구분을 한 번에 처리한 결과.
   ```json
   "script": [{"speaker": "ML|FL|SUP|NAR|UNK", "line": "..."}]
   ```
   ML=남주, FL=여주, SUP=조연, NAR=나레이션/자막 낭독, UNK=화자 추정 불가.
3. **`transcript_form` 신설** — `dialogue`(대사 중심) / `narration_recap`(줄거리 요약 나레이션 — 대사 아님!) /
   `monologue`(내적 독백·심리 보이스오버 — 대사도 요약도 아님) / `mixed` / `none`(transcript 없음).
   파이프라인에서 대사와 줄거리 요약은 쓰임이 다르므로 반드시 구분.
4. **`hook_desc` 재정의** — "transcript 앞 N자"가 아니라 **"첫 3초에 무슨 일이 일어나는가"** 한 문장.
   transcript+desc 기반 LLM 1차 추정이므로 `hook_desc_confidence` < 0.6이면 사람이 영상 확인.
5. 태그 값 사전은 crawler `docs/분류프롬프트_v2.md` §3을 계승 (general_* 제거만 다름). `tag_version: "v3.0"`.
6. `needs_review`: `tag_confidence < 0.7 or hook_desc_confidence < 0.6`.
7. `legacy_tags`: v1/v2 태깅 이력 보존 (재태깅 전후 대조용).

## 갱신 워크플로 (다음 배치)

1. `python3 build.py --csv ...` 로 뷰어 갱신 (기존 흐름 그대로)
2. 신규 drama_clip 편을 `태깅프롬프트_v3.md`로 정제·태깅 → `reference_db.json`에 추가, 비본편은 `excluded.json`
3. `patterns/` 요약 재생성 (표본 분포가 바뀌었을 때)

> 장기적으로 이 정제 단계는 crawler 파이프라인 s5에 흡수되어야 함 (분류프롬프트 v2 → v3 개정).
