#!/bin/zsh
# co-writer-bot 실행 래퍼 (launchd에서 호출). .env 로드 후 봇 실행.
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
set -a
[ -f .env ] && source .env
set +a
exec python3 app.py
