# -*- coding: utf-8 -*-
"""프로젝트 저장 브리지 — 확정된 스틸컷을 그 작품의 프로젝트 디렉토리에 저장.

fixed-images(참조 이미지)는 openrouter_image.vp_fixed_dir()가 이미 브리지한다.
이 모듈은 '생성물 확정'만 담당: <프로젝트>/outputs/에 파일 저장 + visual.db(generations)에 기록.
visual.db 스키마/헬퍼는 shared/db.py(구 visual-pipeline repo, 이 repo로 통합됨).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import urllib.request
import uuid

from . import config
from . import openrouter_image as oi

# CDN이 urllib 기본 User-Agent("Python-urllib/3.x")를 봇으로 보고 막는 경우 대응(2026-07-14).
_DOWNLOAD_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

log = logging.getLogger("storyboard-bot")

vp_db = None
try:
    from shared import db as vp_db  # noqa: F401
except Exception:
    log.exception("shared.db 임포트 실패 — 파일 저장만 되고 DB 기록은 생략됨")
    vp_db = None


def available(work: str | None) -> bool:
    return oi.vp_project_dir(work) is not None


_PROJECT_TEMPLATE = {
    "$schema_version": "1.0",
    "metadata": {
        "purpose": "Shared project metadata for Claude Code agents and visual generation pipelines",
        "shared_file": True,
        "source_of_truth": True,
        "managed_by": "",
        "usage_note": "All Claude Code agents must read this file before image or video generation.",
        "write_policy": {"default": "read_only", "allowed_writers": [], "manual_edit_allowed": True},
    },
    "shared_paths": {
        "fixed_images_root": "fixed-images",
        "generated_images_root": "generated",
        "outputs_root": "outputs",
        "logs_root": "logs",
        "database": "visual.db",
    },
    "claude_code": {
        "shared_usage": True,
        "required_read_before_run": True,
        "agents": [],
        "rules": [
            "Resolve all relative paths from project.project_root.",
            "Use fixed_images as the only approved identity references.",
            "Do not overwrite fixed images automatically.",
            "Do not create a new character identity when a fixed image exists.",
            "Write generated candidates only under generated_images_root.",
            "Use element_id only after element.status is trained.",
            "Stop generation when required character data is missing.",
        ],
    },
    "characters": [],
}


def ensure_project(work: str | None) -> bool:
    """★2026-07-21: "이 작품은 visual-pipeline 프로젝트가 없어서 저장 못 함"으로 조용히
    끝내지 말고, 없으면 그 자리에서 만들어야 한다는 사용자 지적 — 기존 프로젝트(코니/날혐남)와
    동일한 구조(project.json + fixed-images/ + outputs/{stills,compiled,videos} + logs/)로
    새 프로젝트 폴더를 자동 생성한다. 이미 있으면(available) 아무것도 안 하고 True."""
    if not work:
        return False
    if available(work):
        return True
    root = getattr(config, "FIXED_IMAGES_ROOT", None)
    if not root:
        return False
    try:
        pdir = root / work
        (pdir / "fixed-images").mkdir(parents=True, exist_ok=True)
        (pdir / "outputs" / "stills").mkdir(parents=True, exist_ok=True)
        (pdir / "outputs" / "compiled").mkdir(parents=True, exist_ok=True)
        (pdir / "outputs" / "videos").mkdir(parents=True, exist_ok=True)
        (pdir / "logs").mkdir(parents=True, exist_ok=True)
        try:
            from .shared import works
            page_id = works.page_of(work) or ""
        except Exception:
            page_id = ""
        meta = {
            **_PROJECT_TEMPLATE,
            "project": {
                "slug": f"auto-{work}",
                "work_name": work,
                "project_root": str(pdir),
                "notion_page_id": page_id,
                "status": "draft",
            },
        }
        (pdir / "project.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("visual-pipeline 프로젝트 자동 생성: %r -> %s", work, pdir)
        return True
    except Exception:
        log.exception("visual-pipeline 프로젝트 자동 생성 실패: %r", work)
        return False


def _scene_dir_name(scene_num: int | None) -> str:
    return f"{scene_num}씬" if scene_num else "미분류씬"


def save_still(work: str, *, scene_num: int | None, prompt_summary: str,
              png: bytes, requested_by: str | None = None, cuts: list | None = None,
              episode: int | str | None = None) -> str | None:
    """확정된 스틸컷을 <프로젝트>/outputs/stills/<N화>/<N씬>/에 컷별 개별 파일로 저장 +
    (가능하면) visual.db generations에 기록. 반환: 저장된 씬 폴더의 상대경로(프로젝트 루트
    기준). 프로젝트를 못 찾으면 None.

    ★2026-07-15(사용자 요청 — "스틸컷 저장 폴더가 너무 더러움"): 기존엔 outputs/ 바로 밑에
    "still_s{씬}_{배치}_{uuid}.png"(합성 그리드) + 그걸 감싸는 "..._cuts/" 폴더(컷별 파일)가
    평평하게 뒤섞여 쌓여 지저분했다. outputs/stills/<화>/<씬>/ 밑에 cut{n}.png들을 폴더화 없이
    바로 두는 구조로 정리(save_video가 이미 쓰던 화별 폴더링 패턴과 동일선상).
    합성 그리드 PNG는 더 이상 디스크에 저장하지 않는다 — 그리드는 Slack 메시지 첨부로 이미
    보여지고, 디스크 저장의 유일한 용도는 "확정 결과 경로" 표시였는데 이제 씬 폴더 경로 자체가
    그 역할을 한다.
    cut 파일명이 컷 번호로 결정적(cut{n}.png)이라 배치별 delete-before-save 정리 로직이
    통째로 불필요해졌다(예전엔 batch_key로 배치끼리 안 건드리게 격리해야 했던 문제 —
    ★2026-07-15 그 이전 커밋 참고 — 자체가 사라짐. 배치2를 저장해도 cut5~8.png만 덮어쓰고
    배치1의 cut1~4.png는 파일명이 달라 건드릴 일이 없다).
    meta.json은 이 씬 폴더에 하나만 두고, 이번 호출이 저장하는 컷 번호들의 항목만
    읽기-병합-쓰기(load-merge-write)한다 — 다른 배치가 이미 써둔 컷 항목을 지우면 안 됨
    (오늘 있었던 배치 간 데이터 유실 버그의 재발 방지)."""
    proj = oi.vp_project_dir(work)
    if not proj:
        return None
    scene_dir = proj / "outputs" / "stills" / _episode_dir_name(episode) / _scene_dir_name(scene_num)
    scene_dir.mkdir(parents=True, exist_ok=True)
    rel = str((scene_dir.relative_to(proj)))

    if cuts:
        meta_path = scene_dir / "meta.json"
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        except Exception:
            existing = {}
        # 리스트(구/신 저장 모두 대비)든 dict든 들어올 수 있어 항상 {n(str): {...}} 형태로 정규화.
        if isinstance(existing, list):
            existing = {str(m.get("n")): m for m in existing if m.get("n") is not None}
        for c in cuts:
            (scene_dir / f"cut{c['n']}.png").write_bytes(c["png"])
            existing[str(c["n"])] = {k: c.get(k) for k in
                        ("n", "caption", "prompt", "characters", "places", "props", "scene_text")}
        meta_path.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")

    if vp_db is not None:
        try:
            con = vp_db.connect(proj)
            gid = vp_db.log_generation(
                con, prompt=prompt_summary, kind="image",
                application="storyboard-bot/still", model=config.OPENROUTER_IMAGE_MODEL,
                requested_by=requested_by, scene=(f"씬{scene_num}" if scene_num else None))
            vp_db.update_generation(con, gid, status="promoted", output_path=rel)
            con.close()
        except Exception:
            log.exception("visual.db 기록 실패(파일은 정상 저장됨)")
    return rel


def _episode_dir_name(episode: int | str | None) -> str:
    return f"{episode}화" if episode else "미분류"


def still_path(work: str, *, scene_num: int | None, cut_num: int | None,
               episode: int | str | None = None):
    """이 씬·컷 스틸컷(cut{n}.png)의 로컬 경로(Path)를 반환. 프로젝트 못 찾으면 None.
    save_still이 저장하는 경로 규칙과 동일하게 재구성한다."""
    proj = oi.vp_project_dir(work)
    if not proj:
        return None
    return (proj / "outputs" / "stills" / _episode_dir_name(episode)
            / _scene_dir_name(scene_num) / f"cut{cut_num}.png")


def still_has_grid_backup(work: str, *, scene_num: int | None, cut_num: int | None,
                          episode: int | str | None = None) -> bool:
    """이 컷 스틸이 얼굴 격자(face_grid)로 덮여 덮어써졌는지 판별. 격자 적용 시 원본을 항상
    '<파일>.orig.bak'으로 백업하므로(overwrite_still_with_backup), 그 백업 파일의 존재를
    '격자 적용된 승인 프레임' 마커로 쓴다(별도 상태 저장 없이 파일로만 신호)."""
    p = still_path(work, scene_num=scene_num, cut_num=cut_num, episode=episode)
    return bool(p and (p.parent / f"{p.name}.orig.bak").exists())


def overwrite_still_with_backup(work: str, *, scene_num: int | None, cut_num: int | None,
                                episode: int | str | None = None,
                                new_png: bytes, original_png: bytes | None = None) -> bool:
    """스틸(cut{n}.png)을 new_png로 덮어쓰되 원본을 '<파일>.orig.bak'으로 백업(없을 때만).
    이 백업 파일이 still_has_grid_backup의 '격자 적용됨' 마커가 된다. 디스크에 스틸 파일이
    아직 없으면 original_png를 백업으로 기록하고 new_png를 새로 쓴다. 성공 시 True."""
    p = still_path(work, scene_num=scene_num, cut_num=cut_num, episode=episode)
    if not p:
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        bak = p.parent / f"{p.name}.orig.bak"
        if not bak.exists():
            if p.exists():
                shutil.copy(p, bak)
            elif original_png is not None:
                bak.write_bytes(original_png)
        p.write_bytes(new_png)
        return True
    except Exception:
        log.exception("스틸 격자 덮어쓰기 실패")
        return False


def trim_head_seconds(local_path: str, seconds: float = 0.1, timeout: int = 60) -> bool:
    """이미 저장된 mp4의 맨 앞 `seconds`초를 잘라 같은 경로에 덮어쓴다. 성공하면 True.
    ★격자 anchor 컷 전용 — 격자로 덮은 첫 프레임이 승인 앵커로 생성에 쓰이지만 최종 영상에
    그 격자 프레임이 잠깐 비치지 않도록 앞부분을 잘라낸다. 0.1초는 프레임 단위 정확도가
    필요해 스트림 복사(-c copy)의 키프레임 정렬 오차를 피하려 재인코딩한다(짧아 부담 적음).
    실패해도 원본은 그대로 두고 False 반환(영상 자체는 유효하므로 흐름을 막지 않는다)."""
    import os
    import pathlib
    src = pathlib.Path(local_path)
    if not src.exists():
        return False
    tmp = src.with_name(src.stem + "_trim" + src.suffix)
    cmd = [config.FFMPEG_BIN, "-y", "-ss", str(seconds), "-i", str(src),
           "-map", "0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-c:a", "copy", "-movflags", "+faststart", str(tmp)]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            log.error("영상 앞 트림 실패(원본 유지): %s", r.stderr.decode("utf-8", "ignore")[-500:])
            if tmp.exists():
                tmp.unlink()
            return False
        os.replace(tmp, src)
        return True
    except Exception:
        log.exception("영상 앞 트림 예외(원본 유지)")
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        return False


def still_cut_path(work: str, scene_num: int | None, cut_num: int,
                   episode: int | str | None = None) -> Path | None:
    """save_still이 이 컷을 저장한다면(또는 이미 저장했다면) 쓰는 결정적 로컬 경로. 파일이
    실제로 존재하는지는 호출부에서 확인해야 한다 — 프로젝트를 못 찾으면 None."""
    proj = oi.vp_project_dir(work)
    if not proj:
        return None
    return (proj / "outputs" / "stills" / _episode_dir_name(episode)
           / _scene_dir_name(scene_num) / f"cut{cut_num}.png")


def save_video(work: str, *, scene_num: int | None, cut_num: int | None, url: str,
              episode: int | str | None = None,
              prompt_summary: str = "", application: str = "", requested_by: str | None = None,
              cost: float = 0.0, timeout: int = 300) -> str | None:
    """완성된 영상(URL)을 <프로젝트>/outputs/videos/<화>/에 로컬 mp4로 다운로드해 저장 +
    (가능하면) visual.db generations에 기록. 반환: 저장된 로컬 절대경로(str).
    프로젝트를 못 찾거나 다운로드 실패하면 None.

    ★2026-07-14: 영상 결과물이 URL로만 남고 로컬에 안 남아서, CapCut 드래프트(로컬 파일
    경로만 지원)에 못 넣던 문제를 해결하기 위함(pycapcut_client.build_draft의 clips가
    로컬 경로를 요구함).

    ★2026-07-14: 화 구분 없이 outputs/videos/ 밑에 모든 화의 컷을 평평하게 저장하면 서로 다른
    화가 같은 씬 번호("씬1")를 쓸 때 파일이 섞인다 — 화별 하위 폴더(outputs/videos/<N>화/)로
    나눠 저장, episode를 모르면 "미분류" 폴더로 폴백."""
    proj = oi.vp_project_dir(work)
    if not proj:
        # ★2026-07-16: 이 경로가 로그 없이 조용히 None을 반환해서, "다운로드 실패"로만
        # 사용자에게 보이는 실패의 실제 원인(프로젝트 폴더를 못 찾음 — FIXED_IMAGES_ROOT
        # 미설정 등)을 로그에서 전혀 추적할 수 없었다(실측: 원인 규명에 시간 소요).
        log.error(f"영상 저장 실패 — vp_project_dir('{work}')가 None(프로젝트 폴더를 못 찾음)")
        return None
    out_dir = proj / "outputs" / "videos" / _episode_dir_name(episode)
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"video_s{scene_num or 0}_cut{cut_num or 0}_{uuid.uuid4().hex[:8]}.mp4"
    dest = out_dir / fname
    try:
        # ★2026-07-14: 영상 백엔드를 openrouter_video로 바꾼 뒤 다운로드가 401 Unauthorized로
        # 실패(실측: 생성 자체는 성공, 결과 URL을 urllib 기본 헤더로 그냥 GET할 때만 막힘).
        # openrouter_video.py 응답 필드명은 "unsigned_urls"라 서명 자체는 불필요해 보이지만,
        # urllib 기본 User-Agent("Python-urllib/3.x")를 CDN이 봇으로 차단하는 흔한 패턴일
        # 가능성이 커서 브라우저 UA로 교체 + (그래도 안 되는 경우 대비) OpenRouter API 키를
        # Authorization으로도 같이 보냄 — 둘 다 붙여도 무해하고, 정확한 401 원인은 다음 실패
        # 시 로그의 상태 확인 후 좁힐 것.
        headers = {"User-Agent": _DOWNLOAD_UA}
        if config.OPENROUTER_API_KEY:
            headers["Authorization"] = f"Bearer {config.OPENROUTER_API_KEY}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            dest.write_bytes(r.read())
    except Exception:
        log.exception("영상 다운로드 실패 — URL은 정상 결과물, 로컬 저장만 실패")
        return None
    rel = f"outputs/videos/{_episode_dir_name(episode)}/{fname}"

    if vp_db is not None:
        try:
            con = vp_db.connect(proj)
            gid = vp_db.log_generation(
                con, prompt=prompt_summary, kind="video", application=application,
                requested_by=requested_by, scene=(f"씬{scene_num}" if scene_num else None))
            vp_db.update_generation(con, gid, status="promoted", output_path=rel, output_url=url,
                                    cost=cost)
            con.close()
        except Exception:
            log.exception("visual.db 기록 실패(파일은 정상 저장됨)")
    return str(dest)


def save_video_bytes(work: str, *, scene_num: int | None, cut_num: int | None, data: bytes,
                     episode: int | str | None = None, prompt_summary: str = "",
                     application: str = "attached-video", requested_by: str | None = None,
                     cost: float = 0.0) -> str | None:
    """★2026-07-21: 사용자가 첨부한 완성 영상(mp4 bytes)을 재생성 없이 그대로 그 컷의
    영상으로 저장한다 — save_video가 URL을 받아 다운로드하는 것과 달리 바이트를 바로 쓴다.
    save_video와 동일한 파일명 규칙(video_s{씬}_cut{컷}_{uuid8}.mp4, 화별 폴더)을 써서
    video_index.list_episode_videos가 스캔해 합본(compile_episode)에 그대로 물린다
    (같은 컷에 봇 생성본이 이미 있어도 mtime 최신이 이 첨부본이라 자동으로 이게 정본이 된다).
    반환: 저장된 로컬 절대경로(str). 프로젝트를 못 찾으면 None."""
    proj = oi.vp_project_dir(work)
    if not proj:
        log.error(f"영상(첨부) 저장 실패 — vp_project_dir('{work}')가 None(프로젝트 폴더를 못 찾음)")
        return None
    if not data:
        log.error("영상(첨부) 저장 실패 — 빈 데이터")
        return None
    out_dir = proj / "outputs" / "videos" / _episode_dir_name(episode)
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"video_s{scene_num or 0}_cut{cut_num or 0}_{uuid.uuid4().hex[:8]}.mp4"
    dest = out_dir / fname
    try:
        dest.write_bytes(data)
    except Exception:
        log.exception("영상(첨부) 로컬 저장 실패")
        return None
    rel = f"outputs/videos/{_episode_dir_name(episode)}/{fname}"

    if vp_db is not None:
        try:
            con = vp_db.connect(proj)
            gid = vp_db.log_generation(
                con, prompt=prompt_summary or "첨부 영상 직접 저장(재생성 없음)", kind="video",
                application=application, requested_by=requested_by,
                scene=(f"씬{scene_num}" if scene_num else None))
            vp_db.update_generation(con, gid, status="promoted", output_path=rel, cost=cost)
            con.close()
        except Exception:
            log.exception("visual.db 기록 실패(파일은 정상 저장됨)")
    return str(dest)


def find_existing_video(work: str, scene_num: int | None, cut_num: int | None,
                        episode: int | str | None = None) -> str | None:
    """이 씬·컷의 영상이 이미 outputs/videos/<화>/에 저장돼있으면 그 로컬 경로를 반환(없으면 None).
    ★2026-07-15 "단계 안에서의 재개" — save_video의 파일명이 video_s{씬}_cut{컷}_{uuid}.mp4라
    uuid가 매번 달라 정확한 경로를 미리 알 수 없으므로 glob으로 찾는다. 같은 컷을 여러 번
    재시도했을 수 있어(자동주행 컷 단위 재시도) 여러 개가 매칭되면 가장 최근(mtime) 것을 쓴다."""
    proj = oi.vp_project_dir(work)
    if not proj:
        return None
    video_dir = proj / "outputs" / "videos" / _episode_dir_name(episode)
    if not video_dir.exists():
        return None
    matches = sorted(video_dir.glob(f"video_s{scene_num or 0}_cut{cut_num or 0}_*.mp4"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    return str(matches[0]) if matches else None


def extract_last_frame_png(video_path: str, timeout: int = 30) -> bytes | None:
    """이 영상 파일의 마지막 프레임을 PNG bytes로 추출. 실패하면 None(호출자는 그냥 이어붙일
    참조 없이 진행 — 필수 기능이 아니라 연결 매끄러움을 위한 보조 참조라 실패해도 전체 흐름은
    막지 않는다).

    ★2026-07-14: 같은 씬 안 컷들을 영상화할 때, 직전 컷 영상이 끝나는 프레임을 다음 컷
    영상화의 추가 참조로 넘겨(app.py의 씬 단위 순차 영상화) 컷 사이 전환이 하드컷처럼
    어색하게 느껴지던 문제를 완화한다(스틸컷 생성 때 이미 쓰던 prev_png 체이닝을 영상화에도
    적용)."""
    try:
        r = subprocess.run(
            [config.FFMPEG_BIN, "-y", "-sseof", "-1", "-i", video_path,
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, timeout=timeout, check=True)
        return r.stdout
    except Exception:
        log.exception(f"영상 마지막 프레임 추출 실패: {video_path}")
        return None


def extract_first_frame_png(video_path: str, timeout: int = 30) -> bytes | None:
    """★2026-07-15: 자동주행 영상 일관성 후검사용 — extract_last_frame_png와 대칭으로 첫 프레임도
    필요(첫/끝 프레임만 확인, 전체 클립은 안 봄). -sseof -1 대신 -ss 0만 다르다."""
    try:
        r = subprocess.run(
            [config.FFMPEG_BIN, "-y", "-ss", "0", "-i", video_path,
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, timeout=timeout, check=True)
        return r.stdout
    except Exception:
        log.exception(f"영상 첫 프레임 추출 실패: {video_path}")
        return None


_CUT_PNG_RE = re.compile(r"cut(\d+)\.png$", re.I)


def _recover_cuts_from_pngs(scene_dir) -> list | None:
    """meta.json이 없거나 깨졌을 때 cut{n}.png 파일들에서 최소 컷 메타를 복구."""
    if not scene_dir.exists():
        return None
    out = []
    for p in sorted(scene_dir.glob("*.png")):
        m = _CUT_PNG_RE.match(p.name)
        if m:
            out.append({"n": int(m.group(1)), "caption": "", "png": p.read_bytes()})
    return sorted(out, key=lambda m: m["n"]) if out else None


def scan_episode_stills(work: str, episode: int | str | None) -> dict | None:
    """존재 질문(스틸컷 있어?/몇 컷?)의 근거를 '생성 기록'이 아니라 '실제 디스크 파일'로 삼기
    위한 스캔(★2026-07-21). outputs/stills/<N화>/ 밑의 실제 PNG를 직접 세므로, 사용자가 파일을
    수동으로 밀어넣어 생성 기록이 없어도 잡는다. 씬폴더(<N씬>/cut*.png)와 씬폴더 없이 평평하게
    떨어진 PNG를 모두 센다. 프로젝트를 못 찾으면 None."""
    proj = oi.vp_project_dir(work)
    if not proj:
        return None
    sdir = proj / "outputs" / "stills" / _episode_dir_name(episode)
    if not sdir.exists():
        return {"scenes": [], "cut_count": 0, "loose_cut_count": 0}
    scenes, total = [], 0
    for sub in sorted(sdir.iterdir()):
        if sub.is_dir():
            n = len(list(sub.glob("*.png")))
            if n:
                scenes.append(sub.name)
                total += n
    loose = len(list(sdir.glob("*.png")))   # 씬폴더 없이 밀어넣은 수동 스틸
    return {"scenes": scenes, "cut_count": total + loose, "loose_cut_count": loose}


def load_latest_cuts(work: str, scene_num: int | None,
                     episode: int | str | None = None) -> list | None:
    """그 작품·씬의 확정 스틸컷 컷별 원본(png+메타)을 디스크에서 복원.
    영상화 드롭다운 메시지가 만료됐거나 봇이 재시작된 뒤에도 "이 스틸컷으로 영상 만들어줘"가
    다시 동작하게 하기 위함(2026-07-13). 없으면 None.

    ★2026-07-15: save_still이 outputs/stills/<화>/<씬>/ 한 폴더에 모든 배치의 컷을 직접
    (파일명이 cut{n}.png로 결정적이라 배치 구분 없이) 모아 저장하는 구조로 바뀌면서, 예전처럼
    씬당 여러 배치 폴더(still_s{씬}_b1-4_..._cuts 등)를 mtime순으로 뒤져 병합할 필요가
    없어졌다 — 폴더가 하나뿐이라 그냥 그 폴더의 meta.json + cut*.png만 읽으면 됨."""
    proj = oi.vp_project_dir(work)
    if not proj:
        return None
    scene_dir = (proj / "outputs" / "stills" / _episode_dir_name(episode)
                / _scene_dir_name(scene_num))
    meta_path = scene_dir / "meta.json"
    if not meta_path.exists():
        # ★2026-07-21 수동 삽입 대비: meta.json이 없어도 cut{n}.png가 디스크에 있으면
        # 파일명에서 최소 메타(n)를 복구해 영상화가 되게 한다 — 사용자가 스틸을 폴더에 직접
        # 밀어넣으면 생성 기록(meta.json)이 안 남아, 파일은 있는데 영상화가 막히던 문제.
        return _recover_cuts_from_pngs(scene_dir)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return _recover_cuts_from_pngs(scene_dir)
    if isinstance(meta, dict):
        meta = list(meta.values())
    out = [{**m, "png": p.read_bytes()}
           for m in meta if (p := scene_dir / f"cut{m['n']}.png").exists()]
    if not out:
        return None
    return sorted(out, key=lambda m: m["n"])


# ── 화 아웃풋 초기화(★2026-07-15, 사용자 요청 — 테스트/전체 재생성용) ────────
#   영상(outputs/videos/<N>화/)은 이미 화별 하위폴더라 안전하게 통째로 지울 수 있다.
#   합본(outputs/compiled/)은 화 접두사("N화..." — episode_title 규칙, app.py 참고)로
#   시작하는 파일만 골라 지운다 — 확정본(_최종.mp4)도 포함해서 지운다(사용자가 명시적으로
#   요청한 기능이므로).
#   ★2026-07-15: 스틸컷도 이제 outputs/stills/<N>화/ 밑에 화별로 폴더링돼(사용자 요청 —
#   "저장 폴더가 너무 더러움" 정리) 다른 화 스틸컷을 건드릴 위험 없이 안전하게 통째로 지울 수
#   있게 됐다 — 예전엔 씬 번호로만 저장되고 화 구분이 파일명에 없어 대상에서 제외했었는데,
#   이제 그 이유가 사라져 영상·합본과 동일하게 삭제 대상에 포함한다.
def _episode_output_paths(work: str, episode: int | str):
    proj = oi.vp_project_dir(work)
    if not proj:
        return None, None, None
    video_dir = proj / "outputs" / "videos" / _episode_dir_name(episode)
    compiled_dir = proj / "outputs" / "compiled"
    stills_dir = proj / "outputs" / "stills" / _episode_dir_name(episode)
    return video_dir, compiled_dir, stills_dir


def preview_episode_outputs(work: str, episode: int | str) -> dict | None:
    """실제로 지우기 전에 뭐가 지워질지 미리 센다(확인 메시지용). 프로젝트를 못 찾으면 None."""
    video_dir, compiled_dir, stills_dir = _episode_output_paths(work, episode)
    if video_dir is None:
        return None
    video_files = sorted(p.name for p in video_dir.glob("*.mp4")) if video_dir.exists() else []
    compiled_files = (sorted(p.name for p in compiled_dir.glob(f"{episode}화*.mp4"))
                      if compiled_dir.exists() else [])
    still_scenes = sorted(p.name for p in stills_dir.iterdir() if p.is_dir()) if stills_dir.exists() else []
    return {"video_dir": str(video_dir), "video_files": video_files, "compiled_files": compiled_files,
            "still_dir": str(stills_dir), "still_scenes": still_scenes}


def delete_episode_outputs(work: str, episode: int | str) -> dict:
    """화 하나의 영상화(outputs/videos/<N>화/)·합본(outputs/compiled/의 그 화 접두사 파일,
    확정본 포함)·스틸컷(outputs/stills/<N>화/) 아웃풋을 실제로 삭제.
    반환: {"video_files": [...], "compiled_files": [...], "still_scenes": [...]}(실제로 지워진 것만)."""
    video_dir, compiled_dir, stills_dir = _episode_output_paths(work, episode)
    deleted = {"video_files": [], "compiled_files": [], "still_scenes": []}
    if video_dir and video_dir.exists():
        deleted["video_files"] = sorted(p.name for p in video_dir.glob("*.mp4"))
        shutil.rmtree(video_dir, ignore_errors=True)
    if compiled_dir and compiled_dir.exists():
        for p in sorted(compiled_dir.glob(f"{episode}화*.mp4")):
            try:
                p.unlink()
                deleted["compiled_files"].append(p.name)
            except Exception:
                log.exception(f"합본 파일 삭제 실패: {p}")
    if stills_dir and stills_dir.exists():
        deleted["still_scenes"] = sorted(p.name for p in stills_dir.iterdir() if p.is_dir())
        shutil.rmtree(stills_dir, ignore_errors=True)
    return deleted
