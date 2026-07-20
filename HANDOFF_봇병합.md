# HANDOFF — co-writer-bot ↔ storyboard-bot 완전 합체

> 작성: 2026-07-16 · 목적: 다른 세션이 이어받아 진행할 수 있게 지금까지의 조사·결정·진행 상황을 정리.
> 작업 위치: `/Users/cony/dev/my-bot/co-writer-bot-merge` (co-writer-bot repo의 git worktree,
> 브랜치 `merge-storyboard-bot`). 원본 두 봇: `/Users/cony/dev/my-bot/co-writer-bot`(@co-writer),
> `/Users/cony/dev/my-bot/storyboard-bot`(@storyboard) — 이 둘은 그대로 두고 여기서 읽어와 이식한다.

## 0-1. ⚠️ 중요 — 두 원본 봇은 "고정된 스냅샷"이 아니라 움직이는 타깃

(2026-07-16 추가) storyboard-bot은 **다른 세션에서 사용자가 실시간으로 계속 기능 개발 중**임이
확인됨(중단 버튼, 진행률 표시, `_HELP` 재구성, 자연어 인식 확장 등 커밋이 몇 분 간격으로 계속
들어옴 — 세션 시작 시점 6244줄 → 확인 시점 6517줄). co-writer-bot도 마찬가지로 계속 변경될 수
있음. 이 merge 작업(Phase 1~3 포함, 특히 §3-3 함수/상수 충돌 감사와 dispatch 순서 매핑/검증)은
**아래 커밋을 프리즈 기준선으로 삼아** 진행했다:

- `co-writer-bot`: `1992284` (2026-07-16 11:10:22, "merge: 신입 실무자 온보딩 UX 개선")
- `storyboard-bot`: `c474164` (2026-07-16 11:33:21, "Merge: 콘티 조회를 화 번호 기준으로 검증")

**Phase 4(명령어 이식/스텁 제거)를 시작하기 전에 반드시**: 두 원본 repo를 이 커밋들과 다시
`git diff`해서, 그 사이 들어온 신규 기능(특히 storyboard의 중단 버튼·진행률 표시 등)이 지금까지의
분석(dispatch 매핑/추출 결과)에 반영 안 됐다는 걸 감안하고 재검토할 것. 원본 두 봇 자체는 이
merge 작업 동안 건드리지 않으므로(읽기 전용) 원본이 계속 발전해도 안전하지만, **분석 결과물은
이 프리즈 시점 기준**이라는 걸 잊지 말 것.

**(2026-07-16 12:1x 갱신) 사용자 확인: storyboard-bot 실시간 개발 종료.** 최종 프리즈 기준:
- `co-writer-bot`: `1992284` (변동 없음)
- `storyboard-bot`: `9e48fdc` (2026-07-16 12:07:48, "fix: 샷분해 프롬프트가 컷라인... 강화",
  6632줄 — 이전 프리즈 `c474164`/6517줄에서 더 진행됨)

이 커밋을 기준으로 Phase 3 스캐폴딩(순서 시뮬레이터/추출 스크립트) 재실행 + 실제
`co-writer-bot-merge` 반영을 진행함. 아래 §2-1 내용은 이 최종 프리즈 기준으로 갱신된 결과.

**(2026-07-16 14:4x 추가) storyboard-bot 커밋 하나 더 발생 확인: `609db1f`** (2026-07-16
14:37:51, "feat: 콘티변환규칙 v2.8 프레임 안 완전성 원칙 상세콘티 프롬프트에 이식", app.py
줄수는 그대로 6632줄). "실시간 개발 종료" 선언 이후에도 하나 더 들어온 케이스 — 발견 즉시 diff
확인(`app.py`/`bot/openrouter_image.py`/`bot/prompts.py` 3개 파일만 변경) 후 반영 완료:
- `bot/openrouter_image.py`: `shot_ref_entries()`/`reference_priority_block()`가 (role, url)
  2-tuple → (role, url, gender) 3-tuple로 바뀜(실제 프로덕션 사고 2건 수정 — "다른 인물이 같은
  얼굴로 나옴"[caption 텍스트 스캔이 costume 아닌 타입까지 훑던 버그], "인물 성별이 바뀜"[생성
  프롬프트에 성별 텍스트 앵커가 전혀 없어 참조 이미지 하나에만 의존하던 문제]) — `merge`의
  `bot/openrouter_image.py`에 동일 패치 적용 완료, py_compile/import 재확인함.
- `bot/dispatch_storyboard.py`의 두 호출부(`_render_cuts`/`_autopilot_regen_shot_png`, 옛
  `oi.shot_ref_entries` 2-tuple 언패킹)도 3-tuple에 맞게 함께 수정 안 했으면 `ValueError` 런타임
  에러가 났을 것 — 같이 수정함.
- `bot/prompts.py`(=merge의 `bot/sb_prompts.py`, generator.py/prompts.py/video_guide.py 이름
  충돌로 `sb_` 접두어로 분리된 그 파일): 프롬프트 텍스트만 추가(코드 구조 변경 없음) — 동일하게
  반영함.
- **아직 커밋 안 함** — 위 3개 파일 수정은 unstaged 상태. 다음 세션이 이어받으면 diff
  재확인 후 커밋하는 게 안전.
- 이 사례가 보여주는 것: storyboard-bot에 "실시간 개발 종료"를 들었어도, 방심하지 말고 매
  세션/매 phase 시작 전에 `git log`로 새 커밋 있는지 다시 확인할 것.

## 0. 이게 뭔가

두 개의 완전히 분리된 Slack 봇(별도 앱/토큰/프로세스)을 **하나의 봇**으로 완전 합체한다.
- **co-writer-bot**(`@co-writer`): 드라마 작가 보조 — 기획/생성/피드백/트렌드/동기화/별칭/좋아·별로.
  ~240KB `app.py`, Claude Agent SDK(로컬 CLI) 또는 Anthropic API로 생성.
- **storyboard-bot**(`@storyboard`): 영상 제작 파이프라인 — 대본→씬설계→상세콘티→샷분해/스틸컷→
  영상화→합본. ~397KB `app.py`, OpenRouter HTTP로 LLM/이미지/영상/TTS/음악 전부 생성.

co-writer-bot에는 이미 storyboard 명령어 자리가 스텁으로 만들어져 있음(`CMD_STORYBOARD`/
`CMD_STORYBOARD2`/`CMD_STORYBOARD_IMG` 등, `co-writer-bot/app.py:59-80` 부근) — "따로 있는 봇
쓰세요"로만 응답. 이 스텁을 진짜 storyboard 로직으로 교체하는 게 이번 작업의 핵심.

