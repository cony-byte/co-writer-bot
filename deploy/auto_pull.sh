#!/bin/zsh
# 주기적으로 origin 최신 커밋을 받아오고, 변경이 있으면 봇을 재시작한다.
# launchd(ai.tain.<bot>-autopull)가 일정 주기로 호출. 개발은 다른 머신에서만 하고
# 이 머신은 오직 pull + 재시작만 한다는 정책 — 로컬에서 직접 커밋하지 않는 걸 전제로
# --ff-only만 씀(로컬 커밋이 있으면 그건 실수이므로 조용히 넘기지 않고 에러 로그로 남김).
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

LABEL="ai.tain.co-writer-bot"
LOG="logs/autopull.log"
mkdir -p logs

# ── 생성 진행 중이면 이번 주기 건너뜀(재시작이 진행 중 생성을 끊지 않도록) ──
# (2026-07-16 재설계, HANDOFF_봇병합.md §3-5 리스크 4) 예전엔 두 원장(inflight.json=요청
# 처리중, jobs.json=이미지/스틸 생성중)이 '비어있지 않고 최근(<=900s) 갱신'됐으면 바쁨으로
# 봤다. 이 900s는 jobs.json에 실제로 아무도 안 써서(죽은 검사) 그동안은 문제가 안 됐지만,
# job_ledger.py가 storyboard의 pending_jobs() 재생 모델로 교체되며 진짜로 쓰이기 시작한다
# (job_ledger.py 상단 docstring 참고) — 그러면 900s 컷오프가 실제 버그가 된다: 자동주행처럼
# 여러 씬을 이어서 처리하는 체인은 20분 넘게 걸릴 수 있는데(.env의 SB_AGENT_TIMEOUT=600s는
# LLM 호출 "1회"의 최대 대기일 뿐, 자동주행은 이걸 씬마다 여러 번 + 이미지/영상 생성까지
# 순차로 거침), 파일 mtime이 900s보다 오래되면 "진짜 진행 중"인데도 안 바쁨으로 오판해 생성
# 도중 재시작해버린다.
#
# 핵심 질문을 "파일이 최근에 갱신됐나"에서 "이 파일들을 쓰는 프로세스가 지금 살아있나"로
# 바꾼다: 살아있는 프로세스만 이 파일들을 유효하게 갱신할 수 있으므로, 프로세스가 살아있고
# 파일에 내용이 있으면 나이 체크 없이 그 자체로 신뢰한다. 프로세스가 안 살아있으면(크래시·
# 정지) 파일 내용/나이와 무관하게 안 바쁨으로 본다 — 지금 실제로 진행 중인 게 없고, 재시작이
# 곧 복구 수단(새 프로세스가 뜨면 _replay_inflight/_resume_pending_jobs가 남은 기록을 정리·
# 재개/안내한다). launchctl 자체가 판정 불가(라벨 미등록 등, 개발 환경)면 훨씬 긴 age
# fallback(아래 참고)으로 후퇴 — 이 경우에만 나이를 본다.
_liveness() {
  # 반환: 0=러닝 PID 확인됨(살아있음) / 1=라벨은 있으나 PID 없음(정지·크래시) /
  #       2=판정 불가(launchctl 실패·라벨 미등록 — 라이브니스 신뢰 못 함)
  local out
  out=$(launchctl list "$LABEL" 2>/dev/null) || return 2
  [ -n "$out" ] || return 2
  if echo "$out" | grep -q '"PID" = [0-9]'; then
    return 0
  fi
  return 1
}

