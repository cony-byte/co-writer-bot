# -*- coding: utf-8 -*-
"""환경 설정. 모든 값은 환경변수로 주입 (.env.example 참고)."""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")  # Socket Mode (xapp-)

# 백엔드: "agent" = Claude Agent SDK(이 머신의 Claude Code 팀 로그인 재사용, 키 불필요)
#         "api"   = Anthropic SDK 직접 호출(API 키 또는 ant 프로필 필요)
BACKEND = os.environ.get("COWRITER_BACKEND", "agent")

MODEL = os.environ.get("COWRITER_MODEL", "claude-opus-4-8")       # api 백엔드용
AGENT_MODEL = os.environ.get("COWRITER_AGENT_MODEL", "claude-sonnet-5")  # agent 백엔드용 (Sonnet 고정)
MAX_TOKENS = int(os.environ.get("COWRITER_MAX_TOKENS", "16000"))
AGENT_TIMEOUT = int(os.environ.get("COWRITER_AGENT_TIMEOUT", "150"))  # agent 생성 최대 대기(초)
# agent 백엔드 최대 턴 수. 프롬프트 크기와 무관하게도 'max turns' 에러가 자주 나서 상향(2026-07-13).
AGENT_MAX_TURNS = int(os.environ.get("COWRITER_AGENT_MAX_TURNS", "20"))

# 생성 검증 관문(3단계 감사) 기본 ON. 끄려면 COWRITER_VERIFY_GATE=0.
# 개별 요청은 '검증생략'/'빠르게' 플래그로 끄거나 '검증'으로 켤 수 있음.
VERIFY_GATE = os.environ.get("COWRITER_VERIFY_GATE", "1") != "0"

# 레퍼런스 DB — story-v1-scripts repo의 reference/ 디렉터리 (통합 DB v5: reference_db.json).
# 사례 선별(retrieval)과 트렌드서치(v4_tagged 편)가 같은 단일 DB를 읽는다.
# 기본값: 이 repo에 동기화된 사본(data/reference). scripts/sync_reference.py로 갱신.
REFERENCE_DIR = Path(os.environ.get("COWRITER_REFERENCE_DIR", BASE_DIR / "data" / "reference"))

# 사내 작가 템플릿 (기획안/대본) — 템플릿화 작업은 별도 트랙에서 진행.
# 이 디렉터리에 *.md 파일이 생기면 자동으로 시스템 프롬프트에 주입된다.
TEMPLATES_DIR = Path(os.environ.get("COWRITER_TEMPLATES_DIR", BASE_DIR / "templates"))

# 대본 확정 저장 시 생성되는 흐름 요약 캐시(회차 연속성 참고용, 시트에는 없는 로컬 전용 데이터).
SCRIPT_SUMMARIES_PATH = BASE_DIR / "data" / "script_summaries.json"

# 노션에서 직접 읽은 대본 캐시(page last_edited 기준 무효화 — 안 바뀌었으면 풀 페치 생략).
NOTION_SCRIPTS_CACHE_PATH = BASE_DIR / "data" / "notion_scripts_cache.json"

# 스레드 히스토리를 몇 메시지까지 모델에 넘길지
THREAD_HISTORY_LIMIT = int(os.environ.get("COWRITER_THREAD_LIMIT", "40"))

# 구글 시트 스토리 바이블 — 입력은 슬랙 봇, 열람은 시트. Apps Script 웹앱(google_sheet/Code.gs) 경유.
# URL·SECRET 둘 다 설정돼야 바이블 기능 활성. 없으면 봇은 바이블 없이 동작(패턴·사례 기반 생성만).
SHEET_WEBAPP_URL = os.environ.get("SHEET_WEBAPP_URL", "")
SHEET_SECRET = os.environ.get("SHEET_SECRET", "")
SHEET_CACHE_TTL = int(os.environ.get("COWRITER_SHEET_TTL", "60"))  # 초 (기본 1분, 2026-07-13: 5분→1분 —
# 대본이 이 캐시 안에서 노션 직접 읽기(_notion_scripts)를 하므로, 이 값이 노션 대본 수정 반영
# 지연의 상한이기도 함. 짧게 잡을수록 시트/노션 조회가 그만큼 자주 일어남(호출당 ~2초).

# 노션 통합(읽기 전용) — [동기화]가 이 토큰으로 기획안 페이지를 직접 읽어 시트에 반영.
# NOTION_PAGES: 작품명 → 페이지ID 매핑(JSON). 예: {"날혐남":"679beda6e49082b6963d01ddbc5c24a4"}
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
try:
    NOTION_PAGES = json.loads(os.environ.get("NOTION_PAGES", "{}"))
except Exception:
    NOTION_PAGES = {}

# OpenRouter 이미지 생성 — 상세 콘티 → GPT 이미지(9:16)로 스토리보드 스틸 생성.
# Unified Image API: POST https://openrouter.ai/api/v1/images (data[].b64_json)
# 키 없으면 이미지 기능 비활성(콘티까지만 동작).
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_IMAGE_MODEL = os.environ.get("OPENROUTER_IMAGE_MODEL", "openai/gpt-image-2")  # = "GPT 이미지 2.0"
OPENROUTER_IMAGE_ASPECT = os.environ.get("OPENROUTER_IMAGE_ASPECT", "9:16")
# 스토리보드 그리드 패널 비율 (예시 콘택트시트가 가로형이라 기본 16:9). 바꾸려면 이 값만.
OPENROUTER_PANEL_ASPECT = os.environ.get("OPENROUTER_PANEL_ASPECT", "16:9")
OPENROUTER_GRID_COLS = int(os.environ.get("OPENROUTER_GRID_COLS", "6"))     # 그리드 열 수
OPENROUTER_IMG_WORKERS = int(os.environ.get("OPENROUTER_IMG_WORKERS", "4"))  # 이미지 병렬 생성 수
OPENROUTER_IMG_TIMEOUT = int(os.environ.get("OPENROUTER_IMG_TIMEOUT", "600"))  # 이미지 1장 HTTP 대기(초) — 넉넉히
OPENROUTER_LLM_MODEL = os.environ.get("OPENROUTER_LLM_MODEL", "anthropic/claude-sonnet-4.5")  # 컷 분해 등 LLM(HTTP)
# 캐릭터 일관성용 참조 이미지 폴더: <refs>/<작품>/<인물>.(png|jpg|jpeg|webp)
# 여기 넣어두면 그 인물이 나오는 컷 생성 시 input_references(data URL)로 자동 첨부됨.
OPENROUTER_REFS_DIR = Path(os.environ.get("OPENROUTER_REFS_DIR", BASE_DIR / "data" / "refs"))