## 1. Phase 0 — 이미 확정된 결정 (프로젝트 owner 승인 완료)

1. **Slack 앱/토큰: `@co-writer` 유지.** storyboard-bot의 토큰은 폐기. 근거: co-writer-bot에
   이미 storyboard 스텁이 있음(호스트가 되도록 이미 설계됨), 이 worktree 자체가 co-writer-bot
   repo 안에 있음, storyboard-bot의 `works.py`가 이미 co-writer의 env를 "원본"으로 취급하는
   코드(`_cowriter_env_pages()`)를 갖고 있음.
2. **`app.py` 구조: 지금 기회로 모듈 분할.** 하나의 거대 파일(합치면 ~640KB, 10,600줄)로
   만들지 않는다. 목표 레이아웃(제안, Phase 3에서 확정):
   ```
   bot/
     dispatch.py            # 공용 진입점: on_message/on_mention, _handle 래퍼, inflight 추적
     dispatch_cowriter.py   # co-writer 전용 명령: 기획/생성/피드백/트렌드/동기화/별칭/좋아·별로
     dispatch_storyboard.py # storyboard 전용 명령: 씬설계/콘티/샷분해/스틸컷/영상화/합본
     shared/
       slack_io.py          # _reply, _post_chunks, _thread_messages, _mrkdwn, _thinking,
                             # _update_note, _clean, _looks_like_mention 등 공용 플러밍
       files.py             # _files_text, _image_files, _decode_text, _hwpx_text, _parse_json_array
       works.py             # 조정 완료본(§3)
       notion_sync.py       # 조정 완료본(§3)
       job_ledger.py        # 조정 완료본(§3)
     # 나머지 기능 모듈은 거의 그대로: prompts.py, generator.py, trend_search.py, sheet_bible.py,
     # verify.py, video_guide.py, tag_vocab.py, retrieval.py, reference.py (co-writer 쪽)
     # edit_plan.py, episode_compile.py, conti_state.py, interrupted_state.py,
     # pending_element_state.py, pending_element_pick_state.py, still_state.py, video_index.py,
     # vp_store.py, pycapcut_client.py, higgsfield_image.py, higgsfield_video.py,
     # openrouter_music.py, openrouter_tts.py, openrouter_video.py (storyboard 쪽, 거의 그대로 이식)
   ```
3. **job_ledger(재시작 복구) 방식: storyboard의 `pending_jobs()` 재생 모델을 기본으로.**
   co-writer의 `finish_job(None)` 안전장치·`started` 타임스탬프·`rest` 기본값(`=""`)은 그대로
   포함, storyboard의 `finish_by_thread()`도 그대로 포함(co-writer엔 없던 기능). co-writer의
   `auto_pull.sh` busy-gate 로직은 이 통합 모델에 맞춰 다시 설계 필요(§5 리스크 4번 참고).

## 2. 지금까지 실제로 한 일 (진행 상황) — 2026-07-16 갱신

**Phase 1, Phase 2 완료(검증까지 마침). 아직 커밋 안 함(worktree에 untracked/modified로만 존재).**
Phase 3는 순서/추출 스캐폴딩까지 완성, storyboard-bot이 아직 활발히 개발 중이라 실제
`co-writer-bot-merge`에 반영하는 건 보류 중.

### Phase 1 — ✅ 완료
- storyboard-bot 전용 모듈 15개 `bot/`에 복사 완료(conti_state.py 등 — 파일 목록은 아래 그대로).
- `bot/openrouter_image.py`: storyboard 버전(773줄)으로 교체 완료. 5개 호출부 전부 호환 확인됨.
  단, storyboard의 `generate()`가 참조하는 `config.OPENROUTER_IMAGE_QUALITY`/`MODERATION`이
  merge된 `config.py`엔 아직 없음 — Phase 2/3에서 config.py 병합 시 반드시 추가해야 함(안 그러면
  `AttributeError`).
- `bot/storyboard_grid.py`: storyboard 버전으로 교체 완료(`no_text` 파라미터 추가, 호출부 1곳
  keyword-only라 안전 확인됨).
- `bot/job_ledger.py`: storyboard의 `pending_jobs()` 재생 모델 베이스 + co-writer의
  `finish_job(None)` 가드/`started` 타임스탬프/`rest=""` 기본값 + storyboard의
  `finish_by_thread()` 전부 포함한 병합본 작성 완료.
