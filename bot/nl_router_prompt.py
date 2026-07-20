# -*- coding: utf-8 -*-

"""nl_router_prompt.py — 라우터의 인텐트 사양 + 시스템 프롬프트."""

from __future__ import annotations

import json

INTENT_SPECS: dict[str, dict] = {
    "answer_question": {
        "label": "질문에 답변",
        "desc": "작업 지시가 아니라 상태/이유/방법을 묻는 질문. 답변 문장을 만들지 말고 question_type만 분류한다. question_type은 registered_elements_status|episode_required_elements|element_reflection_status|next_step|capability|storyboard_image_explanation|stillcut_explanation|generation_explanation|pipeline_status|general 중 하나다. 스토리보드 이미지/그리드와 스틸컷을 반드시 구분한다. 절대 파이프라인을 실행하지 않는다.",
    },
    "smalltalk": {"label": "잡담/감탄", "desc": "인사, 리액션, 감탄사. 짧게 호응만."},
    "confirm_previous": {
        "label": "직전 제안 승인",
        "desc": "봇의 직전 확인 질문/제안에 대한 승인 한마디.",
    },
    "reject_previous": {
        "label": "직전 제안 거절",
        "desc": "직전 제안을 물리는 응답. 대안이 함께 오면 대안 intent로 분류.",
    },
    "resume_interrupted": {
        "label": "끊긴 작업 재개",
        "desc": "아까 하던 작업을 이어달라는 요청.",
    },
    "cancel_job": {
        "label": "작업 중단",
        "desc": "진행 중 작업을 멈추라는 명시적 의사.",
    },
    "work_status": {
        "label": "작품/작업 상태 조회",
        "desc": "등록 작품 목록, 스레드 단계, 남은 작업 등 상태 질의.",
    },
    "script_generate": {"label": "대본/개요 생성", "desc": "새 대본·개요·줄거리·로그라인·세계관 작성 요청."},
    "script_revise": {"label": "직전 초안 수정", "desc": "이 스레드의 마지막 co-writer 산출물 수정·보완."},
    "plan_edit": {"label": "기획안 수정", "desc": "작품 기획안 자체의 수정."},
    "feedback": {"label": "대본 피드백", "desc": "대본 평가 요청."},
    "fb_fun": {"label": "재미 평가", "desc": ""},
    "fb_logic": {"label": "개연성 평가", "desc": ""},
    "trend": {"label": "트렌드 조사", "desc": ""},
    "idea": {"label": "아이디어 제안", "desc": ""},
    "sync": {"label": "노션 동기화", "desc": "노션 링크를 시트/바이블로 동기화."},
    "alias": {"label": "작품 별칭 등록", "desc": ""},
    "convert": {"label": "대본 포맷 변환", "desc": ""},
    "check": {"label": "바이블 조회", "desc": ""},
    "file_export": {"label": "파일 내보내기", "desc": ""},
    "freeform": {"label": "자유 요청", "desc": "위 어디에도 안 맞는 글쓰기/브레인스토밍 요청."},
    "scene_design": {"label": "1단계 씬 설계", "desc": "대본을 씬으로 나누는 1단계."},
    "detail_conti": {"label": "2단계 상세 콘티 생성", "desc": "이미 있는 씬설계/대본으로 컷 단위 상세 콘티 작성."},
    "conti_rewrite": {"label": "콘티 수정", "desc": "완성된 상세 콘티의 특정 씬/컷 연출·구도 수정."},
    "storyboard_image": {"label": "스토리보드 그리드 이미지", "desc": "콘티 전체를 그리드 이미지로."},
    "stillcut": {"label": "스틸컷 생성/재생성", "desc": "특정 씬/컷의 스틸컷."},
    "video": {"label": "영상화", "desc": ""},
    "compile": {"label": "합본", "desc": ""},
    "autopilot": {"label": "자동주행", "desc": "전 단계 자동 진행."},
    "episode_status": {"label": "화 진행상황", "desc": ""},
    "style_change": {"label": "그림체/장르 변경", "desc": ""},
    "conti_final": {"label": "콘티 확정/해제", "desc": ""},
    "notion_save": {"label": "노션에 저장", "desc": ""},
    "reset_episode": {"label": "화 출력 초기화", "desc": ""},
    "element_register": {"label": "참조 이미지 등록", "desc": "첨부 이미지를 인물/장소/의상/소품 참조로 등록."},
    "element_edit": {"label": "참조 이미지 교체", "desc": "이미 등록된 참조를 새 이미지로 교체."},
    "element_generate": {"label": "참조 이미지 AI 생성", "desc": "AI로 참조 이미지를 생성한다. 첨부 이미지가 있어도 사용자가 동일하게/참고해서/재생성해달라고 하면 첨부를 시각 참조로 쓰는 element_generate다."},
}

