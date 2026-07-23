# -*- coding: utf-8 -*-
"""CapCut 드래프트(업로드) 편집 — 슬랙에 올린 draft(zip 또는 draft_content.json/draft_info.json)를
읽어 자연어 편집 지시를 적용하고 편집본을 돌려준다(★2026-07-22, ★2026-07-23 확장).

draft JSON은 평문이라 pyCapCut의 load_template(설치 버전 0.0.3 버그로 실패) 없이 직접 파싱·
수정한다. 트랜지션/필터 리소스 ID만 pyCapCut의 메타데이터(TransitionType/FilterType)에서 가져오고,
새 미디어(음악 교체·클립 교체·삽입)의 길이는 pyCapCut의 VideoMaterial/AudioMaterial(pymediainfo)로
실측한다 — 그래서 그 파일이 실제 디스크 경로로 존재해야 한다(호출자가 첨부를 임시파일로 내려받아
넘긴다).

지원 편집:
- 기본: 순서 변경 / 컷 제거 / 길이(trim) / 배속(speed) / 트랜지션 추가
- 오디오: 배경음악 교체(replace_audio, 없으면 새로 추가) / 볼륨(volume) / 음소거(mute)
- 자막: 자막 추가(add_text, 컷에 맞춰) / 자막 수정(edit_text)
- 필터: 컷에 CapCut 필터 적용(filter)
- 클립: 컷의 영상 소재 교체(replace_clip) / 새 컷 삽입(insert)

미지원(범위 밖): 키프레임 애니메이션, 마스크, 색보정 등 정교한 컴포지팅."""
from __future__ import annotations

import io
import json
import logging
import re
import uuid
import zipfile

log = logging.getLogger("storyboard-bot")

_US = 1_000_000   # 1초 = 1,000,000 마이크로초(CapCut 시간 단위)


def available() -> bool:
    try:
        import pycapcut  # noqa: F401
        return True
    except Exception:
        return False


# ── draft 읽기/쓰기 ─────────────────────────────────────────────
# ★2026-07-23 Mac CapCut 앱은 pyCapCut(Windows CapCut/JianYing 기준)이 다루는
# 'draft_content.json'이 아니라 'draft_info.json'을 쓴다(실측: 최상위 draft_info.json은
# 보통 비어있고, 실제 트랙/소재는 Timelines/<uuid>/draft_info.json에 들어있음 — 멀티타임라인
# 구조). 두 파일명을 다 인식하고, 후보가 여럿이면 '트랙이 실제로 있는' 것을 우선한다.
_DRAFT_FILENAMES = ("draft_content.json", "draft_info.json")


def _has_content(d: dict) -> bool:
    """이 draft JSON에 실제로 편집할 게 있는지(트랙에 세그먼트가 하나라도 있는지)."""
    for t in d.get("tracks", []) or []:
        if t.get("segments"):
            return True
    return False


def read_draft(data: bytes, filename: str) -> dict | None:
    """업로드 파일(zip 또는 draft_content.json/draft_info.json) → {content, zip, content_arcname}.
    실패 시 None. zip이면 후보 파일들을 다 파싱해보고, 실제 트랙(세그먼트)이 있는 걸 고른다
    (Mac CapCut은 최상위 draft_info.json이 비어있고 Timelines/<uuid>/ 안에 실제 데이터가 있음).
    나중에 재-zip할 수 있게 고른 항목의 arcname을 들고 있는다. extra_files는 apply_ops가
    새로 넣는 미디어(음악/클립 교체·삽입)를 담는 자리(arcname → bytes)."""
    name = (filename or "").lower()
    try:
        if name.endswith(".json") or data[:1] == b"{":
            return {"content": json.loads(data.decode("utf-8")), "zip": None, "extra_files": {}}
        if name.endswith(".zip") or data[:2] == b"PK":
            zf = zipfile.ZipFile(io.BytesIO(data))
            cand = [n for n in zf.namelist()
                    if "__MACOSX" not in n and any(n.endswith(fn) for fn in _DRAFT_FILENAMES)
                    and not n.endswith(".bak")]
            if not cand:
                return None
            parsed = []
            for n in cand:
                try:
                    parsed.append((n, json.loads(zf.read(n).decode("utf-8"))))
                except Exception:
                    continue
            if not parsed:
                return None
            # 트랙에 세그먼트가 있는 것을 우선(중첩 Timelines가 보통 실제 데이터를 가짐),
            # 없으면 첫 후보(예: 오래된 draft_content.json 단일 구조)로 폴백.
            chosen = next((p for p in parsed if _has_content(p[1])), parsed[0])
            arcname, content = chosen
            return {"content": content, "zip": data, "content_arcname": arcname, "extra_files": {}}
    except Exception:
        log.exception("draft 읽기 실패")
    return None


