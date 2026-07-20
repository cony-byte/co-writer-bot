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
# visual-pipeline 프로젝트 루트(인물/장소/의상 fixed-images + outputs/stills·videos 공유) —
# ★2026-07-16(Phase 6 컷오버 후 실측 버그): storyboard-bot/bot/config.py에는 있었는데 Phase 3
# 병합 때 이 파일에서 누락돼, config.FIXED_IMAGES_ROOT를 읽는 openrouter_image._vp_project_dir가
# 항상 AttributeError 없이 그냥 None을 반환(getattr(config, "FIXED_IMAGES_ROOT", None) 폴백)해서
# 얼굴/의상 참조 이미지가 하나도 안 붙은 채 스틸컷이 생성되고 있었다(사용자 실측: "인물 참조
# 이미지 붙고 있어?" → 확인해보니 전혀 안 붙음). storyboard-bot과 동일하게 추가.
FIXED_IMAGES_ROOT = Path(os.environ.get(
    "FIXED_IMAGES_ROOT", str(BASE_DIR / "data" / "projects")))

# ============================================================================
# (Phase 3 merge-time addition, 2026-07-16) storyboard-bot-only config constants.
# Additive only -- nothing above this line was removed or changed. Brought over
# verbatim (values/env-var-names/comments unchanged) from storyboard-bot/bot/config.py
# because dispatch_storyboard.py + the storyboard-only modules already copied into
# bot/ (higgsfield_image.py, higgsfield_video.py, openrouter_video.py, openrouter_music.py,
# openrouter_tts.py, storyboard_grid.py, episode_compile.py, sb_generator.py, etc.)
# reference these names on `config.*` and raised AttributeError at import time without them.
#
# RESOLVED (2026-07-16, Phase 4 constant audit): OPENROUTER_IMAGE_ASPECT defaulted to "9:16"
# in co-writer's config (this file, above) vs "16:9" in storyboard-bot's. Traced every real
# call site of openrouter_image.generate() across both dispatch_cowriter.py and
# dispatch_storyboard.py (incl. the grid/_do_images path, which explicitly passes
# `aspect_ratio=config.OPENROUTER_PANEL_ASPECT`, and the stillcut path, which explicitly
# passes `aspect_ratio=STILL_ASPECT` = "9:16") -- EVERY current call site passes an explicit
# aspect_ratio, so config.OPENROUTER_IMAGE_ASPECT is only ever read as generate()'s internal
# last-resort fallback (openrouter_image.py's `ar = aspect_ratio or config.OPENROUTER_IMAGE_ASPECT`)
# when literally nothing else specifies one -- not currently reachable in practice, but should
# still default sensibly for any future direct call. Kept co-writer's "9:16": this pipeline's
# product is vertical short-form drama (mobile/세로 포맷, matches STILL_ASPECT and the final
# compiled-video canvas 1080x1920) -- storyboard-bot's "16:9" default here was very likely a
# leftover from before OPENROUTER_PANEL_ASPECT existed as its own dedicated grid-aspect
# constant. No code change needed (this file's original co-writer default already had the
# thing worth keeping) -- this comment replaces the earlier "flagged, unresolved" note.
# ============================================================================

# ★2026-07-15: 상세 콘티(2단계)를 화 전체 한 번의 호출로 뽑으면 컷 수가 많은 화·구도헤더 등
# 요구사항이 늘어난 뒤로는 5분 타임아웃에 자주 걸려서(사용자 지적) 씬 단위로 쪼개 병렬 호출하게
# 바꿈 — 이 워커 수는 동시에 진행할 씬 개수.
CONTI_SCENE_WORKERS = int(os.environ.get("SB_CONTI_SCENE_WORKERS", "3"))

