# -*- coding: utf-8 -*-
"""replay.py — 라우터 리플레이 하네스 (자기성장형 평가 파이프라인 산출물 3, 2026-07-21).

로그된 결정(logs/router_decisions.jsonl)을 현재 워킹트리의 tool_router로 재실행하고,
새 결정을 로그된 결정과 diff 해 개선/회귀/변경 3-버킷으로 분류한다.

  python3 -m tests.replay --days 3 [--candidate <name>] [--limit N]
                          [--only-labeled] [--all] [--yes]

주의: 리플레이는 **현재 디스크의 코드**(bot.tool_router)를 in-process로 돌린다.
      다른 브랜치를 체크아웃하지 않는다(크로스-브랜치 실행은 범위 밖). 따라서
      후보를 평가하려면: 먼저 후보 브랜치를 체크아웃한 뒤 리플레이를 돌린다.
      --candidate 는 리포트 헤더에 이름만 기록하는 정보용 플래그다.

각 재실행은 실LLM 호출(≈2–3s, ≈$0.01/건)이므로 표본/상한을 반드시 지원하고 실행 전
예상 비용/시간을 출력한다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from bot import router_log, tool_router
from bot import config

# 대략적 단가/지연 추정치(리포트용). 캐싱 포함 실측 근사.
_COST_PER_CALL = 0.01
_SEC_PER_CALL = 2.5
_MAX_WORKERS = 6

REVIEW_QUEUE_PATH = config.BASE_DIR / "logs" / "review_queue.jsonl"

# 시그니처 비교에 쓰는 핵심 슬롯(스펙: work/episode/scene/cuts).
_KEY_SLOTS = ("work", "episode", "scene", "cuts")


# ---------------------------------------------------------------------------
# 시그니처 & diff
# ---------------------------------------------------------------------------
def _slots_sig(slots) -> tuple:
    slots = slots or {}
    return tuple((k, slots.get(k)) for k in _KEY_SLOTS)


def _route_signature(route_or_decision) -> tuple:
    """로그된 route(dict) 또는 새 Decision을 (type, first_tool, key_slots)로 정규화."""
    if isinstance(route_or_decision, dict):
        # 로그된 route
        r = route_or_decision
        rtype = r.get("type")
        tool = r.get("tool")
        slots = r.get("slots")
    else:
        # tool_router.Decision
        d = route_or_decision
        rtype = getattr(d, "type", None)
        calls = list(getattr(d, "calls", None) or [])
        first = calls[0] if calls else {}
        tool = getattr(d, "tool", None) or (first.get("tool") if isinstance(first, dict) else None)
        slots = getattr(d, "arguments", None)
        if slots is None and isinstance(first, dict):
            slots = first.get("arguments")
    return (rtype, tool, _slots_sig(slots))


def _sig_str(sig: tuple) -> str:
    rtype, tool, slots = sig
    kv = " ".join(f"{k}={v!r}" for k, v in slots if v is not None)
    return f"{rtype}:{tool or '-'}" + (f" [{kv}]" if kv else "")


# ---------------------------------------------------------------------------
# 라벨(실패) 로드
# ---------------------------------------------------------------------------
def load_labeled_request_ids(path=None) -> set:
    """review_queue.jsonl에서 실패-라벨된 request_id 집합. 없으면 빈 집합."""
    p = path or REVIEW_QUEUE_PATH
    ids: set = set()
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
                rid = rec.get("request_id")
                if rid:
                    ids.add(rid)
    except FileNotFoundError:
        return ids
    except Exception:
        return ids
    return ids


# ---------------------------------------------------------------------------
# 선택
# ---------------------------------------------------------------------------
def select_records(records, labeled_ids, *, only_labeled, limit):
    """(replayable, skipped) 반환 후 라벨/상한 적용. skipped = ctx_snapshot null."""
    replayable = [r for r in records if r.get("ctx_snapshot") is not None]
    skipped = [r for r in records if r.get("ctx_snapshot") is None]
    if only_labeled:
        selected = [r for r in replayable if r.get("request_id") in labeled_ids]
    else:
        selected = list(replayable)
    if limit is not None:
        selected = selected[:limit]
    return selected, skipped


# ---------------------------------------------------------------------------
# 재실행 + 분류
# ---------------------------------------------------------------------------
def _replay_one(rec):
    """단건 재실행. 예외는 삼켜 error로 표시(전체 실행을 중단하지 않음)."""
    try:
        decision = tool_router.decide_from_context(rec.get("text") or "", rec.get("ctx_snapshot") or {})
        return rec, _route_signature(decision), None
    except Exception as e:  # noqa: BLE001
        return rec, None, repr(e)


def classify(rec, new_sig, error, labeled_ids):
    """3-버킷 분류. 반환: (bucket, logged_sig, new_sig).
    bucket ∈ {improved, regression, changed, preserved, error}."""
    logged_sig = _route_signature(rec.get("route") or {})
    if error is not None:
        return "error", logged_sig, None
    is_labeled = rec.get("request_id") in labeled_ids
    differs = new_sig != logged_sig
    if not differs:
        return "preserved", logged_sig, new_sig
    if is_labeled:
        # 라벨된(불량) 케이스가 바뀜 → 고쳐졌다고 본다.
        return "improved", logged_sig, new_sig
    # 라벨 안 된(양호) 케이스가 바뀜 → 이전에 좋던 걸 깨뜨렸을 수 있다.
    return "regression", logged_sig, new_sig


def run_replay(selected, labeled_ids, *, workers=_MAX_WORKERS):
    buckets = {"improved": [], "regression": [], "changed": [], "preserved": [], "error": []}
    if not selected:
        return buckets
    with ThreadPoolExecutor(max_workers=min(workers, len(selected))) as ex:
        futures = [ex.submit(_replay_one, rec) for rec in selected]
        for fut in as_completed(futures):
            rec, new_sig, error = fut.result()
            bucket, logged_sig, nsig = classify(rec, new_sig, error, labeled_ids)
            buckets[bucket].append({
                "request_id": rec.get("request_id"),
                "text": rec.get("text"),
                "logged_sig": logged_sig,
                "new_sig": nsig,
                "error": error,
            })
    return buckets


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------
def _pct(n, d):
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


def print_report(buckets, selected, labeled_ids, *, candidate, mode, skipped_n):
    n_labeled = sum(1 for r in selected if r.get("request_id") in labeled_ids)
    n_nonlabeled = len(selected) - n_labeled
    improved = buckets["improved"]
    regression = buckets["regression"]
    changed = buckets["changed"]
    preserved = buckets["preserved"]
    error = buckets["error"]

    # fix rate = improved / (실패-라벨 재실행), preservation = preserved / (비라벨 재실행)
    # preserved 중 비라벨 개수로 preservation 산출.
    preserved_nonlabeled = sum(1 for r in preserved if r["request_id"] not in labeled_ids)

    print("=" * 72)
    print(f"라우터 리플레이 리포트  (candidate={candidate or '(current working tree)'})")
    print("  주의: 리플레이는 현재 워킹트리의 bot.tool_router를 실행함(브랜치 체크아웃 없음).")
    print(f"  모드: {mode}   선택={len(selected)}건  스킵(ctx null)={skipped_n}건")
    print("=" * 72)
    print(f"개선(improved)   : {len(improved)}")
    print(f"회귀(regression) : {len(regression)}")
    print(f"변경(changed)    : {len(changed)}")
    print(f"보존(preserved)  : {len(preserved)}")
    if error:
        print(f"오류(error)      : {len(error)}")
    print("-" * 72)
    print(f"fix rate         : {_pct(len(improved), n_labeled)}  "
          f"(개선 {len(improved)} / 실패-라벨 재실행 {n_labeled})")
    print(f"preservation rate: {_pct(preserved_nonlabeled, n_nonlabeled)}  "
          f"(보존 {preserved_nonlabeled} / 비라벨 재실행 {n_nonlabeled})")
    print("=" * 72)

    def _dump(title, items, loud=False):
        if not items:
            return
        bar = "!" if loud else "-"
        print(f"\n{bar * 3} {title} ({len(items)}) {bar * 3}")
        for it in items:
            txt = (it["text"] or "").replace("\n", " ")
            if len(txt) > 70:
                txt = txt[:67] + "..."
            if it.get("error"):
                print(f"  · {txt}\n      ERROR: {it['error']}")
            else:
                print(f"  · {txt}\n      {_sig_str(it['logged_sig'])}  →  {_sig_str(it['new_sig'])}")

    # 회귀를 가장 시끄럽게.
    _dump("!!! REGRESSIONS (병합 차단 신호) !!!", regression, loud=True)
    _dump("IMPROVED", improved)
    _dump("CHANGED", changed)
    _dump("ERRORS", error)

    return len(regression)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="python3 -m tests.replay",
        description=(
            "로그된 라우터 결정을 현재 워킹트리 코드로 재실행해 개선/회귀/변경으로 분류한다. "
            "각 재실행은 실LLM 호출(비용/지연 발생)."
        ),
        epilog=(
            "주의: --candidate 는 다른 브랜치를 체크아웃하지 않는다. 리플레이는 항상 현재 "
            "디스크의 bot.tool_router 를 in-process로 실행한다. 후보 평가 워크플로: "
            "후보 브랜치 체크아웃 → 리플레이 실행. --candidate 는 리포트에 이름만 남긴다."
        ),
    )
    p.add_argument("--days", type=int, default=3, help="최근 N일 결정 재실행(기본 3)")
    p.add_argument("--candidate", default=None, help="리포트 헤더용 라벨(체크아웃 안 함)")
    p.add_argument("--limit", type=int, default=None, help="선택 후 재실행 상한")
    p.add_argument("--only-labeled", action="store_true", help="실패-라벨된 결정만 재실행")
    p.add_argument("--all", action="store_true", help="윈도 내 재실행 가능한 모든 결정 재실행")
    p.add_argument("--yes", action="store_true", help="확인 프롬프트 건너뜀")
    p.add_argument("--log-path", default=None, help="router_decisions.jsonl 경로 오버라이드(테스트용)")
    p.add_argument("--queue-path", default=None, help="review_queue.jsonl 경로 오버라이드(테스트용)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    since_ts = time.time() - args.days * 86400
    records = router_log.read_records(since_ts=since_ts, path=args.log_path)
    labeled_ids = load_labeled_request_ids(path=args.queue_path)

    # 모드 결정: --all 이 명시되면 all, --only-labeled 명시되면 labeled.
    # 둘 다 없으면: 큐 비어있지 않으면 labeled 기본, 비었으면 all(경고).
    if args.all and args.only_labeled:
        print("[replay] --all 과 --only-labeled 를 동시에 줄 수 없습니다.", file=sys.stderr)
        return 2
    if args.all:
        only_labeled = False
    elif args.only_labeled:
        only_labeled = True
    else:
        if labeled_ids:
            only_labeled = True
        else:
            only_labeled = False
            print("[replay] review_queue 가 비어 --all 모드로 진행합니다.", file=sys.stderr)
    mode = "only-labeled" if only_labeled else "all"

    selected, skipped = select_records(records, labeled_ids, only_labeled=only_labeled, limit=args.limit)

    n = len(selected)
    est_cost = n * _COST_PER_CALL
    est_sec = (n / _MAX_WORKERS) * _SEC_PER_CALL
    print(f"[replay] 모드={mode}  윈도={args.days}일  "
          f"로그={len(records)}건  라벨={len(labeled_ids)}건")
    print(f"[replay] 선택 {n}건, 스킵(ctx null) {len(skipped)}건")
    print(f"[replay] 예상: ~{n} calls, ≈${est_cost:.2f}, ≈{est_sec:.0f}s")

    if n == 0:
        print("[replay] 재실행할 레코드가 없습니다.")
        return 0

    if not args.yes and sys.stdin.isatty():
        try:
            resp = input("[replay] 실LLM 호출로 비용이 발생합니다. 진행할까요? [y/N] ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in ("y", "yes"):
            print("[replay] 취소됨.")
            return 0
    elif not args.yes:
        print("[replay] 비대화형(stdin non-TTY): 확인 없이 진행합니다.")

    t0 = time.time()
    buckets = run_replay(selected, labeled_ids)
    elapsed = time.time() - t0

    n_regressions = print_report(
        buckets, selected, labeled_ids,
        candidate=args.candidate, mode=mode, skipped_n=len(skipped),
    )
    print(f"\n[replay] 완료 ({elapsed:.1f}s). 회귀 {n_regressions}건.")
    return 1 if n_regressions > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