def write_draft(state: dict) -> tuple[bytes, str]:
    """편집된 content(+ extra_files로 새로 추가된 미디어)를 원래 형식으로 직렬화 → (bytes, filename)."""
    content = state["content"]
    new_json = json.dumps(content, ensure_ascii=False).encode("utf-8")
    extra_files = state.get("extra_files") or {}
    if not state.get("zip"):
        return new_json, "draft_content.json"
    # zip: draft_content.json 항목만 교체하고, 새 미디어(extra_files)를 추가하고,
    # 나머지(기존 미디어 등)는 그대로 복사.
    src = zipfile.ZipFile(io.BytesIO(state["zip"]))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in src.infolist():
            if item.filename == state["content_arcname"]:
                z.writestr(item, new_json)
            else:
                z.writestr(item, src.read(item.filename))
        for arcname, data in extra_files.items():
            z.writestr(arcname, data)
    return out.getvalue(), "edited_draft.zip"


# ── 트랙 조회/생성 ──────────────────────────────────────────────
def _track(content: dict, ttype: str) -> dict | None:
    for t in content.get("tracks", []):
        if t.get("type") == ttype:
            return t
    return None


def _video_track(content: dict) -> dict | None:
    return _track(content, "video")


def _ensure_track(content: dict, ttype: str) -> dict:
    """그 타입 트랙을 찾아 반환, 없으면 새로 만들어 추가(pyCapCut이 add_track으로 만드는
    것과 동일한 최소 필드 구성 — 실측 확인)."""
    t = _track(content, ttype)
    if t is not None:
        return t
    t = {"attribute": 0, "flag": 0, "id": uuid.uuid4().hex, "is_default_name": True,
         "name": ttype, "type": ttype, "segments": []}
    content.setdefault("tracks", []).append(t)
    return t


def _mat_name(content: dict, seg: dict) -> str:
    mid = seg.get("material_id")
    for v in content.get("materials", {}).get("videos", []):
        if v.get("id") == mid:
            return v.get("material_name") or v.get("path", "").split("/")[-1] or "?"
    return "?"


def segment_view(content: dict) -> list[dict]:
    """LLM/사용자에게 보여줄 컷 요약 — [{index, cut_no, name, start_s, dur_s, speed}].
    cut_no는 1-based 컷 번호(사용자가 말하는 '1컷'/'3컷'과 그대로 대응) — LLM이 계산으로
    index=N-1을 유도하다 실수하지 않게, index(0-based)와 나란히 명시적으로 준다
    (★2026-07-23 실측: '1컷 2배속'을 index=1로 잘못 매핑하던 오프바이원 버그 대응)."""
    vt = _video_track(content)
    if not vt:
        return []
    out = []
    for i, s in enumerate(vt["segments"]):
        tr = s.get("target_timerange", {})
        out.append({"index": i, "cut_no": i + 1, "name": _mat_name(content, s),
                    "start_s": round(tr.get("start", 0) / _US, 2),
                    "dur_s": round(tr.get("duration", 0) / _US, 2),
                    "speed": float(s.get("speed", 1.0))})
    return out


