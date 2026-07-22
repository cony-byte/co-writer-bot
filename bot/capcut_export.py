# -*- coding: utf-8 -*-
"""CapCut(캡컷) 프로젝트(draft) 내보내기 — pyCapCut로 이 회차의 생성 영상들을 순서대로 얹은
CapCut 드래프트를 만들고, 미디어까지 zip으로 묶어 반환한다(★2026-07-22).

용도: 자동 합본(ffmpeg mp4)은 그대로 두고, "더 손보고 싶을 때 CapCut으로 넘기는" 일방향
내보내기. draft_content.json은 미디어를 '절대경로'로 참조하므로(라이브러리 한계), 미디어를
draft 폴더 안 materials/로 복사해 함께 zip한다 — 사용자 기기의 CapCut에서 열 때 미디어가
offline으로 뜨면 그 materials/ 폴더로 relink하면 된다(첫 실측 검증 후 경로 처리 개선 예정).

pyCapCut(win용 uiautomation은 sys_platform 가드로 mac에서도 설치됨)·pymediainfo 필요.
실측: darwin에서 import·VideoSegment·save 정상 동작 확인(2026-07-22)."""
from __future__ import annotations

import json
import logging
import shutil
import zipfile
from pathlib import Path

log = logging.getLogger("storyboard-bot")


def available() -> bool:
    try:
        import pycapcut  # noqa: F401
        return True
    except Exception:
        return False


# 세로 숏폼 기준(스틸/영상 파이프라인과 동일). 필요하면 호출부에서 바꾼다.
_W, _H = 1080, 1920


def build_episode_draft(name: str, ordered_cuts: list[dict], out_root: Path) -> Path | None:
    """ordered_cuts=[{"scene","cut","path"}, ...] 순서대로 한 비디오 트랙에 이어붙인 CapCut
    드래프트를 out_root 아래 만든다(폴더명=name). 미디어는 <draft>/materials/로 복사하고
    draft_content.json의 경로를 그 사본으로 바꿔 zip 이식성을 높인다. 반환: draft 폴더 경로."""
    import pycapcut as cc
    from pycapcut import trange

    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    df = cc.DraftFolder(str(out_root))
    script = df.create_draft(name, _W, _H, allow_replace=True)
    script.add_track(cc.TrackType.video)

    added = 0
    cursor = 0   # 트랙 위 현재 위치(마이크로초) — 컷을 순차로 이어붙인다(겹치면 SegmentOverlap)
    for c in ordered_cuts:
        p = c.get("path")
        if not p or not Path(p).exists():
            log.warning("CapCut 내보내기 — 파일 없음, 건너뜀: %s", p)
            continue
        try:
            mat = cc.VideoMaterial(str(p))            # pymediainfo로 실측 길이 읽음
            seg = cc.VideoSegment(mat, trange(cursor, mat.duration))  # cursor부터 클립 전체 길이
            script.add_segment(seg)
            cursor += mat.duration
            added += 1
        except Exception:
            log.exception("CapCut 내보내기 — 세그먼트 추가 실패: %s", p)
    if not added:
        return None
    script.save()

    draft_dir = out_root / name
    _bundle_media(draft_dir)
    return draft_dir


def _bundle_media(draft_dir: Path) -> None:
    """draft_content.json이 절대경로로 참조하는 미디어를 <draft>/materials/로 복사하고 경로를
    그 사본으로 치환 — zip을 그대로 풀어도(같은 폴더 구조 유지 시) 최대한 찾게 한다."""
    content = draft_dir / "draft_content.json"
    if not content.exists():
        return
    try:
        d = json.loads(content.read_text(encoding="utf-8"))
    except Exception:
        log.exception("draft_content.json 읽기 실패 — 미디어 번들 생략")
        return
    materials_dir = draft_dir / "materials"
    materials_dir.mkdir(exist_ok=True)
    for vid in (d.get("materials") or {}).get("videos", []):
        src = vid.get("path")
        if not src or not Path(src).exists():
            continue
        dst = materials_dir / Path(src).name
        try:
            if not dst.exists():
                shutil.copy2(src, dst)
            vid["path"] = str(dst)   # 절대경로(=번들 사본)로 치환 — offline이면 이 폴더로 relink
        except Exception:
            log.exception("미디어 복사 실패: %s", src)
    try:
        content.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.exception("draft_content.json 재작성 실패")


def zip_draft(draft_dir: Path) -> Path:
    """draft 폴더 전체를 zip으로 묶어 반환(폴더명.zip, draft_dir 옆에 생성)."""
    draft_dir = Path(draft_dir)
    zip_path = draft_dir.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in draft_dir.rglob("*"):
            if f.is_file():
                z.write(f, arcname=str(Path(draft_dir.name) / f.relative_to(draft_dir)))
    return zip_path