- `.env`/`.env.example`: 병합 완료. Slack 토큰은 co-writer 값 채택, `COWRITER_WORKS_PATH` 삭제,
  storyboard 전용 값(`HF_API_KEY`/`FIXED_IMAGES_ROOT` 등) 이식. **주의**: `.env.example`의
  Higgsfield 섹션 — `HIGGSFIELD_API_KEY`/`HIGGSFIELD_SECRET`(스틸컷 이미지 백엔드,
  `higgsfield_image.py`/`config.py`가 읽음)과 `HF_API_KEY`/`HF_API_SECRET`(영상화,
  `higgsfield_video.py`가 직접 읽음)은 **서로 다른 기능이 쓰는 별개 키 쌍**임(초안에서 "레거시
  이름"이라고 잘못 적었던 걸 수정함) — 둘 다 필요하면 각각 설정해야 함.

### Phase 2 — ✅ 완료
- `bot/works.py`: 양방향 병합 완료. 병합 중 발견: HANDOFF 초안이 "storyboard에서
  `_TRAILING_SUFFIXES`/`_looks_like_bad_work`를 가져와야 한다"고 적었던 건 사실 co-writer
  버전에 이미 다 있어서 불필요했음. storyboard 전용이었던 `COWRITER_WORKS_PATH`
  오버라이드/`_cowriter_env_pages()`는 `.env` 병합에서 그 변수 자체를 삭제했으므로 죽은 코드가
  되어 **드롭함**(파일 docstring에 이유 기록됨).
- `bot/notion_sync.py`: storyboard 버전(613줄, 상위집합)을 베이스로, `upsert_section()`만
  co-writer의 "같은 헤딩 접두어끼리 묶어 삽입" 로직으로 교체 완료(시그니처 동일 확인됨,
  호출부 전부 호환).

### Phase 3 — ✅ 최종 반영 완료 (2026-07-16, storyboard-bot 최종 프리즈 `9e48fdc` 기준)

실제 `bot/dispatch.py`(라우터, ~500줄) + `bot/dispatch_cowriter.py`(113개) +
`bot/dispatch_storyboard.py`(182개) + `bot/shared/{slack_io,files}.py` 작성 완료.
`works.py`/`notion_sync.py`/`job_ledger.py`는 `bot/shared/`로 이동(§1-2 레이아웃대로),
`app.py`(레거시 모놀리식, 아직 dispatch.py에 연결 안 됨)의 import 경로만 따라서 수정.
`bot/` 전체 39개 모듈 + `app.py` 전부 `py_compile` 통과, `from bot import dispatch` 실제 임포트
성공(직접 재현 확인), `_handle_dispatch`를 적대적 테스트 케이스로 실행해 확정된 순서와 일치 확인.

**조립 중 발견한 진짜 문제(직접 diff로 재확인함)**: `generator.py`/`prompts.py`/`video_guide.py`도
`generator.py`(HANDOFF §3-5 리스크 3, "두 백엔드 절대 합치면 안 됨")처럼 이름은 같지만 완전히
다른 파일이었는데, 이전까지 아무도 발견 못 했음(실제 두 파일이 한 디렉토리에 같이 있어야 비로소
드러나는 문제라 이번 조립 단계 전엔 안 보였음). co-writer의 `generator.py`엔 storyboard의
"그만" 중단 기능이 의존하는 `job_key`/`cancel()`/`cancel_prefix()`/`CANCEL_MSG`가 아예 없음
(직접 grep으로 재확인함) — 그대로 `from bot import generator`했으면 중단 기능이 조용히
`AttributeError`로 깨졌을 것. `bot/sb_generator.py`/`bot/sb_prompts.py`/`bot/sb_video_guide.py`로
storyboard 버전을 별도 이식하고 `dispatch_storyboard.py`에서 그쪽을 참조하도록 처리함.

`config.py`에 storyboard 전용 상수 31개 추가(Higgsfield/OpenRouter 영상·TTS·음악/CapCut/합본/
자동주행 등). `OPENROUTER_IMAGE_ASPECT`(co-writer `9:16` vs storyboard `16:9`) 충돌은 해결 안 하고
주석으로 명시적으로 남겨둠(Phase 4 몫).

**남은 것(문서/코드에 TODO(Phase 4)로 명시됨)**:
- `_HELP`/`_GUIDE` 통합(지금은 `[도움말]`/새로 생긴 무조건 발동 "도움말" 이스케이프 둘 다 storyboard
  `_HELP`만 보여줌 — co-writer 전용 스레드에서도 그럼, 콘텐츠 문제일 뿐 라우팅 버그는 아님).
- `cw._ALL_CMD_NAMES`(오타 제안용)에 storyboard 명령어 이름 미포함.
- `OPENROUTER_IMAGE_ASPECT` 충돌 미해결.
- **새 진입점(entry point) 없음** — `dispatch.py`가 `start_background_jobs()`를 노출하지만
  아무도 안 부름. 새 슬림 `app.py`(← `bot.dispatch` import + 양쪽 `healthcheck()` +
  `start_background_jobs()` + `SocketModeHandler` 시작)를 만드는 건 **Phase 6(컷오버) 몫**.
- 기존에 알려진 사소한 gap(수정 안 함, 스코프 밖): `vp_store.py`가 최상위 `shared` 패키지(우리
  새로 만든 `bot/shared/`와는 다른, visual-pipeline 쪽 레거시 패키지)를 못 찾아 DB 인덱싱만
  생략되고 파일 저장은 정상 동작(이미 있던 문제, 이번에 만든 `bot/shared/`와 이름만 같을 뿐
  실제로 안 겹침 — 임포트 네임스페이스가 `bot.shared` vs top-level `shared`라 안전).

### (구) Phase 3 스캐폴딩 메모 — 아래는 프리즈 갱신 전 1차 산출물, 위 최종 반영에 흡수됨
가장 위험한 단계(§3-5 리스크 1: 자연어 디스패치 조용한 충돌)라서 실제 코드 작성 전에 검증 도구부터
만듦. 둘 다 **재사용 가능한 스크립트**로 완성돼 있어서, storyboard-bot 개발이 진정되면 다시 실행만
하면 됨(로직을 새로 짤 필요 없음):

1. **디스패치 순서 매핑 + 검증** — `/Users/cony/dev/my-bot/_dispatch_order_check/`
   - `simulate_dispatch.py`: 순수 Python 시뮬레이터. 메시지+상태(dict)를 넣으면 병합된 순서대로
     어느 핸들러가 먼저 걸리는지 계산. 두 봇의 모든 정규식이 소스 라인 참조 주석과 함께 원문
     그대로 박혀있음.
   - `test_corpus.py`: 74개 케이스(정상 1개씩 + 적대적/경계 케이스), 전부 통과 확인됨(직접 재실행
     검증 완료).
   - **확정된 병합 순서**(원래 6단계 가설에서 실제 버그 3개 발견·수정됨):
     0. 중복 이벤트 가드 → 1. storyboard `_STOP_RE` → 2. storyboard 중단된 작업 재개
     → 3. 브래킷 명령 파싱(co-writer 브래킷 체인 → storyboard 브래킷 체인 → 알수없는 브래킷 폴백)
     → 4. storyboard `_maybe_*` 24개(기존 상대순서 유지) → 5. co-writer 좁은 인라인 체크
        (단, storyboard 진행 중 스레드에선 `_is_confirm` 억제 — FIX-2)
     → 6. storyboard catch-all(`_do_storyboard_auto_chain`, 단 co-writer 도메인 단어 있으면
        억제 — FIX-1, 그리고 "콘티"만으론 트리거 안 되고 동작 동사 필요 — FIX-3)
     → 7. co-writer 폴백(`_do_revise`/`_do_freeform`) — 진짜 최후의 보루.
   - 원래 가설(co-writer 폴백이 6번, storyboard catch-all이 7번)은 **틀렸음** —
     storyboard의 암묵적 시작 기능이 영원히 도달 불가능해지는 구조적 버그였음. 반드시 위 순서대로.
   - 사용자 확인 완료: 한 슬랙 스레드가 두 도메인 상태를 동시에 가질 수 있다고 가정하고 설계
     (봇이 하나로 합쳐지므로 도메인 분리 가정은 안전하지 않음).
   - 남은 진짜 모호함(사람 판단 보류, Phase 5 실사용으로 미룸): "3화 콘티 피드백 줘"처럼
     피드백/콘티 키워드가 동시에 있는 케이스 — 현재 시뮬레이터 기본값은 storyboard 우선.
   - 부가 발견: `_do_export`는 HANDOFF §3-3이 "완전히 다름"이라 적었던 것과 달리 실제로는
     파일명 접두어(`cowriter_` vs `storyboard_`)만 다른 사실상 동일 로직(직접 diff로 재확인됨).
     co-writer의 `_do_export`/`_do_ref`는 현재 co-writer 자체 디스패치에서 호출되는 곳이 없는
     죽은 코드(`CMD_FILE`/`CMD_REF`가 스텁으로 빠짐) — 병합 브래킷 체인에서 storyboard 버전이
     그냥 이기면 됨.

2. **기계적 코드 추출 도구** — `/Users/cony/dev/my-bot/_dispatch_extract_check/`
   - `extract.py`: `ast` 모듈로 두 `app.py`의 함수/상수를 정확한 라인 범위로 파싱해서
     **byte-for-byte 그대로**(LLM이 다시 타이핑하지 않고) 새 파일 구조로 옮기는 스크립트.
   - `draft/` 아래 초안 생성됨: `shared/slack_io.py`(12개 함수), `shared/files.py`(6개),
     `dispatch_cowriter.py`(113개), `dispatch_storyboard.py`(180개, 이름 충돌 6개는 `sb_` 접두어로
     구분), `dispatch.py`(라우터는 스텁 — 위 순서 시뮬레이터 결과를 반영해서 나중에 채울 것).
   - 전부 520개 슬라이스 byte-fidelity 자체검증 통과 + `python3 -m py_compile` 통과.
   - **⚠️ 이 draft는 storyboard-bot 커밋 `8c3a500`(2026-07-16 11:21) 기준 스냅샷** — 지금 HEAD
     (`c474164` 이후로도 계속 바뀌는 중)보다 뒤처짐. storyboard-bot 개발이 진정되면 **다시 실행**
     해서 최신 커밋 기준으로 갱신할 것(스크립트 로직 자체는 재사용, 재실행만 하면 됨).
   - 실제 이름 충돌 확인된 것: `_do_storyboard`(895줄, 진짜 워크호스, `stage=1/2` 렌더러) —
     co-writer의 동명 스텁과 충돌 → `sb_do_storyboard`로 rename. (`_do_storyboard_auto`/
     `_do_storyboard_auto_chain`은 이것과 다른, 안 겹치는 별개 함수 — catch-all이 부르는 진입점.)
   - HANDOFF §3-3의 카운트 정정: `_maybe_*` 24개(65도 23도 아님), 이름 충돌 함수 26개(10 아님),
     상수 충돌 16개(14 아님, `BOT_USER_ID`/`app`/`log` 누락됐었음).
   - 판단 필요했던 항목들(파일 내 주석으로 기록됨): `_thinking`/`_update_note`/`_post_chunks`는
     세 개가 한 세트로 상위집합 채택(중단 버튼 기능 때문에 서로 얽혀 있음); `_image_files`는
     storyboard의 4-tuple 채택(co-writer의 죽은 `_do_ref`가 3-tuple로 unpack하는 부분은 되살리면
     깨짐 — 플래그해둠); `_work_from_thread`는 직접 손으로 병합(storyboard 구조 + co-writer의 더
     엄격한 정규식); storyboard의 `hf_video`가 실제로는 `openrouter_video`에 바인딩돼있고
     `higgsfield_video`가 아닌 것을 발견 — 그대로 옮기면 엉뚱한 영상 백엔드가 연결될 뻔했음, 확인 필요.

### Phase 4 — ✅ 대부분 완료 (2026-07-16, §3-3 26개 함수/16개 상수 충돌 실제 감사·반영)

extract.py의 `all_collision_functions()`/`all_collision_constants()`를 직접 실행해 정확한
26개 함수·16개 상수 충돌 목록을 뽑아 하나씩 확인:

- **함수 26개**: 17개(공용 플러밍)는 이미 Phase 3에서 canonical 선택 완료(각 함수 위에
  `# --- 함수명 --- canonical: ...` 주석으로 근거 기록됨), 6개(`_do_export`/`_do_ref`/
  `_do_storyboard`/`_sb_script_from_bible`/`_sb_stage`/`_progress_episode`)는 `sb_` 접두어
  rename 완료, 나머지 3개(`_handle`/`on_mention`/`on_message`)는 라우터 레벨이라 애초에
  dispatch.py에서 새로 구현됨 — **전부 해결됨**.
- **상수 16개**: `CMD_RE`/`SUB_RE`/`MENTION_RE`/`SB_GEN_RE`/`SB_BADGE_PLAN`/`CMD_FILE`/
  `CMD_REF`/`_IMG_EXTS`/`_REF_SAVE_EXTS`(9개) 직접 diff 확인 — 진짜 바이트 단위 동일.
  `app`/`log`/`BOT_USER_ID`(3개)는 이미 `shared/slack_io.py`에 단일 소스로 통합됨.
  `_EXPORT_TYPES`(HANDOFF가 "본문 diff 미확인"이라 남겼던 것) — 직접 diff, **완전히 동일**,
  손댈 것 없음. `SB_BADGE_BOARD`(텍스트 다름) — 조사 결과 co-writer 쪽 사용처(`_do_storyboard`
  안)가 **원본 co-writer-bot에서부터 이미 죽은 코드**(호출부 자체가 없음, 병합으로 인한 회귀
  아님)라 실질적으로 안전 — 손 안 댐. `OPENROUTER_IMAGE_ASPECT`(co-writer `9:16` vs storyboard
  `16:9`) — 모든 실제 호출부가 명시적 aspect_ratio를 넘겨서 이 상수는 폴백으로만 쓰이는 게
  확인됨 → 세로 숏폼 포맷에 맞는 co-writer 값(`9:16`) 유지로 결론, `bot/config.py`에 근거
  기록. `_HELP`(통합 금지 대상) — **`_COMBINED_HELP`/`_COMBINED_GUIDE`를 `dispatch.py`에
  새로 작성**해서 `[도움말]`/무조건 발동 "도움말"/빈 멘션 인사 세 곳 전부 교체 완료.

- **조사 중 발견한 진짜 버그(이건 감사 목록에 없었음)**: `_CANCEL`(취소 신호 저장용 mutable
  set)이 `dispatch_cowriter.py`/`dispatch_storyboard.py`에 각각 독립적으로 정의돼 있어서,
  다른 16개 상수와 달리(읽기 전용 값이라 두 벌 있어도 무해) 이건 **런타임에 두 개의 다른 객체**가
  됨 — `dispatch.py`의 STOP 핸들러가 `sb._CANCEL`에만 추가하므로, co-writer 생성 도중
  "그만"/"중단"을 말하면 실제로는 취소가 안 먹혔을 뻔함(응답은 "중단할게요"라고 나가지만 생성은
  계속 진행). `bot/shared/slack_io.py`에 단일 소스로 옮기고 양쪽이 같은 객체를 import하도록
  고침(`cw._CANCEL is sb._CANCEL`이 이제 `True`임을 직접 확인함).

- **남은 것**: `cw._ALL_CMD_NAMES`(오타 제안)는 storyboard 명령어까지 포함해서 `dispatch.py`에
  `_ALL_CMD_NAMES`(합본)로 새로 만들어 반영 완료 — Phase 4 관련 TODO는 이제 없음.

### 아직 안 한 것
- Phase 5(테스트 — 실제 슬랙 테스트 채널에서 모든 명령·자연어 트리거 수동 검증).
- Phase 6(컷오반 — 새 진입점 작성, `@co-writer` Socket Mode를 합쳐진 프로세스로 전환).
- `vp_store.py`의 top-level `shared` 패키지(visual-pipeline 레거시, `bot/shared/`와는 다른
  네임스페이스) 못 찾는 이슈 — 이미 있던 gap, 파일 저장은 정상 동작, DB 인덱싱만 생략됨.
- Phase 4 변경사항은 아직 커밋 전(unstaged) — diff 리뷰 후 커밋 필요. Phase 1~3은 이미 커밋됨
  (커밋 `2818d3a`, `b86d230`).

## 3. 전체 병합 계획 (서브에이전트 조사 결과 요약)

### 3-1. env 변수 — 사실 거의 문제 없음
`NOTION_TOKEN`/`OPENROUTER_API_KEY`/`SHEET_WEBAPP_URL`/`SHEET_SECRET`은 두 봇의 실제 `.env`
값이 **이미 똑같음**(조사 시점 확인) — 이름만 겹치는 게 아니라 값도 같아서 병합이 거의 공짜.
충돌은 `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN`뿐이고 이건 §1-1 결정으로 이미 해결됨(co-writer 값
채택). storyboard 전용(`HF_API_KEY`/`HF_API_SECRET`/`FIXED_IMAGES_ROOT` 등)은 그대로 이식.
`COWRITER_WORKS_PATH`는 두 봇이 한 repo에 살게 되면 의미 없어지므로 삭제 예정.

### 3-2. 공유 모듈 조정 필요 목록

**`works.py`**: 양방향으로 발전(어느 한쪽도 완전한 상위집합 아님). storyboard 쪽에서
`_cowriter_env_pages()`(env 경로 폴백)·`COWRITER_WORKS_PATH` 오버라이드·`_looks_like_bad_work()`
가드를 가져오고, co-writer 쪽에서 `_TRAILING_SUFFIXES`("기획안"/"기획서" 접미어 제거) 로직을
가져와서 합친 파일 하나를 만들어야 함. 기계적인 작업(디자인 결정 없음).

**`notion_sync.py`**: storyboard-bot 버전(594줄)이 co-writer 버전(359줄)의 강한 상위집합
(co-writer가 가진 함수는 전부 storyboard에도 있음) — **storyboard 버전을 베이스로** 채택.
단, `upsert_section()` 함수는 예외 — **co-writer 버전이 더 똑똑함**(같은 헤딩 접두어의 섹션을
찾아 그 뒤에 삽입, storyboard 버전은 그냥 페이지 끝에 추가하는 단순 버전) — 이 함수 본문만
co-writer 걸로 교체해서 합쳐야 함.

**`job_ledger.py`**: §1-3에서 이미 방식 결정됨 — storyboard 베이스 + co-writer의 안전장치들
포함. 아직 실제 병합 파일은 안 만들어짐(§2 참고).

**`openrouter_image.py`**: storyboard-bot 버전(773줄, 35+ 함수 — 엘리먼트 레지스트리·목소리
배정·vision_check 등 전부 포함)이 co-writer-bot 버전(176줄, 11함수)의 강한 상위집합으로 확인됨
— **storyboard 버전을 통째로 채택** 권장. 단, 교체 전에 `co-writer-bot/app.py`의 모든
`oi.`/`openrouter_image.` 호출부가 storyboard의 더 풍부한 시그니처와 호환되는지(특히 중간에
포지셔널 인자가 끼어들어 순서가 깨지는 경우가 없는지) 확인 필요 — 아직 이 확인 자체를 안 함.

**`storyboard_grid.py`**: storyboard-bot 버전이 `no_text: bool = False` 파라미터를 추가로 가짐
(순수 추가형, 기본값 있어서 기존 호출부엔 영향 없을 것으로 보임) — 채택 전 co-writer-bot의
실제 호출부 확인 필요(아직 확인 안 함).

### 3-3. app.py 함수/상수 이름 충돌 감사 (실제 grep 결과, 26개 함수 + 14개 상수)

**공용 플러밍으로 통합 가능(14개)**: `_clean`, `_convo_text`, `_decode_text`, `_files_text`,
`_hwpx_text`, `_image_files`(⚠️ 튜플 크기 다름, 3개 vs 4개 — 시그니처 조정 필요), `_last_assistant_with`,
`_looks_like_mention`, `_md_table_to_csv`, `_mrkdwn`, `_parse_json_array`, `_post_chunks`,
`_reply`, `_thread_messages`, `_thinking`(⚠️ 기본 인자 유무 다름), `_update_note`,
`_work_from_thread`(⚠️ storyboard가 `thread_ts=None` 인자 하나 더 있음 — storyboard 쪽이 상위집합).

**이름은 같지만 기능이 완전히 다름 — 절대 그냥 합치면 안 됨(10개, rename 필요)**: `_do_export`,
`_do_ref`, **`_do_storyboard`**(⚠️ 가장 위험 — co-writer 버전은 스텁 트리거, storyboard 버전은
진짜 씬설계 1단계 진입점. 이름이 같다고 잘못 덮어쓰면 진짜 기능이 조용히 스텁으로 퇴화함),
`_sb_script_from_bible`, `_sb_stage`, `_handle`(→ `dispatch.py`의 라우터가 됨), `_progress_episode`,
`on_mention`, `on_message`.

**상수(거의 다 바이트 단위로 이미 동일함 — co-writer의 스텁이 storyboard 상수를 그대로 복사해둔
상태라 그냥 storyboard 위치를 정본으로 채택하면 됨)**: `CMD_RE`, `SUB_RE`, `MENTION_RE`,
`SB_GEN_RE`, `SB_BADGE_PLAN`, `CMD_FILE`, `CMD_REF`, `_IMG_EXTS`, `_REF_SAVE_EXTS`. 단
**`SB_BADGE_BOARD`는 텍스트가 다름**(storyboard 버전이 최신) — storyboard 값 채택.
`_HELP`는 절대 통합 금지 — 두 봇의 도움말을 이어붙여서 하나의 `도움말`/`[help]` 응답으로.
`_EXPORT_TYPES`는 본문 diff를 아직 안 함 — Phase 4에서 확인 필요.

**Slack `action_id` 충돌**: 0개(co-writer 10개, storyboard 36개, 겹치는 거 없음 — 안전).

**명령어(`CMD_*`) 이름 자체는 안 겹침** — 다만 co-writer의 `CMD_STORYBOARD`/`CMD_STORYBOARD2`/
`CMD_STORYBOARD_IMG`(스텁)를 storyboard의 진짜 `CMD_STORYBOARD_ALL`/`CMD_IMG`/`CMD_CONTI_FINAL`
처리로 완전히 교체해야 함 — 이게 이번 병합의 핵심 "스텁 제거" 지점.

### 3-4. 단계별 계획

| 단계 | 범위 | 완료 기준 | 위임 방식 |
|---|---|---|---|
| 0. 결정 | Slack 정체성/구조/job_ledger 방식 | 서면 승인 | ✅ 완료(§1) |
| 1. 레포/설정 기반 | storyboard 전용 모듈 물리적 이식, .env 병합, openrouter_image.py/storyboard_grid.py 채택, job_ledger 조정 | 모든 storyboard 모듈이 호스트 repo에 존재·import 가능, .env 토큰 하나로 정리, 앱이 부팅됨(명령 라우팅은 아직 안 돼도 됨) | 서브에이전트 1개(모호함 적음) — **부분 진행 중, §2 참고** |
| 2. 공유 모듈 조정 | works.py/notion_sync.py/job_ledger.py 최종 병합본 | 각 파일 하나씩, 양쪽 원래 호출부 기준 검증 | 서브에이전트 1개, 3개 파일 diff를 한 컨텍스트에서 동시에 봐야 함(분산 금지) |
| 3. 디스패치 아키텍처 | §1-2 구조대로 app.py 분할(dispatch.py/dispatch_cowriter.py/dispatch_storyboard.py/shared/) | 두 봇의 전체 명령 집합이 하나의 _handle/on_message로 라우팅됨 | **반드시 하나의 연속 컨텍스트 에이전트만** — storyboard만 해도 `_maybe_*` 자연어 감지기가 65개, 순서에 민감해서 조각내면 위험 |
| 4. 명령어 이식 + 스텁 제거 | co-writer의 스텁(`app.py:59-80` 부근)을 진짜 storyboard 라우팅으로 교체, §3-3의 26개 함수/14개 상수 충돌 실제 반영 | storyboard의 모든 브래킷 명령이 합쳐진 봇에서 작동, "따로 있는 봇 쓰세요" 응답 사라짐 | Phase 3 라우터 뼈대가 있으면 명령어 묶음별(씬설계/콘티, 샷분해/스틸컷, 영상화/합본 등)로 병렬 위임 가능 |
| 5. 테스트 | 테스트 슬랙 채널에서 양쪽 봇의 모든 명령·자연어 트리거 수동 검증, 특히 두 자연어 체인의 교차 오인식 | 조용한 오라우팅 없음 | 명령어 묶음별 병렬 가능하나, 자연어 교차 오인식 검증은 전체를 한눈에 보는 사람/에이전트 필요 |
| 6. 컷오버 | @co-writer의 Socket Mode 연결을 합쳐진 프로세스로, storyboard 프로세스 중지, storyboard 앱은 설치 유지하되 "합쳐졌어요" 안내만(전환 기간 2-4주 후 제거) | 프로덕션에서 합쳐진 봇만 응답 | 단일 에이전트/사용자, 라이브 인프라 건드림 |

### 3-5. 리스크 (우선순위 순)

1. **자연어 디스패치 조용한 충돌** — storyboard 혼자 `_handle` 안에서 65개 `_maybe_*` 정규식
   감지기를 순서대로 거침, co-writer도 자체 인라인 정규식 체인이 있음. 합친 뒤 어느 체인이
   먼저 도는지에 따라 애매한 문장이 **에러 없이 조용히** 엉뚱한 봇 로직으로 새어들어갈 수 있음
   — 가장 위험한 실패 유형, 순서를 신중히 설계하고 반드시 테스트해야 함.
2. **`_do_storyboard` 이름 충돌은 겉만 같은 게 아니라 의미가 다름** — 위 §3-3 참고. 대충
   rename하면 진짜 기능이 스텁으로 조용히 퇴화할 위험. 코드 옮기기 전에 명시적 rename 맵 필요.
3. **두 개의 LLM 생성 백엔드가 영원히 공존해야 함** — co-writer는 Agent SDK/Anthropic API,
   storyboard는 OpenRouter HTTP. "하나의 백엔드로 통일"이 안 됨 — `generator.py`를 절대
   하나로 합치면 안 되고, 두 개의 별도 모듈로 유지해야 함(각자 자기 기능 영역에서만 씀).
4. **job_ledger 재시작 복구 모델 불일치** — co-writer의 busy-gate는 `deploy/auto_pull.sh`가
   파일 최신성을 확인하는 방식에 의존, storyboard는 `pending_jobs()` 재생 방식. §1-3 결정대로
   진행하되, co-writer의 `auto_pull.sh` 게이트 로직을 이 새 모델에 맞게 다시 설계해야 함.
5. **`_image_files`/`_work_from_thread` 시그니처가 이름만 같고 실제로 다름** — 튜플 크기/인자
   개수가 달라서, 어느 쪽 상위집합을 채택하든 두 거대 파일 전체의 모든 호출부를 검색·검증
   필요(find-and-replace 자동화 불가, 하나씩 확인).
6. **`upsert_section()` 퇴행 위험** — 실수로 storyboard의 단순 버전("페이지 끝에 그냥 추가")을
   채택하면, 지금 co-writer가 잘 하고 있는 "같은 헤딩 접두어끼리 묶어서 삽입"이 깨져서 노션
   문서가 지저분해짐 — 사용자가 나중에 불평할 때까지 티가 안 남.
7. **`storyboard_grid.py`의 `no_text` 파라미터 유실 위험** — "거의 똑같아 보이니 co-writer
   걸로" 식으로 얕게 합치면 이 실사용 기능(스틸컷 그리드에 캡션 안 넣는 옵션)이 조용히 사라짐.
8. **`_EXPORT_TYPES` 상수 본문 diff 미확인** — 정의 줄 위치만 확인했고 실제 dict 내용 비교는
   아직 안 함 — Phase 4에서 반드시 확인.

## 4. 다음 세션이 할 일 (제안 순서) — (2026-07-16 갱신: Phase 1~4 전부 완료, 아래는 원래
순서 그대로 남겨두되 실제로는 §2/§5를 볼 것)

