# -*- coding: utf-8 -*-
"""router_labeler.py — 라우터 결정 로그를 훑어 오라우팅(mis-routing)으로 의심되는
결정에 플래그를 붙이는 배치 라벨러 (자기성장형 평가 파이프라인 산출물 2, 2026-07-21).

입력: logs/router_decisions.jsonl (router_log.read_records)
출력: logs/review_queue.jsonl (리플레이 하네스 deliverable 3가 소비하는 고정 계약)

4가지 실패 신호(스펙):
  - resend_similar     : 같은 thread에서 10분 내 유사(≥0.8) 재발화 → 앞선(오발) 결정 플래그
  - followup_negation  : 같은 thread에서 다음 발화가 아니/말고/왜/다시/…라고/…라니까로
                         시작 → 현재(앞선) 결정 플래그
  - safe_stop          : 자기 outcome == "safe_stop" → 플래그
  - cancel_after_exec  : executed 결정 뒤 60초 내 cancel_current_job(취소)이 옴 → executed 플래그

원칙(router_log.py와 동일): 라벨/파일 I/O는 절대 호출부로 예외를 던지지 않는다.
"""
from __future__ import annotations

import difflib
import json
import re
import sys
import time

from . import config
from . import router_log

QUEUE_PATH = config.BASE_DIR / "logs" / "review_queue.jsonl"

_SIMILAR_WINDOW_S = 600.0   # resend_similar / followup_negation 시간창(10분)
_CANCEL_WINDOW_S = 60.0     # cancel_after_exec 시간창(60초)
_SIMILAR_RATIO = 0.8

# 후속 부정/재요청 신호로 보는 접두사(선행 공백/멘션 허용)
_NEGATION_PREFIXES = ("아니", "말고", "왜", "다시")
# "…라고" / "…라니까" 는 접미(끝맺음) 신호 — 다음 발화가 이걸로 끝나면 재정정으로 본다
_NEGATION_SUFFIXES = ("라고", "라니까")

_MENTION_RE = re.compile(r"<@[^>]+>")
_PUNCT_WS_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _norm(text: str) -> str:
    """유사도 비교용 정규화: 멘션 제거 + 공백/구두점 제거 + 소문자."""
    t = _MENTION_RE.sub(" ", text or "")
    t = _PUNCT_WS_RE.sub("", t)
    return t.lower()


def _strip_lead(text: str) -> str:
    """선행 공백/멘션을 벗겨 접두 검사를 하기 위한 문자열."""
    t = _MENTION_RE.sub("", text or "")
    return t.lstrip()


def _ts(rec: dict) -> float:
    try:
        return float(rec.get("ts") or 0.0)
    except Exception:
        return 0.0


def _route_tool(rec: dict) -> str | None:
    route = rec.get("route")
    if isinstance(route, dict):
        return route.get("tool")
    return None


def _is_negation(text: str) -> bool:
    s = _strip_lead(text)
    if not s:
        return False
    if s.startswith(_NEGATION_PREFIXES):
        return True
    # "…라고" / "…라니까" 로 끝나는 정정 발화
    stripped = _PUNCT_WS_RE.sub("", s)
    return stripped.endswith(_NEGATION_SUFFIXES)


