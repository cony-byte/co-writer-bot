# -*- coding: utf-8 -*-
"""스레드별 '최근 스틸컷' 정보 — 재시작에도 안 날아가게 파일로 저장.

장소/배경 피드백 감지(_maybe_place_feedback)가 "이 스레드 마지막 스틸컷이 어느
작품·씬이었나"를 알아야 하는데, 메모리 dict만 쓰면 봇 재시작(배포 등) 때마다
날아가서 피드백 감지가 조용히 안 먹는 문제가 있었다(2026-07-10).
"""
from __future__ import annotations

import json
import threading

from . import config

_PATH = config.BASE_DIR / "data" / "last_still.json"
_LOCK = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def set_last(thread_ts: str, work: str, scene_num: int | None, rest: str) -> None:
    """생성될 때마다 갱신(확정 여부 무관) — 장소 피드백 등 '방금 뭘 생성했나' 용도.
    ★"confirmed" 키는 건드리지 않음(merge) — 안 그러면 확정 안 한 새 씬을 생성하기만
    해도 "마지막 확정 스틸컷" 포인터가 덮어써져서, 컷 원본이 저장 안 된 씬을 가리키게
    되는 버그가 있었다(2026-07-14, "컷별 원본을 못 찾았어요" 오탐)."""
    with _LOCK:
        d = _load()
        entry = d.get(thread_ts) or {}
        entry.update({"work": work, "scene_num": scene_num, "rest": rest})
        d[thread_ts] = entry
        _save(d)


def get_last(thread_ts: str) -> dict | None:
    return _load().get(thread_ts)


def set_confirmed(thread_ts: str, work: str, scene_num: int | None, rest: str) -> None:
    """확정 저장(✅) 성공 시에만 갱신 — "이 스틸컷으로 영상 만들어줘"는 이 포인터를 써야
    확정 안 된 최신 생성물이 아니라 실제 컷 원본이 저장된 씬을 정확히 가리킨다."""
    with _LOCK:
        d = _load()
        entry = d.get(thread_ts) or {}
        entry["confirmed"] = {"work": work, "scene_num": scene_num, "rest": rest}
        d[thread_ts] = entry
        _save(d)


def get_confirmed(thread_ts: str) -> dict | None:
    e = _load().get(thread_ts) or {}
    return e.get("confirmed")
