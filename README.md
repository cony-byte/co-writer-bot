# co-writer-bot

숏폼 로맨스 드라마 **보조 작가 슬랙 에이전트**.
목표: 작가가 3일에 1편 쓰던 기획안·대본을 **1일 1편 이상**으로 — 초안 생산과 반복 수정은 봇이, 판단은 작가가.

## 뭘 근거로 쓰나

| 입력 | 출처 | 상태 |
|---|---|---|
| 패턴 요약 (훅·절단점·트로프 분포) | [story-v1-scripts](https://github.com/cony-byte/story-v1-scripts) `reference/patterns/` | ✅ 연동 |
| 유사 사례 2~3편 (정제 대본 포함) | 같은 repo `reference/reference_db.json` (drama_clip **97편**, 통합 스키마 **v5**) | ✅ 연동 |
| 사내 작가 기획안·대본 템플릿 | `templates/*.md` | ✅ 투입 (숏폼드라마 템플릿·루브릭 v0) |

생성 프롬프트에는 원본 97편이 아니라 **패턴 요약 + 요청과 유사한 사례 2~3편**만 들어간다 (레퍼런스 DB 설계 원칙).

## 데이터가 어디서 오나 (4-repo 흐름)

봇은 **로우데이터를 직접 읽지 않는다** — LLM으로 태깅·정제된 `reference_db.json`(정제 DB)만 읽는다. 그 정제 DB는 crawler의 로우데이터에서 **자동으로 따라오지 않고**, 아래 3단계를 거친다:

```
story-v1-crawler (로우데이터 SSOT · 로컬 매일 실행 → data/out/*.csv 커밋)
  └─① LLM 재태깅 배치 [scripts에서 수동]        → story-v1-scripts reference_db.json (v5)
       └─② python3 scripts/sync_reference.py [수동] → co-writer-bot data/reference/ 사본
            └─③ @co-writer reload [슬랙]            → 봇에 반영
```

- **①이 병목이다.** crawler가 매일 로우데이터를 갱신해도, scripts에서 재태깅(정제) 배치를 돌리지 않으면 봇엔 새 편이 안 들어온다. 정제는 기계 변환이 아니라 LLM 작업이라 자동화돼 있지 않다.
- 봇은 `data/reference/`에 있는 **자기 사본**을 읽는다(오프라인·기동 안정성). scripts가 갱신되면 ②·③을 돌려야 반영된다.
- co-writer-bot 자체는 **로우데이터·crawler를 몰라도 된다** — scripts의 정제 DB만 sync하면 끝. (상위 데이터 흐름은 crawler `HANDOFF.md` 참조)

## 슬랙 사용법

```
@co-writer 기획안: 재벌 남주 x 계약결혼, 오피스, 정체 숨김
  → 로그라인 / 훅 설계(첫 3초) / 절단점 설계 / 회차 구성

(스레드에서) @co-writer 3화 절단점을 정체 폭로 직전 컷으로 바꿔줘
  → 해당 부분만 고친 수정본

(스레드에서) @co-writer 대본: 1화
  → ML/FL/SUP/NAR 화자 표기 대본, 훅 대사로 시작 → 절단점 대사로 끝
```

### 트렌드 검색 (통합 DB 성과 집계)

"트렌드/요즘/인기/잘나가/순위/톱클립" 등이 들어간 질문은 생성 대신 트렌드서치로 갑니다.

```
@co-writer 요즘 뭐가 트렌드야?          → 정서축·조합·훅·톱클립 종합
@co-writer 요즘 잘 나가는 조합 뭐야       → 트로프 조합 순위
@co-writer 엔딩은 뭘로 끊는 게 좋아?      → 절단점 유형 순위
@co-writer 후회남 쪽 훅은 어때?          → 카테고리로 좁혀 집계
```

성과지수 = 조회수(로그) × 반응률·저장률 가중. 통합 DB(`data/reference/reference_db.json`, v5)의
`v4_tagged` 편만 집계하고 신뢰도 0.6 미만·표본 부족 축은 경고를 붙입니다. `publish_dt`(실제 게시일)가
2구간 이상 쌓이면 자동으로 상승/하락 추세 비교로 전환됩니다.

### 작품 바이블 (구글 시트)

**입력은 슬랙 봇, 열람은 구글 시트.** `[작품명]`으로 작품을 지정하면 봇이 시트에서 그 작품의
바이블(로그라인·인물·줄거리·기존 화)을 읽어 **PART D 실패방지**(그 화 시점에 안 맞는 캐릭터·
급전개·톤 붕괴 차단)를 적용하고, 생성 결과를 시트에 되저장합니다.

```
@co-writer [작품X] 24화 대본 써줘        → 24화 시점 규칙 적용해 생성 + 시트 24화_대본 저장
@co-writer [작품X] 인물: 연우는 1~38화…   → 생성 없이 시트에 설정 저장
@co-writer [작품X] 로그라인: …            → 로그라인 저장  (타겟/줄거리/회차표 동일)
@co-writer [작품X] 현재 24화              → 진행상태 갱신
새로고침                                 → 시트 바이블 캐시 무효화
```

시트 구분(kind): `현재화·로그라인·타겟정서·인물·줄거리·회차표·N화_개요·N화_대본·기획안`.
업서트 키는 (작품, 구분) — 같은 자리는 최신본으로 덮어씁니다. 시트 미설정이면 바이블 없이 생성만.

설정은 `google_sheet/README.md` 참고 (Apps Script 웹앱 배포 → URL·SECRET을 `.env`에).

### 생성 검증 관문 (3단계 감사)

생성은 규칙을 프롬프트에 넣어도 창작 몰입 중 흘리기 쉽다. 그래서 **생성과 감사를 분리**한다:
`[생성]`으로 개요/대본 초안이 나오면, 생성기와 **다른 감사자 페르소나**가 초안을 바이블 기준으로
1콜 재검한다(체크리스트 6항목 = 금지사항·시점캐릭터·캐릭터붕괴·타임라인·개요준수·설정혼입, 각 PASS/FAIL/NA).
FAIL이 있으면 **그 위반만 최소 교정**한 판본을 1회 더 만들고, 답변 말미에 결과를 남긴다
(`✅ 자동검증 통과` 또는 `🔧 자동검증: N개 항목 교정 (…)`).

- 기본 **ON** (바이블이 있을 때만 작동 — 기준이 없으면 건너뜀).
- 끄기: 요청에 `검증생략`/`빠르게` 붙이거나 (`[생성] <작품> 대본 / 24화 검증생략`), 전역은 `COWRITER_VERIFY_GATE=0`.
- 강도 1~5 일괄 비교(5장)는 과부하라 검증을 돌리지 않는다. 구현: `bot/verify.py`.

### 기타

- 채널: 멘션으로 반응. **스레드가 곧 작업 단위** — 같은 스레드에서 계속 주고받으면 맥락 유지.
- DM: 멘션 없이 동작.
- `@co-writer reload`: 레퍼런스·템플릿·트렌드 DB 갱신분 반영. `새로고침`: 시트 바이블 캐시 무효화.

## 설치

```bash
python3 -m pip install -r requirements.txt
python3 scripts/sync_reference.py       # 레퍼런스 DB 동기화
cp .env.example .env && vi .env         # 슬랙 토큰 2개만 입력
set -a && source .env && set +a
python3 app.py                          # 기동 시 환경 헬스체크 후 시작
```

### Anthropic 인증 — API 키 불필요 (기본)

기본 백엔드(`agent`)는 **이 머신의 Claude Code 팀 로그인을 재사용**합니다
(Claude Agent SDK가 로컬 `claude` CLI를 통해 호출). 사용량은 팀 구독에서 차감됩니다.

**최초 1회**: 터미널에서 CLI 로그인이 필요합니다 —

```bash
claude          # 대화형 실행 → /login → 브라우저에서 팀 계정 로그인
claude -p "ping"   # headless 동작 확인 (응답이 나오면 준비 완료)
```

> 데스크톱 앱으로만 로그인돼 있으면 CLI 쪽 자격증명(키체인)이 비어 있어
> headless 호출이 "Not logged in"이 됩니다 — 위 1회 로그인으로 해결.

API 키로 직접 호출하려면 `.env`에 `COWRITER_BACKEND=api` + `ANTHROPIC_API_KEY`를 설정하세요
(프롬프트 캐싱·adaptive thinking을 쓰는 원 API 경로).

### Slack 앱 설정 (api.slack.com/apps → Create New App → From a manifest)

```yaml
display_information:
  name: co-writer
features:
  bot_user:
    display_name: co-writer
    always_online: true
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - chat:write
      - channels:history
      - groups:history
      - im:history
      - im:read
      - im:write
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.im
  socket_mode_enabled: true
```

1. 앱 생성 후 **App-Level Token** 발급 (`connections:write`) → `SLACK_APP_TOKEN` (xapp-)
2. **Install to Workspace** → `SLACK_BOT_TOKEN` (xoxb-)
3. 사용할 채널에 `/invite @co-writer`

## 구조

```
app.py                  # Slack 이벤트 핸들러 (Socket Mode, 무상태 — 스레드를 매번 다시 읽음)
bot/tool_router.py      # 자연어 → native tool call / 답변 / 명확화
bot/tool_router_slack.py # 검증 후 즉시 실행·진행 중단 버튼
bot/tool_registry.py    # 실행 함수 whitelist·스키마·검증·위험도·실행 adapter
bot/config.py           # 환경변수
bot/reference.py        # 레퍼런스 DB·패턴·템플릿·시트 바이블 로드
bot/retrieval.py        # 한국어 키워드 → v5 통합 태그(catharsis·trope 등) → 유사 사례 2~3편 선별
bot/prompts.py          # 시스템 프롬프트 조립 (고정부는 프롬프트 캐싱)
bot/generator.py        # 생성 백엔드 (agent=Claude Code 구독 재사용 / api=Anthropic SDK)
bot/trend_search.py     # 통합 DB의 v4_tagged 편 성과 가중 트렌드 집계
scripts/sync_reference.py  # story-v1-scripts/reference → data/reference 동기화 (수동)
data/reference/         # sync된 정제 DB(v5)·패턴·템플릿 사본 — 봇이 읽는 것
templates/              # 사내 템플릿 자리 (*.md를 넣으면 자동 주입)
```

자연어 실행 경계와 고위험 확인 정책은 [`ARCHITECTURE.md`](ARCHITECTURE.md)를 참고한다.
자연어는 intent로 분류한 뒤 명령어로 재변환하지 않는다. 모델은 등록된 함수만 직접 호출하고
복합 요청은 순서 있는 함수 계획으로 반환한다. 코드는 모든 인자와 첨부 ID를 먼저 검증하고
즉시 실행하며, 진행 카드의 중단 버튼은 정확한 실행 ID가 일치할 때만 현재 작업을 멈춘다.

## 운영 메모

- **레퍼런스 갱신 흐름** (위 "데이터가 어디서 오나" 3단계): crawler 로우데이터 커밋 → ① scripts에서 LLM 재태깅 배치(수동) → ② `python3 scripts/sync_reference.py`(수동) → ③ 슬랙 `@co-writer reload`. 봇은 자기 사본을 읽으므로 scripts만 갱신하고 ②·③을 안 돌리면 반영 안 됨.
- 시스템 프롬프트 고정부(역할+패턴+템플릿)에 프롬프트 캐시 브레이크포인트 — 같은 5분 안 반복 호출은 ~90% 저렴.
- 봇 산출물 검증: 생성된 기획안의 정서축(catharsis)·훅·절단점이 패턴 문서의 실측 유형과 일치하는지 작가가 확인하는 것이 리뷰 포인트.
