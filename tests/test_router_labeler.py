# -*- coding: utf-8 -*-
"""test_router_labeler.py — router_labeler 유닛 테스트(순수 파이썬, LLM/네트워크 없음).

실행: python3 -m tests.test_router_labeler
합성 픽스처로 4개 신호가 각각 발화하는지, 정상 대화에서 오탐이 0인지,
run()의 멱등성을 검증한다.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from bot import router_labeler


def _rec(request_id, thread_ts, ts, text, outcome="executed", tool=None):
    return {
        "ts": ts, "request_id": request_id, "channel": "C1",
        "thread_ts": thread_ts, "text": text,
        "route": {"type": "tool_call", "tool": tool, "tools": [tool] if tool else []},
        "executed_handler": tool, "outcome": outcome,
    }


def _signals_for(labeled, rid):
    for r in labeled:
        if r["request_id"] == rid:
            return set(r["signals"])
    return None


def test_resend_similar():
    recs = [
        _rec("a", "T1", 1000.0, "3화 스토리보드 만들어줘"),
        _rec("b", "T1", 1120.0, "3화 스토리보드 만들어 줘!!"),  # 120s 뒤 유사
    ]
    labeled = router_labeler.label_records(recs)
    assert _signals_for(labeled, "a") == {"resend_similar"}, labeled
    # 뒤 발화(b)는 자기 뒤에 유사 재발화가 없으므로 플래그 안됨
    assert _signals_for(labeled, "b") is None, labeled
    print("  ✅ resend_similar")


def test_resend_similar_out_of_window():
    recs = [
        _rec("a", "T1", 1000.0, "3화 스토리보드 만들어줘"),
        _rec("b", "T1", 2000.0, "3화 스토리보드 만들어줘"),  # 1000s > 600s
    ]
    labeled = router_labeler.label_records(recs)
    assert labeled == [], labeled
    print("  ✅ resend_similar (시간창 밖 → 무플래그)")


def test_followup_negation():
    recs = [
        _rec("a", "T1", 1000.0, "4화 컷 나눠줘"),
        _rec("b", "T1", 1050.0, "아니 그거 말고 5화"),  # 다음이 '아니'로 시작
    ]
    labeled = router_labeler.label_records(recs)
    assert _signals_for(labeled, "a") == {"followup_negation"}, labeled
    print("  ✅ followup_negation")


def test_followup_negation_suffix():
    recs = [
        _rec("a", "T1", 1000.0, "제목 지어줘"),
        _rec("b", "T1", 1030.0, "<@U1> 5화라고"),  # 멘션 후 '…라고'로 끝
    ]
    labeled = router_labeler.label_records(recs)
    assert _signals_for(labeled, "a") == {"followup_negation"}, labeled
    print("  ✅ followup_negation (…라고 접미)")


def test_safe_stop():
    recs = [_rec("a", "T1", 1000.0, "위험한 요청", outcome="safe_stop")]
    labeled = router_labeler.label_records(recs)
    assert _signals_for(labeled, "a") == {"safe_stop"}, labeled
    print("  ✅ safe_stop")


def test_cancel_after_exec():
    recs = [
        _rec("a", "T1", 1000.0, "전체 렌더 돌려줘", outcome="executed", tool="render_all"),
        _rec("b", "T1", 1030.0, "아 잠깐 멈춰", outcome="executed", tool="cancel_current_job"),
    ]
    labeled = router_labeler.label_records(recs)
    # a: executed 뒤 60초 내 cancel → cancel_after_exec.
    # b: '아' 시작이지만 부정 접두사 아님(아니/말고/왜/다시). a에 대해선 cancel 신호만.
    assert _signals_for(labeled, "a") == {"cancel_after_exec"}, labeled
    print("  ✅ cancel_after_exec")


def test_clean_conversation_zero_flags():
    """정상적인 다중 메시지 대화 — 오탐 0이어야 한다."""
    recs = [
        _rec("a", "T9", 1000.0, "3화 스토리보드 만들어줘", outcome="executed", tool="make_storyboard"),
        _rec("b", "T9", 1100.0, "좋아 이제 4화도 해줘", outcome="executed", tool="make_storyboard"),
        _rec("c", "T9", 1300.0, "제목은 뭐가 좋을까?", outcome="answer"),
        _rec("d", "T9", 1500.0, "고마워", outcome="short_ack"),
    ]
    labeled = router_labeler.label_records(recs)
    assert labeled == [], labeled
    print("  ✅ clean conversation → 0 flags (오탐 없음)")


def test_multiple_signals_merge():
    """같은 결정이 여러 신호에 걸리면 하나의 레코드로 병합."""
    recs = [
        _rec("a", "T1", 1000.0, "5화 렌더", outcome="safe_stop"),
        _rec("b", "T1", 1050.0, "아니 다시", outcome="answer"),
    ]
    labeled = router_labeler.label_records(recs)
    # a: safe_stop + followup_negation(다음이 '아니')
    assert _signals_for(labeled, "a") == {"safe_stop", "followup_negation"}, labeled
    # request_id 'a' 는 정확히 한 레코드
    assert sum(1 for r in labeled if r["request_id"] == "a") == 1, labeled
    print("  ✅ 다중 신호 병합 (1 request_id = 1 레코드)")


def test_run_idempotent():
    recs = [
        _rec("a", "T1", 1000.0, "3화 스토리보드 만들어줘"),
        _rec("b", "T1", 1120.0, "3화 스토리보드 만들어 줘!!"),
        _rec("c", "T2", 2000.0, "위험", outcome="safe_stop"),
    ]
    with tempfile.TemporaryDirectory() as d:
        log_path = Path(d) / "router_decisions.jsonl"
        queue_path = Path(d) / "review_queue.jsonl"
        import json
        with open(log_path, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        n1 = router_labeler.run(log_path=log_path, queue_path=queue_path, now=lambda: 5000.0)
        n2 = router_labeler.run(log_path=log_path, queue_path=queue_path, now=lambda: 6000.0)
        lines = queue_path.read_text(encoding="utf-8").strip().splitlines()
        assert n1 == 2, f"첫 실행 신규 {n1} (기대 2: a=resend, c=safe_stop)"
        assert n2 == 0, f"두번째 실행 신규 {n2} (기대 0, 멱등)"
        assert len(lines) == 2, f"큐 라인 {len(lines)} (기대 2)"
        # labeled_ts는 첫 실행 값 유지(재작성돼도 기존 레코드는 보존)
        for line in lines:
            rec = json.loads(line)
            assert rec["labeled_ts"] == 5000.0, rec
            assert "signals" in rec and rec["signals"]
        print(f"  ✅ run() 멱등성 (1차 {n1}건, 2차 {n2}건, 큐 {len(lines)}줄, labeled_ts 보존)")


def main() -> int:
    print("router_labeler 테스트:")
    test_resend_similar()
    test_resend_similar_out_of_window()
    test_followup_negation()
    test_followup_negation_suffix()
    test_safe_stop()
    test_cancel_after_exec()
    test_clean_conversation_zero_flags()
    test_multiple_signals_merge()
    test_run_idempotent()
    print("\n전부 통과 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