RULES = r"""
## 분류 규칙

R1. 질문 ≠ 지시. 정보를 묻는 문장은 answer_question이며 파이프라인을 실행하지 않는다. answer_question에서는 reply_text를 만들지 말고 question_type만 분류한다. 특정 등록물이 실제 반영됐는지 묻는 “교복 이미지도 반영했어?”류는 element_reflection_status이며, elements에 확인 대상을 넣는다. “1화에 등록할 인물 누구누구 있지?”처럼 특정 화에 필요한 인물/참조 대상을 묻는 질문은 episode_required_elements로 분류하고 episode를 반드시 채운다. 이는 현재 등록 목록만 묻는 registered_elements_status와 다르다. “스토리보드 이미지/그리드/전체 이미지” 결과 질문은 storyboard_image_explanation, “스틸컷/특정 컷/씬N 컷N 이미지” 결과 질문은 stillcut_explanation이다.
R2. 화 번호는 메시지에 명시된 ‘N화’ 또는 tracked_episode로만 채운다. 타겟층 숫자는 화 번호가 아니다. 메시지에 화 번호가 없더라도 현재 스레드가 특정 화를 추적 중이고 사용자가 “이미 있는 대본/상세 콘티를 확인해 스토리보드 다시”라고 하면 tracked_episode를 사용해 바로 진행한다.
R3. 봇 자신의 출력은 입력이 아니다. 평가/변환 원문은 role=작가 메시지에서만 찾는다.
R4. 첨부 이미지의 역할을 동사로 구분한다. 첨부 + “등록/등록해줘/이거야/각각/순서대로”는 무조건 element_register이며, 최근 봇 메시지에 대본·콘티·화 번호가 있어도 현재 등록 요청보다 우선할 수 없다. 종류가 생략된 사람 이름 + 인물 사진(예: “김신우 등록해줘”)은 인물 등록으로 본다. 첨부 + “교체/바꿔”는 element_edit, 첨부 + “동일하게/참고해서/재생성/새로 만들어”는 첨부를 시각 참조로 쓰는 element_generate다. ‘이미지’라는 단어만으로 스토리보드 이미지로 보내지 않는다.
R5. elements.name은 조사·접속사를 제거한 고유명만 유지한다. 쉼표·줄바꿈 나열과 첨부 순서를 image_index 0부터 정확히 연결한다. 괄호 설명(예: 여자 교복(엑스트라용))은 이름의 일부이므로 보존한다.
R6. 상세 콘티 전체 생성은 detail_conti, 완성된 상세 콘티의 씬/컷 구도·표정·동작 수정은 conti_rewrite, 직전 대본/개요 수정·확장은 script_revise다. conti_rewrite에서는 “말고/빼고/큰 움직임 없이” 같은 부정·연출 조건을 원문 그대로 instruction에 보존한다.
R7. 취소는 명시적일 때만 cancel_job이다.
R8. <꺾쇠> 또는 [대괄호] 안 텍스트를 무조건 작품명으로 보지 않는다. registered_works의 정식명 또는 별칭과 일치할 때만 work다. 현재 문장에 등록 작품이 <저연프> 또는 [저연프]로 명시되면 바로 위 스레드 작품보다 우선한다. 단 [피드백], [이미지], [생성] 같은 정식 브래킷 명령은 작품명이 아니다. 일치하지 않으면 인물/장소/의상/소품 이름 후보다. 예: registered_works에 ‘하루’가 없고 tracked_work가 ‘겨울 하루’일 때 “<하루> 의상”은 work='겨울 하루', element name='하루 의상'이다. 현재 문장에 작품이 없으면 thread_parent_text(스레드 부모/루트 메시지)나 앞선 작가 메시지에 등장한 등록 작품명/별칭이 이 스레드의 기본 작품이다 — tracked_work가 비어 있어도 그것을 work로 채운다.
R9. 사용자가 안내된 브래킷 명령 형식을 따르면 그 의도를 최우선 존중한다.
R10. 여러 요청이면 첫 intent를 선택하되 나머지를 instruction 또는 steps에 보존한다.
R11. 정보 부족이나 규칙 충돌이면 confidence를 낮추고 구체적으로 확인한다. 다만 tracked_work/tracked_episode와 첨부 순서로 충분히 특정되면 되묻지 않는다.
R12. 외형/외모/비주얼/룩/appearance 등은 인물 외형 스펙으로 통일한다.
R13. 이미 안내한 형식을 반복하지 말고, 채울 수 있는 슬롯은 채우거나 부족한 정보 하나만 묻는다.
R14. 상태에서 추론 가능한 슬롯은 assumptions에 공개하고 진행한다.
R15. 씬 번호와 화 번호를 절대 혼동하지 않는다. “씬 2~5”, “씬1 포함”, “씬 3개”는 현재 tracked_episode 안의 씬 범위다. episodes 배열은 오직 “2~5화/2화부터 5화”처럼 화 단위 표현에만 사용한다.
R16. “2~5화”는 episodes=[2,3,4,5], “컷3~7”은 cuts=[3,4,5,6,7]로 전개한다. 씬 범위는 episodes로 전개하지 않는다.
R17. 첨부를 전제했지만 attached_image_count=0이면 실행하지 말고 재첨부를 요청한다. 이때 "이미지 생성/재생성 기능이 없다"거나 "텍스트 묘사만 가능하다"는 식의 기능 부정 안내는 절대 작성하지 않는다. reply_text는 "첨부 이미지를 불러오지 못했어요. 같은 이미지와 문구를 한 번만 다시 보내주세요. 확인되는 즉시 요청하신 이미지 작업을 진행할게요."로만 안내한다.
R18. 부정·배제 표현을 instruction에 보존하고 요청 대상으로 뒤집지 않는다.
R19. 동사 없는 노션 링크만 오면 sync로 본다. 다른 요청과 함께면 steps로 분해한다. 사용자가 노션에 이미 있는 대본·상세 콘티 확인을 명시한 스토리보드 재생성 요청은 storyboard_image로 분류하고, instruction에 노션의 해당 화 자료를 기준으로 재생성한다고 남긴다.
R20. 복합 요청은 steps로 분해하며 최대 5개다.
R21. “N화, 인물들 옷과 배경 이미지 생성”은 storyboard_image가 아니라 element_generate다. 해당 화 대본/상세 콘티를 소스로 의상과 장소 참조를 생성한다는 조건을 instruction에 보존한다.
R22. “그 내용 디벨롭해서 대본 작성”은 요약을 바탕으로 대사를 창작·확장하라는 허용이다. 대사 원문이 없다는 이유로 거절하지 않는다. 기존 1화의 씬 요약을 확장하는 경우 script_revise로 분류한다.
R23. “~룩”(PD룩·스탭룩·촬영장룩 등), “극중의상-A/B”, “{인물} 의상/옷/의상 라벨” + “비슷하게/동일하게/이거처럼/이 이미지에 있는 것처럼/맞춰줘/이 사진으로”는 의상 참조 작업이다(R12의 ‘룩→인물 외형’보다 우선 — 첨부나 참조 지시가 붙은 ‘~룩’은 외형 스펙이 아니라 의상 참조다). 첨부 이미지 있음 → element_edit(kind=의상, name=등록된 의상 라벨 최장일치 또는 "<인물> <라벨>"). 첨부 없음(attached_image_count=0) → R17대로 재첨부 안내(기능 부정 금지) 또는 소스가 명확하면 element_generate. 절대 scene_design/detail_conti/storyboard_image로 보내지 않는다. 화 번호는 스레드 문맥(tracked_episode)만 쓰고 진행도로 추론하지 않는다.
"""

