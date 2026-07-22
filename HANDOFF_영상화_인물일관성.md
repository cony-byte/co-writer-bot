# 핸드오프 — 영상화 인물 일관성(새 인물 생성 방지)

증상: 영상화 시 스틸컷과 다른 **새 인물**이 튀어나오거나, 콘티에 없는 사람이 프레임에
추가됨. 이 문서는 영상화가 (1)스틸컷 (2)상세 콘티 (3)참조 이미지를 어떻게 참고하는지와,
드리프트를 줄이는 레버를 정리한다(2026-07-22 기준).

핵심 코드: `bot/dispatch_storyboard.py::_generate_video_for_cut`
(모션 프롬프트 조립 ~L3057~3200, 실제 생성 호출 ~L3225).

---

## 1. 영상은 무엇을 참조하나 (중요)

```
seed_png = cut["png"]                       # ← 그 컷의 '스틸컷' 자체가 첫 프레임 시드
url, cost = hf_video.generate(seed_png, motion_prompt, duration=cut_seconds, generate_audio=want_audio)
```

- **이미지 참조 = 스틸컷 1장(seed_png)뿐**. 인물/의상/장소 등록 참조를 영상 API에 따로 넘기지
  **않는다** — 스틸컷이 이미 그 참조들로 만들어졌으니, 스틸컷의 정체성만 유지하면 된다는 설계.
- **콘티는 텍스트로만** 들어간다: `cut['prompt']`(영어 컷 지시), `cut['caption']`(그 컷 요약),
  `scene_text`(그 씬 콘티 원문 최대 1200자, 대사·지문 포함).
- 모델: seedance-2.0 (`config.OPENROUTER_VIDEO_MODEL`). ★강도 조절 파라미터
  (strength/cfg 등) **없음** — 텍스트 프롬프트 구조로만 정체성을 잡아야 한다.

## 2. 모션 프롬프트 조립 순서 (정체성 락이 앞)

`motion_prompt = grid_anchor + fiction_lock + ref_lock + camera_lock + style_lock + tags +
cut['prompt'] + "Scene action: " + caption + dialogue_lock + "Full scene script: " + scene_text + …`

- **ref_lock**(정체성 핵심): "제공된 참조 이미지가 이 영상의 정확한 첫 프레임 — 얼굴/머리색/
  머리스타일/의상/배경을 바꾸지 마라. 아래 서술된 모션만 애니메이트하고, 변한다고 명시 안 한
  모든 시각 요소는 참조와 동일하게 유지." → **프롬프트 앞쪽**에 둬서 이미지 참조 가중치를 높임.
- **fiction_lock**: 가상 인물·실존인물 아님(안전필터 완화, 맨 앞).
- **camera_lock**: 카메라 고정(명시적 이동 지시 없으면 push-in/zoom 금지).
- **style_lock**: 화풍 프리셋(`_style_for_work`) + `_VIDEO_STYLE_LOCK_EMPHASIS`(스타일별 단정문).
  카메라락 바로 뒤(컷 내용보다 앞)로 옮겨 화풍이 안 밀리게.
- **dialogue_lock**(★2026-07-22): 대사 없는 컷은 "입 다물고 발화 입모양 금지".

## 3. 왜 '새 인물'이 생기나 — 원인과 레버

1. **scene_text가 그 씬 '전체'를 담음** → 그 씬 다른 컷에 나오는 인물/행동까지 텍스트에 있어,
   모델이 이 컷에 없는 사람을 프레임에 추가할 수 있음. 프롬프트에 "animate only this cut's
   action, not other cuts"가 있지만 인물 목록 자체는 노출됨.
   - **레버(권장)**: scene_text 대신 **그 컷의 비트 텍스트만** 넣거나, "등장 인물은 참조
     이미지에 있는 사람뿐 — 새 인물 추가 금지"를 명시. (`shot['characters']`로 이 컷 등장
     인물을 한정할 수 있음.)
2. **ref_lock이 있어도 seedance가 텍스트로 끌려감** → cut['prompt']/caption이 인물을 새로
     묘사하면 시드 얼굴을 무시하고 다시 그림.
   - **레버**: "Do NOT introduce, add, or invent any new person/face not present in the
     reference first frame. The people in the video are EXACTLY those in the reference image,
     same count and identity." 같은 강한 금지문을 ref_lock에 추가.
3. **스틸컷 자체 화질/정체성이 약하면** 영상이 더 쉽게 드리프트 → 스틸 단계 참조가 부실하면
     여기서도 티가 남(스틸 프롬프트 핸드오프 참고).
4. **다인물 프레임**: 두 명 이상일 때 한 명이 다른 인물로 바뀌거나 섞임 → "keep the exact
     number of people and each person's identity from the reference; do not merge or swap
     faces" 명시로 완화.

## 4. 참조가 실제로 잘 붙는지 점검 포인트

- `seed_png = cut["png"]`가 **그 컷의 최신 스틸**인지(재생성/격자 덮기 후 `cut["png"]` 갱신
  경로 확인). face_grid 격자 케이스는 격자 스틸이 시드가 되고 앞 0.1초 트림.
- `_generate_video_for_cut`가 받는 `cut`이 `vp_store.load_latest_cuts`에서 온 최신 png인지.
- scene_text/caption이 **다른 씬** 내용으로 오염되지 않았는지(콘티 씬 경계 파싱).

## 5. 자주 건드리는 것

| 대상 | 위치 |
|---|---|
| 모션 프롬프트 조립 | `_generate_video_for_cut` (~L3057~3200) |
| ref_lock / camera_lock / fiction_lock | 같은 함수 내 (~L3131~3155) |
| dialogue_lock(무대사 입잠금) | 같은 함수 (~L3173 직전) |
| style_lock emphasis | `_VIDEO_STYLE_LOCK_EMPHASIS` (~L2671) |
| 실제 생성 호출 | `hf_video.generate(seed_png, motion_prompt, …)` (~L3225) |
| 실패 사유 분류 | `_classify_video_fail_reason` (~L1207) |

## 6. 개선 우선순위 제안(새 인물 방지)

1. ref_lock에 **"새 인물 추가·얼굴 교체 금지 + 참조와 동일 인원/정체성"** 강한 금지문 추가.
2. scene_text를 **그 컷 비트로 축소**하거나, 이 컷 등장 인물을 `shot['characters']`로 한정
   명시("only these people appear: … , all matching the reference image").
3. (선택) 인물 정체성이 특히 중요한 컷은 스틸 참조를 영상 API refs로도 함께 전달하는 방안
   검토(현재는 seed_png 1장만) — seedance ref 지원 범위 확인 필요.

> 관련 핸드오프: `HANDOFF_실사화스틸컷프롬프트.md`(스틸 참조/순서), `HANDOFF_안전필터우회.md`.
