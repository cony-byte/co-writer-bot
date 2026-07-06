# co-writer-bot

숏폼 로맨스 드라마 **보조 작가 슬랙 에이전트**.
목표: 작가가 3일에 1편 쓰던 기획안·대본을 **1일 1편 이상**으로 — 초안 생산과 반복 수정은 봇이, 판단은 작가가.

## 뭘 근거로 쓰나

| 입력 | 출처 | 상태 |
|---|---|---|
| 패턴 요약 (훅 5유형·절단점 5유형·트로프 분포) | [story-v1-scripts](https://github.com/cony-byte/story-v1-scripts) `reference/patterns/` | ✅ 연동 |
| 유사 사례 2~3편 (정제 대본 포함) | 같은 repo `reference/reference_db.json` (drama_clip 76편, 스키마 v3) | ✅ 연동 |
| 사내 작가 기획안·대본 템플릿 | `templates/*.md` | ⏳ 별도 트랙에서 템플릿화 후 투입 |

생성 프롬프트에는 원본 76편이 아니라 **패턴 요약 + 요청과 유사한 사례 2~3편**만 들어간다 (레퍼런스 DB 설계 원칙).

## 슬랙 사용법

```
@co-writer 기획안: 재벌 남주 x 계약결혼, 오피스, 정체 숨김
  → 로그라인 / 훅 설계(첫 3초) / 절단점 설계 / 회차 구성

(스레드에서) @co-writer 3화 절단점을 정체 폭로 직전 컷으로 바꿔줘
  → 해당 부분만 고친 수정본

(스레드에서) @co-writer 대본: 1화
  → ML/FL/SUP/NAR 화자 표기 대본, 훅 대사로 시작 → 절단점 대사로 끝
```

- 채널: 멘션으로 반응. **스레드가 곧 작업 단위** — 같은 스레드에서 계속 주고받으면 맥락 유지.
- DM: 멘션 없이 동작.
- `@co-writer reload`: 레퍼런스·템플릿 갱신분 반영 (재시작 불필요).

## 설치

```bash
pip install -r requirements.txt
python3 scripts/sync_reference.py       # 레퍼런스 DB 동기화

# Anthropic 인증 — 팀 클로드(구독)로 실행 (API 키 불필요)
brew install anthropics/tap/ant
xattr -d com.apple.quarantine "$(brew --prefix)/bin/ant"
ant auth login                          # 브라우저에서 팀 계정 로그인 → 프로필 저장
ant auth status                         # 어떤 자격증명이 잡혔는지 확인

cp .env.example .env && vi .env         # 슬랙 토큰 2개만 입력
set -a && source .env && set +a
python3 app.py                          # 기동 시 자격증명 헬스체크 후 시작
```

### 팀 클로드 인증 주의사항

- **`ANTHROPIC_API_KEY`를 설정하지 마세요** — 빈 값(`""`)이라도 설정돼 있으면 OAuth 프로필보다 우선돼 인증이 깨집니다. 어딘가에서 export되고 있다면 `unset ANTHROPIC_API_KEY`.
- 갱신 토큰은 언젠가 만료됩니다 — 봇이 인증 오류로 죽으면 `ant auth login`을 다시 실행하고 재기동하면 됩니다.
- 계정이 여러 워크스페이스에 걸쳐 있으면 `ant auth login --profile cowriter`로 전용 프로필을 만들고 `ANTHROPIC_PROFILE=cowriter`로 지정할 수 있습니다.

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
bot/config.py           # 환경변수
bot/reference.py        # 레퍼런스 DB·패턴·템플릿 로드
bot/retrieval.py        # 한국어 키워드 → v3 태그 → 유사 사례 2~3편 선별
bot/prompts.py          # 시스템 프롬프트 조립 (고정부는 프롬프트 캐싱)
bot/generator.py        # Claude API (claude-opus-4-8, adaptive thinking, streaming)
scripts/sync_reference.py  # story-v1-scripts → data/reference 동기화
templates/              # 사내 템플릿 자리 (*.md를 넣으면 자동 주입)
```

## 운영 메모

- **레퍼런스 갱신 흐름**: 크롤러 배치 → story-v1-scripts `reference/` 갱신 → `sync_reference.py` → 슬랙에서 `reload`.
- 시스템 프롬프트 고정부(역할+패턴+템플릿)에 프롬프트 캐시 브레이크포인트 — 같은 5분 안 반복 호출은 ~90% 저렴.
- 봇 산출물 검증: 생성된 기획안의 story_type·훅·절단점이 패턴 문서의 실측 유형과 일치하는지 작가가 확인하는 것이 리뷰 포인트.
