# -*- coding: utf-8 -*-
"""환경 설정. 모든 값은 환경변수로 주입 (.env.example 참고)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")  # Socket Mode (xapp-)

MODEL = os.environ.get("COWRITER_MODEL", "claude-opus-4-8")
MAX_TOKENS = int(os.environ.get("COWRITER_MAX_TOKENS", "16000"))

# 레퍼런스 DB — story-v1-scripts repo의 reference/ 디렉터리.
# 기본값: 이 repo에 동기화된 사본(data/reference). scripts/sync_reference.py로 갱신.
REFERENCE_DIR = Path(os.environ.get("COWRITER_REFERENCE_DIR", BASE_DIR / "data" / "reference"))

# 사내 작가 템플릿 (기획안/대본) — 템플릿화 작업은 별도 트랙에서 진행.
# 이 디렉터리에 *.md 파일이 생기면 자동으로 시스템 프롬프트에 주입된다.
TEMPLATES_DIR = Path(os.environ.get("COWRITER_TEMPLATES_DIR", BASE_DIR / "templates"))

# 스레드 히스토리를 몇 메시지까지 모델에 넘길지
THREAD_HISTORY_LIMIT = int(os.environ.get("COWRITER_THREAD_LIMIT", "40"))