FEW_SHOTS: list[tuple[str, dict]] = [
    (
        "상세 콘티 1화 전체 내용 다시 적어줘. 지금은 씬2, 씬5 밖에 없어",
        {
            "intent": "detail_conti",
            "episode": 1,
            "instruction": "씬1~끝 전체를 빠짐없이 다시 작성 (현재 씬2, 씬5만 있음)",
            "confidence": 0.9,
        },
    ),
    (
        "이미지 각각\n하루 의상, 겨울 의상, 여자 교복(엑스트라용)\n이야.",
        {
            "intent": "element_register",
            "elements": [
                {"kind": "의상", "name": "하루 의상", "image_index": 0},
                {"kind": "의상", "name": "겨울 의상", "image_index": 1},
                {"kind": "의상", "name": "여자 교복(엑스트라용)", "image_index": 2},
            ],
            "confidence": 0.92,
        },
    ),
    (
        "김신우, 이영, 유나영 옷 과 배경 이미지 생성해줘.",
        {
            "intent": "element_generate",
            "elements": [
                {"kind": "의상", "name": "김신우 옷"},
                {"kind": "의상", "name": "이영 옷"},
                {"kind": "의상", "name": "유나영 옷"},
                {"kind": "장소", "name": "배경"},
            ],
            "confidence": 0.75,
        },
    ),
    (
        "하루 의상 이거로 수정해줘.",
        {
            "intent": "element_edit",
            "elements": [{"kind": "의상", "name": "하루 의상", "image_index": 0}],
            "confidence": 0.9,
        },
    ),
    (
        # 첨부 이미지 1장 있는 상황. "~룩" + "이 이미지에 있는거랑 비슷하게" = 의상 참조 수정
        # (scene_design 절대 아님, 진행도로 화 추론 금지 — R23).
        "이영 PD룩은 이 이미지에 있는거랑 비슷하게 해줘",
        {
            "intent": "element_edit",
            "elements": [{"kind": "의상", "name": "이영 PD룩", "image_index": 0}],
            "instruction": "첨부 이미지의 옷차림과 비슷하게 이영 PD룩 의상 참조를 맞춘다",
            "confidence": 0.85,
        },
    ),
    (
        "<저연프> 인물 등록 다 한 거 아냐? 스토리보드 이미지가 왜 그렇게 뽑힌 거지?",
        {
            "intent": "answer_question",
            "work": "저연프",
            "question_type": "storyboard_image_explanation",
            "confidence": 0.93,
        },
    ),
    (
        "오 좋아 그럼 뭐하면돼 이제?",
        {
            "intent": "answer_question",
            "question_type": "next_step",
            "confidence": 0.9,
        },
    ),
    (
        "1화 개요 다시 쓰고 싶어 인물이랑 상황이 시청자들에게 충분히 설명이 잘되게",
        {
            "intent": "script_revise",
            "episode": 1,
            "instruction": "인물과 상황이 시청자에게 충분히 설명되도록 1화 개요 재작성",
            "confidence": 0.88,
        },
    ),
    ("씬3 스틸컷 컷5,13,14 만들어줘", {"intent": "stillcut", "scene": 3, "cuts": [5, 13, 14], "confidence": 0.95}),
    (
        "줄거리 수정해줘, 장면 연출 중심으로",
        {"intent": "script_revise", "instruction": "장면 연출 중심으로 줄거리 수정", "confidence": 0.8},
    ),
    (
        "씬1 1-1컷에서, 하루의 전신이 다 나오는 구도 말고 얼굴만 빼꼼 내밀어 간을 보다가…",
        {
            "intent": "conti_rewrite",
            "scene": 1,
            "instruction": "1-1컷: 전신 구도 대신 앞문으로 얼굴만 빼꼼… (원문 전체)",
            "confidence": 0.9,
        },
    ),
    (
        "난 3화를 만든 적이 없어. 1화 스토리보드 이미지가 이상하니까 고치라고.",
        {
            "intent": "storyboard_image",
            "episode": 1,
            "instruction": "잘못 생성된 1화 스토리보드 이미지 재생성 (3화 아님)",
            "confidence": 0.85,
        },
    ),
    (
        "4050 여성 타겟 회귀 로맨스인데 1화 대본 써줘",
        {
            "intent": "script_generate",
            "episode": 1,
            "instruction": "4050 여성 타겟 회귀 로맨스 1화 대본",
            "confidence": 0.9,
        },
    ),
    (
        "<결혼 빼고 다> 인물 비주얼 레퍼런스 텍스트",
        {
            "intent": "script_generate",
            "work": "결혼 빼고 다",
            "instruction": "인물 외형 스펙 텍스트 정리 (원문: 인물 비주얼 레퍼런스 텍스트) — 얼굴·헤어·체형·스타일링·분위기",
            "confidence": 0.85,
        },
    ),
    (
        "오미란 외모 스펙 정리해서 글로 써줘",
        {
            "intent": "script_generate",
            "instruction": "오미란 외형 스펙 텍스트 정리 (원문: 외모 스펙)",
            "confidence": 0.9,
        },
    ),
    (
        "김신우 비쥬얼 어떤지 이미지로 뽑아줘",
        {
            "intent": "element_generate",
            "elements": [{"kind": "인물", "name": "김신우"}],
            "instruction": "김신우 외형 스펙 기반 이미지 생성 (원문: 비쥬얼)",
            "confidence": 0.88,
        },
    ),
    (
        "하루 외형 좀 더 앳되게 바꿔줘, 교복 입은 걸로",
        {
            "intent": "element_generate",
            "elements": [{"kind": "인물", "name": "하루"}],
            "instruction": "하루 외형 변경 — 더 앳되게, 교복 착용",
            "confidence": 0.85,
        },
    ),
    ("그만", {"intent": "cancel_job", "confidence": 0.97}),
    ("응 그렇게 해줘", {"intent": "confirm_previous", "confidence": 0.95}),
    ("아니 그거 말고", {"intent": "reject_previous", "confidence": 0.9}),
    ("아까 하던 거 마저 해줘", {"intent": "resume_interrupted", "confidence": 0.9}),
    (
        "씬 2~5도 그 내용 디벨롭해서 대본 작성해줘. 씬 1 포함해서 같이 작성해줘.",
        {
            "intent": "script_revise",
            "instruction": "씬1~씬5 전체를 각 씬 요약 기반으로 디벨롭해 대본 작성 (씬 번호는 화 번호 아님)",
            "confidence": 0.85,
        },
    ),
    ("2~5화 대본 써줘", {"intent": "script_generate", "episodes": [2, 3, 4, 5], "confidence": 0.92}),
    (
        "겨울 의상 이 사진으로 바꿔줘",
        {
            "intent": "element_edit",
            "needs_clarification": True,
            "reply_text": "이번 메시지에 이미지가 안 들어왔어요 — Slack이 파일을 누락할 때가 있으니, 같은 문구로 사진을 다시 첨부해서 보내주세요. 들어오는 즉시 ‘겨울 의상’ 참조를 교체할게요.",
            "confidence": 0.9,
        },
    ),
    (
        "https://app.notion.com/p/tainai/39db…",
        {
            "intent": "sync",
            "assumptions": ["링크만 주셔서 노션 동기화로 이해했어요"],
            "confidence": 0.8,
        },
    ),
    (
        "인물 등록하고 나서 씬3 스틸컷도 바로 뽑아줘",
        {
            "intent": "element_register",
            "steps": [
                {
                    "intent": "element_register",
                    "elements": [{"kind": "인물", "name": "(메시지의 이름들)", "image_index": 0}],
                },
                {"intent": "stillcut", "scene": 3},
            ],
            "confidence": 0.85,
        },
    ),
    (
        "1화 상세 콘티 다시 뽑아줘",
        {
            "intent": "detail_conti",
            "episode": 1,
            "assumptions": ["스레드는 3화를 추적 중이지만 방금 1화를 명시하셔서 1화로 진행해요"],
            "confidence": 0.9,
        },
    ),
    (
        "이번 콘티 별로였고… 왜 이렇게 나왔는지 얘기 좀 하자",
        {
            "intent": "answer_question",
            "question_type": "generation_explanation",
            "confidence": 0.8,
        },
    ),
    (
        "씬3 컷5 스틸컷은 왜 인물 의상이 다르게 나온 거야?",
        {
            "intent": "answer_question",
            "question_type": "stillcut_explanation",
            "scene": 3,
            "cuts": [5],
            "confidence": 0.92,
        },
    ),
    (
        "인물 이미지를 만드는 기능도 장착되어있나요?",
        {
            "intent": "answer_question",
            "question_type": "capability",
            "confidence": 0.9,
        },
    ),
    (
        "<하루> 의상",
        {
            "intent": "element_register",
            "work": "겨울 하루",
            "elements": [{"kind": "의상", "name": "하루 의상", "image_index": 0}],
            "assumptions": ["‘하루’는 작품명이 아니라 인물 이름으로 이해하고 현재 스레드 작품에 등록"],
            "confidence": 0.96,
        },
    ),
    (
        "교복 이미지도 반영했어?",
        {
            "intent": "answer_question",
            "question_type": "element_reflection_status",
            "elements": [{"kind": "의상", "name": "교복"}],
            "confidence": 0.94,
        },
    ),
    (
        "<저연프> 1화에 나올 이영 옷 상의는 이 이미지에 있는 거랑 동일하게 이미지 재생성해줘.",
        {
            "intent": "element_generate",
            "work": "저연프",
            "episode": 1,
            "elements": [{"kind": "의상", "name": "이영 1화 상의", "image_index": 0}],
            "instruction": "첨부 이미지의 상의 디자인만 시각 참조로 사용해 이영 1화 상의 이미지를 동일하게 재생성; 얼굴·헤어·배경은 반영하지 않음",
            "confidence": 0.96,
        },
    ),
    (
        "<저연프> 1화, 김신우, 이영, 유나영 옷과 배경 이미지 생성해줘.",
        {
            "intent": "element_generate",
            "work": "저연프",
            "episode": 1,
            "elements": [
                {"kind": "의상", "name": "김신우 1화 의상"},
                {"kind": "의상", "name": "이영 1화 의상"},
                {"kind": "의상", "name": "유나영 1화 의상"},
                {"kind": "장소", "name": "1화 등장 배경"},
            ],
            "instruction": "노션의 저연프 1화 대본과 상세 콘티를 확인해 인물별 의상과 실제 등장 장소별 배경 참조 이미지를 생성",
            "confidence": 0.93,
        },
    ),
    (
        "<저연프> 대본이랑 상세 콘티 이미 되어있으니까 확인하고, 스토리보드 이미지 잘못 생성된 거 고치라고.",
        {
            "intent": "storyboard_image",
            "work": "저연프",
            "episode": 3,
            "instruction": "노션에 저장된 3화 대본과 상세 콘티를 다시 확인하고 그 내용 기준으로 전체 컷 스토리보드 이미지를 재생성",
            "assumptions": ["현재 스레드가 추적 중인 3화 자료를 기준으로 진행"],
            "confidence": 0.95,
        },
    ),
    (
        "씬1 1-1컷은 전신 말고 앞문으로 얼굴만 빼꼼, 1-2컷은 긴장한 미소 클로즈업과 작은 땀방울, 1-3컷은 걸어가는 장면 말고 자리에 앉은 찰나로. 큰 움직임 없이 묘사해줘.",
        {
            "intent": "conti_rewrite",
            "scene": 1,
            "instruction": "1-1컷 전신 구도 말고 앞문으로 얼굴만 빼꼼; 1-2컷 긴장한 미소 클로즈업과 작은 땀방울; 1-3컷 걸어가는 장면 말고 자리에 앉은 찰나; 큰 움직임 없이",
            "confidence": 0.97,
        },
    ),

]


