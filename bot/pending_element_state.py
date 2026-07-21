# -*- coding: utf-8 -*-
"""AI 생성 인물/장소/소품 후보 — "이걸로 등록"/"다시 생성" 확정 대기 상태를 파일로 저장.

메모리 dict(_PENDING_ELEMENT_GEN)만 쓰면 확정 버튼을 누르기 전에 봇이 재시작(배포 등)돼
날아가서 "만료된 요청이에요"로 잘못 뜨는 문제가 있었다(2026-07-14) — still_state와 같은
이유·같은 패턴으로 해결."""
from __future__ import annotations

import json
import threading
import uuid

from . import config

_META_PATH = config.BASE_DIR / "data" / "pending_element.json"
_PNG_DIR = config.BASE_DIR / "data" / "pending_element_png"
_LOCK = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(_META_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    _META_PATH.parent.mkdir(parents=True, exist_ok=True)
    _META_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def set(msg_ts: str, *, work: str, name: str, etype: str, context: str, png: bytes,
        aliases: list[str] | None = None) -> None:
    _PNG_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{msg_ts}_{uuid.uuid4().hex[:8]}.png"
    (_PNG_DIR / fname).write_bytes(png)
    with _LOCK:
        d = _load()
        d[msg_ts] = {
            "work": work, "name": name, "etype": etype, "context": context,
            "png_file": fname, "aliases": aliases or [],
        }
        _save(d)


def pop(msg_ts: str) -> dict | None:
    """읽고 즉시 지운다(확정/재생성 둘 다 1회성 소비) — png bytes를 읽어 dict에 채워 반환."""
    with _LOCK:
        d = _load()
        entry = d.pop(msg_ts, None)
        if entry is None:
            return None
        _save(d)
    png_path = _PNG_DIR / entry.pop("png_file")
    try:
        entry["png"] = png_path.read_bytes()
    except Exception:
        return None
    try:
        png_path.unlink()
    except Exception:
        pass
    return entry
