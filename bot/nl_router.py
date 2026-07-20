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

COWRITER_ROUTER_BACKEND   "agent"(기본, Claude Code 구독 재사용) | "api"
COWRITER_ROUTER_MODEL     agent: 미지정 시 Claude Code 기본 모델 / api: 기본 claude-haiku-4-5
COWRITER_ROUTER_TIMEOUT   초 단위, 기본 12 (라우팅은 빨라야 하므로 생성용 150초와 별도)
COWRITER_ROUTER_ENABLED   "1"(기본) | "0" — 0이면 무조건 legacy_fallback (킬스위치)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field

from . import config
from .nl_router_prompt import INTENT_SPECS, build_system_prompt
from .shared.slack_io import _reply, _thread_messages, app, log

ROUTER_BACKEND = os.environ.get(
    "COWRITER_ROUTER_BACKEND",
    os.environ.get("COWRITER_BACKEND", "agent"),
)
ROUTER_MODEL = os.environ.get("COWRITER_ROUTER_MODEL", "")
ROUTER_TIMEOUT = int(os.environ.get("COWRITER_ROUTER_TIMEOUT", "12"))
ROUTER_ENABLED = os.environ.get("COWRITER_ROUTER_ENABLED", "1") == "1"

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
    reply_text: str | None = None
    question_type: str | None = None
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


def _api_route(system_text: str, prompt: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=ROUTER_MODEL or "claude-haiku-4-5",
        max_tokens=800,
        system=system_text,
        messages=[{"role": "user", "content": prompt}],
        timeout=ROUTER_TIMEOUT,
    )
    return "".join(
        block.text
        for block in resp.content
        if getattr(block, "type", "") == "text"
    ).strip()


def _call_backend(system_text: str, prompt: str) -> str:
    if ROUTER_BACKEND == "api":
        return _api_route(system_text, prompt)
    return asyncio.run(_agent_route(system_text, prompt))


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


def _looks_like_reference_image_request(text: str) -> bool:
    """첨부 이미지를 기준으로 새 이미지를 만들려는 명시적 요청인지 판별."""
    q = text or ""
    has_ref = bool(re.search(
        r"(?:이|그|첨부한?)\s*(?:이미지|사진)|(?:이미지|사진)\s*(?:와|과|처럼|기준|참고)",
        q,
    ))
    has_gen = bool(re.search(r"동일하게|똑같이|참고해서|참조해서|기준으로|재생성|다시\s*(?:생성|만들)|새로\s*만들", q))
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


def recover_event_files(channel: str, thread_ts: str, event: dict) -> list[dict]:
    """기존 핸들러가 쓰도록 복구된 파일을 event에 채우고 반환."""
    return _event_files(channel, thread_ts, event)


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