# quality 미지정 시 provider가 high(장당 ~$0.21)로 갈 수 있어 기본을 낮게 고정(크레딧 절약).
# auto|low|medium|high. 필요하면 .env에서 상향.
OPENROUTER_IMAGE_QUALITY = os.environ.get("OPENROUTER_IMAGE_QUALITY", "low")
# OpenAI gpt-image 계열 안전필터 강도: auto(기본, 연령부적절 콘텐츠 표준 필터) | low(완화).
# 학교폭력/대치 장면 등에서 safety_violations 400 거부가 잦아 low로 낮춤.
OPENROUTER_IMAGE_MODERATION = os.environ.get("OPENROUTER_IMAGE_MODERATION", "low")

# ── OpenRouter 영상(image-to-video, /api/v1/videos) — 2026-07-13 신규 ──
# OpenRouter가 seedance 2.0을 계정 활성화 없이 그냥 노출(Higgsfield는 계정별 활성화 필요해 막혀있었음).
# 모델: bytedance/seedance-2.0(정속) / bytedance/seedance-2.0-fast(저가·속도 우선).
OPENROUTER_VIDEO_MODEL = os.environ.get("OPENROUTER_VIDEO_MODEL", "bytedance/seedance-2.0-fast")
OPENROUTER_VIDEO_POLL_INTERVAL = int(os.environ.get("OPENROUTER_VIDEO_POLL_INTERVAL", "10"))
OPENROUTER_VIDEO_TIMEOUT = int(os.environ.get("OPENROUTER_VIDEO_TIMEOUT", "900"))
# seedance 내장 generate_audio(대사·효과음 자동생성) 스위치.
# ★2026-07-15: 기본값을 True로 전환 — 사용자가 실제 프로덕션 파이프라인(실제 씬 스틸컷 기반,
# 합성 테스트에 쓴 좁은 인물 크롭과 달리 더 넓은/다인물 프레이밍이라 안전필터를 더 안정적으로
# 통과해온 히스토리가 있음)으로 슬랙에서 직접 라이브 테스트하기 위해 켬. 과거 안전필터 오탐
# 이력(합성 테스트에서 InputImageSensitiveContentDetected.PrivacyInformation — generate_audio가
# 아니라 입력 이미지 자체에 대한 실존인물 필터, bot/openrouter_video.py generate() 참고)이
# 있으므로 실사용 중 가끔 이 필터에 걸려 영상화가 실패할 수 있음 — 이는 회귀가 아니라
# 알려진/모니터링 중인 리스크. 필요시 SB_VIDEO_GENERATE_AUDIO=false로 코드 수정 없이 되돌릴 것.
OPENROUTER_VIDEO_GENERATE_AUDIO = os.environ.get("SB_VIDEO_GENERATE_AUDIO", "true").lower() == "true"
# ── OpenRouter TTS(/api/v1/audio/speech) — 2026-07-14 신규(합본 나레이션용) ──
# 한국어 지원 확인된 것 중 언어 커버리지가 가장 넓은 모델(70+ 언어)로 선택.
OPENROUTER_TTS_MODEL = os.environ.get("OPENROUTER_TTS_MODEL", "google/gemini-3.1-flash-tts-preview")
OPENROUTER_TTS_VOICE = os.environ.get("OPENROUTER_TTS_VOICE", "Kore")
OPENROUTER_TTS_TIMEOUT = int(os.environ.get("OPENROUTER_TTS_TIMEOUT", "120"))

# ── OpenRouter 배경음악(Google Lyria 3, chat/completions audio 모드) — 2026-07-15 재작업 ──
# ★한 번 만들어졌다가 사용자 요청("일단 배경음악 제거")으로 완전히 삭제된 적 있는 기능이라
# 한동안 기본 OFF로 뒀었는데, ★2026-07-15 재승인: 세이프티 필터 때문에 대사 없는 컷은
# generate_audio를 꺼둬서(_cut_has_dialogue) 그 컷만 완전 무음이 되고 합본에서 그 무음
# 구간이 어색하게 튀는 문제가 있었다 — 컷별로 정교하게 채우는 대신, 낮은 볼륨의 배경음악을
# 화 전체에 깔아 무음 구간을 자연스럽게 메우는 용도로 다시 기본 켬(사용자 명시 승인).
OPENROUTER_MUSIC_ENABLED = os.environ.get("SB_MUSIC_ENABLED", "true").lower() == "true"