def audio_view(content: dict) -> list[dict]:
    """오디오(음악) 트랙 요약 — [{index, audio_no, name, start_s, dur_s, volume}]. 트랙 없으면 []."""
    at = _track(content, "audio")
    if not at:
        return []
    out = []
    for i, s in enumerate(at["segments"]):
        tr = s.get("target_timerange", {})
        mid = s.get("material_id")
        name = next((a.get("name") for a in content.get("materials", {}).get("audios", [])
                    if a.get("id") == mid), "?")
        out.append({"index": i, "audio_no": i + 1, "name": name,
                    "start_s": round(tr.get("start", 0) / _US, 2),
                    "dur_s": round(tr.get("duration", 0) / _US, 2),
                    "volume": float(s.get("volume", 1.0))})
    return out


def text_view(content: dict) -> list[dict]:
    """자막 트랙 요약 — [{index, text_no, text, start_s, dur_s}]. 트랙 없으면 []."""
    tt = _track(content, "text")
    if not tt:
        return []
    out = []
    for i, s in enumerate(tt["segments"]):
        tr = s.get("target_timerange", {})
        mid = s.get("material_id")
        text = ""
        for tm in content.get("materials", {}).get("texts", []):
            if tm.get("id") == mid:
                try:
                    text = json.loads(tm.get("content") or "{}").get("text", "")
                except Exception:
                    text = ""
                break
        out.append({"index": i, "text_no": i + 1, "text": text,
                    "start_s": round(tr.get("start", 0) / _US, 2),
                    "dur_s": round(tr.get("duration", 0) / _US, 2)})
    return out


def _speed_material(content: dict, seg: dict) -> dict | None:
    ids = set(seg.get("extra_material_refs", []))
    for sp in content.get("materials", {}).get("speeds", []):
        if sp.get("id") in ids:
            return sp
    return None


# 한글/구어 → 실제 TransitionType 멤버 별칭(자주 쓰는 것). CapCut엔 'fade'류 트랜지션 이름이
# 없고(페이드는 세그먼트 속성) 큐브회전 등 명명형이라, 흔한 요청을 실제 멤버로 매핑한다.
_TRANSITION_ALIAS = {
    "큐브": "Cube_Rotate", "큐브회전": "Cube_Rotate", "회전": "Cube_Rotate",
    "플립": "Cubic_Flip", "뒤집": "Cubic_Flip", "페이지": "Flip_Page",
    "플래시": "Flash", "번쩍": "White_Flash", "확대": "Flip_Zoom", "줌": "Flip_Zoom",
    "웨이브": "Big_Wave", "파도": "Big_Wave", "필름": "Film_Burn", "글리치": "Dirty_Frame",
}


def _resolve_meta(name: str, enum_cls, alias: dict, default_member: str):
    """이름(영문 토큰/한글 별칭 부분일치)으로 pyCapCut enum 멤버 → (resource_id, pretty_name).
    _resolve_transition/필터 공용 로직 — 못 찾으면 default_member로 폴백."""
    raw = (name or "").lower()
    for k, member in alias.items():
        if k in (name or ""):
            m = getattr(enum_cls, member, None)
            if m:
                return str(m.value.resource_id), m.value.name
    key = re.sub(r"[^a-z]", "", raw)
    tokens = [t for t in re.findall(r"[a-z]+", raw) if len(t) >= 3]
    exact = all_hit = any_hit = None
    for m in enum_cls:
        n = m.name.lower().replace("_", "")
        if key and key == n:
            exact = m
            break
        if tokens and all(t in n for t in tokens):
            all_hit = all_hit or m
        elif tokens and any(t in n for t in tokens):
            any_hit = any_hit or m
    m = exact or all_hit or any_hit or getattr(enum_cls, default_member, list(enum_cls)[0])
    return str(m.value.resource_id), m.value.name


def _resolve_transition(name: str):
    import pycapcut as cc
    return _resolve_meta(name, cc.TransitionType, _TRANSITION_ALIAS, "Cube_Rotate")


