# -*- coding: utf-8 -*-
"""router_report.py — 주간 라우터 지표 리포트 (자기성장형 평가 파이프라인 산출물 5, 2026-07-21).

산출물 1(router_log.py, 결정 로그)과 산출물 2(router_labeler.py, review_queue.jsonl)를
소스로, 한 시간창(기본 최근 7일)의 지표를 계산해 Slack mrkdwn 메시지로 만든다.

지표:
  - 오라우팅률(mis-route rate) = (시간창 내 실패 라벨된 결정 수) / (시간창 내 전체 결정 수)
  - safe_stop률 = (outcome == "safe_stop") / 전체
  - 백엔드별 지연(latency by backend): route.backend별 count/mean/p50/p95 (null latency 제외)
  - (덤) outcome 분포 카운트, 전체 볼륨, review_queue 신호별 top 카운트

핵심 설계: build_report()는 두 파일에 대한 순수 함수다 — 네트워크/Slack 토큰 의존이 없어
합성 픽스처로 단위테스트 가능하다. slack_io 임포트는 반드시 포스팅 함수 내부에서만
한다(모듈 최상단 임포트 금지) — 임포트 시점에 SLACK_BOT_TOKEN이 필요하기 때문.

p50/p95: numpy 없이 순수 파이썬 nearest-rank 백분위 사용.
  정렬 후 index = ceil(p/100 * N) - 1 (0-based, 경계 클램프). 작은 표본에서도 안전.

CLI / 포스팅:
    python3 -m bot.router_report [--days 7] [--channel C...] [--dry-run]
  채널은 --channel > env COWRITER_REPORT_CHANNEL > (없으면) DRY-RUN(stdout 출력 + 경고).
  --dry-run은 항상 stdout 출력만. slack_io 임포트에 .env(SLACK_BOT_TOKEN) 필요 → 포스팅
  CLI는 .env를 source한 상태로 실행해야 한다(런치드 plist가 그렇게 함).

주간 스케줄 설치(Mac mini, launchd):
    launchctl load  ~/Library/LaunchAgents/ai.tain.co-writer-bot-weekly-report.plist
    launchctl unload ~/Library/LaunchAgents/ai.tain.co-writer-bot-weekly-report.plist   # 제거
  (deploy/ai.tain.co-writer-bot-weekly-report.plist를 LaunchAgents로 심볼릭/복사 후 load.
   매주 월 09:00 KST에 `python3 -m bot.router_report --days 7` 실행. 다른 잡들과 동일하게
   zsh -c 래퍼로 repo에 cd → PATH export → .env source → 모듈 실행.)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

from . import config
from . import router_log

QUEUE_PATH = config.BASE_DIR / "logs" / "review_queue.jsonl"

_WEEK_S = 7 * 24 * 3600.0


# ── 순수 계산 유틸 ──────────────────────────────────────────────────────
def _percentile(sorted_vals: list[float], pct: float) -> float:
    """nearest-rank 백분위. sorted_vals는 오름차순 정렬돼 있어야 한다.
    index = ceil(pct/100 * N) - 1, [0, N-1]로 클램프."""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    k = math.ceil(pct / 100.0 * n) - 1
    if k < 0:
        k = 0
    if k > n - 1:
        k = n - 1
    return float(sorted_vals[k])


def _read_queue(path=None) -> list[dict]:
    """review_queue.jsonl을 읽어 레코드 리스트로 반환(없으면 []). 깨진 줄은 건너뜀."""
    p = path or QUEUE_PATH
    out: list[dict] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    except Exception:
        return out
    return out


def compute_metrics(since_ts: float | None = None,
                    until_ts: float | None = None,
                    log_path=None,
                    queue_path=None) -> dict:
    """두 파일에 대한 순수 지표 계산. build_report()가 이걸 포맷한다."""
    records = router_log.read_records(since_ts=None, path=log_path)

    # 시간창 필터 [since_ts, until_ts). None이면 각각 무한.
    def _in_window(rec: dict) -> bool:
        ts = float(rec.get("ts") or 0)
        if since_ts is not None and ts < since_ts:
            return False
        if until_ts is not None and ts >= until_ts:
            return False
        return True

    recs = [r for r in records if _in_window(r)]
    total = len(recs)

    # 실패 라벨: 시간창 내 결정 중 request_id가 review_queue에 있는 것.
    queue = _read_queue(queue_path)
    failure_ids = {q.get("request_id") for q in queue if q.get("request_id")}
    labeled = sum(1 for r in recs if r.get("request_id") in failure_ids)

    safe_stops = sum(1 for r in recs if r.get("outcome") == "safe_stop")

    # 백엔드별 지연.
    by_backend: dict[str, list[float]] = {}
    for r in recs:
        route = r.get("route") or {}
        backend = route.get("backend")
        lat = route.get("latency_ms")
        if backend is None:
            backend = "(unknown)"
        by_backend.setdefault(backend, [])
        if lat is not None:
            try:
                by_backend[backend].append(float(lat))
            except (TypeError, ValueError):
                pass

    latency = {}
    for backend, vals in by_backend.items():
        svals = sorted(vals)
        n = len(svals)
        latency[backend] = {
            "count": n,
            "mean": (sum(svals) / n) if n else 0.0,
            "p50": _percentile(svals, 50.0),
            "p95": _percentile(svals, 95.0),
        }

    # outcome 분포.
    outcomes: dict[str, int] = {}
    for r in recs:
        oc = r.get("outcome") or "(none)"
        outcomes[oc] = outcomes.get(oc, 0) + 1

    # review_queue 신호별 카운트 — 시간창 내 항목만(ts 기준).
    signals: dict[str, int] = {}
    for q in queue:
        ts = float(q.get("ts") or 0)
        if since_ts is not None and ts < since_ts:
            continue
        if until_ts is not None and ts >= until_ts:
            continue
        for s in (q.get("signals") or []):
            signals[s] = signals.get(s, 0) + 1

    return {
        "since_ts": since_ts,
        "until_ts": until_ts,
        "total": total,
        "labeled": labeled,
        "mis_route_rate": (labeled / total) if total else 0.0,
        "safe_stops": safe_stops,
        "safe_stop_rate": (safe_stops / total) if total else 0.0,
        "latency_by_backend": latency,
        "outcomes": outcomes,
        "signals": signals,
    }


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "전체"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except Exception:
        return str(ts)


def build_report(since_ts: float | None = None,
                 until_ts: float | None = None,
                 log_path=None,
                 queue_path=None) -> str:
    """시간창 지표를 Slack mrkdwn 문자열로 만든다. 기본: 최근 7일.
    slack_io 의존 없음 — Slack 토큰 없이 임포트/호출 가능."""
    now = time.time()
    if since_ts is None and until_ts is None:
        since_ts = now - _WEEK_S
    m = compute_metrics(since_ts=since_ts, until_ts=until_ts,
                        log_path=log_path, queue_path=queue_path)

    lines: list[str] = []
    lines.append("*🧭 라우터 주간 리포트*")
    lines.append(f"_기간: {_fmt_ts(m['since_ts'])} ~ {_fmt_ts(m['until_ts'] or now)}_")
    lines.append(f"• 총 결정 수(volume): *{m['total']}*")
    lines.append(
        f"• 오라우팅률: *{_pct(m['mis_route_rate'])}* "
        f"({m['labeled']}/{m['total']})"
    )
    lines.append(
        f"• safe_stop률: *{_pct(m['safe_stop_rate'])}* "
        f"({m['safe_stops']}/{m['total']})"
    )

    # 백엔드별 지연.
    lines.append("")
    lines.append("*백엔드별 지연 (latency_ms)*")
    lat = m["latency_by_backend"]
    if lat:
        for backend in sorted(lat.keys()):
            s = lat[backend]
            if s["count"]:
                lines.append(
                    f"• `{backend}` — n={s['count']}, "
                    f"mean={s['mean']:.0f}, p50={s['p50']:.0f}, p95={s['p95']:.0f}"
                )
            else:
                lines.append(f"• `{backend}` — n=0 (latency 없음)")
    else:
        lines.append("• (데이터 없음)")

    # outcome 분포.
    lines.append("")
    lines.append("*outcome 분포*")
    if m["outcomes"]:
        for oc, c in sorted(m["outcomes"].items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"• {oc}: {c}")
    else:
        lines.append("• (데이터 없음)")

    # top 실패 신호.
    lines.append("")
    lines.append("*top 실패 신호 (review_queue)*")
    if m["signals"]:
        for sig, c in sorted(m["signals"].items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"• {sig}: {c}")
    else:
        lines.append("• (플래그 없음)")

    return "\n".join(lines)


# ── 포스팅 CLI ──────────────────────────────────────────────────────────
def _post(text: str, channel: str) -> None:
    """Slack에 리포트를 게시한다. slack_io 임포트는 여기서만(토큰 필요)."""
    from .shared.slack_io import app  # noqa: WPS433 (의도적 지연 임포트)
    app.client.chat_postMessage(channel=channel, text=text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="라우터 주간 지표 리포트")
    parser.add_argument("--days", type=float, default=7.0,
                        help="시간창 길이(일). 기본 7.")
    parser.add_argument("--channel", default=None,
                        help="게시할 Slack 채널. 없으면 env COWRITER_REPORT_CHANNEL, "
                             "그래도 없으면 DRY-RUN.")
    parser.add_argument("--dry-run", action="store_true",
                        help="게시하지 않고 stdout에만 출력.")
    args = parser.parse_args(argv)

    now = time.time()
    since_ts = now - args.days * 24 * 3600.0
    text = build_report(since_ts=since_ts, until_ts=now)

    channel = args.channel or os.environ.get("COWRITER_REPORT_CHANNEL")

    if args.dry_run or not channel:
        if not args.dry_run and not channel:
            sys.stderr.write(
                "[router_report] 채널 미설정(--channel/COWRITER_REPORT_CHANNEL) "
                "→ DRY-RUN으로 stdout 출력만 합니다.\n"
            )
        print(text)
        return 0

    try:
        _post(text, channel)
        sys.stderr.write(f"[router_report] posted to {channel}\n")
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[router_report] 게시 실패: {e}\n")
        print(text)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
