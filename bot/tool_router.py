# -*- coding: utf-8 -*-
"""Pure native LLM tool decision boundary.

This module has no Slack dependency.  Batch tests call :func:`decide_from_context`
directly; the Slack adapter lives in ``tool_router_slack.py``.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

from . import config, openrouter_image as oi, tool_registry


log = logging.getLogger("tool-router")


# New explicit switch, with the previous router switch retained as an operational
# fallback so an existing production rollback command keeps working.
ENABLED = os.environ.get(
    "COWRITER_TOOL_ROUTER_ENABLED",
    os.environ.get("COWRITER_ROUTER_ENABLED", "1"),
) == "1"
TIMEOUT = int(os.environ.get("COWRITER_ROUTER_TIMEOUT", "12"))
MODEL = os.environ.get("COWRITER_ROUTER_MODEL", "") or config.OPENROUTER_LLM_MODEL


@dataclass
class Decision:
    type: str
    text: str | None = None
    tool: str | None = None
    arguments: dict | None = None
    calls: list[dict] | None = None
    raw: dict | None = None


_RESPONSE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "respond_with_answer",
            "description": "실행 없이 사용자의 질문에 현재 근거만으로 답하거나 짧게 대화한다.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                           "required": ["text"], "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_for_clarification",
            "description": "안전한 실행에 꼭 필요한 정보 하나가 부족할 때 짧게 질문한다.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                           "required": ["text"], "additionalProperties": False},
        },
    },
]


def _system_prompt_static() -> str:
    """context와 무관한 고정 규칙 부분 — ★2026-07-21(프롬프트 캐싱): 이 텍스트는 메시지마다
    완전히 동일하므로 cache_control로 캐시한다(_system_prompt_blocks 참고). context(스레드
    상태)만 매번 바뀌는 별도 블록으로 분리해 캐시가 안 깨지게 한다."""
    return f"""너는 Slack 창작 제작 봇의 tool caller다.
반드시 제공된 함수만 호출한다. 일반 텍스트를 출력하지 않는다.
- 정보 질문/상태 질문/잡담은 respond_with_answer를 호출한다.
- 실행에 필요한 대상이나 범위가 하나라도 불명확하면 ask_for_clarification을 호출한다.
- respond_with_answer와 ask_for_clarification의 text는 사용자에게 그대로 보인다. 함수명,
  tool, schema, handler, context, attachment_id, sb_stage, 내부 필드명 같은 구현 용어를 절대
  언급하지 말고 자연스러운 한국어로만 답한다.
- 각 함수 JSON schema의 required 배열에 든 값만 필수다. 선택 인자가 없어도 실행 의미가
  명확하면 묻지 말고 생략한다. 특히 상세 콘티는 씬이 없으면 회차 전체를 뜻한다.
- schema에서 work가 required가 아니면 작품명을 묻지 않는다. 현재 활성 스레드의 handler가
  작품을 결정한다. schema에서 episode가 required가 아니면 회차도 묻지 않는다.
- sb_stage가 1 이상이면 현재 스토리보드 작업이 있는 스레드다. 사용자가 씬/컷/현재 결과를
  가리키면 작품·회차를 다시 묻지 말고 해당 실행 함수를 호출한다.
- 질문형 문장, 상태 질문, 원인 질문, 기능 질문, '뭐 하면 돼?'는 실행 의도가 아니다.
  가능한 범위에서 respond_with_answer로 답하고 작업 선택을 되묻지 않는다.
- 생성/수정 문장에 필요한 창작 세부사항이 적어도 schema 필수값이 충족되면 원문 전체를
  instruction으로 보존해 실행한다. 더 좋은 결과를 위한 선택사항을 되묻지 않는다.
- 실제 변경이나 생성은 설명이 정확히 일치하는 실행 함수만 호출한다.
- 사용자가 말하지 않은 작품, 회차, 씬, 컷, 첨부 ID를 만들지 않는다.
- 첨부 파일은 context.attachments에 실제로 있는 id만 사용한다.
- '응/네/그래/좋아/해줘/그걸로/아까 거/계속'처럼 독립적으로 대상을 확정할 수 없는
  짧은 답은 어떤 실행 함수도 호출하지 말고 ask_for_clarification을 호출한다. 실행 확인,
  재개나 후보 선택처럼 대상이 필요한 짧은 답은 Slack 선택 UI만 담당한다.