# 필터 한글 별칭 — CapCut 필터명이 대부분 브랜드성 고유명사라 흔한 표현 몇 개만 매핑.
_FILTER_ALIAS = {
    "빈티지": "VHS_I", "필름": "VHS_I", "청량": "Blue_Hour", "따뜻": "Candlelight",
    "흑백": "Anime_B_W", "블러": "Blur", "몽환": "Dreamy_Bubbles",
}


def _resolve_filter(name: str):
    import pycapcut as cc
    return _resolve_meta(name, cc.FilterType, _FILTER_ALIAS, "Blur")


def _probe_media(path: str):
    """영상/오디오 파일의 (duration_us, kind) — kind는 'video' 또는 'audio'.
    pyCapCut의 VideoMaterial/AudioMaterial(pymediainfo)로 실측(길이 0/실패 시 None)."""
    import pycapcut as cc
    try:
        m = cc.VideoMaterial(path)
        if m.duration:
            return int(m.duration), "video"
    except Exception:
        pass
    try:
        m = cc.AudioMaterial(path)
        if m.duration:
            return int(m.duration), "audio"
    except Exception:
        pass
    return None, None


def _new_video_material(path: str, name: str, duration_us: int, width: int = 1080, height: int = 1920) -> dict:
    """pyCapCut이 실제로 만드는 video material과 동일한 필드 구성(실측 확인, 2026-07-23)."""
    mid = uuid.uuid4().hex
    return {"audio_fade": None, "category_id": "", "category_name": "local", "check_flag": 63487,
            "crop": {"upper_left_x": 0.0, "upper_left_y": 0.0, "upper_right_x": 1.0, "upper_right_y": 0.0,
                    "lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0, "lower_right_y": 1.0},
            "crop_ratio": "free", "crop_scale": 1.0, "duration": duration_us, "height": height,
            "id": mid, "local_material_id": "", "material_id": mid, "material_name": name,
            "media_path": "", "path": path, "type": "video", "width": width}


def _new_audio_material(path: str, name: str, duration_us: int) -> dict:
    """실측 확인된 audio material 필드 구성."""
    mid = uuid.uuid4().hex
    return {"app_id": 0, "category_id": "", "category_name": "local", "check_flag": 3,
            "copyright_limit_type": "none", "duration": duration_us, "effect_id": "", "formula_id": "",
            "id": mid, "local_material_id": mid, "music_id": mid, "name": name, "path": path,
            "source_platform": 0, "type": "extract_music", "wave_points": []}


def _new_text_material(text: str) -> dict:
    """실측 확인된 text material 필드 구성 — content는 CapCut 내부 스타일 JSON 문자열."""
    mid = uuid.uuid4().hex
    content_json = json.dumps({
        "styles": [{"fill": {"alpha": 1.0, "content": {"render_type": "solid",
                    "solid": {"alpha": 1.0, "color": [1.0, 1.0, 1.0]}}},
                   "range": [0, len(text)], "size": 8.0, "bold": False, "italic": False,
                   "underline": False, "strokes": []}],
        "text": text,
    }, ensure_ascii=False)
    return {"id": mid, "content": content_json, "typesetting": 0, "alignment": 0,
            "letter_spacing": 0.0, "line_spacing": 0.02, "line_feed": 1, "line_max_width": 0.82,
            "force_apply_line_max_width": False, "check_flag": 7, "type": "text", "global_alpha": 1.0}


def _new_text_segment(material_id: str, start: int, duration: int) -> dict:
    return {"id": uuid.uuid4().hex, "material_id": material_id,
            "target_timerange": {"start": start, "duration": duration},
            "source_timerange": None, "speed": 1.0, "volume": 1.0, "extra_material_refs": [],
            "clip": {"alpha": 1.0, "flip": {"horizontal": False, "vertical": False}, "rotation": 0.0,
                     "scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0.0, "y": 0.0}},
            "render_index": 0}


def _new_audio_segment(material_id: str, start: int, duration: int, volume: float = 1.0) -> dict:
    return {"id": uuid.uuid4().hex, "material_id": material_id,
            "target_timerange": {"start": start, "duration": duration},
            "source_timerange": {"start": 0, "duration": duration}, "speed": 1.0, "volume": volume,
            "extra_material_refs": [], "clip": None, "render_index": 0}


def _timeline_duration(content: dict) -> int:
    vt = _video_track(content)
    if not vt or not vt["segments"]:
        return int(content.get("duration") or 0)
    last = vt["segments"][-1]["target_timerange"]
    return last["start"] + last["duration"]


def _recompute_starts(vt: dict) -> None:
    """세그먼트 target_timerange.start를 순서대로 재배치(0부터 이어붙임)."""
    cur = 0
    for s in vt["segments"]:
        s["target_timerange"]["start"] = cur
        cur += s["target_timerange"].get("duration", 0)


def _media_path(media_dir, filename: str) -> str | None:
    """apply_ops가 받는 media_dir(임시 폴더)에서 첨부 파일의 실제 경로. media_dir/filename이
    없으면 media_dir 안에서 이름이 일치하는 파일을 대소문자 무시로 탐색."""
    if not media_dir or not filename:
        return None
    from pathlib import Path
    d = Path(media_dir)
    p = d / filename
    if p.exists():
        return str(p)
    for f in d.iterdir():
        if f.name.lower() == filename.lower():
            return str(f)
    return None


def apply_ops(state: dict, ops: list[dict], media_dir=None) -> list[str]:
    """편집 연산 리스트를 state['content']에 적용(제자리 수정), 새 미디어는 state['extra_files']에
    등록. media_dir: 이번 요청에 함께 첨부된 미디어 파일들이 다운로드된 임시 폴더(음악 교체/클립
    교체·삽입에 필요) — 없으면 그 연산들은 건너뛰고 로그를 남긴다. 반환: 적용 로그(사람용)."""
    content = state["content"]
    extra_files = state.setdefault("extra_files", {})
    vt = _video_track(content)
    if not vt:
        return ["비디오 트랙을 못 찾음"]
    logs = []

    def _stage_media(filename: str):
        """첨부 미디어를 materials/<filename>으로 zip에 추가 예약하고 (arcname, duration, kind) 반환."""
        path = _media_path(media_dir, filename)
        if not path:
            return None, None, None
        dur, kind = _probe_media(path)
        if not dur:
            return None, None, None
        arcname = f"materials/{filename}"
        with open(path, "rb") as f:
            extra_files[arcname] = f.read()
        return arcname, dur, kind

    for op in ops:
        kind = op.get("op")
        try:
            if kind == "reorder":
                order = op["order"]
                segs = vt["segments"]
                vt["segments"] = [segs[i] for i in order if 0 <= i < len(segs)]
                logs.append(f"순서 변경 → {order}")
            elif kind == "drop":
                i = int(op["index"])
                if 0 <= i < len(vt["segments"]):
                    vt["segments"].pop(i)
                    logs.append(f"컷#{i} 제거")
            elif kind == "trim":
                i = int(op["index"]); dur = int(float(op["duration_s"]) * _US)
                seg = vt["segments"][i]
                seg["target_timerange"]["duration"] = dur
                sp = float(seg.get("speed", 1.0))
                seg.setdefault("source_timerange", {})["duration"] = int(dur * sp)
                logs.append(f"컷#{i} 길이 {op['duration_s']}s")
            elif kind == "speed":
                i = int(op["index"]); v = max(0.1, float(op["speed"]))
                seg = vt["segments"][i]
                seg["speed"] = v
                sm = _speed_material(content, seg)
                if sm:
                    sm["speed"] = v
                src = seg.get("source_timerange", {}).get("duration")
                if src:
                    seg["target_timerange"]["duration"] = int(src / v)
                logs.append(f"컷#{i} {v}배속")
            elif kind == "transition":
                after = int(op.get("after_index", op.get("index", 0)))
                dur = int(float(op.get("duration_s", 1.0)) * _US)
                rid, pretty = _resolve_transition(op.get("name", ""))
                tmat = {"category_id": "", "category_name": "", "duration": dur,
                        "effect_id": rid, "id": uuid.uuid4().hex, "is_overlap": True,
                        "name": pretty, "platform": "all", "resource_id": rid, "type": "transition"}
                content.setdefault("materials", {}).setdefault("transitions", []).append(tmat)
                if 0 <= after < len(vt["segments"]):
                    vt["segments"][after].setdefault("extra_material_refs", []).append(tmat["id"])
                logs.append(f"컷#{after} 뒤 트랜지션 '{pretty}'")

            # ── 오디오(음악) ────────────────────────────────────
            elif kind in ("replace_audio", "add_audio"):
                fname = op.get("media")
                arcname, dur, mkind = _stage_media(fname)
                if not arcname:
                    logs.append(f"음악 교체 실패 — 첨부 '{fname}'를 못 찾음")
                    continue
                amat = _new_audio_material(arcname, fname, dur)
                content.setdefault("materials", {}).setdefault("audios", []).append(amat)
                at = _ensure_track(content, "audio")
                audio_no = op.get("audio_no")
                if audio_no and 1 <= int(audio_no) <= len(at["segments"]):
                    seg = at["segments"][int(audio_no) - 1]
                    seg["material_id"] = amat["id"]
                    seg["source_timerange"] = {"start": 0, "duration": min(dur, seg["target_timerange"]["duration"])}
                    logs.append(f"오디오#{audio_no} 음악 교체 → {fname}")
                else:
                    total = _timeline_duration(content)
                    d = min(dur, total) if total else dur
                    at["segments"].append(_new_audio_segment(amat["id"], 0, d))
                    logs.append(f"배경음악 추가/교체 → {fname}({d/_US:.1f}s)")
            elif kind in ("volume", "mute"):
                at = _track(content, "audio")
                audio_no = int(op.get("audio_no", 1))
                if not at or not (1 <= audio_no <= len(at["segments"])):
                    logs.append(f"오디오#{audio_no} 없음 — {kind} 건너뜀")
                    continue
                v = 0.0 if kind == "mute" else max(0.0, min(2.0, float(op.get("level", 1.0))))
                at["segments"][audio_no - 1]["volume"] = v
                logs.append(f"오디오#{audio_no} 볼륨 {v}")

            # ── 자막(텍스트) ────────────────────────────────────
            elif kind == "add_text":
                cut_no = int(op.get("cut_no", 1))
                if not (1 <= cut_no <= len(vt["segments"])):
                    logs.append(f"컷#{cut_no} 없음 — 자막 추가 건너뜀")
                    continue
                tr = vt["segments"][cut_no - 1]["target_timerange"]
                text = str(op.get("text", "")).strip()
                if not text:
                    logs.append("자막 내용 없음 — 건너뜀")
                    continue
                tmat = _new_text_material(text)
                content.setdefault("materials", {}).setdefault("texts", []).append(tmat)
                tt = _ensure_track(content, "text")
                tt["segments"].append(_new_text_segment(tmat["id"], tr["start"], tr["duration"]))
                logs.append(f"컷#{cut_no}에 자막 추가: '{text[:20]}'")
            elif kind == "edit_text":
                tt = _track(content, "text")
                text_no = int(op.get("text_no", 1))
                if not tt or not (1 <= text_no <= len(tt["segments"])):
                    logs.append(f"자막#{text_no} 없음 — 수정 건너뜀")
                    continue
                mid = tt["segments"][text_no - 1]["material_id"]
                tmat = next((t for t in content.get("materials", {}).get("texts", [])
                            if t.get("id") == mid), None)
                new_text = str(op.get("text", "")).strip()
                if tmat and new_text:
                    try:
                        c = json.loads(tmat["content"])
                    except Exception:
                        c = {"styles": [{"range": [0, 0]}], "text": ""}
                    c["text"] = new_text
                    if c.get("styles"):
                        c["styles"][0]["range"] = [0, len(new_text)]
                    tmat["content"] = json.dumps(c, ensure_ascii=False)
                    logs.append(f"자막#{text_no} 수정: '{new_text[:20]}'")
                else:
                    logs.append(f"자막#{text_no} 수정 실패")

            # ── 필터 ────────────────────────────────────────────
            elif kind == "filter":
                cut_no = int(op.get("cut_no", 1))
                if not (1 <= cut_no <= len(vt["segments"])):
                    logs.append(f"컷#{cut_no} 없음 — 필터 건너뜀")
                    continue
                rid, pretty = _resolve_filter(op.get("name", ""))
                fmat = {"adjust_params": [], "algorithm_artifact_path": "", "apply_target_type": 0,
                        "bloom_params": None, "category_id": "", "category_name": "",
                        "color_match_info": {"source_feature_path": "", "target_feature_path": "",
                                             "target_image_path": ""},
                        "effect_id": rid, "enable_skin_tone_correction": False, "exclusion_group": [],
                        "face_adjust_params": [], "formula_id": "", "id": uuid.uuid4().hex,
                        "intensity_key": "", "name": pretty, "resource_id": rid, "type": "filter"}
                content.setdefault("materials", {}).setdefault("effects", []).append(fmat)
                vt["segments"][cut_no - 1].setdefault("extra_material_refs", []).append(fmat["id"])
                logs.append(f"컷#{cut_no}에 필터 '{pretty}' 적용")

            # ── 클립 교체/삽입 ──────────────────────────────────
            elif kind == "replace_clip":
                cut_no = int(op.get("cut_no", 1))
                fname = op.get("media")
                if not (1 <= cut_no <= len(vt["segments"])):
                    logs.append(f"컷#{cut_no} 없음 — 교체 건너뜀")
                    continue
                arcname, dur, mkind = _stage_media(fname)
                if not arcname or mkind != "video":
                    logs.append(f"클립 교체 실패 — 첨부 '{fname}'를 영상으로 못 읽음")
                    continue
                vmat = _new_video_material(arcname, fname, dur)
                content.setdefault("materials", {}).setdefault("videos", []).append(vmat)
                seg = vt["segments"][cut_no - 1]
                seg["material_id"] = vmat["id"]
                new_dur = min(dur, seg["target_timerange"]["duration"])
                seg["target_timerange"]["duration"] = new_dur
                seg["source_timerange"] = {"start": 0, "duration": new_dur}
                logs.append(f"컷#{cut_no} 클립 교체 → {fname}")
            elif kind == "insert":
                after_cut_no = int(op.get("after_cut_no", 0) or 0)
                fname = op.get("media")
                arcname, dur, mkind = _stage_media(fname)
                if not arcname or mkind != "video":
                    logs.append(f"삽입 실패 — 첨부 '{fname}'를 영상으로 못 읽음")
                    continue
                vmat = _new_video_material(arcname, fname, dur)
                content.setdefault("materials", {}).setdefault("videos", []).append(vmat)
                new_seg = {"id": uuid.uuid4().hex, "material_id": vmat["id"],
                          "target_timerange": {"start": 0, "duration": dur},
                          "source_timerange": {"start": 0, "duration": dur},
                          "speed": 1.0, "volume": 1.0, "extra_material_refs": [],
                          "render_index": 0}
                pos = max(0, min(len(vt["segments"]), after_cut_no))
                vt["segments"].insert(pos, new_seg)
                logs.append(f"컷#{after_cut_no} 뒤에 '{fname}' 삽입")
            else:
                logs.append(f"알 수 없는 op 무시: {kind}")
        except Exception as e:
            log.exception("CapCut 편집 연산 실패(%s)", kind)
            logs.append(f"op 실패({kind}): {e}")
    _recompute_starts(vt)
    return logs