1. 이 문서 읽고 Phase 0 결정 사항 확인(이미 다 정해짐, 재논의 불필요).
2. Phase 1 마저 끝내기: `openrouter_image.py`/`storyboard_grid.py` 호출부 호환성 확인 후 교체,
   `job_ledger.py` 병합본 작성, `.env`/`.env.example` 만들기(§2의 "아직 안 함" 목록 그대로).
3. Phase 2(공유 모듈 조정: works.py·notion_sync.py) — §3-2에 이미 정확히 뭘 어디서 가져올지
   적어놨으니 기계적으로 진행 가능.
4. Phase 3(디스패치 분할)부터는 정말 신중하게 — 반드시 하나의 연속 컨텍스트로, 조각내지 말 것.
5. 커밋은 이 worktree(`co-writer-bot-merge`, 브랜치 `merge-storyboard-bot`)에다가만, 매 phase
   끝날 때마다 사람이 diff 리뷰 후 커밋(이번 세션 내내 그렇게 했음 — 서브에이전트가 작업하면
   반드시 diff를 직접 읽고 검증한 뒤 커밋하는 패턴 유지).

**(2026-07-16 갱신) 위 5단계 전부 완료됨 — 실제로 다음 세션이 할 일은:**
1. Phase 5(테스트) — 아래 §5-0 참고, 별도 테스트 Slack 앱/토큰 준비가 먼저 필요(운영 중인
   `@co-writer`/storyboard 프로세스에 아직 어떤 영향도 안 줌, 이 worktree는 계속 미배포 상태).
