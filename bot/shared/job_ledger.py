# -*- coding: utf-8 -*-
"""생성 작업 원장 — 재시작(auto-pull kickstart · 크래시) 도중 끊기면 안 되는 장시간 생성
작업 기록.

deploy/auto_pull.sh는 이미 data/jobs.json이 비어있지 않고 최근(<=900s) 갱신됐으면
'생성 중'으로 보고 pull·재시작을 미루도록 짜여 있었지만(2026-07-14), 정작 이 파일에
기록을 남기는 코드가 없어 사실상 죽은 검사였다 — 그 결과 버튼으로 트리거되는 재생성
(draft_regen/revise_generate/char_regen/field_regen 등, message 이벤트의 inflight 추적
바깥에 있는 슬랙 액션 핸들러들) 도중에도 봇이 재시작되어 생성이 여러 번 끊기고
중복·상충 결과가 남는 문제가 있었다(2026-07-15).

storyboard-bot/bot/job_ledger.py와 동일한 구조로 이식 — start_job()으로 시작 기록,
finish_job()으로 완료 시 제거. 프로세스가 죽으면 finish_job이 못 불려서 파일에 남고,
auto_pull.sh의 busy-gate가 그걸 보고 재시작을 미룬다. 또한 storyboard 쪽에서는 다음
기동 때 pending_jobs()로 못 끝낸 기록을 찾아 자동 재실행한다(app.py __main__에서 1회).
"""
from __future__ import annotations

import json
import threading
import time
import uuid

from .. import config

_PATH = config.BASE_DIR / "data" / "jobs.json"
_LOCK = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def start_job(kind: str, channel: str, thread_ts: str, rest: str = "") -> str:
    """생성 시작 기록. 반환: job_id (finish_job에 넘길 키)."""
    with _LOCK:
        d = _load()
        jid = uuid.uuid4().hex
        d[jid] = {"kind": kind, "channel": channel, "thread_ts": thread_ts,
                  "rest": rest, "started": time.time()}
        _save(d)
        return jid


def finish_job(job_id: str | None) -> None:
    """정상 완료(성공이든 실패든 끝까지 실행됨) → 원장에서 제거."""
    if not job_id:
        return
    with _LOCK:
        d = _load()
        if job_id in d:
            del d[job_id]
            _save(d)


def pending_jobs() -> list[dict]:
    """원장에 남아있는(=지난 실행이 못 끝낸) 작업들. [{id,kind,channel,thread_ts,rest,started}, ...]"""
    d = _load()
    return [{"id": jid, **v} for jid, v in d.items()]


def finish_by_thread(thread_ts: str) -> None:
    """(F6) 사용자가 "멈춰"로 명시적으로 취소한 스레드의 남은 기록을 지운다 — 취소 직후
    프로세스가 재시작되면 pending_jobs()가 이 기록을 보고 되살릴 수 있어서(취소한 작업이
    재시작 후 자동으로 다시 돌거나 "재생성해줘" 대상이 되는 문제), 취소 시점에 바로 정리."""
    with _LOCK:
        d = _load()
        changed = False
        for jid, v in list(d.items()):
            if v.get("thread_ts") == thread_ts:
                del d[jid]
                changed = True
        if changed:
            _save(d)


def clear_all() -> None:
    with _LOCK:
        _save({})
