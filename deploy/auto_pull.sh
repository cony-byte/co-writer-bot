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
  echo "[$(date '+%F %T')] 변경 감지 $BEFORE → $AFTER, 재설치·재시작" >> "$LOG"
  python3 -m pip install -q -r requirements.txt >> "$LOG" 2>&1 || true
  launchctl kickstart -k "gui/$(id -u)/$LABEL" >> "$LOG" 2>&1 || true
else
  echo "[$(date '+%F %T')] 변경 없음" >> "$LOG"
fi
