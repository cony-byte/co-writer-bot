# reference/ — 생성 파이프라인용 레퍼런스 DB (스키마 v3)

> `data/library.json`(뷰어용)과 **별도**의 사이드카. build.py 리빌드가 library.json 레코드를
> CSV 필드셋으로 통째로 교체하므로 정제 필드를 library.json에 넣으면 유실된다 — 여기가 정제 레이어의 SSOT.

## 파일

| 파일 | 내용 |
|---|---|
| `reference_db.json` | **drama_clip 76편만.** 정제 대본 + v3 재태깅 + hook_desc + 발행시각 + 화자중립 + 장면 |
| `excluded.json` | 비본편 24편 (trailer_recap 15 / fan_edit 4 / bts 3 / movie_clip 1 / other 1) — 태그 통계 오염 방지용 격리. 정제 안 함 |
| `태깅프롬프트_v3.md` | transcript 정제·재태깅·hook_desc 프롬프트 SSOT (다음 배치 재실행용) |
| `patterns/` | story_type별 패턴 요약 — **생성 파이프라인 프롬프트에 들어가는 건 원본 76편이 아니라 이 요약 + 유사 사례 2~3편** |
| `enrich.py` | reference_db 후처리 — 발행시각(id→시각)·화자중립화·장면(CSV 컷) 병합 |
| `filters.json` | **내부 뷰어 필터 설정 — 여기만 편집.** auto 칩(자동) + keyword_groups(사용자 정의) + sort |
| `viewer_template.html` | 내부 뷰어 UI 템플릿 (`/*__PAYLOAD__*/null` 자리에 데이터 주입) |
| `build_viewer.py` | reference_db + filters → 뷰어 HTML (평문 개발 / AES 암호화 배포) |
| `viewer.html` | 평문 개발 빌드 (gitignore — 전체 대본 노출, 커밋 금지) |

## 내부 뷰어 (사내 전용, 비밀번호 게이트)

`data/`의 공개 라이브러리 뷰어(`docs/index.html`, rebuild-library가 관리)와 **별개**. 정제된 76편을 대본·화자·장면·발행시각까지 보여주는 내부 도구.

```bash
python3 reference/enrich.py --csv <system repo 로우데이터 CSV>...   # 데이터 강화(발행시각·화자·장면)
python3 reference/build_viewer.py                                    # 로컬 미리보기(구운 단일파일) → reference/viewer.html
python3 reference/build_viewer.py --deploy                           # Cloudflare 배포(데이터 분리형) → cloudflare/index.html + data.json
```

**발행 = Cloudflare Pages + Cloudflare Access** (2026-07-06 확정). 세팅·갱신 절차는 [`cloudflare/README.md`](../cloudflare/README.md).
- **데이터 분리형**: `index.html`(껍데기)이 `data.json`을 런타임 fetch → 데이터만 바꾸면 사이트 갱신. Cloudflare Build command를 걸면 push마다 자동 재빌드("계속 갱신").
- 접근 제어는 **Cloudflare Access**(문 앞 이메일/SSO 인증)가 담당 → 사람별 초대·즉시 차단·감사 로그. 파일은 **평문**이면 충분(Access가 서빙 전 차단).
- repo는 private이라 `reference/*.json` 원본도 비노출. 배포 디렉터리는 `cloudflare/` (docs/ 공개 뷰어와 분리).
- (선택) 이중 잠금이 필요하면 `build_viewer.py --password '비번'` → `data.json`이 AES-256-GCM 암호문이 되고 뷰어가 비번 입력 시 복호화.

### 뷰어 반영 요구사항 (2026-07-06)
- 후킹 태그 필터 제거 / 전체 대본 표시(화자1·화자2·나레이션 중립 구분 + role_hint 보존) / 컷 타임라인(시간·샷·인물) / 발행시각(id 인코딩 추출, 트렌드↔고전 구분) / filters.json 기반 키워드 필터.
- **전체 스크립트 한계**: 76편 중 정제 대본 38편뿐. 나머지는 시드 배치가 whisper STT를 못 잡음(무발화 또는 오디오 수집 실패). 전체 대본·줄별 타임스탬프·진짜 화자분리는 **오디오 재크롤링** 후 채워진다.

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