2. Phase 6(컷오버) 실행 전 반드시 §5(롤백 계획) 먼저 읽고 사전 준비 체크리스트부터 이행할 것.

## 5. Phase 6 배포/롤백 계획 (2026-07-16 작성 → 같은 날 실제로 컷오버 완료됨, §5-7 참고)

### 5-0. 실제 운영 구조 확인 (이 계획의 전제)

이번 세션에서 직접 확인한 사실:
- `co-writer-bot`(라이브)은 `/Users/cony/dev/my-bot/co-writer-bot`에서 `python3 app.py`를
  launchd(`ai.tain.co-writer-bot`, `KeepAlive=true`)로 상시 구동. 별도 launchd job
  (`ai.tain.co-writer-bot-autopull`, 300초=5분마다)이 `deploy/auto_pull.sh`를 돌려서
  `origin master`를 `--ff-only`로 pull하고, 바뀌면 import 검증 후 `launchctl kickstart -k`로
  재시작. `storyboard-bot`도 동일 구조(`ai.tain.storyboard-bot` + `-autopull`).
- **이 merge worktree(`co-writer-bot-merge`, 브랜치 `merge-storyboard-bot`)는 위 라이브 경로와
  완전히 별개의 디렉토리** — 지금까지 이 브랜치에 아무리 커밋해도 라이브 봇 동작에 전혀 영향
  없음(실제로 `git ls-remote origin refs/heads/merge-storyboard-bot`가 계속 비어있음 = 원격에도
  없음, `auto_pull.sh`는 `master`만 봄).
