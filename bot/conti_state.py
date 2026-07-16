# -*- coding: utf-8 -*-
"""스레드별 '지금 붙어있는 상세 콘티가 몇 화인지' 기록 (파일로 영구 저장).

[스틸컷]/[이미지]에서 화 번호를 지정했는데 그 스레드에 실제로 붙어있는 콘티가
다른 화면, 조용히 무시하고 엉뚱한 화로 진행하던 문제를 막기 위함(2026-07-10).
"""
from __future__ import annotations

import json
import threading

from . import config

_PATH = config.BASE_DIR / "data" / "conti_state.json"
_LOCK = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def set_episode(thread_ts: str, work: str, episode: int | None, human_final: bool = False) -> None:
    """콘티 기록 갱신. human_final=True면 '실무자가 확정한 최종본'으로 표시(재생성 시 경고용).
    봇이 새로 생성할 때(human_final=False, 기본)는 이전 human_final 표시를 자연히 덮어써 지운다."""
    with _LOCK:
        d = _load()
        entry = d.get(thread_ts) or {}
        entry["work"] = work
        if episode:
            entry["episode"] = episode
        entry["human_final"] = human_final
        d[thread_ts] = entry
        _save(d)


def get_episode(thread_ts: str) -> dict | None:
    return _load().get(thread_ts)


def is_human_final(thread_ts: str) -> bool:
    e = get_episode(thread_ts)
    return bool(e and e.get("human_final"))


# ── 자동주행 진행 상태(2026-07-15, "이 스레드를 읽고 다시 [자동주행]을 치면 알아서 다음 단계로
#    이어지게") ────────────────────────────────────────────────────────────────
# _do_autopilot이 단계를 하나씩 완전히 끝낼 때마다 여기에 "몇 단계까지 끝났는지" 기록해두고,
# 같은 스레드에서 화 번호도 안 붙인 맨몸 "[자동주행]"이 다시 오면 이 기록으로 work/화/씬 범위/
# 다음 시작 단계를 알아서 채운다. conti_state.json과 같은 파일에 저장 — 스레드별 상태를 이미
# 이 파일에서 관리하고 있어 새 파일을 또 만들 필요가 없다.


def set_autopilot_progress(thread_ts: str, work: str, episode: int | None,
                           scene_only: int | None, last_stage: int) -> None:
    with _LOCK:
        d = _load()
        entry = d.get(thread_ts) or {}
        entry["autopilot"] = {"work": work, "episode": episode,
                              "scene_only": scene_only, "last_stage": last_stage}
        d[thread_ts] = entry
        _save(d)


def get_autopilot_progress(thread_ts: str) -> dict | None:
    """last_stage(1~6)까지 끝난 자동주행 기록. 6단계(합본)까지 끝났으면(더 이어갈 단계가 없음)
    None을 반환해 "이어서 진행" 판단부에서 자연히 새 실행처럼 취급되게 한다."""
    entry = (_load().get(thread_ts) or {}).get("autopilot")
    if not entry or entry.get("last_stage", 0) >= 6:
        return None
    return entry


def clear_autopilot_progress(thread_ts: str) -> None:
    with _LOCK:
        d = _load()
        entry = d.get(thread_ts) or {}
        entry.pop("autopilot", None)
        d[thread_ts] = entry
        _save(d)