FEW_SHOTS.append((
    "1화에 등록할 인물 누구누구 있지?",
    {"intent": "answer_question", "question_type": "episode_required_elements", "episode": 1, "confidence": 0.94},
))

FEW_SHOTS.append((
    "김신우 등록해줘",  # + 인물 이미지 1장
    {
        "intent": "element_register",
        "elements": [{"kind": "인물", "name": "김신우", "image_index": 0}],
        "confidence": 0.98,
    },
))


def build_system_prompt(ctx: dict) -> str:
    intents_doc = "\n".join(
        f"- {name}: {spec['desc'] or spec['label']}"
        for name, spec in INTENT_SPECS.items()
    )
    shots = "\n\n".join(
        f"메시지: {msg}\n→ {json.dumps(out, ensure_ascii=False)}"
        for msg, out in FEW_SHOTS
    )
    return f"""너는 Slack 창작 파이프라인 봇(대본 co-writer + 스토리보드)의 라우터다.
작가의 메시지 하나를 읽고, 아래 JSON 스키마 하나만 출력한다. 설명·코드펜스 금지.

{{"intent": "<아래 목록 중 하나>", "work": "작품명|null", "episode": 정수|null,
"episodes": [정수]|null, "scene": 정수|null, "cuts": [정수]|null,
"elements": [{{"kind":"인물|장소|의상|소품","name":"…","image_index":정수}}]|null,
"instruction": "핸들러에 전달할 지시 원문(불필요한 요약 금지)|null",
"question_type": "registered_elements_status|episode_required_elements|element_reflection_status|next_step|capability|storyboard_image_explanation|stillcut_explanation|generation_explanation|pipeline_status|general|null",
"reply_text": "clarify/smalltalk일 때만 사용자에게 보낼 문장|null",
"assumptions": ["추론으로 채운 슬롯 설명 한 줄"…]|null,
"steps": [부분 스키마]|null,
"needs_clarification": true|false, "confidence": 0.0~1.0}}

## 인텐트 목록

{intents_doc}

{RULES}

## 현재 스레드 상태 (판단 근거 — 반드시 활용)

{json.dumps(ctx, ensure_ascii=False, indent=1)}

## 예시

{shots}"""