- 확인 여부나 위험도는 네가 결정하지 않는다. 코드는 함수별 정책으로 처리한다.
- "등록 안 된 인물/장소/의상/소품 있어?"류 질문에 respond_with_answer로 답할 때는 대본·콘티에서
  뽑은 후보 이름을 반드시 context.registered_elements(kind별 목록)와 대조한 뒤에만 "등록 안 됨"
  이라고 말한다. registered_elements에 있는 이름(라벨 표기가 조금 달라도 같은 대상이면, 예:
  "이영 PD룩"과 "PD룩" 등)은 절대 미등록으로 답하지 않는다 — 대본/콘티 본문에 이름이 나온다는
  사실만으로 미등록이라고 추측하지 않는다.
- "등록 안 됨"(=참조 이미지 자체가 아직 없어 등록이 필요함)과 "이 화 대본/콘티에 등장하지
  않음"(=이미 등록은 돼 있고 단지 이번 화 분량에 안 나옴)은 서로 다른 상태이니 절대 섞어 쓰지
  않는다. registered_elements에 있지만 이번 화 대본에는 이름이 없는 대상은 "등록 안 된"
  목록에 넣지 말고, 필요하면 별도로 "등록은 돼 있지만 이 화에는 등장하지 않음"이라고 구분해서
  언급한다. "등록 안 된 인물"류 목록에는 registered_elements에 실제로 없는 이름만 넣는다.
- 등록 여부·상태 조회 질문(예: "등록 안 된 게 뭐야", "지금 상태 어때")은 매번 그 시점의
  context.registered_elements/registered_works 등 현재 자료로 처음부터 다시 판단한다.
  recent_messages에 같은 질문과 너의 이전 답이 이미 있어도(사용자가 반복해서 다시 물어본
  경우 포함) 그 이전 답을 참고하거나 베끼지 않는다 — 다시 묻는다는 것 자체가 그새 등록
  상태가 바뀌었을 수 있다는 뜻이므로, 이전 답과 다르더라도 지금 context가 맞다.
- 생성물(스틸컷/영상/합본)의 존재·개수·진행 상태 질문("1화 스틸컷 있어?", "영상 다 만들었어?",
  "몇 컷 뽑았어?")은 반드시 context.answer_sources.generated_artifacts만 근거로 답한다.
  그 필드의 stillcut_scenes에 씬이 들어 있으면 그 씬 스틸컷은 이미 생성·저장된 것이다(절대
  "아직 생성 안 됨"이라고 답하지 마라). generated_artifacts 필드 자체가 없으면 생성 여부를
  추측하지 말고 "지금 확인이 안 된다"고만 답한다. 대본/콘티가 있다는 사실(detail_conti_excerpt
  등)로 스틸컷/영상 생성 여부를 추론하지 않는다 — 콘티 존재와 생성물 존재는 별개다.
- 한 문장에 서로 다른 작업이 명시되면 필요한 실행 함수를 사용자 문장 순서대로 모두 호출한다.
- context.resolved_defaults에 값이 있으면 그것은 코드가 검증한 현재 작품/회차다. 사용자가
  다른 값을 명시하지 않은 한 그 값을 사용하고 다시 묻지 않는다.
- '스토리보드 이미지/그리드'는 generate_storyboard_grid, '스틸컷'은
  generate_stillcuts, '상세 콘티'는 generate_detail_conti다.
- '[피드백]' 또는 '피드백해줘'는 review_script다.
- '등록/확정'은 register_reference_image(s), 이미 등록된 대상의 '이미지'를 '교체/바꿔/수정'
  (새 첨부 이미지로)은 replace_reference_image다.
- 이미 등록된 참조의 '이름'을 바꾸는 것('A를 B로 바꿔줘', '이름 변경/개명', 첨부 이미지 없음)은
  rename_reference다. 이미지 교체(replace)와 혼동하지 않는다 — 새 이미지 첨부가 없고 옛 이름과
  새 이름만 주어지면 rename_reference로 본다.
- '씬N 스틸컷', '컷N 다시 뽑아', '스토리보드 보고 스틸컷/영상화'는 현재 작업 대상이므로
  작품·회차가 없어도 해당 함수다. '씬N 콘티 수정/바꿔/손봐/다듬어'는 rewrite_conti이며
  구체적 변경 내용이 없어도 원문을 instruction으로 전달한다.
