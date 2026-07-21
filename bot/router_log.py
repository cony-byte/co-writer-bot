# -*- coding: utf-8 -*-
"""router_log.py — 라우터 결정 로그 (자기성장형 평가 파이프라인의 기반, 2026-07-21).

모든 인바운드 메시지가 tool_router를 거칠 때 결정 1건을 logs/router_decisions.jsonl에
1줄(JSON)로 남긴다. 이 로그가 나머지 평가 인프라 전체의 단일 소스다:
  - 실패 라벨러(router_labeler.py) — 오라우팅으로 의심되는 결정에 플래그
  - 리플레이 하네스(tests/replay.py) — (text, ctx_snapshot)을 후보 라우터로 재실행해 diff
  - 주간 지표(router_report.py) — 오라우팅률/safe_stop률/지연

레코드 스키마(1줄 = 1 결정):
  {
    "ts": <epoch float>,
    "iso": "<ISO8601 KST>",
    "request_id": "<client_msg_id|event_ts|생성값>",   # 중복 이벤트 dedup·리플레이 키
    "channel": "...",
    "thread_ts": "...",
    "text": "<사용자 발화>",
    "ctx_snapshot": {...},        # 라우터에 실제 주입된 컨텍스트 그대로(재현성 핵심).
                                  #   결정을 못 만든 경우(LLM 실패 등) null일 수 있음.
    "route": {
        "type": "answer|clarification|tool_call|tool_calls|null",
        "tool": "<첫 실행 tool|null>",
        "tools": ["..."],         # 전체 호출 tool 목록(복합 요청 포함)
        "slots": {...},           # 첫 실행 호출의 arguments(있으면)
        "conf": null,             # native function-calling엔 confidence 개념이 없어 항상 null
        "backend": "<모델 id>",
        "latency_ms": <int|null>
    },
    "executed_handler": "<실제 실행/응답한 tool 또는 응답 종류>",
    "outcome": "executed|answer|clarification|short_ack|deterministic_answer|
                safe_stop|killswitch_legacy|exception"
  }

원칙: **로깅은 절대 라우팅을 깨지 않는다.** 모든 I/O·직렬화를 try/except로 감싸고,
실패하면 조용히 로그만 포기한다(라우팅 흐름에는 영향 0).
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta

from . import config

LOG_PATH = config.BASE_DIR / "logs" / "router_decisions.jsonl"
_KST = timezone(timedelta(hours=9))
_WRITE_LOCK = threading.Lock()

# 실무 안전장치: 발화 텍스트/컨텍스트가 비정상적으로 크면(붙여넣은 대본 전체 등) 잘라 저장한다
# — 로그가 무한정 커지는 걸 막되 재현성엔 충분한 길이.
_MAX_TEXT = 4000
_MAX_CTX_JSON = 60000


class DecisionRecord:
    """capture() 컨텍스트매니저가 돌려주는 가변 레코드. 브랜치마다 outcome/decision을
    채우면 __exit__ 시점에 한 줄로 기록된다."""

    __slots__ = ("ts", "request_id", "channel", "thread_ts", "text",
                 "ctx_snapshot", "route", "executed_handler", "outcome", "_written")

    def __init__(self, channel: str, thread_ts: str, text: str, request_id: str):
        self.ts = time.time()
        self.request_id = request_id
        self.channel = channel
        self.thread_ts = thread_ts
        self.text = (text or "")[:_MAX_TEXT]
        self.ctx_snapshot = None
        self.route = None
        self.executed_handler = None
        self.outcome = None
        self._written = False

    def set_decision(self, decision) -> None:
        """tool_router.Decision을 route/ctx_snapshot으로 반영한다. 예외를 삼켜 라우팅에
        영향 주지 않는다."""
        try:
            raw = getattr(decision, "raw", None) or {}
            ctx = raw.get("context")
            self.ctx_snapshot = _clip_ctx(ctx)
            calls = list(getattr(decision, "calls", None) or [])
            tools = [c.get("tool") for c in calls if isinstance(c, dict)]
            first = calls[0] if calls else {}
            self.route = {
                "type": getattr(decision, "type", None),
                "tool": getattr(decision, "tool", None) or (tools[0] if tools else None),
                "tools": tools,
                "slots": (first.get("arguments") if isinstance(first, dict) else None),
                "conf": None,  # native function calling: 신뢰도 없음
                "backend": raw.get("backend"),
                "latency_ms": raw.get("latency_ms"),
            }
        except Exception:
            pass

    def _to_json_line(self) -> str:
        return json.dumps({
            "ts": self.ts,
            "iso": datetime.fromtimestamp(self.ts, _KST).isoformat(),
            "request_id": self.request_id,
            "channel": self.channel,
            "thread_ts": self.thread_ts,
            "text": self.text,
            "ctx_snapshot": self.ctx_snapshot,
            "route": self.route,
            "executed_handler": self.executed_handler,
            "outcome": self.outcome,
        }, ensure_ascii=False)


def _clip_ctx(ctx):
    """ctx_snapshot을 그대로 저장하되(재현성), 과도하게 크면 잘라 로그 폭주를 막는다.
    이미지 바이트 같은 건 ctx에 없음(attachments는 {id,name,mimetype} 메타뿐)."""
    if ctx is None:
        return None
    try:
        s = json.dumps(ctx, ensure_ascii=False)
        if len(s) <= _MAX_CTX_JSON:
            return ctx
        # 너무 크면 recent_messages를 줄여 재직렬화 — 그래도 크면 통째로 표시만.
        trimmed = dict(ctx)
        if isinstance(trimmed.get("recent_messages"), list):
            trimmed["recent_messages"] = trimmed["recent_messages"][-4:]
        s2 = json.dumps(trimmed, ensure_ascii=False)
        if len(s2) <= _MAX_CTX_JSON:
            trimmed["_clipped"] = True
            return trimmed
        return {"_clipped": True, "_reason": "ctx too large to log"}
    except Exception:
        return {"_clipped": True, "_reason": "ctx not json-serializable"}


class _Capture:
    def __init__(self, rec: DecisionRecord):
        self.rec = rec

    def __enter__(self) -> DecisionRecord:
        return self.rec

    def __exit__(self, exc_type, exc, tb):
        # 라우팅 도중 예외가 위로 전파되는 경우에도 outcome을 남기고 기록한다.
        if exc is not None and not self.rec.outcome:
            self.rec.outcome = "exception"
        _write(self.rec)
        return False  # 예외는 그대로 전파(우리가 삼키지 않는다)


def capture(channel: str, thread_ts: str, text: str, event: dict) -> _Capture:
    """with router_log.capture(...) as rec: ... 형태로 사용. 블록이 어떤 경로로 끝나든
    (정상 return / 예외) 레코드 1줄을 기록한다. request_id는 event에서 뽑아 중복 이벤트를
    같은 키로 묶는다(리플레이/dedup용)."""
    request_id = ""
    try:
        request_id = str((event or {}).get("client_msg_id")
                         or (event or {}).get("event_ts")
                         or (event or {}).get("ts")
                         or uuid.uuid4().hex)
    except Exception:
        request_id = uuid.uuid4().hex
    return _Capture(DecisionRecord(channel, thread_ts, text, request_id))


def _write(rec: DecisionRecord) -> None:
    if rec._written:
        return
    rec._written = True
    try:
        line = rec._to_json_line()
    except Exception:
        return
    try:
        with _WRITE_LOCK:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        # 디스크/권한 문제 등 — 로깅 실패가 봇을 멈추게 하지 않는다.
        pass


# ── 읽기 유틸 (라벨러/리플레이/리포트 공용) ─────────────────────────────
def read_records(since_ts: float | None = None, path=None) -> list[dict]:
    """jsonl을 파싱해 레코드 리스트로 반환(깨진 줄은 건너뜀). since_ts가 주어지면 그 이후만."""
    p = path or LOG_PATH
    out: list[dict] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if since_ts is not None and float(rec.get("ts") or 0) < since_ts:
                    continue
                out.append(rec)
    except FileNotFoundError:
        return []
    except Exception:
        return out
    return out
