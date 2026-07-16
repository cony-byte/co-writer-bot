# -*- coding: utf-8 -*-
"""미등록 인물/장소 일괄 선택 드롭다운(element_gen_pick)의 대기 상태 — 파일로 저장.

메모리 dict만 쓰면 드롭다운을 띄운 뒤 봇이 재시작(배포 등)돼 상태가 날아가서, 실제로는
멀쩡히 보이는 드롭다운을 클릭해도 "만료된 요청이에요"로 잘못 뜨는 문제가 있었다
(2026-07-13 실측 — pending_element_state.py/still_state.py와 같은 이유·같은 패턴).
같은 드롭다운을 반복 선택할 수 있어야 해서 pop 없이 get만 제공한다."""
from __future__ import annotations

import json
import threading

from . import config

_PATH = config.BASE_DIR / "data" / "pending_element_pick.json"
_LOCK = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def set(msg_ts: str, *, work: str, context: str) -> None:
    with _LOCK:
        d = _load()
        d[msg_ts] = {"work": work, "context": context}
        _save(d)


def get(msg_ts: str) -> dict | None:
    return _load().get(msg_ts)