- 이 세션이 실행되는 머신에서 `launchctl list`로는 두 launchd job이 안 보임 — 실제 운영은
  다른 호스트(storyboard-bot의 `HANDOFF_맥미니이전.md` 언급으로 보아 별도 맥미니)에서 도는 걸로
  추정. 즉 **컷오버·롤백 실행 자체는 그 호스트에 접근 가능한 사람/세션이 해야 함** — 이
  문서는 "무엇을, 어떤 순서로" 하면 되는지의 절차서 역할.

### 5-1. 컷오버 사전 준비 체크리스트 (실행 전에 반드시)

1. **되돌아갈 지점 명확히 태깅**: 컷오버 직전 `co-writer-bot`(라이브 디렉토리)의 `master` HEAD에
   `git tag pre-merge-cutover-YYYYMMDD` (실행일로 치환) — "master를 그냥 몇 커밋 전으로
   되돌리면 되지 않나"에 의존하지 않고 정확한 지점을 이름으로 고정.
2. **storyboard-bot 프로세스는 삭제하지 말고 멈추기만**: `launchctl unload`로 내려도 `.plist`
   파일과 저장소 자체는 그대로 둔다 — 롤백 시 `launchctl load`만으로 바로 복구되게.
3. **양쪽 `data/` 디렉토리 백업**: 컷오버 직전 `co-writer-bot/data/`와 `storyboard-bot/data/`
   전체를 타임스탬프 붙여 별도 위치로 복사(`jobs.json`/`inflight.json`/`notion_pages.json`/
   `conti_state` 등 — 병합 후엔 이 파일들이 한 디렉토리로 합쳐지므로, 합치기 직전 "분리돼
   있던 마지막 상태" 스냅샷이 롤백 시 유일한 복구 지점이 됨).
4. **Phase 5(테스트)를 별도 테스트 앱으로 먼저 마칠 것** — 지금 알려진 미해결 모호함
  (`adv-a3`: "3화 콘티 피드백 줘"류 케이스, §3-5 리스크 전반)이 실사용에서 실제로 문제가
  되는지 컷오버 전에 최대한 걸러낼 것.
5. **새 진입점 스크립트 자체도 미리 작성·리뷰**(Phase 6 몫, 아직 없음 — `bot/dispatch.py`의
  `start_background_jobs()`을 부르고 양쪽 백엔드 `healthcheck()` 확인 후 `SocketModeHandler`
  시작하는 짧은 `app.py` 교체본. 이 merge worktree 안에서 먼저 만들고 로컬에서 import까지
  검증한 뒤에 라이브 경로로 가져갈 것).

### 5-2. 롤백 트리거 기준 (아래 중 하나라도 관찰되면 즉시 롤백 절차 진행)

- 새 프로세스가 부팅 자체를 못 함(import 에러 등) — 사실 `auto_pull.sh`의 기존 안전장치(30초
  타임아웃 import 검증, 실패 시 재시작 자체를 취소)가 이미 이 경우는 막아줘서 옛 프로세스가
  계속 서비스 중일 것 — 그래도 로그(`logs/autopull.log`) 확인 필요.
- 부팅은 됐는데 기본 명령(`[생성]`/`[스토리보드]` 등)이 무응답이거나 에러.
- **조용한 오라우팅** 관찰 — 사용자가 co-writer 요청했는데 storyboard 파이프라인(비용 큰
  생성)이 도는 경우, 또는 반대. 이게 가장 늦게 발견될 수 있는 유형이니 컷오버 직후 최소
  30분~1시간은 로그/실사용 응답을 집중 관찰.
- Slack API 인증 에러(두 앱이 겹쳐 붙거나 토큰 문제).
- `data/jobs.json`/`data/notion_pages.json` 등 공유 데이터 파일 파싱 에러(JSON 깨짐)나
  내용이 명백히 이상함(예: 등록된 작품이 갑자기 없어 보임).

### 5-3. 롤백 절차

**A. 긴급(빠른 경로)** — 실사용자가 지금 영향받고 있을 때:
1. 운영 호스트에 직접 접속해 `co-writer-bot` 디렉토리를 `git checkout pre-merge-cutover-YYYYMMDD`.
2. `launchctl kickstart -k gui/<uid>/ai.tain.co-writer-bot`으로 즉시 재시작.
3. storyboard-bot을 unload했었다면 `launchctl load`로 복구.
4. `origin master`도 같은 커밋으로 강제 정렬해둬야 다음 `auto_pull.sh` 주기가 다시 새 코드로
   덮어쓰지 않음(`git push --force-with-lease` 또는 `git revert`로 새 커밋을 얹어 push — 후자가
   기록이 남아 더 안전).

**B. 표준(급하지 않을 때)**:
1. `master`에 컷오버 커밋을 되돌리는 `git revert` 커밋 생성 후 `origin master`에 push.
2. 다음 `auto_pull.sh` 주기(최대 5분) 안에 자동으로 pull·재시작됨 — `logs/autopull.log`로
   확인.
3. storyboard-bot 프로세스 복구는 A-3과 동일.

### 5-4. 데이터 처리(롤백 시 가장 까다로운 부분)

컷오버~롤백 사이에 병합 봇이 만든 새 기록(신규 작품 등록, 진행 중이던 job_ledger 항목,
새로 등록된 인물/장소 엘리먼트 등)은 **병합된 단일 `data/` 구조 기준**이라, 롤백해서 분리된
옛 구조로 돌아가면 그 사이 데이터는 수동으로 옮기거나(작은 diff면 가능) 유실을 감수해야 함.
이 리스크를 줄이려면:
- 컷오버는 트래픽이 적은 시간대에.
- 컷오버 직후 관찰 윈도우(30분~1시간)를 짧게 잡고, 그 안에 문제 있으면 바로 롤백 —
  "며칠 지나서 문제 발견" 시나리오면 데이터 되돌리기가 훨씬 어려워짐.
- 롤백이 필요해지면, 컷오버 이후 실제로 들어온 새 작품 등록/생성 요청이 있었는지
  `data/jobs.json`/`data/notion_pages.json`의 mtime으로 먼저 확인하고, 있으면 그 내용을
  롤백 후의 옛 분리 구조 쪽 해당 파일에 수동으로 반영할지 이 시점에 사람이 판단.

### 5-5. storyboard 앱 "합쳐졌어요" 안내 복구

컷오버 때 storyboard 앱을 실제 기능 대신 "합쳐졌어요, `@co-writer`를 쓰세요" 안내만 하도록
바꿔놨다면(§1 결정사항 참고, 전환기간 2-4주), 롤백 시엔 그 안내 커밋 자체도 되돌리고
원래 storyboard 프로세스를 완전히 복구해야 실제 파이프라인 기능이 돌아옴 — 단순히 프로세스만
`launchctl load`하는 것과 별개로, 안내-전용 커밋이 반영된 상태면 코드 자체도 되돌려야 함.

### 5-6. 롤백 후 확인

- 옛 `co-writer-bot`/`storyboard-bot` 두 프로세스가 각각 정상 응답하는지 별도 확인.
- 무엇이 잘못됐는지 원인 조사 후, 이 문서(§3-5 리스크 또는 새 섹션)에 실패 사례로 기록 —
  다음 컷오버 시도 때 같은 문제가 재발하지 않게.

### 5-7. 실제 컷오버 실행 기록 (2026-07-16, 이 §5 작성 당일)

Phase 5(별도 테스트 앱)를 건너뛰고 사용자 판단으로 바로 진행됨 — 아래가 실제로 일어난 일:

1. 이 세션에서 새 진입점(`app.py` → `bot/dispatch.py` 기동 조립, 커밋 `3d926ea`)까지 만들고
   `merge-storyboard-bot` 브랜치에 push. `master`에 직접 push하려던 시점에 **다른 세션이 이미
   `master`를 앞서 진행**하고 있는 게 발견됨(`git fetch`로 `1901fd3`/`88ba39f` 등 확인) —
   같은 작업(Phase 1~4 병합)을 그 세션이 독자적으로 이미 merge+push 완료한 상태였음.
2. 이후 새 진입점만 추가로 merge-storyboard-bot에 push해서 그 세션이 이어서 master에
   반영(`88ba39f`) — **실제 컷오버가 이 시점에 이미 실행됨**, 되돌릴 지점 태깅이나
   `data/` 백업 같은 §5-1 사전 준비는 못 거치고 진행됨(기록해둠 — 다음에 비슷한 상황이면
   미리 확인할 것).
3. storyboard-bot 프로세스는 멈추지 않고 계속 독립적으로 돌아가는 중(§0-1 스타일 참고 —
   사용자가 "storyboard-bot 저장소/프로세스 처리는 나중에 결정"이라고 명시적으로 보류함,
   2026-07-16).
4. **컷오버 직후 실사용에서 라우팅 버그 실제 발견**: "씬3 스틸컷 컷5,13,14 만들어줘"가 화
   번호가 없다는 이유로 `_maybe_generate_request` 게이트를 못 넘고 storyboard catch-all(씬
   설계부터 재시작)로 샘 — 씬 번호도 화 번호와 동일하게 게이트 통과 조건에 추가해서 수정
   (커밋 `3a3e82f`). §3-5 리스크 1("조용한 오라우팅")이 실제로 발생한 사례 — Phase 5를
   건너뛴 대가가 바로 나타남, 앞으로 유사 패턴 있는지 계속 주의.
5. 그 외에도 컷오버 이후 실사용 중 발견·수정된 것들: `FIXED_IMAGES_ROOT` config 누락(인물
   참조 이미지 전혀 안 붙던 사고, 다른 세션이 발견), 콘티 작성 시 구도 헤더 오남용·라벨나열
   실제 사고 2건(콘티변환규칙 v2.9 강화로 대응), 영상화 자연어의 콤마 컷 리스트 파싱 누락.
6. **결론/교훈**: 여러 세션이 동시에 같은 저장소를 다루는 상황에서 "누가 push할지"는 실행
   직전에 반드시 `git fetch`로 재확인할 것(이번엔 다행히 같은 작업이라 충돌 없이 합쳐졌지만,
   다른 작업이었다면 충돌·덮어쓰기 위험 있었음). Phase 5를 생략하고 컷오버한 결과 실제
   버그가 최소 1건 실사용자에게 노출됐다가 빠르게 잡힌 것 — 이번엔 피해가 적었지만
   (씬 설계가 잘못 재시작된 정도), 다음 유사 결정에서는 이 사례를 참고할 것.