- 첨부와 함께 인물·의상·장소·소품을 등록/교체하거나 그것을 생성하라는 문장은 work가
  없어도 reference 함수를 호출한다. '룩'은 의상이다.
- 'N화 이미지/스토리보드 생성'은 특정 씬·컷이나 스틸컷이라는 말이 없으면
  generate_storyboard_grid다.
- 문장 앞의 '확정했어/이미 있다/노션에 있다'는 뒤 작업의 전제 설명일 수 있다. 뒤에
  '영상을 만들어/이미지를 다시 만들어'가 있으면 그 최종 요청만 실행하고 전제에 대한
  finalize 또는 sync를 추가하지 않는다.
- 방송명·프로그램명·로고 이름 고정은 작품 별칭 등록이 아니다. register_work_alias를
  호출하지 않는다.
- 등록된 참조 이미지를 '보여줘/뭔지 보여줘/이미지 보여줘'는 show_reference다(새로 생성 아님).
  '참조 삭제/지워줘'는 delete_reference, '원본으로 되돌려줘'는 restore_reference다.
- 영상은 나레이션(화면 밖 목소리) 음성만 지원하고, 화면 속 인물이 입을 맞춰 말하는
  립싱크는 지원하지 않는다. '이영이 (입으로) 말하게 해줘/대사 립싱크' 요청은 실행하지 말고
  '나레이션 음성만 가능하고 립싱크는 미지원'이라고 답한다(임의로 립싱크로 처리 금지).
- 다음은 정보가 충분한 예이며 ask_for_clarification을 호출하면 안 된다.
  * sb_stage가 있는 상태의 '1씬만 만들어줘' → generate_stillcuts(scene=1)
  * 직전 봇이 컷 번호를 물은 뒤 '씬2 3컷만' → generate_stillcuts(scene=2,cuts=[3])
  * '씬2 콘티 수정/바꿔/손봐/다듬어줘' → rewrite_conti(scene=2, instruction=원문)
  * 이미지가 첨부된 '인물 <겨울>, <하루>' → 첨부 순서대로 register_reference_images
  * '김신우 비주얼 이미지로 뽑아줘' → work 없이 generate_reference_image
  * 첨부 스토리보드 또는 노션 스토리보드 그대로 스틸컷 → generate_stillcuts
  * 이미지 3장 첨부 + '씬1 컷1,2,3으로 저장해줘' → save_stillcuts(scene=1, cuts=[1,2,3])
  * 이미지 첨부 + '내가 준 이미지 다시 만들지 말고 그대로 영상으로 만들어줘' →
    save_stillcuts와 generate_video 두 호출(저장이 먼저)
  * 첨부 없이 '의상 유나경 출연자룩-B를 출연자룩-A로 바꿔줘' →
    rename_reference(kind=의상, old_name=유나경 출연자룩-B, new_name=유나경 출연자룩-A)
  * mp4 3개 첨부 + '씬1 컷1,2,3 영상으로 저장해줘' → save_videos(scene=1, cuts=[1,2,3])
  * '첨부로 단계 건너뛰려면 뭘 올리면 돼?' 또는 그리드 한 장으로 콘티 없이 스틸컷 요청 →
    explain_stage_skip
  * 현재 스틸컷 출력 직후 '남자만 빼' → generate_stillcuts 또는 rewrite_conti
  * 현재 씬4 컷2 영상화, 나머지는 스틸컷 → generate_video와 generate_stillcuts 두 호출
  * '노션에 대본과 콘티가 있으니 스토리보드 이미지 다시 만들어' → URL과 '동기화'라는
    동사가 없으므로 generate_storyboard_grid 하나만 호출
  * '1화 스토리보드 이미지가 이상하니까 고쳐' → 기존 1화 그리드 재생성인
    generate_storyboard_grid(episode=1); 수정 세부사항을 다시 묻지 않는다.
  * 메시지 본문에 '*1화 대본*'과 대본 초안이 있고 '콘티로 만들어' →
    generate_scene_design(episode=1); 작품명을 묻지 않는다.
  * 스토리보드 이미지가 첨부되고 '이 스토리보드 그대로 1화 스틸컷' →
    generate_stillcuts(episode=1, attachment_id=실제 첨부 ID); 작품·씬을 묻지 않는다.
- 현재 메시지의 첨부 이미지를 '참고해서/구도 그대로' 봇이 새 스틸컷을 만들라는(생성/제작)
  요청에는 반드시 context.attachments의 해당 id를 generate_stillcuts.attachment_id로 넣는다.
