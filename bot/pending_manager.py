# -*- coding: utf-8 -*-

"""pending_manager.py — 대기 상태(확인 카드/씬 선택/재개 제안 등) 공용 상태머신.

★2026-07-21: 이 봇엔 "봇이 뭔가 물어보고 사용자 답을 기다리는" pending 패턴이 여러 곳에
독립적으로 있었다(_FAILED_EVENTS의 resume-offer, _PENDING_SCENE_PICK, _PENDING_CUT_CONFIRM,
pending_element_state, _PENDING_PLACE_FEEDBACK 등) — 각자 "값이 있으면 처리, 없으면 스킵"
정도의 얕은 게이트만 두고 있어서 같은 버그 클래스가 반복됐다:
  - 같은 이벤트가 두 번 오면(Slack 재전송/중복) 같은 pending을 두 번 소비할 수 있음
    (읽기와 소비가 분리돼 있어 그 사이에 경쟁 조건이 생김).
  - 실패를 "완료"와 구분 못 해서, 실패한 재실행이 다시 pending으로 저장되면 무한 루프가 됨.
  - 오래된 pending이 며칠 뒤 뜬금없이 살아나는 걸 막을 TTL이 없음.

이 모듈은 그 공통 부분(상태 필드 + consume-first 순서 + TTL + replay 재저장 금지)을
한 곳에 모은다. 각 pending 종류(kind)는 이 클래스를 통해서만 상태를 바꾼다.

상태 흐름:
    waiting → consuming → completed
    waiting → consuming → failed
(consuming에서 다시 waiting으로 자동으로는 돌아가지 않는다 — 실패는 실패로 남는다.)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

WAITING = "waiting"
CONSUMING = "consuming"
COMPLETED = "completed"
FAILED = "failed"
EXPIRED = "expired"

DEFAULT_TTL_SECONDS = 600  # 10분 — 이 시간 지난 pending은 waiting이어도 만료 처리


@dataclass
class PendingRecord:
    request_id: str
    kind: str
    status: str
    payload: dict = field(default_factory=dict)
    created_at: float = 0.0
    attempts: int = 0
    error: str | None = None


class PendingManager:
    """thread_id + kind로 키를 잡는 pending 상태머신. 프로세스 메모리에만 유지한다
    (기존 _PENDING_* dict들과 동일한 영속성 수준 — 봇 재시작하면 사라짐. 파일 영속이
    필요한 케이스(pending_element_state 등 이미지 바이트를 들고 있어야 하는 경우)는
    이 클래스로 대체하지 않고 그대로 둔다 — 이건 "상태 전이 규칙"만 통일하는 것)."""

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS, now_fn=time.time):
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], PendingRecord] = {}
        self._executed_request_ids: set[str] = set()
        self._ttl = ttl_seconds
        self._now = now_fn

    def _key(self, thread_id: str, kind: str) -> tuple[str, str]:
        return (thread_id, kind)

    def create(self, thread_id: str, kind: str, payload: dict, *, request_id: str) -> PendingRecord:
        """새 pending을 waiting 상태로 만든다. 같은 thread_id+kind에 이미 pending이
        있으면 덮어쓴다(직전 것은 자동 만료 취급 — 새 요청이 우선)."""
        with self._lock:
            rec = PendingRecord(
                request_id=request_id, kind=kind, status=WAITING,
                payload=dict(payload), created_at=self._now(), attempts=0,
            )
            self._records[self._key(thread_id, kind)] = rec
            return rec

    def peek(self, thread_id: str, kind: str) -> PendingRecord | None:
        """상태를 바꾸지 않고 조회만 한다(예: "제안을 이미 보냈나?" 확인용)."""
        with self._lock:
            rec = self._records.get(self._key(thread_id, kind))
            if rec is None:
                return rec
            if rec.status == WAITING and self._now() - rec.created_at > self._ttl:
                rec.status = EXPIRED
            return rec

    def consume(self, thread_id: str, kind: str, *, request_id: str | None = None) -> PendingRecord | None:
        """waiting 상태인 pending을 찾아 즉시 consuming으로 바꾸고 반환한다(원자적 —
        읽기와 상태 변경이 lock 안에서 같이 일어나 경쟁 조건이 없다). 이미 소비됐거나
        만료됐거나 request_id가 이미 실행된 적 있으면 None.

        핸들러는 반드시 이 반환값이 있을 때만 실제 작업을 실행하고, 끝나면
        complete()/fail()을 호출해야 한다 — "먼저 소비, 그다음 실행"이 이 클래스가
        강제하는 순서다."""
        with self._lock:
            key = self._key(thread_id, kind)
            rec = self._records.get(key)
            if rec is None:
                return None
            if rec.status != WAITING:
                return None
            if self._now() - rec.created_at > self._ttl:
                rec.status = EXPIRED
                return None
            if request_id and request_id in self._executed_request_ids:
                # 같은 요청(Slack 재전송 등)이 이미 실행됨 — 다시 소비하지 않는다.
                return None
            rec.status = CONSUMING
            rec.attempts += 1
            if request_id:
                self._executed_request_ids.add(request_id)
            return rec

    def complete(self, thread_id: str, kind: str) -> None:
        with self._lock:
            rec = self._records.get(self._key(thread_id, kind))
            if rec is not None:
                rec.status = COMPLETED

    def fail(self, thread_id: str, kind: str, error: str | None = None) -> None:
        with self._lock:
            rec = self._records.get(self._key(thread_id, kind))
            if rec is not None:
                rec.status = FAILED
                rec.error = error

    def expire(self, thread_id: str, kind: str) -> None:
        with self._lock:
            rec = self._records.get(self._key(thread_id, kind))
            if rec is not None:
                rec.status = EXPIRED

    def clear(self, thread_id: str, kind: str) -> None:
        with self._lock:
            self._records.pop(self._key(thread_id, kind), None)


def run_pending(
    manager: PendingManager,
    thread_id: str,
    kind: str,
    request_id: str | None,
    fn,
    *args,
    is_replay: bool = False,
    **kwargs,
) -> Any:
    """consume → fn(*payload) → complete/fail을 한 번에 처리하는 헬퍼.

    ★핵심 규칙(이 함수가 강제): fn이 실패해도, 그리고 그 실패가 "재실행(replay) 자체의
    실패"라면 절대 같은 kind로 새 pending을 다시 만들지 않는다 — is_replay=True로 호출된
    fn 안에서 실패 시 이 kind에 대해 manager.create()를 다시 호출하면 무한 루프가 된다.
    호출부(fn)는 event/payload에 "_replay" 표시를 남겨 자기 자신이 재실행인지 판단하고,
    재실행 실패 시엔 재저장하지 않는 책임을 진다 — 이 헬퍼는 상태 전이만 보장한다.
    fn이 반환하는 값을 그대로 돌려준다. consume()이 None이면(이미 소비됐거나 만료) None."""
    rec = manager.consume(thread_id, kind, request_id=request_id)
    if rec is None:
        return None
    try:
        result = fn(rec, *args, is_replay=is_replay, **kwargs)
        manager.complete(thread_id, kind)
        return result
    except Exception as exc:
        manager.fail(thread_id, kind, error=str(exc))
        raise
