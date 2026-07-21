# -*- coding: utf-8 -*-

"""LLM 자연어 라우터 프롬프트.

자유문장을 미리 정한 질문 유형에 끼워 맞추지 않는다.
LLM은 오직 answer / action / clarify 중 하나를 선택한다.
실제 실행은 action 화이트리스트를 기존 핸들러에 연결해 수행한다.
"""

from __future__ import annotations

import json

# 실행 가능한 동작만 화이트리스트로 유지한다. 자연어 "유형" 목록이 아니다.
ACTION_SPECS: dict[str, dict] = {
    "confirm_previous": {"label": "직전 제안 승인", "desc": "봇의 직전 확인 질문이나 제안을 승인한다."},
    "reject_previous": {"label": "직전 제안 거절", "desc": "봇의 직전 제안을 거절한다."},
    "resume_interrupted": {"label": "끊긴 작업 재개", "desc": "중단되거나 실패한 직전 작업을 재개한다."},
    "cancel_job": {"label": "작업 중단", "desc": "진행 중 작업을 명시적으로 중단한다."},
    "script_generate": {"label": "대본/개요 생성", "desc": "새 대본, 개요, 줄거리, 로그라인, 세계관을 작성한다."},
    "script_revise": {"label": "대본/초안 수정", "desc": "기존 대본이나 직전 초안을 수정한다."},
    "plan_edit": {"label": "기획안 수정", "desc": "작품 기획안을 수정한다."},
    "feedback": {"label": "대본 피드백", "desc": "대본을 평가하거나 피드백한다."},
    "fb_fun": {"label": "재미 평가", "desc": "재미와 몰입도를 평가한다."},
    "fb_logic": {"label": "개연성 평가", "desc": "개연성과 논리를 평가한다."},
    "trend": {"label": "트렌드 조사", "desc": "트렌드 조사 작업을 실행한다."},
    "idea": {"label": "아이디어 제안", "desc": "새 아이디어를 제안한다."},
    "sync": {"label": "노션 동기화", "desc": "노션 자료를 동기화한다."},
    "alias": {"label": "작품 별칭 등록", "desc": "작품 별칭을 등록한다."},
    "convert": {"label": "대본 포맷 변환", "desc": "대본 형식을 변환한다."},
    "check": {"label": "바이블 조회", "desc": "기존 바이블 조회 핸들러를 실행해야 하는 명시적 명령이다."},
    "file_export": {"label": "파일 내보내기", "desc": "결과물을 파일로 내보낸다."},
    "scene_design": {"label": "1단계 씬 설계", "desc": "대본을 씬 단위로 설계한다."},
    "detail_conti": {"label": "2단계 상세 콘티 생성", "desc": "컷 단위 상세 콘티를 생성한다."},
    "conti_rewrite": {"label": "콘티 수정", "desc": "완성된 콘티의 특정 씬이나 컷을 수정한다."},
    "storyboard_image": {"label": "스토리보드 이미지", "desc": "상세 콘티 전체를 스토리보드 그리드 이미지로 만든다."},
    "stillcut": {"label": "스틸컷 생성", "desc": "특정 씬 또는 컷의 스틸컷을 만든다."},
    "video": {"label": "영상화", "desc": "스틸컷이나 콘티를 영상으로 만든다."},
    "compile": {"label": "합본", "desc": "결과물을 합본한다."},
    "autopilot": {"label": "자동주행", "desc": "여러 제작 단계를 자동 진행한다."},
    "episode_status": {"label": "화 진행상황", "desc": "기존 진행상황 명령 핸들러를 실행한다."},
    "style_change": {"label": "그림체/스타일 변경", "desc": "생성 스타일을 변경한다."},
    "conti_final": {"label": "콘티 확정/해제", "desc": "콘티 확정 상태를 변경한다."},
    "notion_save": {"label": "노션 저장", "desc": "결과를 노션에 저장한다."},
    "reset_episode": {"label": "화 초기화", "desc": "해당 화의 출력 상태를 초기화한다."},
    "element_register": {"label": "참조 이미지 등록", "desc": "첨부 이미지를 인물, 의상, 장소, 소품 참조로 등록한다."},
    "element_edit": {"label": "참조 이미지 교체", "desc": "등록된 참조 이미지를 교체하거나 수정한다."},
    "element_generate": {"label": "참조 이미지 생성", "desc": "새 참조 이미지를 생성하거나 첨부 이미지를 기준으로 재생성한다."},
}