- Slack 첨부가 아니라 '노션에 첨부해둔/올려둔 스토리보드 이미지'를 보고 구도·연출을 그대로
  하라는 스틸컷 요청은 노션 URL이나 재첨부를 요구하지 말고 generate_stillcuts에
  use_notion_storyboard_ref=true를 넣어 바로 호출한다(이미지 회수는 코드가 한다). 이 경우
  sync_notion을 호출하지 않는다.
  예: '씬 1. 스틸컷은 내가 노션에 첨부해둔 스토리보드 이미지를 보고 연출과 구도는 똑같이
  하도록 해. 임의로 생성하지 말고.' → generate_stillcuts(scene=1, use_notion_storyboard_ref=true)
- 첨부 이미지 '자체'를 결과물로 삼아 재생성 없이 저장하라는 요청('저장해줘', '이대로 저장',
  '내가 준 그림 그대로 스틸컷으로', '새로 만들지 말고 이 이미지로', '씬N 컷1,2,3으로 저장')은
  generate_stillcuts가 아니라 save_stillcuts다. 봇이 새로 그리는 것과 정반대이며, 첨부한
  이미지 파일 자체가 그 씬의 스틸컷이 된다. 여러 장이면 업로드 순서대로 컷에 매핑되므로
  개별 attachment_id를 나열할 필요가 없다.
- 첨부 이미지를 '재생성하지 말고/그대로' 저장한 뒤 이어서 영상으로 만들라는 한 문장은
  save_stillcuts와 generate_video를 그 순서로 두 번 호출한다.
- 첨부된 완성 영상(mp4) '자체'를 그 씬 컷의 영상으로 저장하라는 요청('이 영상 그대로 저장',
  '내가 만든 영상 넣어줘', '씬N 컷1,2,3 영상으로 저장')은 save_videos다. 봇이 새로 만드는
  generate_video와 정반대이며, 첨부 영상이 그 컷의 영상이 되고 이후 합본에 그대로 쓰인다.
  여러 개면 업로드 순서대로 컷에 매핑되므로 개별 첨부 ID를 나열할 필요가 없다.
- '첨부로 단계를 건너뛰는 법/어떤 파일을 첨부하면 되는지' 같은 방법 질문, 또는 지원되지 않는
  건너뛰기(예: 스토리보드 그리드 한 장을 콘티 없이 그대로 스틸컷으로 저장하라는 요청)에는
  explain_stage_skip을 호출한다. 그리드는 구도 참조로만 쓰이고 그 자체가 씬으로 저장되지 않는다.
- 노션 URL 자체 또는 '동기화' 요청만 sync_notion이다. '노션에 있는 자료를 확인해서
  스토리보드 이미지를 만들어'는 sync가 아니라 generate_storyboard_grid다.
- URL 끝이 말줄임표로 표시돼도 사용자가 동기화를 명시했으면 sync_notion을 호출한다.
- 비주얼 스펙, 룩앤필, 캐릭터 시트를 '정리해줘/써줘'라고 하면 질문 답변이 아니라
  generate_script로 새 텍스트를 생성한다."""


def _system_prompt(context: dict) -> str:
    """레거시 호환용 — 캐싱 없이 통짜 문자열 하나로 필요한 호출부(있다면)를 위해 유지.
    실제 tool_chat 호출은 _system_prompt_blocks를 쓴다."""
    return f"""{_system_prompt_static()}

