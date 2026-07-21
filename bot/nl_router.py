# -*- coding: utf-8 -*-

"""nl_router.py — LLM 기반 자연어 라우터 (regex 체인의 자유문장 구간 대체).

설계 원칙

1. "이해는 LLM, 실행은 기존 핸들러."
   라우터는 메시지를 분류해 (intent, slots)만 뽑고, 실행은 검증된 기존 핸들러
   (_dispatch_bracket_command / cw._do_revise / sb 핸들러들)에 위임한다.
   가능한 intent는 전부 기존 브래킷 명령의 정식 문자열로 변환해 재주입한다
   (예: intent=feedback, work=저연프, episode=1 → "[피드백] <저연프> 1화").
   → 핸들러 로직을 하나도 새로 짜지 않고, 브래킷 경로의 테스트 커버리지를 그대로 재사용.
2. 결정적 경로는 그대로 둔다.
   dedup / _STOP_RE(앵커드 정확일치) / 도움말 / 재시도 / 브래킷 명령 /
   pending-state 카드 응답(레코드가 있을 때만 발동하는 *maybe**)은 LLM을 태우지 않는다.
   라우터는 그 뒤의 "자유 문장 구간"(기존 step 4 자유문장 매처들 + step 5~7)만 대체한다.
3. 실패 시 무조건 기존 체인으로 폴백.
   타임아웃(기본 12초) / JSON 파싱 실패 / 백엔드 예외 → legacy_fallback(event) 호출.
   최악의 경우에도 지금(regex 체인)보다 나빠지지 않는다.
4. 새 메시지는 진행 중 작업을 절대 암묵적으로 죽이지 않는다 (job guard).
   기존 사고: 새 메시지가 job_key=thread_ts 작업을 cancel → 죽은 작업의
   CANCEL_MSG("🛑 중단했어요.")가 사용자 답변을 대체.
   → 이제 취소는 intent=cancel_job일 때만. 그 외 변형(mutating) intent는
   작업 종료까지 큐잉하고 사용자에게 상황을 알린다. 질문(answer_question)은
   작업 중에도 즉시 답한다.

환경변수

COWRITER_ROUTER_BACKEND   "openrouter"(기본, 2026-07-21) | "agent" | "api" | "auto"
                          openrouter = OPENROUTER_API_KEY로 oi.chat() 단발 호출(기본 모델
                          anthropic/claude-sonnet-4.5, config.OPENROUTER_LLM_MODEL로 재정의
                          가능) — agent 백엔드가 매 요청마다 이 머신에서 Claude Code CLI를
                          서브프로세스로 띄우는 방식이라 실사용 중 "Reached maximum number of
                          turns"/타임아웃성 실패가 반복 관측됐음(2026-07-21). 단발 API 호출로
                          바꿔 그 실패 클래스를 없애고, 분류 품질도 sonnet급으로 유지한다.
                          agent/api/auto를 명시하면 이전처럼 그 경로로 강제 고정(폴백 없음,
                          단 auto는 agent→api 폴백 유지).
COWRITER_ROUTER_MODEL     openrouter: 미지정 시 config.OPENROUTER_LLM_MODEL(기본
                          anthropic/claude-sonnet-4.5) / agent: 미지정 시 Claude Code 기본
                          모델 / api: 기본 claude-haiku-4-5
COWRITER_ROUTER_TIMEOUT   초 단위, 기본 12 (라우팅은 빨라야 하므로 생성용 150초와 별도)
COWRITER_ROUTER_FALLBACK_TIMEOUT  auto 모드에서 2차 api 백엔드 타임아웃(초), 기본 8
COWRITER_ROUTER_ENABLED   "1"(기본) | "0" — 0이면 무조건 legacy_fallback (킬스위치)
OPENROUTER_API_KEY        openrouter 백엔드가 쓰는 키 — 이미지 생성/컷분해(oi.chat)와 동일한
                          키를 그대로 재사용하므로 별도 발급 불필요.
ANTHROPIC_API_KEY         auto/api 백엔드가 쓰는 anthropic SDK 키(현재 기본 경로 아님) — 라우터
                          폴백 분류 호출에만 쓰이고(저렴한 haiku, 짧은 프롬프트), 실제 생성
                          (대본/이미지/영상) 호출은 기존 agent(Claude Code 구독)로 유지돼
                          과금이 분리된다.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field

from . import config
from .nl_router_prompt import ACTION_SPECS, INTENT_SPECS, build_system_prompt
from .shared.slack_io import _reply, _thread_messages, app, log

ROUTER_BACKEND = os.environ.get(
    "COWRITER_ROUTER_BACKEND",
    os.environ.get("COWRITER_BACKEND", "openrouter"),
)
ROUTER_MODEL = os.environ.get("COWRITER_ROUTER_MODEL", "")
ROUTER_TIMEOUT = int(os.environ.get("COWRITER_ROUTER_TIMEOUT", "12"))
ROUTER_FALLBACK_TIMEOUT = int(os.environ.get("COWRITER_ROUTER_FALLBACK_TIMEOUT", "8"))
ROUTER_ENABLED = os.environ.get("COWRITER_ROUTER_ENABLED", "1") == "1"

# ★2026-07-21 작업: 백엔드별 성공/실패/지연 계측 — safe_stop 발생률을 로그로 추적하기 위함.
# 프로세스 재시작 때마다 리셋되는 누적 카운터(프로세스 수명 = 로그 tail 기준 "오늘")라
# 별도 날짜 버킷 없이도 launchd 재기동 주기(하루 미만) 안에서는 "하루 단위" 근사로 충분하다.
_METRICS_LOCK = threading.Lock()
_METRICS = {
    "agent": {"ok": 0, "fail": 0, "latency_sum": 0.0},
    "api": {"ok": 0, "fail": 0, "latency_sum": 0.0},
    "openrouter": {"ok": 0, "fail": 0, "latency_sum": 0.0},
    "safe_stop": 0,
}


def _record_metric(backend: str, ok: bool, latency: float) -> dict:
    with _METRICS_LOCK:
        m = _METRICS[backend]
        m["ok" if ok else "fail"] += 1
        m["latency_sum"] += latency
        return {k: dict(v) if isinstance(v, dict) else v for k, v in _METRICS.items()}


def record_safe_stop() -> int:
    """safe_stop(안전 정지) 발생 시 dispatch.py의 _safe_fallback에서 호출 — 누적 건수 반환."""
    with _METRICS_LOCK:
        _METRICS["safe_stop"] += 1
        return _METRICS["safe_stop"]


def _metrics_summary() -> str:
    with _METRICS_LOCK:
        a, p, o, s = (
            _METRICS["agent"], _METRICS["api"], _METRICS["openrouter"], _METRICS["safe_stop"],
        )

        def _avg(m):
            n = m["ok"] + m["fail"]
            return (m["latency_sum"] / n) if n else 0.0

        return (
            f"openrouter(ok={o['ok']},fail={o['fail']},avg={_avg(o):.1f}s) "
            f"agent(ok={a['ok']},fail={a['fail']},avg={_avg(a):.1f}s) "
            f"api(ok={p['ok']},fail={p['fail']},avg={_avg(p):.1f}s) "
            f"safe_stop_cum={s}"
        )

# 스레드 이력에서 라우터 컨텍스트로 넘길 최근 메시지 수
_CTX_MESSAGES = 12
_CTX_MSG_MAXLEN = 500


@dataclass
class Route:
    intent: str
    work: str | None = None
    episode: int | None = None
    episodes: list[int] | None = None
    scene: int | None = None
    cuts: list[int] | None = None
    elements: list[dict] | None = None
    instruction: str | None = None
    display_label: str | None = None
    reply_text: str | None = None
    mode: str = "action"
    question_type: str | None = None  # legacy compatibility; 새 라우터는 사용하지 않음
    assumptions: list[str] | None = None
    steps: list[dict] | None = None
    needs_clarification: bool = False
    confidence: float = 0.0
    raw: dict = field(default_factory=dict)


async def _agent_route(system_text: str, prompt: str) -> str:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    options = ClaudeAgentOptions(
        system_prompt=system_text,
        model=ROUTER_MODEL or None,
        max_turns=1,
        allowed_tools=[],
    )

    async def _run() -> str:
        out: list[str] = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                out += [
                    block.text
                    for block in message.content
                    if isinstance(block, TextBlock)
                ]
        return "".join(out).strip()

    return await asyncio.wait_for(_run(), timeout=ROUTER_TIMEOUT)


def _api_route(system_text: str, prompt: str, timeout: int | None = None) -> str:
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=ROUTER_MODEL or "claude-haiku-4-5",
        # ★2026-07-21: 복합 요청(steps 여러 개 + elements 여러 개)이 800 토큰에서 JSON이 중간에
        # 끊겨 파싱 실패로 이어지는 실측 사고(compound-register-then-still) — 여유를 둔다.
        max_tokens=1200,
        system=system_text,
        messages=[{"role": "user", "content": prompt}],
        timeout=timeout if timeout is not None else ROUTER_TIMEOUT,
    )
    return "".join(
        block.text
        for block in resp.content
        if getattr(block, "type", "") == "text"
    ).strip()


def _openrouter_route(system_text: str, prompt: str, timeout: int | None = None) -> str:
    """★2026-07-21: 라우터 기본 백엔드 — OPENROUTER_API_KEY로 단발 chat completion 호출
    (이미지 생성/컷분해와 동일한 키·인프라 재사용, oi.chat()). agent 백엔드(CLI 서브프로세스)의
    "Reached maximum number of turns"/타임아웃성 실패를 없애기 위해 도입."""
    from . import openrouter_image as oi

    model = ROUTER_MODEL or config.OPENROUTER_LLM_MODEL
    return oi.chat(
        system_text, prompt, model=model,
        timeout=timeout if timeout is not None else ROUTER_TIMEOUT,
    ).strip()


def _call_backend(system_text: str, prompt: str) -> str:
    """★2026-07-21: 기본 백엔드는 openrouter(단발 API 호출, agent의 CLI 서브프로세스 방식이
    갖던 "Reached maximum number of turns"류 실패가 없다). agent/api/auto를 명시하면 그
    이전 경로로 강제 고정(auto는 agent→api 1회 폴백 유지, 이중화 로직 그대로)."""
    if ROUTER_BACKEND == "openrouter":
        t0 = time.time()
        try:
            out = _openrouter_route(system_text, prompt)
            _record_metric("openrouter", True, time.time() - t0)
            return out
        except Exception:
            _record_metric("openrouter", False, time.time() - t0)
            raise
    if ROUTER_BACKEND == "api":
        t0 = time.time()
        try:
            out = _api_route(system_text, prompt)
            _record_metric("api", True, time.time() - t0)
            return out
        except Exception:
            _record_metric("api", False, time.time() - t0)
            raise
    if ROUTER_BACKEND == "agent":
        t0 = time.time()
        try:
            out = asyncio.run(_agent_route(system_text, prompt))
            _record_metric("agent", True, time.time() - t0)
            return out
        except Exception:
            _record_metric("agent", False, time.time() - t0)
            raise

    # auto
    t0 = time.time()
    try:
        out = asyncio.run(_agent_route(system_text, prompt))
        _record_metric("agent", True, time.time() - t0)
        return out
    except Exception as agent_exc:
        _record_metric("agent", False, time.time() - t0)
        log.warning("nl_router: agent 백엔드 실패 → api 백엔드로 1회 폴백: %s", agent_exc)
        t1 = time.time()
        try:
            out = _api_route(system_text, prompt, timeout=ROUTER_FALLBACK_TIMEOUT)
            _record_metric("api", True, time.time() - t1)
            return out
        except Exception as api_exc:
            _record_metric("api", False, time.time() - t1)
            log.warning("nl_router: api 폴백도 실패 → safe_stop 경로로: %s", api_exc)
            raise


def _extract_json(text: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start = t.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in router output: {text[:200]!r}")

    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(t[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(t[start : i + 1])
    raise ValueError(f"unbalanced JSON in router output: {text[:200]!r}")


def _event_files(channel: str, thread_ts: str, event: dict) -> list[dict]:
    """Slack 이벤트에서 첨부 파일을 복구한다.

    app_mention 이벤트에는 화면상 파일이 있어도 event["files"]가 빠지는 경우가 있다.
    그때 원본 메시지를 conversations.replies로 다시 읽고, 찾은 파일은 event에도
    되채워 이후 기존 등록/생성 핸들러가 그대로 사용할 수 있게 한다.
    """
    direct = list(event.get("files") or [])
    if direct:
        return direct

    message_ts = str(event.get("ts") or "")
    root_ts = str(event.get("thread_ts") or thread_ts or message_ts)
    if not (channel and message_ts and root_ts):
        return []
    # 1) 스레드 메시지 재조회. app_mention 이벤트에서 files가 빠져도 실제 원문에는
    # 남아 있는 경우가 가장 흔하다.
    try:
        resp = app.client.conversations_replies(
            channel=channel, ts=root_ts, limit=config.THREAD_HISTORY_LIMIT
        )
        for message in resp.get("messages", []):
            same_ts = str(message.get("ts") or "") == message_ts
            same_client_id = bool(
                event.get("client_msg_id")
                and message.get("client_msg_id") == event.get("client_msg_id")
            )
            if not (same_ts or same_client_id):
                continue
            recovered = list(message.get("files") or [])
            if recovered:
                event["files"] = recovered
                log.info("nl_router: Slack 원본 메시지에서 첨부 %d개 복구", len(recovered))
            return recovered
    except Exception as exc:
        log.warning("nl_router conversations.replies 첨부 재조회 실패: %s", exc)

    # 2) 일부 채널/이벤트 조합에서는 replies 결과에 현재 메시지가 안 잡힌다.
    # 같은 ts 주변의 채널 원문을 한 번 더 조회한다.
    try:
        resp = app.client.conversations_history(
            channel=channel, latest=message_ts, inclusive=True, limit=5
        )
        for message in resp.get("messages", []):
            same_ts = str(message.get("ts") or "") == message_ts
            same_client_id = bool(
                event.get("client_msg_id")
                and message.get("client_msg_id") == event.get("client_msg_id")
            )
            if not (same_ts or same_client_id):
                continue
            recovered = list(message.get("files") or [])
            if recovered:
                event["files"] = recovered
                log.info("nl_router: Slack 채널 원문에서 첨부 %d개 복구", len(recovered))
            return recovered
    except Exception as exc:
        log.warning("nl_router conversations.history 첨부 재조회 실패: %s", exc)
    return []


def _presupposes_attachment(text: str) -> bool:
    """이 문구가 '지금 이 메시지에 첨부가 있다'를 전제하는지(사고3 레이스 감지용).
    ★2026-07-21 작업3: '비슷하게/이거로/이거랑/첨부' 등까지 넓혀 PD룩 참조 케이스도 포함."""
    q = text or ""
    return bool(re.search(
        r"이\s*(?:이미지|사진|그림)|이걸로|이거로|이거랑|이 사진|이 이미지|첨부(?:했|한|해)",
        q,
    ))


def _looks_like_reference_image_request(text: str) -> bool:
    """첨부 이미지를 기준으로 새 이미지를 만들거나 참조를 교체·맞추려는 요청인지 판별."""
    q = text or ""
    has_ref = _presupposes_attachment(q) or bool(re.search(
        r"(?:이미지|사진)\s*(?:와|과|처럼|기준|참고)", q))
    has_gen = bool(re.search(
        r"동일하게|똑같이|비슷하게|맞춰|맞게|참고해서|참조해서|기준으로|재생성|"
        r"다시\s*(?:생성|만들)|새로\s*만들|교체|바꿔|수정", q))
    return has_ref and has_gen


def preflight_missing_attachment(channel: str, thread_ts: str, query_text: str, event: dict) -> bool:
    """이미지 참조 요청인데 파일 복구까지 실패한 경우 안전 문구로 여기서 소비한다.

    이 가드는 LLM 호출보다 먼저 실행되어 라우터 실패/legacy 폴백 시에도
    "이미지 기능을 지원하지 않는다"는 잘못된 자유응답이 나갈 수 없게 한다.
    """
    if not _looks_like_reference_image_request(query_text):
        return False
    if _event_files(channel, thread_ts, event):
        return False
    _reply(
        channel,
        thread_ts,
        "첨부 이미지를 불러오지 못했어요. 같은 이미지와 문구를 한 번만 "
        "다시 보내주세요. 확인되는 즉시 요청하신 이미지 작업을 진행할게요.",
    )
    return True


_ATTACH_RACE_WAIT_SEC = float(os.environ.get("COWRITER_ATTACH_RACE_WAIT", "2.5"))


def recover_event_files(channel: str, thread_ts: str, event: dict,
                        query_text: str | None = None) -> list[dict]:
    """기존 핸들러가 쓰도록 복구된 파일을 event에 채우고 반환.
    ★2026-07-21 작업3(Slack 파일 이벤트 레이스): 문구가 첨부를 전제(_presupposes_attachment)
    하는데 파일이 아직 안 보이면, Slack이 파일 전달을 늦출 수 있으므로 잠깐 기다렸다가 원본
    메시지를 한 번 더 재조회한다(1회)."""
    files = _event_files(channel, thread_ts, event)
    if files:
        return files
    if query_text and _presupposes_attachment(query_text):
        try:
            time.sleep(_ATTACH_RACE_WAIT_SEC)
        except Exception:
            pass
        files = _event_files(channel, thread_ts, event)
    return files


def _element_name(element: dict) -> str:
    """elements.json의 현행 display와 레거시 name/tag_name을 모두 지원."""
    return str(
        element.get("display")
        or element.get("name")
        or element.get("tag_name")
        or ""
    ).strip()


def _canonical_work(work: str | None) -> str | None:
    if not work:
        return work
    try:
        from .shared import works
        return works.resolve(work) or work
    except Exception:
        return work


def _load_registered_elements(work: str | None) -> dict[str, list[str]]:
    """질문 시점의 실제 디스크 레지스트리를 정식 작품명 기준으로 다시 읽는다."""
    result: dict[str, list[str]] = {}
    canonical = _canonical_work(work)
    if not canonical:
        return result
    try:
        from . import openrouter_image as oi
        for element in oi.load_elements(canonical):
            kind = str(element.get("type") or "?")
            name = _element_name(element)
            if name:
                result.setdefault(kind, []).append(name)
    except Exception:
        log.exception("nl_router 등록 엘리먼트 조회 실패: work=%s", canonical)
    return result


def _build_context(channel: str, thread_ts: str, event: dict, query_text: str = "") -> dict:
    """라우터 프롬프트에 넣을 스레드 상태 스냅숏."""
    from . import dispatch_storyboard as sb
    from .shared import works

    msgs = _thread_messages(channel, thread_ts)
    tracked = sb.conti_state.get_episode(thread_ts) or {}
    try:
        stage = sb.sb_stage(
            msgs,
            work=tracked.get("work"),
            episode=tracked.get("episode"),
        )
    except Exception:
        stage = 0

    try:
        registry = works.all_works_with_aliases()
    except Exception:
        try:
            registry = {work: [] for work in works.all_names()}
        except Exception:
            registry = {}

    elements = {}
    try:
        if tracked.get("work"):
            from . import openrouter_image as oi
            for el in oi.load_elements(_canonical_work(tracked["work"])):
                name = _element_name(el)
                if name:
                    elements.setdefault(el.get("type", "?"), []).append(name)
    except Exception:
        pass

    last_output = None
    for msg in msgs[::-1]:
        if msg.get("role") == "assistant":
            last_output = (msg.get("content") or "")[:200]
            break

    recent = []
    for msg in msgs[-_CTX_MESSAGES:]:
        role = "작가" if msg.get("role") == "user" else "봇"
        recent.append(
            {
                "role": role,
                "text": (msg.get("content") or "")[:_CTX_MSG_MAXLEN],
            }
        )

    files = _event_files(channel, thread_ts, event)
    # ★2026-07-21 작업1: 스레드 부모(루트) 메시지 텍스트 — 스레드 상단에 작품명만 적어둔 경우
    # (예: 루트가 "저연프")를 이 스레드의 기본 작품으로 쓰기 위한 근거.
    thread_parent_text = ((msgs[0].get("content") if msgs else "") or "")[:_CTX_MSG_MAXLEN]

    # answer 모드는 question_type 없이 LLM이 직접 답하므로, 현재 작품/화의 실제 자료를
    # 가능한 범위에서 함께 제공한다. 실패해도 라우팅 자체는 계속한다.
    answer_sources = {}
    base_ctx = {
        "channel": channel,
        "thread_ts": thread_ts,
        "thread_parent_text": thread_parent_text,
        "tracked_work": tracked.get("work"),
        "tracked_episode": tracked.get("episode"),
        "registered_works": registry,
    }
    resolved_work = _resolve_work_ctx(query_text, base_ctx)
    ep_match = re.search(r"(\d+)\s*화", query_text or "")
    resolved_episode = int(ep_match.group(1)) if ep_match else tracked.get("episode")
    if resolved_work:
        try:
            from .sheet_bible import SheetBible
            bible = SheetBible().get(resolved_work) or {}
            # 바이블 전체를 넘기면 프롬프트가 지나치게 길어지므로 핵심 필드만 제한한다.
            answer_sources["bible"] = {
                k: bible.get(k) for k in ("title", "logline", "characters", "places", "props", "progress")
                if bible.get(k)
            }
            if resolved_episode:
                script = ((bible.get("scripts") or {}).get(f"{resolved_episode}화") or "").strip()
                if script:
                    answer_sources["episode_script_excerpt"] = script[:12000]
        except Exception as exc:
            log.warning("nl_router answer source bible load failed: %s", exc)
        if resolved_episode:
            try:
                conti, source = sb._fetch_external_conti(resolved_work, resolved_episode)
                if conti:
                    answer_sources["detail_conti_excerpt"] = conti[:12000]
                    answer_sources["detail_conti_source"] = source
            except Exception as exc:
                log.warning("nl_router answer source conti load failed: %s", exc)

    return {
        "channel": channel,
        "thread_ts": thread_ts,
        "thread_parent_text": thread_parent_text,
        "tracked_work": tracked.get("work"),
        "tracked_episode": tracked.get("episode"),
        "sb_stage": stage,
        "registered_works": registry,
        "registered_elements": elements,
        "last_bot_output_head": last_output,
        "attached_image_count": sum(
            1
            for file in files
            if str(file.get("mimetype", "")).startswith("image/")
        ),
        "attached_file_names": [file.get("name", "") for file in files][:10],
        "answer_sources": answer_sources,
        "recent_messages": recent,
    }


def _registered_work_names(ctx: dict) -> set[str]:
    registry = ctx.get("registered_works") or {}
    names: set[str] = set()
    if isinstance(registry, dict):
        for work, aliases in registry.items():
            names.add(str(work).strip())
            for alias in aliases or []:
                names.add(str(alias).strip())
    return {name for name in names if name}


# 기존 브래킷 명령은 작품명 대괄호 표기와 구분한다.
_COMMAND_TOKENS = {
    "생성", "기획", "피드백", "재미", "개연성", "트렌드", "아이디어",
    "동기화", "별칭", "변환", "확인", "파일", "스토리보드", "이미지",
    "합본", "자동주행", "진행상황", "스타일", "콘티확정", "화초기화",
}


def _canonical_for_token(token: str, ctx: dict) -> str | None:
    """등록 작품명/별칭 토큰을 정식 작품명으로 해석한다(별칭이면 정식명으로 치환)."""
    registry = ctx.get("registered_works") or {}
    if not isinstance(registry, dict):
        return None
    if token in registry:
        return token
    for work, aliases in registry.items():
        if token in (aliases or []):
            return str(work)
    return None


def _explicit_work_token(text: str, ctx: dict) -> str | None:
    """현재 문장의 <작품명> 또는 [작품명]을 등록 작품/별칭 기준으로 찾아 정식명으로 반환한다.

    대괄호는 [피드백], [이미지] 같은 정식 명령과 충돌하므로 등록된 작품명/별칭과
    정확히 일치하는 경우에만 작품 토큰으로 인정한다. 꺾쇠도 같은 기준을 적용해
    <하루>처럼 미등록 캐릭터 이름을 작품명으로 오인하지 않는다. 별칭으로 매칭된
    경우에도 정식명으로 치환해 반환한다(★2026-07-21 사고2: 별칭 그대로 반환되던 문제).
    """
    for match in re.finditer(r"<([^<>]+)>|\[([^\[\]]+)\]", text):
        token = (match.group(1) or match.group(2) or "").strip()
        if not token or token in _COMMAND_TOKENS:
            continue
        canonical = _canonical_for_token(token, ctx)
        if canonical:
            return canonical
    return None


def _resolve_work_ctx(text: str, ctx: dict) -> str | None:
    """★2026-07-21 작업1: 작품 해석을 works.resolve_work 단일 경로로 통일.
    ctx의 channel/thread_ts가 있으면 스레드 부모·이력 스캔까지 포함(별칭·부분표기·스레드
    상단 작품명 회수). 실패 시 명시 토큰/tracked_work로 폴백."""
    ch, tts = ctx.get("channel"), ctx.get("thread_ts")
    if ch and tts:
        try:
            from .shared import works
            w = works.resolve_work(text or "", ch, tts)
            if w:
                return w
        except Exception:
            log.exception("resolve_work 실패")
    return _explicit_work_token(text or "", ctx) or (ctx.get("tracked_work") or None)


_QUESTION_END_RE = re.compile(
    r"(?:뭐(?:지|야|예요|임)?|무엇|누구|어디|왜|어떻게|어떤\s+.+|있(?:지|어|나요)|됐(?:지|어|나요)|맞(?:지|아|나요)|알려줘)\s*[?？]*$"
)
_MUTATING_INTENTS = {
    "script_generate", "script_revise", "plan_edit", "scene_design", "detail_conti",
    "conti_rewrite", "storyboard_image", "stillcut", "video", "compile", "autopilot",
    "convert", "element_register", "element_edit", "element_generate", "reset_episode",
}


def is_question_text(text: str) -> bool:
    """명시적인 조회 질문인지 판별한다. 작업 동사가 있는 요청은 제외한다."""
    q = (text or "").strip()
    if not q:
        return False
    if re.search(r"(?:만들어|생성해|재생성|등록해|수정해|바꿔|고쳐|작성해|써줘|진행해|돌려줘)", q):
        return False
    return bool(
        q.endswith(("?", "？"))
        or _QUESTION_END_RE.search(q)
        or re.search(r"(?:뭐뭐|누구누구|무슨\s+상태|등록되어\s*있|반영했어)", q)
    )


def _extract_episode(text: str, ctx: dict) -> int | None:
    m = re.search(r"(\d+)\s*화", text or "")
    if m:
        return int(m.group(1))
    tracked = ctx.get("tracked_episode")
    return tracked if isinstance(tracked, int) else None


def _extract_character_for_costume(text: str) -> str | None:
    """'1화에 나올 이영 의상은 뭐지?'에서 이영을 결정적으로 추출한다."""
    patterns = (
        r"(?:\d+\s*화에\s*(?:나올|등장할)?\s*)([가-힣A-Za-z0-9_·-]{1,20})\s*(?:의상|옷|복장)",
        r"([가-힣A-Za-z0-9_·-]{1,20})\s*(?:의상|옷|복장)(?:은|는|이|가)?\s*(?:뭐|무엇|어떤)",
    )
    for pattern in patterns:
        m = re.search(pattern, text or "")
        if m:
            name = m.group(1).strip()
            if name not in {"나올", "등장할", "인물", "캐릭터"}:
                return name
    return None


_NOTION_IMG_MENTION_RE = re.compile(r"노션.{0,10}첨부(?:해\s*둔|해\s*논|한)?.{0,10}(?:이미지|사진|스토리보드)")
_COMPOSITION_MATCH_RE = re.compile(r"구도|연출|블로킹")
_COMPOSITION_SAME_RE = re.compile(r"그대로|똑같이|동일하게|같게")
_NO_ARBITRARY_GEN_RE = re.compile(r"임의로\s*(?:생성|만들)|마음대로\s*(?:생성|만들)|자유롭게\s*(?:생성|만들)")
# ★2026-07-21(추가): "이 스토리보드 그리드를 보고 씬1 스틸컷을 똑같이 생성해줘"처럼 노션이
# 아니라 이 스레드에 직접 첨부한 이미지를 구도 레퍼런스로 쓰라는 요청은 "구도/연출/블로킹"
# 같은 단어 없이도 "보고 ... 그대로/똑같이" 패턴만으로 나타난다.
_COMPOSITION_LOCK_RE = re.compile(r"(?:보고|참고해서|참조해서).{0,20}(?:그대로|똑같이|동일하게|같게)")
# ★2026-07-21(실사용 추가 리포트): "이 스토리보드 그대로 1화 스틸컷을 만들고 싶어"처럼 "보고"
# 동사 없이 "이 스토리보드/이미지 ... 그대로/똑같이"만 오는 어순은 위 _COMPOSITION_LOCK_RE가
# 놓쳤다(오탐 걱정으로 "보고"를 필수로 뒀던 게 오히려 재현률을 깎아먹음) — 지시사("이")로
# 특정 이미지를 가리키는 것만으로도 충분한 신호이므로 별도로 잡는다.
_COMPOSITION_DEMONSTRATIVE_SAME_RE = re.compile(
    r"이\s*(?:스토리보드|이미지|사진|그리드)[^.!?\n]{0,20}(?:그대로|똑같이|동일하게|같게)"
)


def _wants_notion_composition_ref(text: str) -> bool:
    """★2026-07-21 "씬 1. 스틸컷은 내가 노션에 첨부해둔 스토리보드 이미지를 보고 연출과
    구도는 똑같이 하도록 해. 임의로 생성하지 말고." — 노션에 이미 붙여둔 이미지를 스틸컷
    구도/연출의 확정 레퍼런스로 삼으라는, LLM 자유생성이 아니라 결정적으로 처리해야 하는
    요청이다. 세 조건(노션+첨부+이미지/스토리보드 언급, 구도/연출 매칭, "똑같이/그대로" 동일
    지시)이 모두 있어야 잡는다 — 어느 하나만으로는 오탐(예: 그냥 "노션 이미지 보여줘")이 남.
    "임의로 생성하지 말고" 같은 부정 지시는 필수 조건은 아니지만(사용자가 안 붙일 수도 있음),
    있으면 이 경로를 더 강하게 확신할 수 있어 로그/신뢰도 표시에만 참고한다."""
    t = text or ""
    return bool(_NOTION_IMG_MENTION_RE.search(t) and _COMPOSITION_MATCH_RE.search(t)
                and _COMPOSITION_SAME_RE.search(t))


def _wants_slack_composition_ref(text: str) -> bool:
    """★2026-07-21(실사용 확인: "이 스토리보드 그리드를 보고 씬1 스틸컷을 똑같이 생성해줘" +
    이미지 첨부 — 실제로는 이 요청이 첨부 이미지를 전혀 참조하지 않고 그냥 자유 생성되고
    있었음. 후속 리포트: "이 스토리보드 그대로 1화 스틸컷을 만들고 싶어"처럼 "보고" 동사 없는
    어순도 여전히 안 잡혀 같은 문제가 재발함). 이 스레드에 방금 첨부한 이미지를 구도 고정
    레퍼런스로 쓰라는 요청 — 노션 언급이 없어도 "보고/참고해서/참조해서 ... 그대로/똑같이"
    패턴이거나, "이 스토리보드/이미지/사진/그리드 ... 그대로/똑같이" 지시사 패턴이면 잡는다.
    호출부에서 반드시 이벤트에 실제 첨부 이미지가 있는지(_image_files) 같이 확인해야 한다 —
    텍스트만으로는 참조할 이미지가 없을 수 있음."""
    t = text or ""
    return bool(_COMPOSITION_LOCK_RE.search(t) or _COMPOSITION_DEMONSTRATIVE_SAME_RE.search(t))


def _deterministic_question_route(query_text: str, ctx: dict) -> Route | None:
    """실사고가 반복된 상태조회 질문은 LLM 호출 없이 안전하게 분류한다."""
    text = (query_text or "").strip()
    if not is_question_text(text):
        return None

    work = _resolve_work_ctx(text, ctx)
    episode = _extract_episode(text, ctx)

    costume_character = None
    if re.search(r"(?:의상|옷|복장)", text) and re.search(r"(?:뭐|무엇|어떤)", text):
        costume_character = _extract_character_for_costume(text)
    if costume_character:
        return Route(
            intent="answer_question",
            work=work if isinstance(work, str) else None,
            episode=episode,
            elements=[{"kind": "인물", "name": costume_character}],
            question_type="episode_character_costume",
            confidence=1.0,
            raw={"_context": ctx, "deterministic": True},
        )

    required_match = re.search(
        r"(\d+)\s*화.{0,20}(?:등록할|필요한|나올|등장할).{0,15}(?:인물|캐릭터|사람)",
        text,
    )
    if required_match and re.search(r"누구|뭐뭐|목록|알려", text):
        return Route(
            intent="answer_question",
            work=work if isinstance(work, str) else None,
            episode=int(required_match.group(1)),
            question_type="episode_required_elements",
            confidence=1.0,
            raw={"_context": ctx, "deterministic": True},
        )

    if re.search(r"(?:인물|장소|의상|옷|소품).{0,15}(?:등록|참조).{0,15}(?:뭐뭐|무엇|누구|있)", text):
        return Route(
            intent="answer_question",
            work=work if isinstance(work, str) else None,
            episode=episode,
            question_type="registered_elements_status",
            confidence=1.0,
            raw={"_context": ctx, "deterministic": True},
        )

    if re.fullmatch(r"(?:지금\s*)?(?:뭐해|무슨\s*작업\s*중(?:이야|이야\?)?|진행\s*상황(?:은|이)?\??)", text):
        return Route(
            intent="answer_question",
            work=work if isinstance(work, str) else None,
            episode=episode,
            question_type="pipeline_status",
            confidence=1.0,
            raw={"_context": ctx, "deterministic": True},
        )

    return None


def _apply_safety_normalization(r: Route, query_text: str, ctx: dict) -> Route:
    """LLM 분류 뒤 실제 사고가 컸던 슬롯 혼동만 결정적으로 보정한다."""
    text = query_text.strip()

    # 명시적인 질문이 생성/씬설계 등 변형 intent로 분류되면 실행 전에 차단한다.
    # 특히 "1화에 나올 이영 의상은 뭐지?"가 scene_design으로 가는 사고를 막는다.
    if is_question_text(text) and r.intent in _MUTATING_INTENTS:
        r.intent = "answer_question"
        r.reply_text = None
        character = _extract_character_for_costume(text)
        if character and re.search(r"의상|옷|복장", text):
            r.question_type = "episode_character_costume"
            r.elements = [{"kind": "인물", "name": character}]
            r.episode = _extract_episode(text, ctx)
        else:
            r.question_type = r.question_type or "general"
        r.episodes = None
        r.scene = None
        r.cuts = None
        r.steps = None
        r.needs_clarification = False

    # answer_question은 LLM 생성 답변을 절대 사용하지 않는다.
    if r.intent == "answer_question":
        r.reply_text = None

        # “1화에 등록할 인물 누구누구 있지?”는 현재 등록 목록 조회가 아니라
        # 해당 화 대본/상세 콘티에서 필요한 인물을 찾는 질문이다. 모델이
        # registered_elements_status로 보낸 경우에도 결정적으로 바로잡는다.
        required_match = re.search(
            r"(\d+)\s*화.{0,20}(?:등록할|필요한|나올|등장할).{0,15}(?:인물|캐릭터|사람)",
            text,
        )
        if required_match and re.search(r"누구|뭐뭐|목록|알려", text):
            r.question_type = "episode_required_elements"
            r.episode = int(required_match.group(1))

    # 현재 메시지에 등록 작품명이 명시돼 있으면 스레드의 이전 작품보다 우선한다.
    # <저연프>와 [저연프]를 모두 지원하되 [피드백]/[이미지] 등 명령은 제외한다.
    explicit_work = _explicit_work_token(text, ctx)
    if explicit_work:
        r.work = explicit_work

    # “씬 2~5”를 “2~5화”로 팬아웃하는 사고 차단.
    has_scene_range = bool(re.search(r"씬\s*\d+\s*(?:~|-|부터)\s*\d+", text))
    has_episode_range = bool(re.search(r"\d+\s*(?:~|-|부터)\s*\d+\s*화", text))
    if has_scene_range and not has_episode_range:
        r.episodes = None
        tracked_episode = ctx.get("tracked_episode")
        if isinstance(tracked_episode, int):
            r.episode = tracked_episode

    # <...>는 등록 작품/별칭과 일치할 때만 작품명이다.
    angle = re.search(r"<([^<>]+)>", text)
    if angle and r.work == angle.group(1).strip():
        if r.work not in _registered_work_names(ctx):
            tracked_work = ctx.get("tracked_work")
            r.work = tracked_work if isinstance(tracked_work, str) and tracked_work else None
            if r.intent in ("element_register", "element_edit", "element_generate"):
                token = angle.group(1).strip()
                if not r.elements and re.search(r"의상|옷", text):
                    r.elements = [{"kind": "의상", "name": f"{token} 의상", "image_index": 0}]

    attached = int(ctx.get("attached_image_count") or 0)
    generate_from_ref = bool(re.search(r"동일하게|참고해서|참조해서|재생성|새로\s*만들", text))
    register_words = bool(re.search(r"등록|각각|순서대로|(?:이야|입니다)[.!]?$", text))

    # 사용자가 "이 이미지/사진/첨부"를 전제했는데 Slack API에서도 파일을 못 찾은 경우.
    # LLM의 임의 안내(예: "이미지 생성 기능이 없다")는 절대 노출하지 않고,
    # 실제 기능을 정확히 설명하는 짧은 재첨부 문구로 고정한다.
    expects_image = bool(re.search(r"(?:이|그|첨부한?)\s*(?:이미지|사진)|이걸로|첨부했", text))
    if expects_image and attached == 0:
        r.needs_clarification = True
        r.reply_text = (
            "첨부 이미지를 불러오지 못했어요. 같은 이미지와 문구를 한 번만 "
            "다시 보내주세요. 확인되는 즉시 요청하신 이미지 작업을 진행할게요."
        )
        return r

    # 이미지가 첨부됐고 현재 메시지에 등록 의사가 명시되면, 최근 봇 출력의
    # 대본/콘티/화 번호와 무관하게 참조 등록이 항상 우선한다.
    # 예: “김신우 등록해줘” + 인물 사진 1장 → element_register(인물 김신우).
    explicit_register = bool(
        attached
        and not generate_from_ref
        and re.search(r"(?:등록(?:해|해줘|해주세요|할게|이야)?|참조로\s*(?:써|등록)|고정값으로)", text)
    )
    if explicit_register:
        r.intent = "element_register"
        # 화 번호는 지우지 않는다 — 의상/장소/소품 라벨 자동 매칭(resolve_element_label)이
        # 콘티/대본을 찾을 때 episode가 필요하다(★2026-07-21 사고4: 여기서 무조건 None으로
        # 밀어써서 "1화 옥상 배경 이 사진으로 등록해줘" 같은 요청의 라벨 해석이 깨짐).
        r.episodes = None
        r.scene = None
        r.cuts = None
        # 복합 요청(등록 + 다른 작업 순차 실행)은 LLM이 이미 여러 단계로 분해한
        # steps를 그대로 보존한다. 단일 등록 오분류 보정일 때만 초기화한다
        # (★2026-07-21 사고3: 여기서 무조건 None으로 밀어써서 순차 스텝이 사라짐).
        if not (r.steps and len(r.steps) >= 2):
            r.steps = None
        r.needs_clarification = False

        # LLM이 이미 올바른 구조를 뽑았다면 그대로 사용하고, 비어 있을 때만
        # 현재 사용자 문장에서 이름과 종류를 결정적으로 복구한다.
        if not r.elements:
            cleaned = re.sub(r"<@[^>]+>|@[\w.-]+", " ", text)
            cleaned = re.sub(r"<[^<>]+>|\[[^\[\]]+\]", " ", cleaned)
            cleaned = re.sub(
                r"(?:이|그)?\s*(?:이미지|사진|첨부(?:파일)?|참조(?:\s*이미지)?|고정값)",
                " ",
                cleaned,
            )
            cleaned = re.sub(
                r"(?:로|으로)?\s*(?:등록(?:해|해줘|해주세요|할게)?|참조로\s*(?:써|등록)|고정값으로)\s*[.!?]*$",
                "",
                cleaned,
            ).strip(" ,./")

            kind = "인물"
            if re.search(r"의상|옷|교복|유니폼|복장", cleaned):
                kind = "의상"
            elif re.search(r"장소|배경|교실|방|거실|복도|학교|회사|집", cleaned):
                kind = "장소"
            elif re.search(r"소품|가방|휴대폰|핸드폰|차량|자동차", cleaned):
                kind = "소품"

            # 종류 표시는 이름에서 제거하되, “여자 교복(엑스트라용)”처럼
            # 실제 명칭에 포함된 단어는 보존한다. 단순 “인물 김신우”만 정리한다.
            if kind == "인물":
                cleaned = re.sub(r"^(?:인물|캐릭터|사람)\s+", "", cleaned).strip()

            names = [
                part.strip()
                for part in re.split(r"\s*,\s*|\s+및\s+|\s+그리고\s+", cleaned)
                if part.strip()
            ]
            if names:
                r.elements = [
                    {"kind": kind, "name": name, "image_index": idx}
                    for idx, name in enumerate(names[:attached])
                ]

    # 첨부 이미지를 시각 참조로 새 이미지를 만들어달라는 요청은 등록이 아니다.
    if attached and r.intent == "element_register" and generate_from_ref:
        r.intent = "element_generate"

    # 첨부 여러 장의 이름 매핑 설명은 AI 생성이 아니라 등록이다.
    if attached and r.intent == "element_generate" and register_words and not generate_from_ref:
        r.intent = "element_register"

    # “인물 옷과 배경 이미지 생성”은 스토리보드 이미지가 아니다.
    if r.intent == "storyboard_image" and re.search(r"옷|의상|배경", text) and re.search(r"이미지\s*(?:생성|만들)", text):
        r.intent = "element_generate"

    # 기존 대본·상세 콘티를 확인해 스토리보드를 고치라는 요청은 추적 중 화로 진행.
    if (
        r.intent == "storyboard_image"
        and r.episode is None
        and re.search(r"대본.*상세\s*콘티|상세\s*콘티.*대본", text)
        and re.search(r"스토리보드.*(?:고치|다시|재생성)|(?:고치|다시|재생성).*스토리보드", text)
    ):
        tracked_episode = ctx.get("tracked_episode")
        if isinstance(tracked_episode, int):
            r.episode = tracked_episode

    return r


def route(
    channel: str,
    thread_ts: str,
    query_text: str,
    event: dict,
) -> Route | None:
    """자유문장을 answer/action/clarify 중 하나로 해석한다."""
    if not ROUTER_ENABLED:
        return None

    ctx = _build_context(channel, thread_ts, event, query_text=query_text)
    system_text = build_system_prompt(ctx)
    t0 = time.time()
    try:
        raw = _call_backend(system_text, query_text)
        data = _extract_json(raw)
    except Exception as exc:
        log.warning(
            "nl_router 실패 → safe_stop 경로: %s (%.1fs) | %s",
            exc, time.time() - t0, _metrics_summary(),
        )
        return None

    mode = str(data.get("mode") or "").strip().lower()
    if mode not in {"answer", "action", "clarify"}:
        log.warning("nl_router 미지의 mode %r → safe_stop", mode)
        return None

    if mode == "answer":
        answer = str(data.get("answer") or "").strip()
        if not answer:
            log.warning("nl_router answer 모드인데 answer가 비어 있음 → safe_stop")
            return None
        r = Route(
            intent="answer_question",
            mode="answer",
            reply_text=answer,
            confidence=float(data.get("confidence") or 0.0),
            raw={**data, "_context": ctx},
        )
    elif mode == "clarify":
        question = str(data.get("question") or "").strip()
        if not question:
            question = "정확히 어떤 작업을 원하시는지 한 번만 더 알려주세요."
        r = Route(
            intent="clarify",
            mode="clarify",
            reply_text=question,
            confidence=float(data.get("confidence") or 0.0),
            raw={**data, "_context": ctx},
        )
    else:
        action = str(data.get("action") or "").strip()
        if action not in ACTION_SPECS:
            log.warning("nl_router 미허용 action %r → safe_stop", action)
            return None
        r = Route(
            intent=action,
            mode="action",
            work=data.get("work") or None,
            episode=data.get("episode") if isinstance(data.get("episode"), int) else None,
            episodes=data.get("episodes") if isinstance(data.get("episodes"), list) else None,
            scene=data.get("scene") if isinstance(data.get("scene"), int) else None,
            cuts=data.get("cuts") if isinstance(data.get("cuts"), list) else None,
            elements=data.get("elements") if isinstance(data.get("elements"), list) else None,
            instruction=(data.get("instruction") or "").strip() or query_text.strip() or None,
            display_label=(data.get("display_label") or "").strip() or None,
            assumptions=data.get("assumptions") if isinstance(data.get("assumptions"), list) else None,
            steps=data.get("steps") if isinstance(data.get("steps"), list) else None,
            confidence=float(data.get("confidence") or 0.0),
            raw={**data, "_context": ctx},
        )

    log.info(
        "nl_router: mode=%s action=%s work=%r ep=%r conf=%.2f (%.1fs)",
        r.mode, r.intent, r.work, r.episode, r.confidence, time.time() - t0,
    )
    return r


def _default_clarify(r: Route) -> str:
    name = INTENT_SPECS.get(r.intent, {}).get("label", r.intent)
    return (
        f"확실하게 하고 싶어서 확인할게요 — 지금 요청이 *{name}* 맞을까요?\n"
        f'맞으면 "응", 아니면 원하시는 걸 한 번 더 말씀해 주세요.'
    )


_PENDING_LOCK = threading.Lock()
_PENDING: dict[str, list[dict]] = {}

_MUTATING = {
    "script_generate",
    "scene_design",
    "detail_conti",
    "conti_rewrite",
    "storyboard_image",
    "stillcut",
    "video",
    "compile",
    "autopilot",
    "script_revise",
    "plan_edit",
    "convert",
    "element_generate",
    "reset_episode",
}


def _active_job(thread_ts: str):
    from . import dispatch_storyboard as sb
    try:
        for j in sb.job_ledger.pending_jobs():
            if j.get("thread_ts") == thread_ts:
                return j
    except Exception:
        pass
    return None


def guard_or_queue(channel: str, thread_ts: str, event: dict, r: Route) -> bool:
    """True면 이번 이벤트는 큐잉/안내로 소비됨. False면 계속 실행."""
    job = _active_job(thread_ts)
    if not job:
        return False
    if r.intent == "cancel_job":
        return False
    if r.intent not in _MUTATING:
        return False

    with _PENDING_LOCK:
        _PENDING.setdefault(thread_ts, []).append(event)
    _reply(
        channel,
        thread_ts,
        f"⏳ 지금 이 스레드에서 `{job.get('kind', '작업')}`이 진행 중이에요. "
        f"끝나는 대로 방금 요청을 이어서 처리할게요.\n"
        f'(진행 중 작업을 멈추려면 "중단"이라고 답해주세요 — 새 요청 때문에 '
        f"기존 작업이 임의로 취소되는 일은 이제 없어요.)",
    )
    return True


def drain_pending(thread_ts: str, handle_fn) -> None:
    """job_ledger.finish_* 훅에서 호출해 대기 이벤트를 순서대로 재처리."""
    with _PENDING_LOCK:
        queued = _PENDING.pop(thread_ts, [])
    for event in queued:
        try:
            handle_fn(event)
        except Exception:
            log.exception("drain_pending: queued event 처리 실패")


# ★2026-07-21 작업③, 2026-07-21 개편: safe_stop으로 조용히 버려지던 발화를 스레드당 최근
# 1건만 보관해뒀다가, 다음에 이 스레드에서 라우팅이 정상 성공하면 "아까 못 처리한 요청
# 이어서 할까요?"를 딱 한 번 제안한다. 자동으로 재실행하지 않는다(안전 정지 사유가 아직
# 남아있을 수 있으므로) — 사용자가 긍정으로 답할 때만 dispatch.py의 resume 핸들러가 저장된
# event를 다시 전체 파이프라인에 넣는다.
#
# PendingManager로 통일(상태머신 waiting→consuming→completed/failed + request_id 중복
# 실행 방지 + TTL) — 예전엔 bool "prompted" 하나로만 게이트해서: (1) 같은 이벤트가 두 번
# 들어와도 막을 방법이 없었고, (2) 재실행이 다시 실패하면 execute() 안의 safe_stop 경로가
# stash_failed_event를 또 호출해 같은 kind로 재저장 → 다음 성공 라우팅 때 또 제안 → 무한
# 재시도 루프가 될 수 있었다(재실행 실패를 "완료"와 구분 못 함). 이제 재실행 이벤트에는
# _cowriter_replay=True 표시를 남기고, 그 표시가 있는 이벤트의 실패는 재저장하지 않는다.
from . import pending_manager as _pm

_RESUME_MANAGER = _pm.PendingManager()
_RESUME_KIND = "resume_offer"

_RESUME_YES_RE = re.compile(r"^\s*(응|어|네|넹|넵|그래|좋아|좋아요|이어서\s*해줘?|해줘)\s*[.!~]*\s*$")


def stash_failed_event(thread_ts: str, event: dict, query_text: str) -> None:
    # 재실행 자체가 실패한 경우엔 다시 pending으로 저장하지 않는다(무한루프 방지) — 이건
    # "이번엔 진짜 처음 실패한 발화"일 때만 새 제안 대상으로 만든다.
    if event.get("_cowriter_replay"):
        log.info("resume_offer: 재실행 실패 — 재저장하지 않음 thread=%s", thread_ts)
        return
    request_id = event.get("client_msg_id") or event.get("event_ts") or event.get("ts")
    _RESUME_MANAGER.create(
        thread_ts, _RESUME_KIND,
        {"event": event, "query": query_text, "prompted": False},
        request_id=f"stash:{request_id}",
    )


def offer_resume_if_pending(channel: str, thread_ts: str, current_event: dict) -> None:
    """라우팅이 방금 정상 성공했을 때 dispatch.py가 호출 — 대기 중인 실패 발화가 있고 아직
    제안 안 했으면 1회만 물어본다(peek만 — 여기선 아직 소비하지 않는다, 소비는 사용자가
    실제로 긍정 답변했을 때 _maybe_resume_offer_reply에서)."""
    rec = _RESUME_MANAGER.peek(thread_ts, _RESUME_KIND)
    if rec is None or rec.status != _pm.WAITING:
        return
    if rec.payload.get("prompted") or rec.payload["event"] is current_event:
        return
    rec.payload["prompted"] = True
    _reply(
        channel, thread_ts,
        f"📌 아까 처리하지 못한 요청이 있어요 — \"{rec.payload['query']}\" 이어서 할까요? "
        '("응"이라고 답해주시면 다시 처리할게요)',
    )


def _maybe_resume_offer_reply(channel: str, thread_ts: str, query: str, handle_fn) -> bool:
    """대기 중인 실패 발화 제안에 대한 긍정 답변만 결정적으로 잡아 재실행한다."""
    if not _RESUME_YES_RE.match(query):
        return False
    peeked = _RESUME_MANAGER.peek(thread_ts, _RESUME_KIND)
    if peeked is None or not peeked.payload.get("prompted"):
        return False
    rec = _RESUME_MANAGER.consume(thread_ts, _RESUME_KIND)
    if rec is None:
        return False
    _reply(channel, thread_ts, "🔁 이어서 처리할게요…")
    replay_event = dict(rec.payload["event"])
    replay_event["_cowriter_replay"] = True  # 이 재실행이 또 실패해도 재저장 금지
    try:
        handle_fn(replay_event)
        _RESUME_MANAGER.complete(thread_ts, _RESUME_KIND)
    except Exception:
        log.exception("resume_offer: 재처리 실패")
        _RESUME_MANAGER.fail(thread_ts, _RESUME_KIND, error="handle_fn 예외")
    return True


def _q(value: str | None) -> str:
    return (value or "").strip()


def _bracket(cmd: str, r: Route, body: str | None = None) -> str:
    """정식 브래킷 명령 문자열 합성."""
    parts = [f"[{cmd}]"]
    if r.work:
        parts.append(f"<{r.work}>")
    if r.episode is not None:
        parts.append(f"{r.episode}화")
    if body:
        parts.append(body)
    return " ".join(parts)


_PROPOSAL_LOCK = threading.Lock()
_LAST_PROPOSAL: dict[str, Route] = {}


def _remember_proposal(thread_ts: str, r: Route) -> None:
    with _PROPOSAL_LOCK:
        _LAST_PROPOSAL[thread_ts] = r


def _pop_proposal(thread_ts: str) -> Route | None:
    with _PROPOSAL_LOCK:
        return _LAST_PROPOSAL.pop(thread_ts, None)


def _echo_assumptions(channel: str, thread_ts: str, r: Route) -> None:
    """추론으로 채운 슬롯을 실행 전에 공개한다."""
    if r.assumptions:
        _reply(
            channel,
            thread_ts,
            "📌 "
            + " · ".join(r.assumptions)
            + "\n*(다르면 지금 바로 정정해주세요 — 진행하면서 반영할게요)*",
        )




def _episode_required_people(work: str, episode: int) -> tuple[list[str], list[str], str | None]:
    """해당 화의 대본/상세 콘티에서 등장인물을 찾고 등록 여부와 비교한다.

    반환: (등장인물 전체, 아직 미등록 인물, 오류 안내). LLM에게 이름을 만들게 하지 않고
    바이블의 인물명과 대본/콘티의 `등장:` 선언을 코드로만 대조한다.
    """
    from . import dispatch_storyboard as sb
    from . import openrouter_image as oi
    from . import reference

    bible = None
    try:
        sheet = reference.sheet()
        if sheet:
            bible = sheet.get(work)
    except Exception:
        log.exception("episode_required_people: bible load failed work=%s", work)

    script = ""
    script_error = None
    try:
        script, script_error = sb._script_for(work, episode, bible)
    except Exception:
        log.exception("episode_required_people: script load failed work=%s ep=%s", work, episode)

    conti = ""
    try:
        conti, _source = sb._fetch_external_conti(work, episode)
        conti = conti or ""
    except Exception:
        log.exception("episode_required_people: conti load failed work=%s ep=%s", work, episode)

    source_text = "\n".join(x for x in (script, conti) if x).strip()
    if not source_text:
        if script_error:
            return [], [], f"{episode}화 대본을 읽는 중 오류가 났어요: {script_error}"
        return [], [], f"<{work}> {episode}화 대본이나 상세 콘티를 찾지 못했어요."

    people: list[str] = []

    # 가장 신뢰할 수 있는 후보는 바이블에 등록된 인물명이다. 해당 화 원문에 실제 등장한
    # 이름만 골라내므로 전체 작품 인물을 무작정 나열하지 않는다.
    characters = (bible or {}).get("characters") or {}
    if isinstance(characters, dict):
        for name in characters:
            clean = str(name).strip()
            if clean and clean in source_text:
                people.append(clean)

    # 상세 콘티의 `등장:` 선언은 바이블에 아직 반영되지 않은 인물도 포함할 수 있다.
    for line in source_text.splitlines():
        m = re.match(r"^\s*등장\s*:\s*(.+)$", line)
        if not m:
            continue
        raw = m.group(1)
        for chunk in re.split(r"\s*[·,/]\s*", raw):
            name = re.sub(r"\s*\(.*$", "", chunk).strip()
            name = re.sub(r"\s*(?:및|와|과)$", "", name).strip()
            if name and len(name) <= 30 and name not in ("없음", "엑스트라"):
                people.append(name)

    people = list(dict.fromkeys(people))
    if not people:
        return [], [], f"<{work}> {episode}화 자료는 읽었지만 등장인물 이름을 구조적으로 찾지 못했어요."

    registered: list[str] = []
    try:
        for el in oi.load_elements(work):
            kind = str(el.get("type") or "")
            if kind in ("person", "인물"):
                name = _element_name(el)
                if name:
                    registered.append(name)
    except Exception:
        log.exception("episode_required_people: element load failed work=%s", work)

    def _is_registered(name: str) -> bool:
        compact = re.sub(r"\s+", "", name)
        for actual in registered:
            normalized = re.sub(r"\s+", "", actual)
            if compact == normalized or compact in normalized or normalized in compact:
                return True
        return False

    missing = [name for name in people if not _is_registered(name)]
    return people, missing, None


def _episode_character_costume(work: str, episode: int, character: str) -> tuple[str | None, str | None]:
    """노션/캐시의 바이블·대본·상세콘티에서 특정 인물의 해당 화 의상을 찾는다."""
    from . import dispatch_storyboard as sb
    from . import reference

    canonical = _canonical_work(work) or work
    bible = None
    try:
        sheet = reference.sheet()
        if sheet:
            bible = sheet.get(canonical) or sheet.get(work)
    except Exception:
        log.exception("episode_character_costume: bible load failed work=%s", canonical)

    script = ""
    try:
        script, _err = sb._script_for(canonical, episode, bible)
        script = script or ""
    except Exception:
        log.exception("episode_character_costume: script load failed work=%s ep=%s", canonical, episode)

    conti = ""
    try:
        conti, _source = sb._fetch_external_conti(canonical, episode)
        conti = conti or ""
    except Exception:
        log.exception("episode_character_costume: conti load failed work=%s ep=%s", canonical, episode)

    # 바이블의 캐릭터 구조에서 의상 필드를 먼저 확인한다.
    characters = (bible or {}).get("characters") or {}
    if isinstance(characters, dict):
        info = characters.get(character)
        if isinstance(info, dict):
            for key in ("costume", "costumes", "outfit", "wardrobe", "의상", "복장", "옷"):
                value = info.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip(), "작품 바이블"
                if isinstance(value, list) and value:
                    clean = [str(x).strip() for x in value if str(x).strip()]
                    if clean:
                        return ", ".join(clean), "작품 바이블"

    source_text = "\n".join(x for x in (script, conti) if x).strip()
    if not source_text:
        return None, f"<{canonical}> {episode}화 대본이나 상세 콘티를 읽지 못했어요."

    escaped = re.escape(character)
    patterns = (
        rf"{escaped}\s*\(\s*(?:의상\s*:\s*)?([^\n\)]+)\)",
        rf"{escaped}[^\n]{{0,30}}(?:의상|복장|옷)\s*[:：-]\s*([^\n]+)",
        rf"등장\s*:\s*[^\n]*{escaped}\s*\(([^\n\)]+)\)",
    )
    for pattern in patterns:
        m = re.search(pattern, source_text, flags=re.I)
        if m:
            value = re.sub(r"\s+", " ", m.group(1)).strip(" ·,/")
            if value:
                return value, "1화 대본/상세 콘티"

    # 구조가 느슨한 문서도 지원: 캐릭터와 의상 키워드가 함께 있는 줄을 그대로 근거로 반환.
    for line in source_text.splitlines():
        clean = line.strip()
        if character in clean and re.search(r"의상|복장|옷|교복|셔츠|재킷|후드|원피스|정장", clean):
            clean = re.sub(r"^[\-*•\s]+", "", clean)
            if len(clean) <= 240:
                return clean, "1화 대본/상세 콘티"

    return None, f"<{canonical}> {episode}화 자료는 읽었지만 {character}의 의상 표기를 찾지 못했어요."


_ELEMENT_KIND_KO_TO_EN = {"의상": "costume", "장소": "place", "소품": "prop", "인물": "person"}


def _episode_place_hint(work: str, episode: int, hint: str) -> tuple[str | None, str | None]:
    """해당 화 상세콘티/대본에서 장소 표기를 찾는다(씬 헤더 "S#N 장소-세트", "장소:", "배경:")."""
    from . import dispatch_storyboard as sb

    canonical = _canonical_work(work) or work
    script = ""
    try:
        script, _err = sb._script_for(canonical, episode, None)
        script = script or ""
    except Exception:
        log.exception("episode_place_hint: script load failed work=%s ep=%s", canonical, episode)
    conti = ""
    try:
        conti, _source = sb._fetch_external_conti(canonical, episode)
        conti = conti or ""
    except Exception:
        log.exception("episode_place_hint: conti load failed work=%s ep=%s", canonical, episode)

    source_text = "\n".join(x for x in (script, conti) if x).strip()
    if not source_text:
        return None, f"<{canonical}> {episode}화 대본이나 상세 콘티를 읽지 못했어요."

    hint_compact = re.sub(r"\s+", "", hint or "")
    patterns = (
        r"S\s*#\s*\d+\s*([^\n\-–—]+)",
        r"(?:장소|배경)\s*[:：]\s*([^\n]+)",
    )
    found: list[str] = []
    for pattern in patterns:
        for m in re.finditer(pattern, source_text, flags=re.I):
            value = re.sub(r"\s+", " ", m.group(1)).strip(" ·,/-–—")
            if value and len(value) <= 30 and "\n" not in value:
                found.append(value)
    if hint_compact:
        matched = [v for v in found if hint_compact in re.sub(r"\s+", "", v)]
        if matched:
            return matched[0], "1화 대본/상세 콘티"
    if found:
        return found[0], "1화 대본/상세 콘티"
    return None, f"<{canonical}> {episode}화 자료는 읽었지만 장소 표기를 찾지 못했어요."


def _episode_prop_hint(work: str, episode: int, character: str | None, hint: str) -> tuple[str | None, str | None]:
    """해당 화 지문에서 소품 표기를 찾는다(캐릭터의 지문 우선, "소품:" 라인, hint 명사와 결합)."""
    from . import dispatch_storyboard as sb

    canonical = _canonical_work(work) or work
    script = ""
    try:
        script, _err = sb._script_for(canonical, episode, None)
        script = script or ""
    except Exception:
        log.exception("episode_prop_hint: script load failed work=%s ep=%s", canonical, episode)
    conti = ""
    try:
        conti, _source = sb._fetch_external_conti(canonical, episode)
        conti = conti or ""
    except Exception:
        log.exception("episode_prop_hint: conti load failed work=%s ep=%s", canonical, episode)

    source_text = "\n".join(x for x in (script, conti) if x).strip()
    if not source_text:
        return None, f"<{canonical}> {episode}화 대본이나 상세 콘티를 읽지 못했어요."

    hint_compact = re.sub(r"\s+", "", hint or "")
    found: list[str] = []
    for m in re.finditer(r"소품\s*[:：]\s*([^\n]+)", source_text, flags=re.I):
        value = re.sub(r"\s+", " ", m.group(1)).strip(" ·,/")
        if value and len(value) <= 30 and "\n" not in value:
            found.append(value)
    lines = source_text.splitlines()
    char_lines = [ln for ln in lines if character and character in ln] if character else []
    for ln in char_lines + lines:
        clean = ln.strip()
        if hint_compact and hint_compact in re.sub(r"\s+", "", clean) and len(clean) <= 240:
            m = re.search(rf"([^\s(]*{re.escape(hint)}(?:\([^\n\)]+\))?)", clean)
            if m:
                value = m.group(1).strip(" ·,/")
                if value:
                    found.append(value)
    if found:
        return found[0], "1화 대본/상세 콘티"
    return None, f"<{canonical}> {episode}화 자료는 읽었지만 {hint or ''} 소품 표기를 찾지 못했어요."


def resolve_element_label(
    work: str, episode: int | None, kind: str, character: str | None, hint: str = ""
) -> tuple[str | None, list[str], str]:
    """(확정 라벨 | None, 후보 라벨들, 근거 설명) 반환. kind는 "costume"|"place"|"prop".

    스틸컷 참조는 라벨 일치로 붙기 때문에, "이영 상의"/"옥상 배경" 같은 사용자 호칭이 아니라
    콘티에 실제로 쓰인 라벨로 등록해야 한다. 공통 우선순위:
    1) 해당 화 상세콘티/대본에서의 표기(kind별 추출) — 결과가 짧은 고유 라벨(공백·줄바꿈
       없는 20~30자 이하)일 때만 후보로 쓴다.
    2) 등록된 동일 kind 엘리먼트 중 character/hint와 부분 일치하는 것.
    3) 작품 바이블(costume만 우선 지원, 기존 동작 유지).
    episode가 None이면 1)을 건너뛰고 2)만 본다.
    """
    canonical = _canonical_work(work) or work
    candidates: list[str] = []
    reason = ""

    if episode is not None:
        if kind == "costume" and character:
            label_raw, source = _episode_character_costume(canonical, episode, character)
        elif kind == "place":
            label_raw, source = _episode_place_hint(canonical, episode, hint)
        elif kind == "prop":
            label_raw, source = _episode_prop_hint(canonical, episode, character, hint)
        else:
            label_raw, source = None, None
        label = (label_raw or "").strip()
        if label and len(label) <= 30 and "\n" not in label:
            candidates.append(label)
            reason = f"{episode}화 {source or '콘티/대본'}"

    if not candidates:
        try:
            from . import openrouter_image as oi

            hint_compact = re.sub(r"\s+", "", hint or "")
            char_compact = re.sub(r"\s+", "", character or "")
            for el in oi.load_elements(canonical):
                if str(el.get("type") or "") != kind:
                    continue
                display = _element_name(el)
                if not display:
                    continue
                compact = re.sub(r"\s+", "", display)
                if kind == "costume":
                    if char_compact and char_compact not in compact:
                        continue
                    if hint_compact and hint_compact not in compact and char_compact not in compact:
                        continue
                else:
                    if hint_compact and hint_compact not in compact:
                        continue
                    if not hint_compact and char_compact and char_compact not in compact:
                        continue
                candidates.append(display)
        except Exception:
            log.exception("resolve_element_label: 등록 엘리먼트 스캔 실패 work=%s kind=%s", canonical, kind)
        if candidates:
            kind_label = {"costume": "의상", "place": "장소", "prop": "소품"}.get(kind, kind)
            reason = f"등록된 {kind_label} 엘리먼트"

    uniq = list(dict.fromkeys(c for c in candidates if c))
    if len(uniq) == 1:
        return uniq[0], uniq, reason
    if len(uniq) > 1:
        return None, uniq, reason
    subject = character or hint or ""
    fallback_reason = reason or (
        f"{episode}화 콘티/대본에서 {subject} 라벨을 찾지 못했어요."
        if episode is not None
        else f"{subject} 라벨을 찾지 못했어요."
    )
    return None, [], fallback_reason


def resolve_costume_label(
    work: str, episode: int | None, character: str, hint: str = ""
) -> tuple[str | None, list[str], str]:
    """레거시 호출부 호환용 얇은 래퍼 — resolve_element_label(kind="costume")에 위임."""
    return resolve_element_label(work, episode, "costume", character, hint=hint)


def _answer_question(
    channel: str,
    thread_ts: str,
    event: dict,
    r: Route,
    legacy_fallback,
) -> None:
    """question_type 없이 LLM이 현재 상태 자료를 근거로 작성한 답을 전달한다."""
    answer = (r.reply_text or "").strip()
    if not answer:
        legacy_fallback(event)
        return
    _reply(channel, thread_ts, answer)


def _apply_element_label_resolution(r: Route, ctx: dict) -> str | None:
    """의상/장소/소품 엘리먼트 하나로 좁혀진 등록/수정/생성 요청에서 콘티 라벨을 확정해
    r.display_label을 덮어쓴다("이영 상의"/"옥상 배경" 같은 사용자 호칭이 아니라 콘티에
    실제로 쓰인 라벨로 등록해야 스틸컷 참조가 붙는다). 후보가 여러 개면
    r.needs_clarification을 세팅한다(호출부가 바로 리턴). 반환값은 성공/실패 시 함께 보낼
    안내 문구(없으면 None). kind=="인물"(person)은 이 로직의 대상이 아니다."""
    if not r.elements or len(r.elements) != 1:
        return None
    el0 = r.elements[0]
    kind_ko = str(el0.get("kind") or "")
    kind = _KO_KIND_TO_EN.get(kind_ko)
    if not kind:
        return None
    character = str(el0.get("character") or "").strip() or None
    work = r.work or ctx.get("tracked_work")
    if not work:
        return None

    name = str(el0.get("name") or r.display_label or "").strip()
    if not name:
        return None

    # LLM이 character 슬롯을 못 채운 실사용 케이스 대비 — 결정적 패턴으로 한 번 더 시도한다.
    # 이게 없으면 의상 라벨 매칭 자체가 스킵되고, name이 지저분한 원문 그대로 노출/등록된다
    # (실사용 사고: "1화에 나올 이영 옷 상의는 이 이미지에 있는 거랑 동일하게..." →
    # character 미채움 → 매칭 스킵 → "이영 옷 상의는 있는 거랑 이미지"로 등록됨).
    if kind == "costume" and not character:
        character = _extract_character_for_costume(r.instruction or name)

    # 이미 등록된 라벨/별칭과 정확히 일치하면(사용자가 콘티 라벨을 직접 부른 경우)
    # 해석을 건너뛰고 그대로 사용한다.
    try:
        from . import openrouter_image as oi

        canonical = _canonical_work(work) or work
        name_compact = re.sub(r"\s+", "", name)
        for el in oi.load_elements(canonical):
            if str(el.get("type") or "") != kind:
                continue
            display = _element_name(el)
            if display and re.sub(r"\s+", "", display) == name_compact:
                return None
    except Exception:
        log.exception("_apply_element_label_resolution: 등록 엘리먼트 사전 체크 실패 work=%s", work)

    if kind == "costume" and not character:
        return None

    episode = r.episode if r.episode is not None else ctx.get("tracked_episode")
    part = str(el0.get("part") or "").strip()
    hint = part or name
    ep_txt = f"{episode}화 " if episode is not None else ""

    confirmed, candidates, _reason = resolve_element_label(work, episode, kind, character, hint=hint)
    if confirmed:
        original = (r.display_label or name).strip()
        r.display_label = confirmed
        if original and original != confirmed:
            el0["register_alias"] = original
        if kind == "costume":
            return f"📌 {ep_txt}콘티에서 {character} 의상은 '{confirmed}' — 이 라벨로 등록해요 (스틸컷에 자동 반영되도록)"
        if kind == "place":
            return f"📌 {ep_txt}콘티의 장소는 '{confirmed}' — 이 라벨로 등록해요 (스틸컷에 자동 반영되도록)"
        return f"📌 {ep_txt}콘티에서 소품은 '{confirmed}' — 이 라벨로 등록해요 (스틸컷에 자동 반영되도록)"

    if candidates:
        r.needs_clarification = True
        subject = character or name
        kind_label = kind_ko
        if len(candidates) >= 4:
            numbered = "\n".join(f"{i+1}. {c}" for i, c in enumerate(candidates))
            r.reply_text = (
                f"{ep_txt}콘티에 {subject} {kind_label}이 {len(candidates)}개예요 — "
                f"숫자로 답변해주세요.\n{numbered}\n{len(candidates)+1}. 새 라벨로"
            )
        else:
            opts = " ".join(f"[{c}]" for c in candidates) + " [새 라벨로]"
            r.reply_text = f"{ep_txt}콘티에 {subject} {kind_label}이 {len(candidates)}개예요 — 어느 라벨로 등록할까요? {opts}"
        return None

    subject = character or name
    if kind == "costume":
        return (
            f"⚠️ {ep_txt}콘티에 {subject} 의상 라벨이 없어서 새 이름으로 등록했어요 — "
            "콘티 지문이 이 라벨을 언급해야 스틸컷에 반영돼요. 콘티에 반영해드릴까요?"
        )
    if kind == "place":
        return (
            f"⚠️ {ep_txt}콘티에 {subject} 장소 라벨이 없어서 새 이름으로 등록했어요 — "
            "콘티 지문이 이 라벨을 언급해야 스틸컷에 반영돼요. 콘티에 반영해드릴까요?"
        )
    return (
        f"⚠️ {ep_txt}콘티에 {subject} 소품 라벨이 없어서 새 이름으로 등록했어요 — "
        "콘티 지문이 이 라벨을 언급해야 스틸컷에 반영돼요. 콘티에 반영해드릴까요?"
    )


def execute(
    channel: str,
    thread_ts: str,
    event: dict,
    r: Route,
    dispatch_bracket,
    legacy_fallback,
    _depth: int = 0,
) -> None:
    """Route를 기존 핸들러 호출로 변환한다."""
    from . import dispatch_cowriter as cw
    from . import dispatch_storyboard as sb

    if r.intent == "confirm_previous":
        prev = _pop_proposal(thread_ts)
        if prev is None:
            _reply(
                channel,
                thread_ts,
                r.reply_text
                or "확인해드릴 대기 중인 요청이 없어요 — 원하시는 걸 말씀해 주세요.",
            )
            return
        prev.needs_clarification = False
        prev.confidence = max(prev.confidence, 0.9)
        execute(
            channel,
            thread_ts,
            event,
            prev,
            dispatch_bracket,
            legacy_fallback,
            _depth,
        )
        return

    if r.intent == "reject_previous":
        _pop_proposal(thread_ts)
        _reply(
            channel,
            thread_ts,
            r.reply_text or "알겠어요, 그 건은 접어둘게요. 어떻게 할까요?",
        )
        return

    if r.needs_clarification:
        _remember_proposal(thread_ts, r)
        _reply(channel, thread_ts, r.reply_text or _default_clarify(r))
        return

    if r.intent == "clarify" or r.mode == "clarify":
        _reply(channel, thread_ts, r.reply_text or "정확히 어떤 작업을 원하시는지 한 번만 더 알려주세요.")
        return

    if r.intent == "answer_question" or r.mode == "answer":
        _answer_question(channel, thread_ts, event, r, legacy_fallback)
        return

    if r.intent in ("element_register", "element_edit", "element_generate"):
        ctx = r.raw.get("_context") if isinstance(r.raw, dict) else None
        note = _apply_element_label_resolution(r, ctx if isinstance(ctx, dict) else {})
        if r.needs_clarification:
            _remember_proposal(thread_ts, r)
            _reply(channel, thread_ts, r.reply_text or _default_clarify(r))
            return
    else:
        note = None

    if r.intent == "smalltalk":
        _reply(channel, thread_ts, r.reply_text or "네, 말씀해 주세요.")
        return

    if r.steps and _depth == 0:
        _echo_assumptions(channel, thread_ts, r)
        for step in r.steps[:5]:
            sub = Route(
                intent=str(step.get("action") or step.get("intent") or ""),
                raw=step,
                work=step.get("work") or r.work,
                episode=(
                    step.get("episode")
                    if isinstance(step.get("episode"), int)
                    else None
                ),
                episodes=(
                    step.get("episodes")
                    if isinstance(step.get("episodes"), list)
                    else None
                ),
                scene=(
                    step.get("scene")
                    if isinstance(step.get("scene"), int)
                    else None
                ),
                cuts=(
                    step.get("cuts")
                    if isinstance(step.get("cuts"), list)
                    else None
                ),
                elements=(
                    step.get("elements")
                    if isinstance(step.get("elements"), list)
                    else None
                ),
                instruction=(step.get("instruction") or "").strip() or None,
                display_label=(step.get("display_label") or "").strip() or None,
                confidence=r.confidence,
            )
            if sub.intent in INTENT_SPECS:
                execute(
                    channel,
                    thread_ts,
                    event,
                    sub,
                    dispatch_bracket,
                    legacy_fallback,
                    _depth + 1,
                )
        return

    if guard_or_queue(channel, thread_ts, event, r):
        return

    if r.intent in _MUTATING and _depth == 0:
        _echo_assumptions(channel, thread_ts, r)

    intent, body = r.intent, _q(r.instruction)

    if intent == "resume_interrupted":
        rec = sb.interrupted_state.get(thread_ts)
        if rec:
            sb.interrupted_state.clear(thread_ts)
            _reply(channel, thread_ts, "🔁 끊겼던 작업을 이어서 다시 시도할게요…")
            sb.sb_do_storyboard(
                channel,
                thread_ts,
                rec["rest"],
                stage=(1 if rec["kind"] == "plan" else 2),
            )
        else:
            _reply(
                channel,
                thread_ts,
                "이어서 할 끊긴 작업이 없어요 — 새로 시작하려면 원하시는 작업을 말씀해 주세요.",
            )
        return

    bracket_map = {
        "script_generate": ("생성", body),
        "plan_edit": ("기획", body),
        "feedback": ("피드백", body),
        "fb_fun": ("재미", body),
        "fb_logic": ("개연성", body),
        "trend": ("트렌드", body),
        "idea": ("아이디어", body),
        "sync": ("동기화", body),
        "alias": ("별칭", body),
        "convert": ("변환", body),
        "check": ("확인", body),
        "file_export": ("파일", body),
        "scene_design": ("스토리보드", body),
        "storyboard_image": ("이미지", body),
        "compile": ("합본", body),
        "autopilot": ("자동주행", body),
        "episode_status": ("진행상황", body),
        "style_change": ("스타일", body),
        "conti_final": ("콘티확정", body),
        "reset_episode": ("화초기화", body),
    }
    if intent in bracket_map:
        cmd, bracket_body = bracket_map[intent]
        episodes = r.episodes or ([r.episode] if r.episode is not None else [None])
        if len(episodes) > 1:
            _reply(
                channel,
                thread_ts,
                f"요청 확인: {', '.join(f'{ep}화' for ep in episodes)} 순서대로 진행할게요.",
            )
        for episode in episodes[:8]:
            sub = Route(
                intent=intent,
                work=r.work,
                episode=episode,
                instruction=r.instruction,
            )
            dispatch_bracket(
                channel,
                thread_ts,
                _bracket(cmd, sub, bracket_body),
                event,
            )
        return

    if intent == "detail_conti":
        rest = f"<{r.work}> {r.episode}화" if (r.work and r.episode) else (body or "")
        sb.sb_do_storyboard(channel, thread_ts, rest, stage=2)
        return
    if intent == "conti_rewrite":
        # ★2026-07-21 작업2(+후속): 트리거 판정(_CONTI_REWRITE_RE 등)을 재심사하지 않고
        # 실행부를 직접 호출 — 라우터가 이미 확정한 work/episode/scene을 전부 명시 파라미터로
        # 넘긴다(scene을 "씬N " 텍스트로 body에 심어 넘기던 예전 방식은 파라미터 우선 원칙이
        # 반쪽만 지켜지는 것이라 제거 — _do_conti_rewrite가 이제 scene을 직접 받는다).
        if not sb._do_conti_rewrite(channel, thread_ts, body, event,
                                    work=r.work, episode=r.episode, scene=r.scene):
            legacy_fallback(event)
        return
    if intent == "stillcut":
        rest = " ".join(x for x in (
            f"<{r.work}>" if r.work else "",
            f"{r.episode}화" if r.episode is not None else "",
            f"씬{r.scene}" if r.scene is not None else "",
            ("컷" + ",".join(map(str, r.cuts))) if r.cuts else "",
        ) if x)
        # ★2026-07-21: "노션에 첨부해둔 스토리보드 이미지 보고 구도 그대로" 요청은 LLM에
        # 자유생성을 맡기지 않고 여기서 결정적으로 가로챈다 — 노션 이미지를 못 찾으면
        # 절대 조용히 free-generation으로 폴백하지 않는다("임의로 생성하지 말고"는 명시적
        # 부정 제약이라 무시하면 안 됨, R18과 동일한 부정 보존 원칙).
        raw_text = _q(event.get("text")) or ""
        check_text = body or raw_text
        if r.scene is not None:
            # ★2026-07-21(추가): 이 스레드에 방금 첨부한 이미지를 구도 레퍼런스로 쓰라는
            # 요청("이 스토리보드 그리드를 보고 씬1 스틸컷을 똑같이 생성해줘" + 첨부) —
            # 노션 언급 없이도 첨부 이미지가 실제로 있으면 그걸 그대로 쓴다. 노션 경로보다
            # 먼저 확인한다: 방금 첨부한 이미지가 있으면 그게 사용자가 말한 "이 이미지"다.
            attached = sb._image_files(event)
            if attached and _wants_slack_composition_ref(check_text):
                ref_data_url = "data:image/png;base64," + base64.b64encode(attached[0][2]).decode("ascii")
                sb._do_stills(channel, thread_ts, rest, feedback=body or None, ref_data_url=ref_data_url)
                return
            # "노션에 첨부해둔 스토리보드 이미지 보고 구도 그대로" 요청은 LLM에 자유생성을
            # 맡기지 않고 여기서 결정적으로 가로챈다 — 노션 이미지를 못 찾으면 절대 조용히
            # free-generation으로 폴백하지 않는다("임의로 생성하지 말고"는 명시적 부정
            # 제약이라 무시하면 안 됨, R18과 동일한 부정 보존 원칙).
            if _wants_notion_composition_ref(check_text):
                ref_bytes = sb._notion_scene_reference_image(r.work, r.episode)
                if ref_bytes is None:
                    _reply(channel, thread_ts,
                           f"노션에서 <{r.work or '작품'}>{f' {r.episode}화' if r.episode else ''} 페이지에 "
                           "첨부된 스토리보드 이미지를 못 찾았어요 — 노션 페이지에 이미지가 잘 붙어있는지 "
                           "확인해주시거나, 이 스레드에 이미지를 직접 첨부해서 다시 요청해주세요. "
                           "(요청하신 대로 구도를 임의로 생성하지는 않았어요.)")
                    return
                ref_data_url = "data:image/png;base64," + base64.b64encode(ref_bytes).decode("ascii")
                sb._do_stills(channel, thread_ts, rest, feedback=body or None, ref_data_url=ref_data_url)
                return
        sb._do_stills(channel, thread_ts, rest, feedback=body or None)
        return
    if intent == "video":
        if not sb._do_video_from_last_still(channel, thread_ts, body or _q(event.get("text")), work=r.work):
            legacy_fallback(event)
        return
    if intent == "notion_save":
        # ★2026-07-21 작업2: _maybe_notion_save_request는 이미 트리거 판정만 하고 실행은
        # _do_save_conti에 위임하는 구조였다(_NOTION_SAVE_NL_RE 재심사 불필요) — 그 실행부를
        # 바로 호출. _do_save_conti는 bool을 반환하지 않고 실패 시 자체적으로 안내 메시지를
        # 보내므로 legacy_fallback 분기가 필요 없다.
        sb._do_save_conti(channel, thread_ts, rest=body or _q(event.get("text")))
        return

    if intent in ("element_register", "element_edit"):
        if not r.elements:
            _reply(channel, thread_ts,
                   "등록할 인물/장소/의상/소품 이름을 못 찾았어요 — 예: `인물 김신우, 이영` + 이미지 첨부")
            return
        elements = r.elements
        # 사용자 노출/등록명은 raw instruction에서 파생된 이름이 아니라 display_label을
        # 우선한다(★2026-07-21: 대상이 하나로 좁혀졌을 때만 — 여러 개면 각자의 name 유지).
        if r.display_label and len(elements) == 1:
            elements = [{**elements[0], "name": r.display_label}]
        by_kind: dict[str, list[str]] = {}
        for el in elements:
            n = (el.get("name") or "").strip()
            if n:
                by_kind.setdefault(el.get("kind", "인물"), []).append(n)
        kinds = list(by_kind)
        kind = kinds[0]
        # ★2026-07-21 작업2: r.elements로 이미 확정된 이름/타입을 텍스트로 재조립해 트리거
        # 정규식(_REF_TYPE_KW 시작 검사 등)을 다시 태우지 않고 실행부를 직접 호출.
        # kind는 라우터 스키마의 한글 라벨("인물" 등)이라 sb._REF_TYPE_KW로 정규화한다.
        etype = sb._REF_TYPE_KW.get(kind.lower(), "person")
        if not sb._do_typed_ref(channel, thread_ts, event, work=r.work, etype=etype, names=by_kind[kind]):
            legacy_fallback(event)
        else:
            if len(kinds) > 1:
                _reply(channel, thread_ts,
                       f"ℹ️ {kind}부터 등록했어요 — {', '.join(kinds[1:])}는 이미지와 함께 따로 보내주세요.")
            if note:
                _reply(channel, thread_ts, note)
        return
    if intent == "element_generate":
        query = body or _q(event.get("text"))
        # ★2026-07-21 작업2: r.elements 전부를 트리거 정규식 재심사 없이 개별 실행 —
        # 라우터가 이미 확정한 work/name/etype을 그대로 쓴다(1개든 여러 개든 동일 배선).
        # 첨부 참조 재생성(_do_element_ref_generate)을 먼저 시도하고, 첨부가 없거나
        # 실패하면 순수 생성(_do_element_gen)으로 넘어간다 — 기존 두 핸들러의 우선순위와 동일.
        # display_label/register_alias는 대상이 하나로 좁혀졌을 때만 유효하므로(둘 다
        # _apply_element_label_resolution이 elements 길이 1일 때만 세팅) 그 경우에만 name을
        # 덮어써 사용자 노출/등록명에 raw instruction 파생 이름이 아니라 이걸 쓴다.
        register_alias = None
        single_label = None
        if r.elements and len(r.elements) == 1:
            register_alias = r.elements[0].get("register_alias")
            single_label = r.display_label

        names = [(el.get("name") or "").strip() for el in (r.elements or [])]
        names = [n for n in names if n]
        if not names:
            _reply(channel, thread_ts,
                   "생성할 인물/장소/의상/소품 이름을 못 찾았어요 — 예: `인물 김신우 이미지 생성해줘`")
            return
        any_done = False
        for el in r.elements:
            name = (el.get("name") or "").strip()
            if not name:
                continue
            if single_label:
                name = single_label
            etype = sb._REF_TYPE_KW.get((el.get("kind") or "").lower(), "person")
            if sb._do_element_ref_generate(channel, thread_ts, query, event,
                                           work=r.work, name=name, etype=etype,
                                           extra_alias=register_alias):
                any_done = True
                continue
            if sb._do_element_gen(channel, thread_ts, event, work=r.work, name=name, etype=etype,
                                  extra_alias=register_alias):
                any_done = True
        if not any_done:
            legacy_fallback(event)
        elif note:
            _reply(channel, thread_ts, note)
        return

    if intent == "script_revise":
        cw._do_revise(channel, thread_ts, body or _q(event.get("text")))
        return

    if intent == "freeform":
        cw._do_freeform(channel, thread_ts, body or _q(event.get("text")))
        return

    if intent == "cancel_job":
        sb._CANCEL.add(thread_ts)
        got = sb.generator.cancel_prefix(thread_ts)
        sb.job_ledger.finish_by_thread(thread_ts)
        sb.interrupted_state.clear(thread_ts)
        _reply(
            channel,
            thread_ts,
            "🛑 진행 중 작업을 중단할게요…"
            if got
            else "🛑 중단 요청했어요 (진행 중인 컷까지만 끝내고 멈춰요).",
        )
        return

    if intent in ("work_status", "list_works", "thread_status"):
        if r.reply_text:
            _reply(channel, thread_ts, r.reply_text)
        else:
            sb._do_thread_status(channel, thread_ts)
        return

    log.warning("nl_router: intent %s 실행 매핑 없음 → legacy 폴백", intent)
    legacy_fallback(event)