# 기존 코드와 테스트의 import 호환성을 유지한다.
INTENT_SPECS = ACTION_SPECS


def build_system_prompt(ctx: dict) -> str:
    actions = "\n".join(
        f"- {name}: {spec['desc']}" for name, spec in ACTION_SPECS.items()
    )
    return f"""너는 Slack 창작 파이프라인의 자연어 해석기다.
사용자 발화를 미리 정한 질문 유형으로 분류하지 않는다.
반드시 아래 세 모드 중 하나만 고르고 JSON 객체 하나만 출력한다. 설명과 코드펜스는 금지한다.

1) answer
- 사용자가 정보를 묻거나, 상태를 확인하거나, 이유/방법을 묻거나, 잡담/감탄을 한다.
- 제공된 현재 스레드 상태와 자료 안에서 바로 자연스럽게 답한다.
- 질문을 pipeline_status, costume_question 같은 하위 유형으로 분류하지 않는다.
- 새로운 작업을 시작하지 않는다.
- 근거가 없으면 지어내지 말고 "현재 확인되는 정보로는 알 수 없다"고 답한다.
- 질문에 직접 답하고 관련 없는 작품명, 화수, 전체 상태를 나열하지 않는다.

2) action
- 실제 생성, 수정, 등록, 저장, 취소, 재개 같은 실행이 필요하다.
- action은 아래 화이트리스트 중 정확히 하나여야 한다.
- 실행에 필요한 슬롯을 최대한 채운다.
- 애매한 자연어를 억지로 action에 넣지 않는다. 실행 대상이나 범위가 불명확하면 clarify를 선택한다.

3) clarify
- answer로 답할 수도 없고 안전하게 action을 실행할 수도 없을 때만 사용한다.
- 사용자에게 필요한 질문 하나만 짧게 묻는다.

출력 스키마:
{{
  "mode": "answer|action|clarify",
  "answer": "answer 모드에서 보낼 최종 답변|null",
  "question": "clarify 모드에서 보낼 확인 질문|null",
  "action": "action 모드의 화이트리스트 이름|null",
  "work": "정식 작품명|null",
  "episode": 정수|null,
  "episodes": [정수]|null,
  "scene": 정수|null,
  "cuts": [정수]|null,
  "elements": [{{"kind":"인물|장소|의상|소품","name":"사용자가 부른 이름","image_index":정수,"character":"인물명|null","part":"부위|null"}}]|null,
  "instruction": "기존 실행 핸들러에 전달할 사용자의 요청 원문 또는 충실한 지시|null",
  "display_label": "사용자 노출용 짧은 라벨|null",
  "assumptions": ["상태에서 추론한 내용"]|null,
  "steps": [{{"action":"화이트리스트 이름", "work":null, "episode":null, "scene":null, "cuts":null, "elements":null, "instruction":null}}]|null,
  "confidence": 0.0
}}

중요 규칙:
- "뭐해", "읽고 있어?", "옷 뭐였지?", "왜 이렇게 나왔어?", "등록됐어?"처럼 답을 원하는 문장은 answer다.
- "만들어줘", "고쳐줘", "등록해줘", "다시 뽑아줘", "중단해"처럼 실제 변경이 필요한 문장은 action이다.
- 질문과 지시가 섞이면 사용자의 최종 요구를 따른다. 실행이 포함되면 action, 정보 확인만이면 answer다.
- 첨부 이미지 + 등록 요청은 element_register, 교체/수정은 element_edit, 참고해서 새로 생성은 element_generate다.
- 씬 번호와 화 번호를 혼동하지 않는다. "씬 2~5"는 episodes가 아니다.
- 작품 별칭은 registered_works를 보고 정식 작품명으로 바꾼다.
- 현재 정보만으로 답할 수 없는 질문은 action으로 보내지 말고 answer에서 모른다고 말하거나, 정말 필요한 정보가 하나뿐이면 clarify한다.
- answer 모드에서는 절대로 작업 시작 안내를 하지 않는다.

실행 가능한 action 화이트리스트:
{actions}

현재 스레드 상태와 근거 자료:
{json.dumps(ctx, ensure_ascii=False, indent=1)}
"""
