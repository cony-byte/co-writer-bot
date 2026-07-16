# -*- coding: utf-8 -*-
"""CapCut 드래프트(프로젝트 파일) 생성 어댑터 — pyCapCut(https://github.com/GuanYixuan/pyCapCut) 사용.

2026-07-14: 이전에 만든 bot/capcut_client.py(가상의 원격 API 골격)는 폐기하고 이걸로 대체.
pyCapCut은 **원격 API가 아니라 로컬 CapCut 드래프트 JSON을 직접 생성하는 라이브러리**다 —
크레딧/네트워크 비용 없음. 다만 중요한 제약이 있다:

★실측 확인(이 맥의 CapCut 설치 기준):
- 드래프트 폴더: `~/Movies/CapCut/User Data/Projects/com.lveditor.draft` (config.CAPCUT_DRAFTS_ROOT)
- `DraftFolder.create_draft(name, width, height, fps)` — 템플릿 없이 새 드래프트 생성 가능.
- **최종 렌더링/내보내기는 여전히 CapCut 데스크톱 앱(공식 문서 기준 Windows 권장)을 사람이
  직접 열어서 해야 함** — 이 봇이 draft_content.json을 만들어주는 것까지가 자동화의 끝이고,
  "영상 파일로 완성"까지 슬랙에서 끝낼 수 있는 게 아니다. 이 점을 사용자에게 항상 명시할 것.
- 클립은 **로컬 파일 경로만** 지원(원격 URL 불가) — 지금 우리 영상화 결과물은 URL만 갖고
  있고 로컬에 다운로드하지 않으므로, CapCut 드래프트에 넣으려면 먼저 mp4를 로컬로 받아와야
  한다(아직 미구현 — 이 모듈은 로컬 경로가 이미 있다고 가정).
"""
from __future__ import annotations

import logging

from . import config

log = logging.getLogger("storyboard-bot")


def available() -> bool:
    try:
        import pycapcut  # noqa: F401
    except ImportError:
        return False
    return config.CAPCUT_DRAFTS_ROOT.exists()


def build_draft(draft_name: str, clips: list[dict], *, width: int = 1080, height: int = 1920,
                fps: int = 30) -> str:
    """clips별로 순서대로 이어붙인 CapCut 드래프트 생성. clips: [{"path": 로컬 mp4/이미지 경로,
    "duration": 초(float), "caption": 자막 텍스트(선택)}]. 반환: 드래프트 폴더 경로(str).

    ⚠️ 여기까지가 자동화 범위 — 실제 렌더링/최종 영상 내보내기는 CapCut 앱을 사람이 직접
    열어서 해야 한다(공식 문서: 드래프트는 Windows CapCut에서 내보내는 걸 권장)."""
    if not available():
        raise RuntimeError("pycapcut 미설치이거나 CapCut 드래프트 폴더를 못 찾음 "
                           f"({config.CAPCUT_DRAFTS_ROOT})")
    import pycapcut as cc

    df = cc.DraftFolder(str(config.CAPCUT_DRAFTS_ROOT))
    script = df.create_draft(draft_name, width, height, fps, allow_replace=True)
    script.add_track(cc.TrackType.video)
    has_captions = any(c.get("caption") for c in clips)
    if has_captions:
        script.add_track(cc.TrackType.text)

    cursor = 0.0
    for c in clips:
        dur = float(c.get("duration") or 5)
        tr = cc.trange(f"{cursor}s", f"{dur}s")
        script.add_segment(cc.VideoSegment(c["path"], tr))
        cap = c.get("caption")
        if cap:
            script.add_segment(cc.TextSegment(cap, tr, style=cc.TextStyle(size=6.0, color=(1.0, 1.0, 1.0))))
        cursor += dur

    script.save()
    return str(config.CAPCUT_DRAFTS_ROOT / draft_name)