_busy() {
  local f content age live
  _liveness
  live=$?

  if [ "$live" -eq 2 ]; then
    # 라이브니스 판정 불가 — 살아있는지 확실치 않으니 예전처럼 무조건 안 바쁨/바쁨으로
    # 단정하지 않고 훨씬 넉넉한 age fallback으로 후퇴한다. 근거: COWRITER_AGENT_TIMEOUT/
    # SB_AGENT_TIMEOUT=600s(.env, LLM 호출 1회 최대 대기) — 자동주행 체인은 이걸 씬마다
    # 여러 번 + 이미지/영상 생성까지 거치므로 한 사이클 전체는 600s를 몇 배 넘길 수 있다.
    # 그 현실적 최악치를 넉넉히 덮도록 3600s(1시간, 옛 900s의 4배)로 잡음 — 무한 지연은
    # 여전히 방지하면서 진짜 장시간 작업은 안 끊는 절충.
    for f in data/inflight.json data/jobs.json; do
      [ -f "$f" ] || continue
      content=$(tr -d '[:space:]' < "$f" 2>/dev/null || true)
      [ -z "$content" ] && continue
      [ "$content" = "{}" ] && continue
      [ "$content" = "[]" ] && continue
      age=$(( $(date +%s) - $(stat -f %m "$f" 2>/dev/null || echo 0) ))
      [ "$age" -le 3600 ] && return 0
    done
    return 1
  fi

  if [ "$live" -ne 0 ]; then
    return 1   # 프로세스 안 살아있음 — 파일 내용/나이 무관 안 바쁨(재시작이 곧 복구 수단)
  fi

  # 프로세스가 살아있음 — 두 원장 중 하나라도 비어있지 않으면 나이 체크 없이 '작업 중'.
  for f in data/inflight.json data/jobs.json; do
    [ -f "$f" ] || continue
    content=$(tr -d '[:space:]' < "$f" 2>/dev/null || true)
    [ -z "$content" ] && continue
    [ "$content" = "{}" ] && continue
    [ "$content" = "[]" ] && continue
    return 0
  done
  return 1
}
if _busy; then
  echo "[$(date '+%F %T')] 생성 진행 중 — 이번 주기 건너뜀(pull·재시작 안 함)" >> "$LOG"
  exit 0
fi

BEFORE=$(git rev-parse HEAD)
{
  echo "[$(date '+%F %T')] pull 시작"
  if ! git pull --ff-only origin master; then
    echo "[$(date '+%F %T')] pull 실패(로컬 커밋과 충돌 가능성) — 수동 확인 필요"
    exit 1
  fi
} >> "$LOG" 2>&1
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" != "$AFTER" ]; then
  echo "[$(date '+%F %T')] 변경 감지 $BEFORE → $AFTER, 검증 중…" >> "$LOG"
  python3 -m pip install -q -r requirements.txt >> "$LOG" 2>&1 || true

  # ── 배포 전 검증: import만 시도 — 새 파일 git add 누락(2026-07-14 사고) 같은
  # ImportError를 여기서 잡는다. 실패하면 재시작을 건너뛰어 "기존(정상) 프로세스가
  # 계속 서비스"하게 하고, git HEAD만 깨진 커밋을 가리킨 채 다음 pull까지 대기.
  # (Slack 연결·스레드는 전부 __main__ 가드 안에 있어 단순 import는 부작용 없음.)
  # perl alarm+exec: macOS 기본 내장 perl로 타임아웃 구현(timeout 커맨드 미보장 대응).
  if ! ( set -a; [ -f .env ] && source .env; set +a
         perl -e 'alarm shift; exec @ARGV' 30 python3 -c "import app" ) >> "$LOG" 2>&1; then
    echo "[$(date '+%F %T')] ❌ 새 코드 import 검증 실패 — 재시작 취소(기존 프로세스 계속 서비스). HEAD=$AFTER 확인 필요" >> "$LOG"
    exit 1
  fi
  echo "[$(date '+%F %T')] 검증 통과 — 재시작" >> "$LOG"
  launchctl kickstart -k "gui/$(id -u)/$LABEL" >> "$LOG" 2>&1 || true
else
  echo "[$(date '+%F %T')] 변경 없음" >> "$LOG"
fi
