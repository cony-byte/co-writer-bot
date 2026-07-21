# co-writer-bot 작업 규칙

## 라우터 변경 = 머지 게이트 필수 (2026-07-21)

자연어 라우터(`bot/tool_router.py`, `bot/tool_registry.py`, 라우터 시스템 프롬프트,
`bot/nl_router.py`의 `_build_context`)를 바꿨으면 **머지 전에 반드시** 머지 게이트를
CI처럼 돌린다:

```
set -a && source .env && set +a
python3 -m tests.merge_gate            # 손 코퍼스 + 실제 로그 리플레이
python3 -m tests.merge_gate --with-migrated   # 이관 시드 코퍼스까지(검수 완료분)
```

**회귀가 0이 아닌 변경은 diff 리포트 첨부 없이 머지 금지.** 회귀가 났는데도 의도된
변경이라면, 무엇이 왜 바뀌는지(리플레이/코퍼스 diff)를 커밋/PR에 첨부해야 한다.

### 왜 이 규칙이 있나
라우터가 nl_router → tool_router로 통째로 바뀌면서, 과거의 회귀 코퍼스가 죽은 코드
대상이 돼 안전망이 사라진 적이 있다. 그 뒤로 실사용 오라우팅이 날 때마다 개별 수정만
반복하고, 한 수정이 다른 케이스를 조용히 깨뜨려도 아무도 몰랐다. 이 게이트가 그걸 막는다.

### 자기성장형 평가 파이프라인 구성요소
- `bot/router_log.py` — 모든 인바운드 라우터 결정을 `logs/router_decisions.jsonl`에 1줄씩
  기록(ctx_snapshot 포함 → 재현성). 라우팅을 절대 안 깨는 게 원칙(모든 I/O try/except).
- `bot/router_labeler.py` — 결정 로그에서 오라우팅 의심 결정을 자동 라벨링해
  `logs/review_queue.jsonl`로. `python3 -m bot.router_labeler`.
- `tests/replay.py` — 로그의 (발화, ctx_snapshot)을 현재 라우터로 재실행해 로그된 결정과
  diff(개선/회귀/변경). 회귀>0이면 exit 1. `python3 -m tests.replay --days 3`.
- `tests/test_tool_router.py` + `tests/tool_router_corpus.json` — 손으로 고정한 회귀 코퍼스.
  새 오라우팅을 발견하면 **케이스를 먼저 추가**하고 고친다.
- `bot/router_report.py` — 주간 지표(오라우팅률/safe_stop률/백엔드 지연) Slack 게시.

### 라우팅 설계 원칙 (반복 사고에서 배운 것)
- **상태/존재 질문은 추측 금지, 사실로 답한다.** 등록 여부는 `registered_elements`,
  생성물(스틸컷/영상) 존재는 `answer_sources.generated_artifacts`만 근거로. 근거가 없으면
  "확인 안 된다"고 답하지 지어내지 않는다.
- **명시적 부정 제약을 삼키지 않는다.** "임의로 생성하지 말고" 같은 요청은 조용히
  자유생성으로 폴백하면 안 된다 — 못 하면 안내하고 멈춘다.
- **결정적으로 처리할 수 있는 건 LLM에 맡기지 않는다**(짧은 긍정 "응"은 버튼 UI로만,
  라벨 매칭은 실행부 코드로).
