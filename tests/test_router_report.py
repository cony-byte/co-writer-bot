# -*- coding: utf-8 -*-
"""test_router_report.py — router_report.build_report/compute_metrics 단위테스트.

pytest 없이 plain main() + assert + print (test_tool_router.py 스타일). 합성 픽스처로
임시 router_decisions.jsonl + review_queue.jsonl을 만들어 지표 계산을 검증한다.
네트워크/Slack 토큰 불필요 — build_report는 두 파일에 대한 순수 함수.

사용법: python3 -m tests.test_router_report
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> int:
    # slack_io 토큰 없이 임포트되는지부터 확인(모듈 최상단에 slack_io import 금지 계약).
    from bot import router_report

    base = 1_000_000.0  # 임의의 epoch 기준
    # 결정 로그: 5건. backend A(3건, latency 10/20/30), backend B(2건, 40/None).
    # outcome: executed x3, safe_stop x1, answer x1.
    decisions = [
        {"ts": base + 1, "request_id": "r1", "thread_ts": "t1", "outcome": "executed",
         "route": {"backend": "A", "latency_ms": 10}},
        {"ts": base + 2, "request_id": "r2", "thread_ts": "t1", "outcome": "executed",
         "route": {"backend": "A", "latency_ms": 20}},
        {"ts": base + 3, "request_id": "r3", "thread_ts": "t2", "outcome": "answer",
         "route": {"backend": "A", "latency_ms": 30}},
        {"ts": base + 4, "request_id": "r4", "thread_ts": "t2", "outcome": "safe_stop",
         "route": {"backend": "B", "latency_ms": 40}},
        {"ts": base + 5, "request_id": "r5", "thread_ts": "t3", "outcome": "executed",
         "route": {"backend": "B", "latency_ms": None}},
    ]
    # review_queue: r2, r4 실패 라벨 (r4는 safe_stop 신호).
    queue = [
        {"request_id": "r2", "ts": base + 2, "thread_ts": "t1", "text": "x",
         "signals": ["resend_similar"], "orig_outcome": "executed", "labeled_ts": base + 100},
        {"request_id": "r4", "ts": base + 4, "thread_ts": "t2", "text": "y",
         "signals": ["safe_stop"], "orig_outcome": "safe_stop", "labeled_ts": base + 100},
    ]

    with tempfile.TemporaryDirectory() as td:
        log_path = Path(td) / "router_decisions.jsonl"
        queue_path = Path(td) / "review_queue.jsonl"
        _write_jsonl(log_path, decisions)
        _write_jsonl(queue_path, queue)

        # 전체 시간창(since=None) — 5건 모두 포함.
        m = router_report.compute_metrics(
            since_ts=None, until_ts=None, log_path=log_path, queue_path=queue_path)

        assert m["total"] == 5, m["total"]
        # 오라우팅률: 라벨된 r2,r4 → 2/5.
        assert m["labeled"] == 2, m["labeled"]
        assert abs(m["mis_route_rate"] - 2 / 5) < 1e-9, m["mis_route_rate"]
        # safe_stop률: 1/5.
        assert m["safe_stops"] == 1, m["safe_stops"]
        assert abs(m["safe_stop_rate"] - 1 / 5) < 1e-9, m["safe_stop_rate"]

        # 백엔드 A: latencies [10,20,30] → mean 20, p50=20, p95=30.
        a = m["latency_by_backend"]["A"]
        assert a["count"] == 3, a
        assert abs(a["mean"] - 20.0) < 1e-9, a
        # nearest-rank: p50 → ceil(0.5*3)-1 = 1 → sorted[1]=20.
        assert a["p50"] == 20.0, a
        # p95 → ceil(0.95*3)-1 = 2 → sorted[2]=30.
        assert a["p95"] == 30.0, a

        # 백엔드 B: latencies [40] (None 제외) → count 1, mean/p50/p95 = 40.
        b = m["latency_by_backend"]["B"]
        assert b["count"] == 1, b
        assert b["mean"] == 40.0 and b["p50"] == 40.0 and b["p95"] == 40.0, b

        # outcome 분포.
        assert m["outcomes"]["executed"] == 3, m["outcomes"]
        assert m["outcomes"]["safe_stop"] == 1, m["outcomes"]
        assert m["outcomes"]["answer"] == 1, m["outcomes"]

        # 신호 카운트.
        assert m["signals"]["resend_similar"] == 1, m["signals"]
        assert m["signals"]["safe_stop"] == 1, m["signals"]

        # 시간창 필터: since=base+3 → r3,r4,r5만(3건). 라벨된 건 r4 → 1/3.
        m2 = router_report.compute_metrics(
            since_ts=base + 3, until_ts=None, log_path=log_path, queue_path=queue_path)
        assert m2["total"] == 3, m2["total"]
        assert m2["labeled"] == 1, m2["labeled"]

        # build_report: 비어있지 않은 mrkdwn 문자열 + 기대 숫자 포함.
        # until_ts를 주어 build_report의 "둘 다 None → 최근7일" 기본창을 우회(픽스처는 옛 epoch).
        text = router_report.build_report(
            since_ts=None, until_ts=base + 100, log_path=log_path, queue_path=queue_path)
        assert isinstance(text, str) and text.strip(), "empty report"
        assert "오라우팅률" in text, text
        assert "40.0%" in text, text            # mis-route 2/5
        assert "20.0%" in text, text            # safe_stop 1/5
        assert "(2/5)" in text, text
        assert "`A`" in text and "`B`" in text, text
        assert "resend_similar" in text, text
        print(text)
        print("\n[missing files → 빈 결과]")

        # 없는 파일 → total 0, rate 0, 예외 없음.
        empty = router_report.build_report(
            since_ts=None, until_ts=base + 100,
            log_path=Path(td) / "nope.jsonl", queue_path=Path(td) / "nope2.jsonl")
        assert "총 결정 수(volume): *0*" in empty, empty

    print("\n✅ all asserts passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
