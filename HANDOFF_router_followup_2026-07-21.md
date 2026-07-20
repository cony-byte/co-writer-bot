# 핸드오프: co-writer-bot 라우터 정리 후속작업 (2026-07-21)

## 배경
당일 "라우팅 아키텍처 정리 + 오라우팅 근절" 핸드오프의 Task 0/1/3/5.3/6.2를
`master` 브랜치에 구현·커밋·푸시 완료. 이 문서는 **그 다음에 남은 작업**을
다른 세션/머신(특히 실제 Slack 토큰과 production 데이터가 있는 맥미니)에서
이어받기 위한 것.

- 작업 위치: `/Users/cony/dev/my-bot/co-writer-bot` (master)
- 완료 커밋:
  - `5370916` 작업0: 라우터 실패 폴백을 "안전 정지"로 뒤집기
  - `2eb27e1` 작업1/3/5.3/6.2: resolve_work 단일화 + 첨부 레이스 + ~룩 라우팅 + 회귀 케이스
- 둘 다 `origin/master`에 이미 푸시됨 (post-commit hook 자동 push).
- `python3 -m py_compile bot/*.py tests/*.py` 통과 확인함(정적 검증만, 실행 검증 아님).

## 이번 세션에서 한 일 요약 (읽고 넘어가도 됨)
- **작업0**: 라우터 실패(타임아웃/파싱/예외/미해결 intent)가 legacy 자유문장
  매처로 새서 엉뚱한 파이프라인을 실행하던 구조를 제거. 성공 실패 시
  읽기전용 핸들러(조회 4종)만 시도 → 그래도 안 되면 스레드당 1회 안내 메시지
  후 조용히 정지. `COWRITER_ROUTER_ENABLED=0` 킬스위치는 그대로 legacy 전체 복귀.
- **작업1**: `bot/shared/works.py`에 `all_works_with_aliases()`/`all_names()`가
  **아예 없어서** `nl_router._build_context`가 매번 예외로 `registered_works={}`
  폴백하던 게 근본 원인(별칭 미해석 사고의 원인). 함수 추가 + `resolve_work()`
  단일 진입점(꺾쇠/대괄호 → 본문 최장일치 → tracked_work → 스레드 이력 스캔)
  구현, 3개 호출부 통일.
- **작업3**: 첨부 이미지 레이스 컨디션 — "이 사진처럼" 류 요청인데 이벤트에
  파일이 아직 안 실려 있으면 2.5초 대기 후 1회 재조회.
- **작업5.3**: "~룩"(PD룩/스탭룩 등) + 첨부/참조 지시 → 의상 참조로 라우팅
  (기존에는 외형 스펙으로 오인해 `scene_design`으로 잘못 보냄).
- **작업6.2**: 회귀 코퍼스 6케이스 추가(42→48), `forbid_intent` 체크 키 추가.

## ⚠️ 이 환경에서 못 끝낸 것 — 다음 세션이 이어서 할 일

### 1. 라이브 회귀 실행 (최우선)
```
cd co-writer-bot
python3 -m tests.test_nl_router --live
```
- 이 개발 샌드박스는 `SLACK_BOT_TOKEN` 등이 없어서 `nl_router` import 시
  `slack_bolt.error.BoltError`로 실행 불가했음. **한 번도 실제로 못 돌려봄.**
- `tests/nl_router_corpus.json` 48케이스(신규 6개 포함: `pdlook-ref-attached`,
  `pdlook-ref-noattach`, `stafflook-ref-attached`, `alias-regstatus-parent`,
  `costume-question-noaction`, `alias-bracket-regstatus`) 전부 통과해야 함.
- 떨어지는 케이스가 있으면 실패 로그(`route.raw` 포함해서 출력됨)를 그대로
  들고 오면 `bot/nl_router_prompt.py`의 RULES/FEW_SHOTS 보강함.

### 2. 수동 테스트 4종 (라이브 봇으로, Slack에서 직접)
정적 코드 검토로만 확인했고 실제 실행은 안 해봤음. 아래 4개를 실제 스레드에서 확인 필요:
1. **별칭 질문** — 별칭으로만 부른 작품에 대해 "이거 등록 상태 어때?" 류 질문
   → `resolve_work`가 스레드 이력/부모 메시지에서 정식 작품명을 찾아내는지.
2. **PD룩+첨부** — "이영 PD룩은 이 이미지에 있는거랑 비슷하게 해줘" + 이미지 첨부
   → `element_edit(kind=의상)`로 가는지 (예전엔 `scene_design`으로 오발).
3. **첨부 레이스** — 이미지 첨부와 거의 동시에 텍스트 전송(Slack이 파일을
   이벤트에 늦게 실어주는 경우) → 2.5초 재조회로 파일 인식되는지.
4. **작업 중 질문** — 자동주행/생성 작업 진행 중에 무관한 질문 던지기 →
   "🛑 중단했어요" 오발 없이 질문에 답하고 원래 작업은 안 끊기는지.

### 3. Task 6.1 — 데이터 정리 (맥미니 production 데이터 필요)
- 작품 `"저는 연프 출연진이 아닌데요 !"`의 `elements.json`에서 잘못 들어간
  "과 배경" costume 항목 삭제.
- 이 checkout(`data/refs/`)엔 해당 작품 데이터 자체가 없음(다른 작품 "날혐남"만
  있음) — production 데이터는 맥미니가 정본이라 거기서만 가능.
- 경로 예상: `data/refs/저는 연프 출연진이 아닌데요 !/elements.json` (실제 경로는
  맥미니에서 `find data/refs -name elements.json` 등으로 확인).

### 4. 알려진 개선 여지 (급하지 않음, 참고만)
- `drain_pending`이 `job_ledger.finish_*` 완료 훅이 아니라 `_handle`의 finally
  에서만 호출됨 → 큐잉된 요청이 "job 끝나는 즉시"가 아니라 "다음 인바운드
  메시지가 올 때" 드레인됨. 정합성 문제는 아니지만 원래 핸드오프 의도(즉시
  드레인)와는 다름. 크로스모듈 배선이 필요해서 이번엔 손 안 댐.

### 5. 아직 손 못 댄 이전 버그 2건 (라우터 작업 이전에 보고됨)
- **webp 첨부 미인식**: `.webp` 확장자 참조 이미지를 봇이 "첨부하신 이미지가
  확인되지 않아요"로 오인. 원인 미조사 상태.
- **콘티 재작성 요청 → "🛑 중단했어요" 오발**: 이건 `cancel-leak-rewrite` 코퍼스
  케이스로 이미 등록돼 있고, 작업0의 폴백 뒤집기로 **구조적으로는 해소됐을
  가능성이 높음**(질문/재작성 요청이 더 이상 legacy 취소 경로로 안 샘) — 다만
  실제 확인은 안 했으니 수동테스트 4번 확인할 때 같이 재현해볼 것.

## 킬스위치 (문제 생기면)
```
COWRITER_ROUTER_ENABLED=0
```
설정 시 전체 legacy 자유문장 체인으로 즉시 복귀(코드 그대로 남아있음).

## 완료 보고 받을 곳
라이브 회귀 + 수동테스트 4종 결과를 정리해서 원 요청자에게 전달하면
회귀 실패 케이스에 대한 프롬프트 보강을 이어서 받을 수 있음.
