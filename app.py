#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""co-writer-bot — 숏폼 드라마 보조 작가 + 스토리보드/영상 제작 파이프라인 통합 슬랙 에이전트.

(2026-07-16, Phase 6 컷오버) co-writer-bot과 storyboard-bot을 하나의 프로세스로 합친 뒤의
새 진입점. 실제 라우팅/명령 로직은 전부 bot/dispatch.py(+dispatch_cowriter.py/
dispatch_storyboard.py/shared/*)에 있음 — 이 파일은 그걸 불러와 기동 시퀀스만 조립한다.
이전의 거대한 단일 app.py(co-writer 전용, ~4400줄)는 자연어 디스패치 순서를 포함해 전부
bot/dispatch.py로 대체됐다 — 자세한 병합 배경은 HANDOFF_봇병합.md 참고.

실행: python3 app.py  (Socket Mode — 공개 URL 불필요)
"""
import threading

from bot import config
from bot import generator          # co-writer 쪽 LLM 백엔드 (Claude Agent SDK/Anthropic API)
from bot import sb_generator        # storyboard 쪽 LLM 백엔드 (OpenRouter) — 절대 하나로 합치지 않음
from bot import dispatch
from bot import dispatch_cowriter as cw
from bot import figma_bridge
from bot.shared.slack_io import app, log

from slack_bolt.adapter.socket_mode import SocketModeHandler

if __name__ == "__main__":
    # 두 백엔드 자격증명 확인 — 하나라도 실패하면 그 도메인의 생성 기능이 막힌 채로 뜨는 대신
    # 여기서 바로 알 수 있게(healthcheck()는 실패 시 예외를 던지는 게 원래 두 봇의 관례).
    generator.healthcheck()
    sb_generator.healthcheck()
    log.info("합쳐진 co-writer+storyboard 봇 시작 (backend=%s, reference=%s, sheet=%s, openrouter=%s)",
             config.BACKEND, config.REFERENCE_DIR, bool(config.SHEET_WEBAPP_URL),
             bool(config.OPENROUTER_API_KEY))

    # co-writer 쪽 배경 동기화(레퍼런스 repo pull + 노션 변경 감지) — 원래 co-writer-bot/app.py
    # __main__의 로직 그대로, dispatch_cowriter.py로 이식된 함수를 그대로 사용.
    _ref_is_repo = (config.REFERENCE_DIR.parent / ".git").exists()
    if _ref_is_repo:
        cw._reference_pull()
    from bot.shared import works
    _n_works = len(works.all_works()) if config.NOTION_TOKEN else 0
    if (config.NOTION_TOKEN and _n_works) or _ref_is_repo:
        threading.Thread(target=cw._notion_autosync_loop, daemon=True).start()
        log.info("co-writer 배경 동기화 ON (레퍼런스 pull=%s · 노션 %d작품 · %d초 주기)",
                 _ref_is_repo, _n_works, cw._NOTION_POLL_SEC)

    # 재시작 복구: co-writer의 inflight 재실행 + storyboard의 job_ledger pending_jobs 재생/안내.
    # (bot/dispatch.py의 start_background_jobs() 참고 — 두 봇의 원래 __main__에 있던 걸 하나로 묶음)
    dispatch.start_background_jobs()

    # ★2026-07-20: 안전필터에 걸린 스틸컷을 피그마로 넘기는 기능 — 꺼져있으면(기본값) no-op.
    figma_bridge.start_server()

    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()
