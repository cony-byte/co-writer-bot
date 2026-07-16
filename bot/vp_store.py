# -*- coding: utf-8 -*-
"""프로젝트 저장 브리지 — 확정된 스틸컷을 그 작품의 프로젝트 디렉토리에 저장.

fixed-images(참조 이미지)는 openrouter_image.vp_fixed_dir()가 이미 브리지한다.
이 모듈은 '생성물 확정'만 담당: <프로젝트>/outputs/에 파일 저장 + visual.db(generations)에 기록.
visual.db 스키마/헬퍼는 shared/db.py(구 visual-pipeline repo, 이 repo로 통합됨).
"""
from __future__ import annotations

import json
import logging
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


def save_video(work: str, *, scene_num: int | None, cut_num: int | None, url: str,
              episode: int | str | None = None,
              prompt_summary: str = "", application: str = "", requested_by: str | None = None,
              cost: float = 0.0, timeout: int = 120) -> str | None:
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
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None
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