# ── 자동주행([자동주행] <작품> <화번호>) — 2026-07-15 신규 ──
# 등록확인→씬설계→상세콘티→샷분해→스틸컷→영상화→합본까지 자동으로 이어서 돌리는 기능.
# ★2026-07-15: 오랫동안 기본 OFF로 두고 취소/크래시복구/실패시재시도/vision검수근거노출 등을
# 전부 보강한 뒤, 최종 통독 검토까지 마치고 사용자 승인으로 기본 ON 전환("자율주행모드 켜줘").
# 첫 실전 테스트는 [자동주행] <작품> <화번호> 씬N(특정 씬 하나로 범위 제한, 아래 scene_only
# 참고)로 좁게 돌려보는 걸 권장 — 켠다고 이 리포의 다른 기존 동작이 바뀌진 않는다(여전히
# 명시적으로 [자동주행] 명령을 쳐야만 이 경로를 탐).
AUTOPILOT_ENABLED = os.environ.get("SB_AUTOPILOT_ENABLED", "true").lower() == "true"
# ★2026-07-15: 스틸컷/영상 후검사(vision_check)가 컷마다 최대 2콜씩 쌓이면(30컷 화 기준 최대 ~90콜,
# 콜당 최대 60초) 자동주행 전체 wall-clock이 AGENT_TIMEOUT(600s)/SHOTS_TIMEOUT(1200s) 관례보다
# 훨씬 길어질 수 있음 — 후검사 전체에 예산을 두고, 넘으면 남은 컷은 검사를 건너뛰고 바로
# pending-review로 흘려보낸다(생성 자체는 계속 진행, 검사만 생략).
# ★2026-07-16 "넉넉하게 올려" — 30분은 실전에서 씬3 후반부터 대부분 컷이 "검사 예산 초과로
# 미검사"로 빠질 만큼 부족했다(실사용자 리포트: 재시도 상향과 맞물려 검사 자체가 더 오래
# 걸리게 됐는데 예산은 그대로였음). 영상화 자체가 이미 컷당 수 분씩 걸려 화 전체가 원래도
# 수십 분~시간 단위인 것과 맞춰, 2시간으로 상향 — 검사가 생성 속도를 못 따라가 뒷부분이
# 통째로 미검사되는 일을 줄인다.
AUTOPILOT_VISION_BUDGET_SEC = int(os.environ.get("SB_AUTOPILOT_VISION_BUDGET_SEC", "7200"))
OPENROUTER_MUSIC_MODEL = os.environ.get("OPENROUTER_MUSIC_MODEL", "google/lyria-3-clip-preview")
OPENROUTER_MUSIC_TIMEOUT = int(os.environ.get("OPENROUTER_MUSIC_TIMEOUT", "120"))
OPENROUTER_MUSIC_VOLUME_DB = float(os.environ.get("OPENROUTER_MUSIC_VOLUME_DB", "-18"))

# ── 피그마 브릿지 — 2026-07-20 신규 ──
# 영상화가 안전필터에 걸린 스틸컷을 사용자가 직접 손볼 수 있게 피그마로 넘기는 기능.
# 피그마 REST API는 파일에 이미지 노드를 추가하는 쓰기 기능이 없어(읽기 전용) 봇이 직접 밀어
# 넣을 수 없다 — 대신 봇이 로컬 큐(figma_bridge.py)에 이미지를 쌓아두고 작은 HTTP 서버로
# 노출하면, 사용자가 설치한 피그마 플러그인(figma-plugin/co-writer-bridge/)이 그 서버를
# 폴링해서 캔버스에 이미지를 직접 삽입한다. 봇과 피그마 데스크톱 앱이 같은 머신(또는 같은
# 네트워크에서 이 포트에 접근 가능)이어야 동작한다.
FIGMA_BRIDGE_ENABLED = os.environ.get("SB_FIGMA_BRIDGE_ENABLED", "false").lower() == "true"
FIGMA_BRIDGE_PORT = int(os.environ.get("SB_FIGMA_BRIDGE_PORT", "8933"))
FIGMA_QUEUE_DIR = os.environ.get("SB_FIGMA_QUEUE_DIR") or str(Path.home() / ".co-writer-figma-queue")

