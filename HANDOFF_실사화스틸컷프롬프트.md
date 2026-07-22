# 핸드오프 — 실사화(realistic) 스틸컷 생성 프롬프트 구조

co-writer-bot에서 실사화 장르 스틸컷을 만들 때 **최종 이미지 프롬프트가 어떻게 조립되고,
참조 이미지가 어떤 순서로 붙는지**(2026-07-22 기준) 정리. 다른 세션이 이어받을 때 참고.

핵심 코드: `bot/dispatch_storyboard.py::_render_cuts` (컷별 생성 루프),
`bot/openrouter_image.py::shot_ref_entries` / `reference_priority_block`.

---

## 1. 진입 흐름

`_do_stills` → 노션 콘티 fetch(`_thread_or_saved_conti(prefer_notion=True)`) →
`_render_cuts_tracked` → `_render_cuts`. 콘티 → 샷 분해(LLM, `storyboard_shots_*`)로 컷 리스트
`shots`(각 컷: `caption/prompt/characters/places/props/scene_text/n`) 생성 후 컷마다 생성.

- 스틸컷 고정값: `aspect_ratio = STILL_ASPECT = "9:16"`(세로), `no_text=True`(그리드 캡션바만
  끄는 옵션이 아니라, 화면 텍스트 금지는 아래 style suffix가 담당).

## 2. 화풍 지시 (style_suffix) — 프롬프트 '맨 앞'

`style_suffix = _style_for_work(work)` = `STYLE_PRESETS[key]` (+작품 art_note).
- 실사화 키 = `"realistic"`(기본). 프리셋 구성:
  - realistic 렌더 서술("~80% realism, clean photographic cinematic look, NOT illustration/
    anime …")
  - `_IDEALIZED_FACE_GUIDANCE`(이상화된 얼굴·가상인물·실존인물 아님)
  - `_STYLE_COMMON_SUFFIX`(화풍 무관 공통): 참조 의상·헤어 그대로 / **화면 텍스트 금지** /
    남녀 동프레임 시 남성이 자연스럽게 더 큼(로맨스 K-드라마) / **한국 드라마 정체성**(로고·
    간판 한글).
- ★맨 앞에 두는 이유: 촬영장/카메라 장비를 묘사하는 컷에서 화풍 지시가 뒤에 있으면 장면
  내용에 밀려 사진처럼 안 나오던 사고 → style_suffix를 컷 내용보다 앞으로.

## 3. 참조 이미지 순서 (refs 리스트)

`ref_entries = oi.shot_ref_entries(work, shot)` 로 시작. 각 엔트리 = `(role, url, gender, name)`
(role ∈ person/costume/place/prop). 여기에 순서대로 덧붙인다:

1. **shot_ref_entries**(등록 요소: 인물 얼굴 / 의상 / 장소 / 소품)
2. **mood 참조**(있으면) — 씬당 1회 생성한 무드/조명 이미지. **반드시 맨 뒤**(앞에 두면
   우세해져 다른 참조 역할 침범).
3. **prev_png**(같은 씬 그룹 내 직전 컷 이미지) — 이어지는 느낌용 체이닝.
4. **ref_data_url**(노션 "구도 그대로" 참조, 있을 때만) — **맨 마지막**.

즉 refs 순서 = `[요소 참조들…, mood, prev_png, 구도참조]`. (생성기가 앞쪽 참조를
우세하게 반영하는 경향이 있어, 외형 참조를 앞에 두고 보조/구도 참조를 뒤로.)

## 4. 프롬프트 텍스트 조립 순서 (한 컷)

`_render_cuts` 루프에서(대략 L1561~ ):

```
prompt = f"{style_suffix}, {s['prompt']}"                 # ① 화풍(앞) + 컷 프롬프트
if ref_data_url:                                          # ② 구도참조 있으면 카메라/구도 고정 지시
    prompt += "CRITICAL: Match the exact camera angle, composition, framing, blocking of the attached storyboard reference … only use other refs for appearance."
role_block = oi.reference_priority_block(ref_entries)     # ③ 참조 역할 분리 선언
if role_block: prompt += role_block
if _is_2d_style: prompt += "Keep the art style, line work, coloring consistent with the attached reference image."  # (2D 웹툰 작품만)
if feedback: prompt += "[★ HIGHEST-PRIORITY USER INSTRUCTION — 콘티와 충돌하면 이 지시 우선 …]"  # ④ 재생성/인라인 지시(맨 끝, 최우선)
png, cost = img.generate(prompt, aspect_ratio="9:16", refs=refs)
```

### ③ reference_priority_block (핵심 — 의상 오염 방지)
`openrouter_image.py::reference_priority_block`. 각 참조가 몇 번째·무슨 역할인지 명시하고
그 역할 밖 정보는 무시하라 선언. 추가로:
- person 참조엔 등록 성별 명시("This character is male …") — 성별 뒤바뀜 방지 이중장치.
- 인물/의상 참조가 **2개 이상**이면 **STRICT WARDROBE SEPARATION** 블록: "각 인물은 자기
  의상 참조만 착용, 서로 옷 바꾸지/섞지 마라. OTS 전경 어깨에도 다른 인물 옷 금지." —
  인물↔의상을 강하게 '묶어서'(빼는 게 아니라) 해결. `(belongs to '이름')`로 소유 명시.

### ④ feedback (최우선 오버라이드)
재생성 피드백 또는 최초 생성 시 곁들인 지시(`_extract_inline_instruction`)가 있으면, 콘티보다
우선하는 지시로 **맨 끝**에 붙는다(참조 이미지 외형은 유지). 없으면 콘티 100%.

## 5. 실패/안전필터

- 세이프티 필터 거부는 자동 재시도 안 함(순화 안내). 단 영상화의 "실존인물" 필터는 face_grid
  자동 격자 재시도(별도 핸드오프 `HANDOFF_안전필터우회.md` 참고).
- 업스트림 일시 오류(502 등)는 같은 프롬프트로 1회 자동 재시도.

## 6. 자주 건드리는 상수/함수

| 대상 | 위치 |
|---|---|
| 컷 생성 루프 | `dispatch_storyboard.py::_render_cuts` (~L1253~1615) |
| 화풍 프리셋 | `STYLE_PRESETS` / `_style_for_work` (~L2591~2637) |
| 공통 접미사 | `_STYLE_COMMON_SUFFIX` (~L2548) |
| 이상화 얼굴 | `_IDEALIZED_FACE_GUIDANCE` (~L1926) |
| 참조 엔트리 | `openrouter_image.py::shot_ref_entries` (L992) |
| 참조 역할/의상분리 | `openrouter_image.py::reference_priority_block` (L1038) |
| 스틸 비율 | `STILL_ASPECT="9:16"` (L2554) |

## 7. 함정

- style_suffix는 **컷 내용보다 앞**에 둬야 화풍이 안 밀린다(뒤로 옮기지 말 것).
- mood 참조는 **맨 뒤**(앞에 두면 다른 참조 침범).
- 의상 오염은 참조를 '빼서'가 아니라 STRICT WARDROBE로 '묶어서' 해결(사용자 지침, 되돌리지 말 것).
- 화면 텍스트 금지는 `_STYLE_COMMON_SUFFIX`가 담당(`no_text`는 그리드 캡션바 옵션일 뿐).
