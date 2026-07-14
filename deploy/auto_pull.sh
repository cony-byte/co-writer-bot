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
# 진행중 작업 원장(inflight.json=요청 처리중, jobs.json=이미지/스틸 생성중)이 '비어있지
# 않고' 최근(<=900s) 갱신됐으면 생성 중으로 보고 pull·재시작을 미룬다. 다음 주기에 재시도.
# 900s 초과분은 죽은 프로세스가 남긴 잔재로 보고 무시(무한 지연 방지).
_busy() {
  local f content age
  for f in data/inflight.json data/jobs.json; do
    [ -f "$f" ] || continue
    content=$(tr -d '[:space:]' < "$f" 2>/dev/null || true)
    [ -z "$content" ] && continue
    [ "$content" = "{}" ] && continue
    [ "$content" = "[]" ] && continue
    age=$(( $(date +%s) - $(stat -f %m "$f" 2>/dev/null || echo 0) ))
    [ "$age" -le 900 ] && return 0
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