# ── 이미지 백엔드 선택 ──
# "openrouter"(기본) = OpenRouter gpt-image-2 / "higgsfield" = Higgsfield /v1/generations.
# OpenRouter는 그대로 두고, IMAGE_BACKEND=higgsfield 로 바꾸면 Higgsfield로 생성.
IMAGE_BACKEND = os.environ.get("IMAGE_BACKEND", "openrouter").lower()

# ── Higgsfield 이미지 (선택 백엔드) ──
# 문서: POST https://api.higgsfield.ai/v1/generations (async → 202+id → GET /v1/generations/{id} 폴링)
# 인증: Authorization: Bearer <key>. Soul(캐릭터 일관성)·reference_image_urls는 계정/모델별 확인 필요.
HIGGSFIELD_API_KEY = os.environ.get("HIGGSFIELD_API_KEY", "")
HIGGSFIELD_SECRET = os.environ.get("HIGGSFIELD_SECRET", "")            # 일부 계정은 key+secret 쌍(선택)
HIGGSFIELD_BASE_URL = os.environ.get("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai").rstrip("/")
HIGGSFIELD_IMAGE_MODEL = os.environ.get("HIGGSFIELD_IMAGE_MODEL", "gpt-image")  # 계정에서 쓰는 모델명으로
HIGGSFIELD_POLL_INTERVAL = int(os.environ.get("HIGGSFIELD_POLL_INTERVAL", "5"))  # 초
HIGGSFIELD_IMG_TIMEOUT = int(os.environ.get("HIGGSFIELD_IMG_TIMEOUT", "600"))    # 잡 완료 최대 대기(초)

# ── Higgsfield 영상(image-to-video) 모델 선택 ──
# 2026-07-13 실측: bytedance/seedance/v1/pro/image-to-video는 이 계정에 접근 권한 없음
# ("Model not found") → kling-video/v2.1/pro/image-to-video로 대체 사용 중.
# seedance API 접근 권한이 열리면 .env에 아래 한 줄만 바꾸면 코드 수정 없이 전환됨:
#   HIGGSFIELD_VIDEO_APPLICATION=bytedance/seedance/v1/pro/image-to-video
HIGGSFIELD_VIDEO_APPLICATION = os.environ.get(
    "HIGGSFIELD_VIDEO_APPLICATION", "kling-video/v2.1/pro/image-to-video")

# ── CapCut(pyCapCut) 드래프트 생성 — 2026-07-14 ──
# 로컬 파일 생성 라이브러리(원격 API 아님, 크레딧 없음). 최종 렌더링은 CapCut 앱을 사람이
# 직접 열어서 해야 함(pycapcut_client.py 상단 참고).
CAPCUT_DRAFTS_ROOT = Path(os.environ.get(
    "CAPCUT_DRAFTS_ROOT",
    str(Path.home() / "Movies/CapCut/User Data/Projects/com.lveditor.draft")))

# ── 합본(에피소드 컴파일, 음성 제외) — 2026-07-14 ──
# ffmpeg는 로컬 바이너리(brew install ffmpeg) — pip 의존성 아님, subprocess로 직접 호출.
COMPILE_WIDTH = int(os.environ.get("SB_COMPILE_WIDTH", "1080"))
COMPILE_HEIGHT = int(os.environ.get("SB_COMPILE_HEIGHT", "1920"))
COMPILE_FPS = int(os.environ.get("SB_COMPILE_FPS", "30"))
COMPILE_TIMEOUT = int(os.environ.get("SB_COMPILE_TIMEOUT", "1800"))
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")
