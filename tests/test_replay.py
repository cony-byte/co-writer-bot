# -*- coding: utf-8 -*-
"""test_replay.py — 리플레이 하네스의 선택/분류/집계 검증(모의 라우터).

실LLM 호출 없이 tool_router.decide_from_context 를 canned Decision 반환으로 몽키패치해
improved/regression/changed/preserved 버킷과 fix/preservation rate, exit code를 검증한다.

  python3 -m tests.test_replay
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

_NOW = time.time()

from bot import tool_router
from tests import replay


def _rec(rid, text, rtype, tool, slots, ctx=True):
    return {
        "ts": _NOW,
        "request_id": rid,
        "channel": "C1",
        "thread_ts": "t1",
        "text": text,
        "ctx_snapshot": ({"foo": "bar"} if ctx else None),
        "route": {"type": rtype, "tool": tool, "tools": [tool] if tool else [], "slots": slots},
        "executed_handler": tool,
        "outcome": "executed",
    }


def _decision(rtype, tool, slots):
    return tool_router.Decision(
        type=rtype, tool=tool, arguments=slots,
        calls=([{"tool": tool, "arguments": slots}] if tool else None),
        raw={},
    )


def main():
    # 시나리오:
    #  r-lab-fixed: 라벨됨, 새 결정 다름 → improved
    #  r-lab-same : 라벨됨, 새 결정 동일 → preserved(고쳐지진 않음)
    #  r-good-brk : 비라벨, 새 결정 다름 → regression
    #  r-good-ok  : 비라벨, 새 결정 동일 → preserved
    #  r-null     : ctx null → skipped(선택 제외)
    records = [
        _rec("r-lab-fixed", "씬1 스틸컷", "clarification", None, None),
        _rec("r-lab-same", "상태 어때", "answer", None, None),
        _rec("r-good-brk", "스토리보드 그려줘", "tool_call", "generate_storyboard_grid",
             {"work": "A", "episode": 1}),
        _rec("r-good-ok", "피드백해줘", "tool_call", "review_script", {"work": "B"}),
        _rec("r-null", "크래시난거", "tool_call", "x", None, ctx=False),
    ]

    # 새(현재 워킹트리) 결정을 request_id로 흉내낸다.
    new_by_text = {
        "씬1 스틸컷": _decision("tool_call", "generate_stillcuts", {"scene": 1}),      # 라벨 → 다름
        "상태 어때": _decision("answer", None, None),                                  # 라벨 → 동일
        "스토리보드 그려줘": _decision("clarification", None, None),                    # 비라벨 → 다름(회귀)
        "피드백해줘": _decision("tool_call", "review_script", {"work": "B"}),           # 비라벨 → 동일
    }

    orig = tool_router.decide_from_context
    tool_router.decide_from_context = lambda text, ctx, **kw: new_by_text[text]
    try:
        with tempfile.TemporaryDirectory() as d:
            log_p = Path(d) / "router_decisions.jsonl"
            q_p = Path(d) / "review_queue.jsonl"
            with open(log_p, "w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            with open(q_p, "w", encoding="utf-8") as f:
                for rid in ("r-lab-fixed", "r-lab-same"):
                    f.write(json.dumps({"request_id": rid, "ts": _NOW,
                                        "signals": ["resend_similar"], "orig_outcome": "executed",
                                        "labeled_ts": _NOW}) + "\n")

            # 선택 검증(only-labeled)
            labeled = replay.load_labeled_request_ids(path=str(q_p))
            assert labeled == {"r-lab-fixed", "r-lab-same"}, labeled

            # since_ts 아주 과거로 → read_records 로 모두 로드
            from bot import router_log
            recs = router_log.read_records(since_ts=0, path=str(log_p))
            assert len(recs) == 5, len(recs)

            # ---- ALL 모드 분류 ----
            selected, skipped = replay.select_records(recs, labeled, only_labeled=False, limit=None)
            assert len(selected) == 4 and len(skipped) == 1, (len(selected), len(skipped))
            buckets = replay.run_replay(selected, labeled)
            got = {k: sorted(x["request_id"] for x in v) for k, v in buckets.items()}
            assert got["improved"] == ["r-lab-fixed"], got
            assert got["regression"] == ["r-good-brk"], got
            assert sorted(got["preserved"]) == ["r-good-ok", "r-lab-same"], got
            assert got["changed"] == [] and got["error"] == [], got

            # rates: fix = improved/라벨재실행(2) = 50%; preservation = 비라벨보존(1)/비라벨재실행(2)=50%
            n_lab = sum(1 for r in selected if r["request_id"] in labeled)
            n_non = len(selected) - n_lab
            assert n_lab == 2 and n_non == 2
            preserved_non = sum(1 for r in buckets["preserved"] if r["request_id"] not in labeled)
            assert preserved_non == 1

            # exit code: 회귀 1건 → main() 이 1 반환
            rc = replay.main(["--days", "9999", "--all", "--yes",
                              "--log-path", str(log_p), "--queue-path", str(q_p)])
            assert rc == 1, rc

            # only-labeled 모드: 라벨 2건만, 회귀 0 → exit 0
            rc2 = replay.main(["--days", "9999", "--only-labeled", "--yes",
                               "--log-path", str(log_p), "--queue-path", str(q_p)])
            assert rc2 == 0, rc2

            # --limit 검증
            sel2, _ = replay.select_records(recs, labeled, only_labeled=False, limit=2)
            assert len(sel2) == 2
    finally:
        tool_router.decide_from_context = orig

    print("OK: 선택/분류/rate/exit-code 검증 통과")


if __name__ == "__main__":
    main()