def _build_context(channel: str, thread_ts: str, event: dict) -> dict:
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
    return {
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


def _explicit_work_token(text: str, ctx: dict) -> str | None:
    """현재 문장의 <작품명> 또는 [작품명]을 등록 작품/별칭 기준으로 찾는다.

    대괄호는 [피드백], [이미지] 같은 정식 명령과 충돌하므로 등록된 작품명/별칭과
    정확히 일치하는 경우에만 작품 토큰으로 인정한다. 꺾쇠도 같은 기준을 적용해
    <하루>처럼 미등록 캐릭터 이름을 작품명으로 오인하지 않는다.
    """
    registered = _registered_work_names(ctx)
    for match in re.finditer(r"<([^<>]+)>|\[([^\[\]]+)\]", text):
        token = (match.group(1) or match.group(2) or "").strip()
        if not token or token in _COMMAND_TOKENS:
            continue
        if token in registered:
            return token
    return None



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


def _deterministic_question_route(query_text: str, ctx: dict) -> Route | None:
    """실사고가 반복된 상태조회 질문은 LLM 호출 없이 안전하게 분류한다."""
    text = (query_text or "").strip()
    if not is_question_text(text):
        return None

    work = _explicit_work_token(text, ctx) or ctx.get("tracked_work")
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
        r.episode = None
        r.episodes = None
        r.scene = None
        r.cuts = None
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
    """분류 성공 시 Route, 실패(폴백해야 함) 시 None."""
    if not ROUTER_ENABLED:
        return None

    ctx = _build_context(channel, thread_ts, event)

    deterministic = _deterministic_question_route(query_text, ctx)
    if deterministic is not None:
        explicit_work = _explicit_work_token(query_text, ctx)
        if explicit_work:
            deterministic.work = explicit_work
        log.info(
            "nl_router: deterministic question=%s work=%r ep=%r",
            deterministic.question_type, deterministic.work, deterministic.episode,
        )
        return deterministic

    system_text = build_system_prompt(ctx)
    t0 = time.time()
    try:
        raw = _call_backend(system_text, query_text)
        data = _extract_json(raw)
    except Exception as exc:
        log.warning(
            "nl_router 실패 → legacy 폴백: %s (%.1fs)",
            exc,
            time.time() - t0,
        )
        return None

    intent = str(data.get("intent") or "").strip()
    if intent not in INTENT_SPECS:
        log.warning("nl_router 미지의 intent %r → legacy 폴백", intent)
        return None

    r = Route(
        intent=intent,
        work=data.get("work") or None,
        episode=data.get("episode") if isinstance(data.get("episode"), int) else None,
        episodes=data.get("episodes") if isinstance(data.get("episodes"), list) else None,
        scene=data.get("scene") if isinstance(data.get("scene"), int) else None,
        cuts=data.get("cuts") if isinstance(data.get("cuts"), list) else None,
        elements=data.get("elements") if isinstance(data.get("elements"), list) else None,
        instruction=(data.get("instruction") or "").strip() or None,
        reply_text=(data.get("reply_text") or "").strip() or None,
        question_type=(data.get("question_type") or "").strip() or None,
        assumptions=(
            data.get("assumptions")
            if isinstance(data.get("assumptions"), list)
            else None
        ),
        steps=data.get("steps") if isinstance(data.get("steps"), list) else None,
        needs_clarification=bool(data.get("needs_clarification")),
        confidence=float(data.get("confidence") or 0),
        raw={**data, "_context": ctx},
    )
    r = _apply_safety_normalization(r, query_text, ctx)

    log.info(
        "nl_router: intent=%s work=%r ep=%r conf=%.2f (%.1fs)",
        r.intent,
        r.work,
        r.episode,
        r.confidence,
        time.time() - t0,
    )

    if (
        r.confidence < 0.55
        and not r.needs_clarification
        and r.intent not in ("answer_question", "freeform", "smalltalk")
    ):
        r.needs_clarification = True
        r.reply_text = r.reply_text or _default_clarify(r)
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


def _answer_question(
    channel: str,
    thread_ts: str,
    event: dict,
    r: Route,
    legacy_fallback,
) -> None:
    """LLM이 답변 문장을 만들지 않고, 구조화 상태로 결정적인 답만 생성한다."""
    ctx = r.raw.get("_context") if isinstance(r.raw, dict) else None
    if not isinstance(ctx, dict):
        legacy_fallback(event)
        return

    qtype = r.question_type or "general"
    work = _canonical_work(r.work or ctx.get("tracked_work"))
    episode = r.episode if r.episode is not None else ctx.get("tracked_episode")
    stage = int(ctx.get("sb_stage") or 0)
    # 컨텍스트 스냅숏은 질문 직전 등록을 놓칠 수 있으므로 실제 파일을 다시 읽는다.
    elements = _load_registered_elements(work) if work else {}

    if qtype == "element_reflection_status":
        if not work:
            _reply(channel, thread_ts, "현재 스레드에서 확인할 작품이 정해져 있지 않아요. 작품명을 함께 알려주세요.")
            return
        targets = []
        for item in r.elements or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            kind = str(item.get("kind") or "").strip()
            if name:
                targets.append((kind, name))
        if not targets:
            legacy_fallback(event)
            return

        def _norm(value: str) -> str:
            value = re.sub(r"\s+", "", value)
            for suffix in ("이미지", "참조", "사진"):
                value = value.replace(suffix, "")
            return value

        matches = []
        missing = []
        for wanted_kind, wanted_name in targets:
            wanted = _norm(wanted_name)
            found = []
            if isinstance(elements, dict):
                for kind, names in elements.items():
                    if wanted_kind and wanted_kind not in (str(kind), "?"):
                        continue
                    for name in names or []:
                        actual = str(name).strip()
                        normalized = _norm(actual)
                        if wanted and (wanted in normalized or normalized in wanted):
                            found.append(actual)
            if found:
                matches.extend(found)
            else:
                missing.append(wanted_name)
        if matches and not missing:
            _reply(channel, thread_ts, f"네, <{work}>에 {', '.join(dict.fromkeys(matches))} 참조가 등록되어 있어요.")
        elif matches:
            _reply(channel, thread_ts, f"<{work}>에서 {', '.join(dict.fromkeys(matches))}은 확인됐지만, {', '.join(missing)}은 등록 상태를 찾지 못했어요.")
        else:
            _reply(channel, thread_ts, f"아직 <{work}>에서 {', '.join(missing)} 참조가 등록된 상태로 확인되지 않아요.")
        return

    if qtype == "episode_character_costume":
        if not work:
            _reply(channel, thread_ts, "어느 작품인지 확인할 수 없어요. 작품명을 함께 알려주세요.")
            return
        if episode is None:
            _reply(channel, thread_ts, "몇 화 의상인지 화 번호를 알려주세요.")
            return
        character = ""
        for item in r.elements or []:
            if isinstance(item, dict) and str(item.get("kind") or "") in ("인물", "person"):
                character = str(item.get("name") or "").strip()
                if character:
                    break
        if not character:
            _reply(channel, thread_ts, "어느 인물의 의상인지 이름을 알려주세요.")
            return
        costume, source = _episode_character_costume(work, int(episode), character)
        if costume:
            _reply(
                channel, thread_ts,
                f"<{work}> {episode}화에서 {character}의 의상은 {costume}예요. "
                f"({source} 기준)",
            )
        else:
            _reply(channel, thread_ts, source or f"<{work}> {episode}화에서 {character}의 의상을 찾지 못했어요.")
        return

    if qtype == "episode_required_elements":
        if not work:
            _reply(channel, thread_ts, "어느 작품의 화인지 확인할 수 없어요. 작품명을 함께 알려주세요.")
            return
        if episode is None:
            _reply(channel, thread_ts, "몇 화에 필요한 인물인지 화 번호를 알려주세요.")
            return
        people, missing, error = _episode_required_people(work, episode)
        if error:
            _reply(channel, thread_ts, error)
            return
        registered = [name for name in people if name not in missing]
        lines = [f"<{work}> {episode}화에 등장하는 인물은 {', '.join(people)}예요."]
        if missing:
            lines.append("아직 인물 참조 등록이 필요한 대상: " + ", ".join(missing))
        else:
            lines.append("등장인물의 인물 참조가 모두 등록되어 있어요.")
        if registered:
            lines.append("이미 등록된 대상: " + ", ".join(registered))
        _reply(channel, thread_ts, "\n".join(lines))
        return

    if qtype == "registered_elements_status":
        if not work:
            _reply(channel, thread_ts, "현재 스레드에서 확인할 작품이 정해져 있지 않아요. 작품명을 함께 알려주세요.")
            return
        rows = []
        if isinstance(elements, dict):
            for kind, names in elements.items():
                clean = [str(n) for n in (names or []) if str(n).strip()]
                if clean:
                    rows.append(f"• {kind}: {', '.join(clean)}")
        if rows:
            _reply(channel, thread_ts, f"현재 <{work}>에 등록된 참조는 다음과 같아요.\n" + "\n".join(rows) + "\n등록 참조는 생성 시 함께 반영되며, 인물과 의상 참조가 충돌하면 결과가 섞일 수 있어요.")
        else:
            _reply(channel, thread_ts, f"현재 확인되는 <{work}> 등록 참조가 없어요.")
        return

    if qtype == "next_step":
        target = f"<{work}> " if work else ""
        ep = f"{episode}화 " if episode is not None else ""
        if stage <= 0:
            msg = f"다음 단계는 {target}{ep}대본을 기준으로 1단계 씬 설계를 만드는 거예요."
        elif stage == 1:
            msg = f"다음 단계는 {target}{ep}씬 설계를 바탕으로 2단계 상세 콘티를 만드는 거예요."
        else:
            msg = f"상세 콘티까지 있어요. 다음은 스틸컷 생성이나 콘티 수정으로 진행하면 돼요."
        _reply(channel, thread_ts, msg)
        return

    if qtype == "capability":
        _reply(
            channel,
            thread_ts,
            "이 봇은 대본·개요 생성과 수정, 씬 설계·상세 콘티, 인물/장소/의상/소품 참조 등록 및 생성, 스틸컷·영상화 요청을 처리할 수 있어요.",
        )
        return

    if qtype in ("storyboard_image_explanation", "stillcut_explanation", "generation_explanation"):
        refs = []
        if isinstance(elements, dict):
            for kind, names in elements.items():
                count = len(names or [])
                if count:
                    refs.append(f"{kind} {count}개")
        ref_text = ", ".join(refs) if refs else "확인되는 등록 참조 없음"

        if qtype == "storyboard_image_explanation":
            _reply(
                channel,
                thread_ts,
                f"스토리보드 이미지는 한 컷만 다시 만드는 스틸컷이 아니라, 상세 콘티의 여러 컷을 한 번에 그리드로 시각화한 결과예요. "
                f"현재 진행 단계는 {stage}, 참조 상태는 {ref_text}예요. "
                "그래서 특정 컷의 자세·표정 문제뿐 아니라 상세 콘티의 컷 설명, 컷 순서, 등록 참조의 누락이나 충돌이 전체 그리드에 함께 영향을 줄 수 있어요. "
                "한 컷만 고치려는 경우에는 해당 씬·컷의 스틸컷 재생성이 더 정확해요.",
            )
            return

        if qtype == "stillcut_explanation":
            _reply(
                channel,
                thread_ts,
                f"스틸컷은 상세 콘티 전체를 그리는 스토리보드 그리드가 아니라, 지정한 씬·컷 한 장을 개별 생성한 결과예요. "
                f"현재 진행 단계는 {stage}, 참조 상태는 {ref_text}예요. "
                "해당 컷의 구도·표정·동작 지시와 인물·의상·장소 참조가 직접 영향을 주므로, 문제가 있는 컷 번호와 바꿀 조건을 함께 주면 그 컷만 재생성할 수 있어요.",
            )
            return

        _reply(
            channel,
            thread_ts,
            f"현재 스레드 기준으로 생성 단계는 {stage}이고, 참조 상태는 {ref_text}예요. "
            "어떤 산출물인지 명확하지 않아 일반 생성 상태만 안내했어요. 스토리보드 그리드인지 특정 씬·컷의 스틸컷인지 알려주면 해당 경로 기준으로 설명할게요.",
        )
        return

    if qtype == "pipeline_status":
        target = work or "미지정"
        ep = str(episode) if episode is not None else "미지정"
        labels = {0: "시작 전", 1: "씬 설계 완료", 2: "상세 콘티 완료"}
        _reply(channel, thread_ts, f"현재 작품은 {target}, 화는 {ep}, 진행 단계는 {labels.get(stage, str(stage))}예요.")
        return

    # 일반 지식 질문은 기존 자유응답 체인으로 넘겨, 라우터가 사실을 지어내지 않게 한다.
    legacy_fallback(event)

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

    if r.intent == "answer_question":
        _answer_question(channel, thread_ts, event, r, legacy_fallback)
        return

    if r.intent == "smalltalk":
        _reply(channel, thread_ts, r.reply_text or "네, 말씀해 주세요.")
        return

    if r.steps and _depth == 0:
        _echo_assumptions(channel, thread_ts, r)
        for step in r.steps[:5]:
            sub = Route(
                intent=str(step.get("intent") or ""),
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
        txt = (f"씬{r.scene} " if r.scene else "") + body
        if not sb._maybe_conti_rewrite_request(channel, thread_ts, txt, event):
            legacy_fallback(event)
        return
    if intent == "stillcut":
        rest = " ".join(x for x in (
            f"<{r.work}>" if r.work else "",
            f"{r.episode}화" if r.episode is not None else "",
            f"씬{r.scene}" if r.scene is not None else "",
            ("컷" + ",".join(map(str, r.cuts))) if r.cuts else "",
        ) if x)
        sb._do_stills(channel, thread_ts, rest, feedback=body or None)
        return
    if intent == "video":
        if not sb._maybe_video_from_last_still(channel, thread_ts, body or _q(event.get("text"))):
            legacy_fallback(event)
        return
    if intent == "notion_save":
        if not sb._maybe_notion_save_request(channel, thread_ts, body or _q(event.get("text"))):
            legacy_fallback(event)
        return

    if intent in ("element_register", "element_edit"):
        if not r.elements:
            _reply(channel, thread_ts,
                   "등록할 인물/장소/의상/소품 이름을 못 찾았어요 — 예: `인물 김신우, 이영` + 이미지 첨부")
            return
        by_kind: dict[str, list[str]] = {}
        for el in r.elements:
            n = (el.get("name") or "").strip()
            if n:
                by_kind.setdefault(el.get("kind", "인물"), []).append(n)
        kinds = list(by_kind)
        kind = kinds[0]
        txt = f"{kind} {', '.join(by_kind[kind])}"
        if not sb._maybe_typed_ref(channel, thread_ts, txt, event):
            legacy_fallback(event)
        elif len(kinds) > 1:
            _reply(channel, thread_ts,
                   f"ℹ️ {kind}부터 등록했어요 — {', '.join(kinds[1:])}는 이미지와 함께 따로 보내주세요.")
        return
    if intent == "element_generate":
        query = body or _q(event.get("text"))
        # 첨부 참조 재생성은 일반 생성 핸들러보다 먼저 처리한다. 일반 핸들러는
        # 첨부가 있으면 등록 경로와 충돌하지 않도록 False를 반환하기 때문이다.
        if sb._maybe_element_ref_generate_request(channel, thread_ts, query, event):
            return
        if not sb._maybe_element_gen_request(channel, thread_ts, query, event):
            legacy_fallback(event)
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
            sb._maybe_thread_status(channel, thread_ts, _q(event.get("text")))
        return

    log.warning("nl_router: intent %s 실행 매핑 없음 → legacy 폴백", intent)
    legacy_fallback(event)