현재 스레드 상태와 근거:
{json.dumps(context, ensure_ascii=False, indent=1)}"""


def _system_prompt_blocks(context: dict) -> list[dict]:
    """★2026-07-21(프롬프트 캐싱): 고정 규칙(캐시 대상) + 매번 바뀌는 스레드 상태(캐시 제외)를
    별도 content block으로 분리해 tool_chat에 넘긴다. 고정 규칙 블록만 cache_control을 찍는다
    — 뒤에 오는 동적 컨텍스트가 매번 달라져도 앞쪽 캐시는 그대로 재사용된다."""
    return [
        {"type": "text", "text": _system_prompt_static(),
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"현재 스레드 상태와 근거:\n{json.dumps(context, ensure_ascii=False, indent=1)}"},
    ]


def _parse_message(message: dict) -> Decision:
    calls = message.get("tool_calls") or []
    if not calls:
        raise ValueError("tool call이 없습니다")
    parsed = []
    for call in calls:
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        raw_args = function.get("arguments") or "{}"
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        if not isinstance(args, dict):
            raise ValueError("tool arguments가 객체가 아닙니다")
        parsed.append({"tool": name, "arguments": args})
    response_calls = [item for item in parsed if item["tool"] in {
        "respond_with_answer", "ask_for_clarification"
    }]
    if response_calls and len(parsed) != 1:
        raise ValueError("응답 함수와 실행 함수를 함께 호출할 수 없습니다")
    name, args = parsed[0]["tool"], parsed[0]["arguments"]
    if name == "respond_with_answer":
        if set(args) != {"text"} or not isinstance(args.get("text"), str) or not args["text"].strip():
            raise ValueError("answer tool은 비어 있지 않은 text 문자열 하나만 허용합니다")
        return Decision(type="answer", text=args["text"].strip(), raw=message)
    if name == "ask_for_clarification":
        if set(args) != {"text"} or not isinstance(args.get("text"), str) or not args["text"].strip():
            raise ValueError("clarification tool은 비어 있지 않은 text 문자열 하나만 허용합니다")
        return Decision(type="clarification", text=args["text"].strip(), raw=message)
    if len(parsed) == 1:
        return Decision(type="tool_call", tool=name, arguments=args, calls=parsed, raw=message)
    return Decision(type="tool_calls", calls=parsed, raw=message)


_SHORT_ACK_RE = re.compile(
    r"\s*(?:응|웅|네|넵|예|그래|좋아|오케이|오키|ㅇㅋ|ㅇㅇ|ok|okay|yes|"
    r"해줘|그렇게\s*해줘|응\s*그렇게\s*해줘|네\s*그렇게\s*해줘|"
    r"그걸로|이걸로|계속|계속해|이어서\s*해)\s*[.!~]*\s*",
    re.I,
)


def _resolved_context(context: dict) -> dict:
    enriched = dict(context or {})
    registry = enriched.get("registered_works") or {}
    work = enriched.get("tracked_work")
    if not work and isinstance(registry, dict) and len(registry) == 1:
        work = next(iter(registry))
    if work and isinstance(registry, dict):
        for canonical, aliases in registry.items():
            if work == canonical or work in (aliases or []):
                work = canonical
                break
    enriched["resolved_defaults"] = {
        "work": work or None,
        "episode": enriched.get("tracked_episode"),
    }
    return enriched


_UNREGISTERED_QUERY_RE = re.compile(r"(등록\s*안\s*된|미등록).{0,12}(인물|장소|의상|소품)", re.S)


def _deterministic_unregistered_answer(query: str, context: dict) -> "Decision | None":
    """★2026-07-21 실측 사고: "등록 안 된 인물/장소/의상 있어?" 질문에 LLM이 매번 답을
    다르게 하거나(temperature=0 고정 후에도), 같은 스레드에서 반복 질문하면 자기 이전
    오답을 그대로 베끼는 문제가 프롬프트 지시 3차례로도 안 고쳐졌다 — LLM에게 registered_
    elements JSON을 보여주고 "대조해서 판단해라"라고 맡기는 방식 자체가 이 질문 유형에는
    신뢰할 수 없다고 판단, 아예 LLM 판단을 안 거치고 코드로 직접 계산한다(_warn_unregistered_
    elements와 동일한 추출+oi.resolve_element 대조 로직 재사용 — 이미 그쪽에서 검증된 방식).
    이름 후보 추출만 LLM(짧은 단일 호출)에 맡기고, "등록 여부" 최종 판정은 100% 코드다."""
    if not _UNREGISTERED_QUERY_RE.search(query or ""):
        return None
    work = (context.get("resolved_defaults") or {}).get("work")
    episode = (context.get("resolved_defaults") or {}).get("episode")
    if not work:
        return None
    sources = context.get("answer_sources") or {}
    conti = sources.get("detail_conti_excerpt") or sources.get("episode_script_excerpt")
    if not conti:
        return None
    from . import dispatch_storyboard as sb
    from . import sb_prompts as prompts
    try:
        raw = oi.chat(prompts.element_extract_system(sb._place_categories(work)),
                     prompts.element_extract_user(conti), timeout=60)
        obj = sb._parse_json_object(raw)
    except Exception:
        log.exception("결정적 등록여부 조회: 후보 추출 실패 — LLM 자유응답으로 폴백")
        return None
    chars = [c.strip() for c in (obj.get("characters") or []) if isinstance(c, str) and c.strip()]
    places = [c.strip() for c in (obj.get("places") or []) if isinstance(c, str) and c.strip()]
    costumes = [c.strip(" ,") for c in (obj.get("costumes") or []) if isinstance(c, str) and c.strip(" ,")]
    props = [c.strip(" ,") for c in (obj.get("props") or []) if isinstance(c, str) and c.strip(" ,")]
    new_chars = [c for c in dict.fromkeys(chars) if not oi.resolve_element(work, c)]
    new_places = [c for c in dict.fromkeys(places) if not oi.resolve_element(work, c)]
    new_costumes = [c for c in dict.fromkeys(costumes) if not oi.resolve_element(work, c)]
    new_props = [c for c in dict.fromkeys(props) if not oi.resolve_element(work, c)]
    ep_label = f"{episode}화 " if episode else ""
    parts = [
        f"현재 <{work}> {ep_label}기준:",
        "**등록 안 된 인물**\n" + ("\n".join(f"- {c}" for c in new_chars) or "없음 — 모두 등록돼 있어요."),
        "**등록 안 된 장소**\n" + ("\n".join(f"- {c}" for c in new_places) or "없음 — 모두 등록돼 있어요."),
        "**등록 안 된 의상**\n" + ("\n".join(f"- {c}" for c in new_costumes) or "없음 — 모두 등록돼 있어요."),
    ]
    if new_props:
        parts.append("**등록 안 된 소품**\n" + "\n".join(f"- {c}" for c in new_props))
    return Decision(type="answer", text="\n\n".join(parts),
                    raw={"deterministic": True, "context": context})


def decide_from_context(query: str, context: dict, *, model: str | None = None,
                        timeout: int | None = None) -> Decision:
    """Return one answer/clarification/tool_call decision without Slack I/O.

    Exceptions intentionally propagate so batch callers can record and retry provider
    errors separately from valid clarification decisions.
    """
    context = _resolved_context(context)
    context["_user_query"] = query
    deterministic = _deterministic_unregistered_answer(query, context)
    if deterministic is not None:
        return deterministic
    # Button-only invariant: a short natural acknowledgement never reaches the model
    # and therefore can never become an executable or resume call.
    if _SHORT_ACK_RE.fullmatch(query or ""):
        return Decision(
            type="clarification",
            text="재개하거나 대상을 선택하려면 위에 표시된 버튼을 눌러주세요.",
            raw={"context": context, "blocked_short_ack": True},
        )
    import time as _time
    _t0 = _time.time()
    message = oi.tool_chat(
        _system_prompt_blocks(context), query,
        _RESPONSE_TOOLS + tool_registry.api_tools(),
        model=model or MODEL,
        timeout=timeout if timeout is not None else TIMEOUT,
    )
    _latency_ms = int((_time.time() - _t0) * 1000)
    decision = _parse_message(message)
    executable = decision.calls or ([{"tool": decision.tool, "arguments": decision.arguments or {}}]
                                    if decision.type == "tool_call" else [])
    for item in executable:
        spec = tool_registry.get(item["tool"])
        if spec:
            item["arguments"] = tool_registry.hydrate_arguments(
                spec, item.get("arguments") or {}, context
            )
    if decision.type == "tool_call" and executable:
        decision.arguments = executable[0]["arguments"]
        decision.calls = executable
    elif decision.type == "tool_calls":
        decision.calls = executable
    decision.raw = {"message": message, "context": context,
                    "latency_ms": _latency_ms, "backend": model or MODEL}
    return decision


def decide(channel: str, thread_ts: str, query: str, event: dict) -> Decision | None:
    """Slack-context convenience wrapper used by the production adapter."""
    if not ENABLED:
        return None
    # Reuse the context collector only; no legacy intent/action result is consumed.
    from . import nl_router
    context = nl_router._build_context(channel, thread_ts, event, query_text=query)
    from . import dispatch_storyboard as sb
    context["interrupted_job"] = sb.interrupted_state.get(thread_ts)
    context["attachments"] = [
        {"id": f.get("id"), "name": f.get("name"), "mimetype": f.get("mimetype")}
        for f in (event.get("files") or [])
    ]
    try:
        return decide_from_context(query, context)
    except Exception:
        log.exception("tool_router 결정 실패 → 안전 정지")
        return None
