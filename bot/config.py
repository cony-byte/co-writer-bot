# -*- coding: utf-8 -*-
"""환경 설정. 모든 값은 환경변수로 주입 (.env.example 참고)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")  # Socket Mode (xapp-)

# 백엔드: "agent" = Claude Agent SDK(이 머신의 Claude Code 팀 로그인 재사용, 키 불필요)
#         "api"   = Anthropic SDK 직접 호출(API 키 또는 ant 프로필 필요)
BACKEND = os.environ.get("COWRITER_BACKEND", "agent")

MODEL = os.environ.get("COWRITER_MODEL", "claude-opus-4-8")       # api 백엔드용
AGENT_MODEL = os.environ.get("COWRITER_AGENT_MODEL", "")           # agent 백엔드용 ("" = Claude Code 기본)
MAX_TOKENS = int(os.environ.get("COWRITER_MAX_TOKENS", "16000"))

# 레퍼런스 DB — story-v1-scripts repo의 reference/ 디렉터리 (통합 DB v5: reference_db.json).
# 사례 선별(retrieval)과 트렌드서치(v4_tagged 편)가 같은 단일 DB를 읽는다.
# 기본값: 이 repo에 동기화된 사본(data/reference). scripts/sync_reference.py로 갱신.
REFERENCE_DIR = Path(os.environ.get("COWRITER_REFERENCE_DIR", BASE_DIR / "data" / "reference"))

# 사내 작가 템플릿 (기획안/대본) — 템플릿화 작업은 별도 트랙에서 진행.
# 이 디렉터리에 *.md 파일이 생기면 자동으로 시스템 프롬프트에 주입된다.
TEMPLATES_DIR = Path(os.environ.get("COWRITER_TEMPLATES_DIR", BASE_DIR / "templates"))

# 스레드 히스토리를 몇 메시지까지 모델에 넘길지
THREAD_HISTORY_LIMIT = int(os.environ.get("COWRITER_THREAD_LIMIT", "40"))
