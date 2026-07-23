# -*- coding: utf-8 -*-
"""CapCut 드래프트(업로드) 편집 PoC — 슬랙에 올린 draft(zip 또는 draft_content.json)를 읽어
자연어 편집 지시(순서/제거/길이/배속/트랜지션)를 적용하고 편집본을 돌려준다(★2026-07-22).

draft_content.json은 평문 JSON이라 pyCapCut의 load_template(설치 버전 0.0.3 버그) 없이 직접
파싱·수정한다. 트랜지션 리소스 ID만 pyCapCut의 TransitionType 메타데이터에서 가져온다.

지원 편집: 순서 변경 / 컷 제거 / 길이(trim) / 배속(speed) / 트랜지션 추가.
미지원(범위 밖): 키프레임 애니메이션 등 복합 이펙트."""
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
        import pycapcut  # noqa: F401  (트랜지션 메타데이터용)
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
    나중에 재-zip할 수 있게 고른 항목의 arcname을 들고 있는다."""
    name = (filename or "").lower()
    try:
        if name.endswith(".json") or data[:1] == b"{":
            return {"content": json.loads(data.decode("utf-8")), "zip": None}
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
            return {"content": content, "zip": data, "content_arcname": arcname}
    except Exception:
        log.exception("draft 읽기 실패")
    return None


def write_draft(state: dict) -> tuple[bytes, str]:
    """편집된 content를 원래 형식(zip이면 zip, 아니면 json)으로 직렬화 → (bytes, filename)."""
    content = state["content"]
    new_json = json.dumps(content, ensure_ascii=False).encode("utf-8")
    if not state.get("zip"):
        return new_json, "draft_content.json"
    # zip: draft_content.json 항목만 교체하고 나머지(미디어 등)는 그대로 복사.
    src = zipfile.ZipFile(io.BytesIO(state["zip"]))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in src.infolist():
            if item.filename == state["content_arcname"]:
                z.writestr(item, new_json)
            else:
                z.writestr(item, src.read(item.filename))
    return out.getvalue(), "edited_draft.zip"


# ── 세그먼트 조회/편집 ───────────────────────────────────────────
def _video_track(content: dict) -> dict | None:
    for t in content.get("tracks", []):
        if t.get("type") == "video":
            return t
    return None


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


def _resolve_transition(name: str):
    """이름(영문 토큰/한글 별칭 부분일치)으로 TransitionType 멤버 → (resource_id, pretty_name).
    못 찾으면 기본값(Cube_Rotate)."""
    import pycapcut as cc
    raw = (name or "").lower()
    # ① 한글 별칭
    for k, member in _TRANSITION_ALIAS.items():
        if k in (name or ""):
            m = getattr(cc.TransitionType, member, None)
            if m:
                return str(m.value.resource_id), m.value.name
    # ② 영문 매칭: 정확일치 > 전체 토큰 포함 > 일부 토큰 포함
    key = re.sub(r"[^a-z]", "", raw)
    tokens = [t for t in re.findall(r"[a-z]+", raw) if len(t) >= 3]
    exact = all_hit = any_hit = None
    for m in cc.TransitionType:
        n = m.name.lower().replace("_", "")
        if key and key == n:
            exact = m
            break
        if tokens and all(t in n for t in tokens):
            all_hit = all_hit or m
        elif tokens and any(t in n for t in tokens):
            any_hit = any_hit or m
    m = exact or all_hit or any_hit or getattr(cc.TransitionType, "Cube_Rotate", list(cc.TransitionType)[0])
    return str(m.value.resource_id), m.value.name


def _recompute_starts(vt: dict) -> None:
    """세그먼트 target_timerange.start를 순서대로 재배치(0부터 이어붙임)."""
    cur = 0
    for s in vt["segments"]:
        s["target_timerange"]["start"] = cur
        cur += s["target_timerange"].get("duration", 0)


def apply_ops(content: dict, ops: list[dict]) -> list[str]:
    """편집 연산 리스트를 draft content에 적용(제자리 수정). 반환: 적용 로그(사람용)."""
    vt = _video_track(content)
    if not vt:
        return ["비디오 트랙을 못 찾음"]
    logs = []
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
                # 소스는 유지, 타임라인 길이 = 소스/속도
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
            else:
                logs.append(f"알 수 없는 op 무시: {kind}")
        except Exception as e:
            logs.append(f"op 실패({kind}): {e}")
    _recompute_starts(vt)
    return logs
