# -*- coding: utf-8 -*-
"""재시작 도중 끊긴 씬설계/상세콘티 작업을 스레드별로 기록 — "재생성해줘"처럼 자연어로만
말해도 방금 끊긴 그 명령을 그대로 다시 돌릴 수 있게(2026-07-13).
job_ledger는 완료되면 기록을 지우지만, 여기는 반대로 "끊긴 뒤 알림까지 보낸" 상태를
사용자가 재시도할 때까지 남겨둔다."""
from __future__ import annotations

import json
import threading

from . import config

_PATH = config.BASE_DIR / "data" / "interrupted_jobs.json"
_LOCK = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def mark(thread_ts: str, kind: str, rest: str) -> None:
    with _LOCK:
        d = _load()
        d[thread_ts] = {"kind": kind, "rest": rest}
        _save(d)


def get(thread_ts: str) -> dict | None:
    return _load().get(thread_ts)


def clear(thread_ts: str) -> None:
    with _LOCK:
        d = _load()
        if thread_ts in d:
            del d[thread_ts]
            _save(d)