def label_records(records: list[dict]) -> list[dict]:
    """순수 함수(I/O 없음): 결정 레코드 리스트 → 리뷰큐 레코드 리스트.

    같은 request_id의 여러 신호는 하나의 레코드로 병합(signals 리스트)한다.
    review_queue 스키마:
      {"request_id","ts","thread_ts","text","signals":[...],"orig_outcome","labeled_ts"}
    (labeled_ts는 run()이 채운다 — 순수 함수 단계에선 넣지 않음)
    """
    if not records:
        return []

    # thread_ts별로 그룹핑(append 순서 유지). thread_ts 없으면 각자 단독 그룹.
    threads: dict[str, list[dict]] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        key = str(rec.get("thread_ts") or f"__solo__{id(rec)}")
        threads.setdefault(key, []).append(rec)

    # request_id → {signals set, rec}
    flagged: dict[str, dict] = {}

    def _flag(rec: dict, signal: str) -> None:
        rid = rec.get("request_id")
        if not rid:
            return
        entry = flagged.get(rid)
        if entry is None:
            entry = {"rec": rec, "signals": set()}
            flagged[rid] = entry
        entry["signals"].add(signal)

    for group in threads.values():
        ordered = sorted(group, key=_ts)
        n = len(ordered)
        for i, rec in enumerate(ordered):
            # safe_stop — 자기 자신
            if rec.get("outcome") == "safe_stop":
                _flag(rec, "safe_stop")

            ts_i = _ts(rec)
            norm_i = _norm(rec.get("text", ""))

            # 다음 레코드 기반 신호(followup_negation)
            if i + 1 < n:
                nxt = ordered[i + 1]
                if 0 <= (_ts(nxt) - ts_i) <= _SIMILAR_WINDOW_S and _is_negation(nxt.get("text", "")):
                    _flag(rec, "followup_negation")

            # resend_similar — 이후 10분 내 유사 재발화가 있으면 앞선(현재) 결정 플래그
            if norm_i:
                for j in range(i + 1, n):
                    later = ordered[j]
                    dt = _ts(later) - ts_i
                    if dt < 0:
                        continue
                    if dt > _SIMILAR_WINDOW_S:
                        break
                    norm_j = _norm(later.get("text", ""))
                    if not norm_j:
                        continue
                    ratio = difflib.SequenceMatcher(None, norm_i, norm_j).ratio()
                    if ratio >= _SIMILAR_RATIO:
                        _flag(rec, "resend_similar")
                        break

            # cancel_after_exec — executed 뒤 60초 내 cancel_current_job
            if rec.get("outcome") == "executed":
                for j in range(i + 1, n):
                    later = ordered[j]
                    dt = _ts(later) - ts_i
                    if dt < 0:
                        continue
                    if dt > _CANCEL_WINDOW_S:
                        break
                    if _route_tool(later) == "cancel_current_job" or "cancel" in str(later.get("outcome") or ""):
                        _flag(rec, "cancel_after_exec")
                        break

    out: list[dict] = []
    for rid, entry in flagged.items():
        rec = entry["rec"]
        out.append({
            "request_id": rid,
            "ts": _ts(rec),
            "thread_ts": rec.get("thread_ts"),
            "text": rec.get("text"),
            "signals": sorted(entry["signals"]),
            "orig_outcome": rec.get("outcome"),
        })
    # 결정 시간순으로 정렬(안정적 출력)
    out.sort(key=lambda r: (r["ts"], r["request_id"]))
    return out


def _read_existing_queue(path) -> dict[str, dict]:
    """기존 review_queue.jsonl을 request_id → 레코드 dict로 읽는다(멱등 병합용)."""
    existing: dict[str, dict] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                rid = rec.get("request_id")
                if rid:
                    existing[rid] = rec
    except FileNotFoundError:
        return {}
    except Exception:
        return existing
    return existing


def run(log_path=None, queue_path=None, now=time.time) -> int:
    """결정 로그를 읽어 라벨링하고 review_queue.jsonl에 멱등 병합한다.
    새로 추가된 플래그 수를 반환. 어떤 I/O 실패도 호출부로 던지지 않는다."""
    try:
        queue_path = queue_path or QUEUE_PATH
        records = router_log.read_records(since_ts=None, path=log_path)
        labeled = label_records(records)
        existing = _read_existing_queue(queue_path)

        try:
            labeled_ts = float(now())
        except Exception:
            labeled_ts = time.time()

        new_count = 0
        merged: dict[str, dict] = dict(existing)
        for rec in labeled:
            rid = rec["request_id"]
            if rid in merged:
                # 이미 있는 request_id는 신호만 병합(중복 append 방지 = 멱등)
                prev = merged[rid]
                prev_signals = set(prev.get("signals") or [])
                new_signals = prev_signals | set(rec.get("signals") or [])
                if new_signals != prev_signals:
                    prev["signals"] = sorted(new_signals)
                continue
            rec = dict(rec)
            rec["labeled_ts"] = labeled_ts
            merged[rid] = rec
            new_count += 1

        # 전체 재작성(멱등 + 신호 병합 반영). append 대신 원자적 재작성.
        try:
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            for rec in sorted(merged.values(), key=lambda r: (float(r.get("ts") or 0), r.get("request_id") or "")):
                lines.append(json.dumps(rec, ensure_ascii=False))
            with open(queue_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
        except Exception:
            return 0
        return new_count
    except Exception:
        return 0


def main() -> int:
    n = run()
    total = len(_read_existing_queue(QUEUE_PATH))
    print(f"router_labeler: 신규 플래그 {n}건, 리뷰큐 총 {total}건 → {QUEUE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
