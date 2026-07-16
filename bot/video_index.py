# -*- coding: utf-8 -*-
"""화(에피소드) 단위 영상 인덱싱 — <프로젝트>/outputs/videos/<화>/에 이미 생성된 컷별 영상을
씬 번호로 그룹핑해 합본(episode_compile)이 쓸 수 있게 해준다.

visual.db의 generations.scene은 "씬N" 문자열만 있고 cut 번호가 없어서(vp_store.py 참고),
DB 조회보다 vp_store.save_video가 쓰는 파일명 패턴(video_s{scene}_cut{cut}_{uuid8}.mp4)을
직접 스캔하는 게 더 간단하고 정확하다.

★2026-07-14: 화 구분 없이 outputs/videos/ 밑에 평평하게 저장하면 서로 다른 화가 같은 씬
번호를 쓸 때 컷이 섞인다 — vp_store.save_video가 이제 outputs/videos/<화>/ 하위 폴더에
저장하므로, 여기도 화를 지정하면 그 폴더만 스캔한다. episode를 안 주면(구버전 폴더 구조
호환 목적) 하위 폴더 전체 + 레거시 평평한 파일까지 다 스캔한다."""
from __future__ import annotations

import re
from pathlib import Path

from . import openrouter_image as oi

_VIDEO_FNAME_RE = re.compile(r"^video_s(\d+)_cut(\d+)_")


def _iter_video_dirs(videos_dir: Path, episode: int | str | None):
    if episode:
        d = videos_dir / f"{episode}화"
        if d.is_dir():
            yield d
        return
    yield videos_dir   # 레거시 평평한 파일(화 폴더로 마이그레이션 전)
    for d in videos_dir.iterdir():
        if d.is_dir():
            yield d


def list_episode_videos(work: str, scene_nums: list[int] | None = None,
                        episode: int | str | None = None) -> dict[int, list[dict]]:
    """<프로젝트>/outputs/videos/<화>/를 스캔해 {scene_num: [{"cut_num", "path", "mtime"}, ...]}로
    반환. 같은 (scene, cut)이 여러 번 재생성됐으면 가장 최근(mtime 최대) 것만 남긴다.
    scene_nums를 주면 그 씬들만 필터링(없으면 전체). episode를 주면 그 화 폴더만, 안 주면
    모든 화 폴더(+레거시 평평한 파일)를 합쳐서 본다."""
    proj = oi.vp_project_dir(work)
    if not proj:
        return {}
    videos_dir = proj / "outputs" / "videos"
    if not videos_dir.exists():
        return {}
    latest: dict[tuple[int, int], dict] = {}
    for d in _iter_video_dirs(videos_dir, episode):
        for p in d.iterdir():
            if not p.is_file():
                continue
            m = _VIDEO_FNAME_RE.match(p.name)
            if not m:
                continue
            scene_num, cut_num = int(m.group(1)), int(m.group(2))
            if scene_nums is not None and scene_num not in scene_nums:
                continue
            mtime = p.stat().st_mtime
            key = (scene_num, cut_num)
            if key not in latest or mtime > latest[key]["mtime"]:
                latest[key] = {"cut_num": cut_num, "path": str(p), "mtime": mtime}
    out: dict[int, list[dict]] = {}
    for (scene_num, _cut_num), v in latest.items():
        out.setdefault(scene_num, []).append(v)
    for scene_num in out:
        out[scene_num].sort(key=lambda v: v["cut_num"])
    return out
