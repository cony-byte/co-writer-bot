# -*- coding: utf-8 -*-
"""
대본 문법 변환기 v1 — 슬랙 에이전트용
줄글 초안 → 방송 표준 촬영대본 포맷으로 재편(re-format). 창작 아님.

확정 규격:
1. 씬 헤더 장소 구분자: " / "
2. 화자명 정렬: 코드가 자동 정렬 (가장 긴 화자명 기준)
3. 프로필 (나이/설정): 회차당 인물별 처음 2회 등장까지 부착, 3회째부터 생략
4. 나레이션 태그: (Na)

구조:
  convert_script(draft, llm) 흐름
    1) LLM으로 초안 → 구조화 블록(JSON) 재편 (창작 금지 규칙 주입)
    2) 코드로 포맷 렌더링: 프로필 카운터 + 화자명 열 정렬

llm 인자: (system, user) -> str 를 만족하는 콜러블. (모델 무관하게 주입 — 봇은 generator.complete)
LLM 없이도 render_from_blocks()로 이미 구조화된 데이터는 렌더 가능(테스트/폴백).
"""
import json
import re
import textwrap

# ---------------- 변환 프롬프트 ----------------
CONVERT_SYSTEM = "넌 방송대본 포맷 정리 전문가다. 역할은 형식 재편이며, 창작이 아니다."

CONVERT_USER_TMPL = """아래 [초안]을 촬영대본 구조로 재편해서 JSON으로만 출력하라.

[절대 규칙]
1. 대사 워딩 변경 금지 (명백한 오타·띄어쓰기 교정만 허용).
2. 사건 순서·구성 변경 금지. 내용 추가·삭제 금지.
3. 초안에 없는 지문·연기톤을 지어내지 마라. 초안에 근거 있는 것만.
4. 애매하면 원문을 보존하라.

[블록 타입]
- scene_header: {{"type":"scene_header","place":"장소(복수는 / 로 연결)","time":"낮/밤/초저녁 등","edit":"(교차)/(회상) 등 없으면 빈칸"}}
- cut: {{"type":"cut","label":"인서트" 또는 "/ 장소."}}
- action: {{"type":"action","text":"지문(현재형 서술)"}}
- dialogue: {{"type":"dialogue","speaker":"이름","profile":"나이/설정 (초안에 인물 소개가 있으면 채우고, 없으면 빈칸)","tone":"(톤/동작) 없으면 빈칸","line":"대사"}}
- narration: {{"type":"narration","speaker":"이름","text":"나레이션"}}

[주의]
- profile은 그 인물의 나이/정체 설명이 초안에 나오면 반드시 채운다(예: "26/ 세라 재단 사생아"). 부착 횟수 제어는 시스템이 하니 매 대사 채워도 됨.
- 대사 중간 동작은 line 안에 (괄호)로 유지.

[초안]
{draft}

JSON 배열만 출력. 설명·주석·마크다운 펜스 금지."""


def convert_script(draft: str, llm, episode_label: str = "") -> str:
    """초안 문자열 → 촬영대본 포맷 문자열."""
    user = CONVERT_USER_TMPL.format(draft=draft)
    raw = llm(CONVERT_SYSTEM, user)
    blocks = _parse_json(raw)
    return render_from_blocks(blocks, episode_label=episode_label)


def _parse_json(raw: str):
    s = raw.strip()
    s = re.sub(r"^```(json)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    return json.loads(s)


# ---------------- 포맷 렌더링 ----------------
PROFILE_MAX = 2          # 규격 3: 회차당 인물별 프로필 부착 횟수
INDENT = "    "          # 지문/블록 좌여백
WRAP_WIDTH = 62          # 대사·지문 줄바꿈 폭(대략)


def render_from_blocks(blocks, episode_label: str = "") -> str:
    """구조화 블록 리스트 → 촬영대본 텍스트. 프로필 카운터 + 화자명 열 정렬."""
    # 1) 프로필 부착을 먼저 확정해야 열 폭을 정확히 계산할 수 있으므로,
    #    사전 패스로 각 dialogue/narration의 최종 라벨을 미리 구한다.
    pre_seen = {}
    labels = []
    for b in blocks:
        if b["type"] == "dialogue":
            name = b["speaker"].strip()
            pre_seen[name] = pre_seen.get(name, 0) + 1
            labels.append(_speaker_label(b, pre_seen[name]))
        elif b["type"] == "narration":
            labels.append(f"{b['speaker'].strip()} (Na)")
    col = (max((len(t) for t in labels), default=6)) + 2  # 여백 2칸
    col = max(col, 10)

    # 2) 실제 렌더 (프로필 카운터 다시 0부터)
    seen = {}
    out = []
    prev_type = None

    for b in blocks:
        t = b["type"]
        if t == "scene_header":
            num = f"{episode_label}. " if episode_label else ""
            edit = (b.get("edit") or "").strip()
            edit = (edit + " ") if edit else ""
            parts = [p for p in [b.get("place", "").strip(), b.get("time", "").strip()] if p]
            header = f"{num}{edit}{' / '.join(parts)}"
            if out:
                out.append("")
            out.append(header.strip())
            out.append("")
        elif t == "cut":
            out.append("")
            out.append(INDENT + b["label"].strip())
            out.append("")
        elif t == "action":
            if prev_type in ("dialogue", "narration", "action"):
                out.append("")
            out.append(_wrap_block(b["text"].strip(), INDENT, INDENT))
        elif t == "dialogue":
            if prev_type in ("action", "narration"):
                out.append("")
            name = b["speaker"].strip()
            seen[name] = seen.get(name, 0) + 1
            label = _speaker_label(b, seen[name])
            tone = (b.get("tone") or "").strip()
            line = ((tone + " ") if tone else "") + b["line"].strip()
            out.append(_render_speaker_line(label, line, col))
        elif t == "narration":
            if prev_type in ("action", "dialogue"):
                out.append("")
            label = f"{b['speaker'].strip()} (Na)"
            out.append(_render_speaker_line(label, b["text"].strip(), col))
        prev_type = t

    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _speaker_label(b, count):
    """대사 화자 라벨. 프로필은 등장 count가 PROFILE_MAX 이하일 때만 부착."""
    name = b["speaker"].strip()
    profile = (b.get("profile") or "").strip()
    if profile and count <= PROFILE_MAX:
        return f"{name}({profile})"
    return name


def _render_speaker_line(label, content, col):
    """화자명 열 정렬 + 대사 줄바꿈 시 대사 열에 들여쓰기."""
    pad = INDENT + label
    gap = col - len(label)
    if gap < 1:
        gap = 1
    first_prefix = pad + " " * gap
    cont_prefix = INDENT + " " * col
    wrapped = textwrap.wrap(content, width=WRAP_WIDTH) or [""]
    lines = [first_prefix + wrapped[0]]
    for w in wrapped[1:]:
        lines.append(cont_prefix + w)
    return "\n".join(lines)


def _wrap_block(text, first_indent, cont_indent):
    wrapped = textwrap.wrap(text, width=WRAP_WIDTH) or [""]
    return "\n".join(
        (first_indent if i == 0 else cont_indent) + w for i, w in enumerate(wrapped)
    )


# ---------------- 데모 (LLM 없이 구조화 블록으로 렌더 검증) ----------------
if __name__ == "__main__":
    demo = [
        {"type": "scene_header", "place": "세라 재단 대표의 사가 / 연우의 방", "time": "초저녁", "edit": ""},
        {"type": "action", "text": "윤서아(26/ 세라 재단 대표의 사생아)가 따뜻한 홍차와 얇게 썬 사과가 정갈하게 놓인 트레이를 들고 선다. 긴장으로 굳은 채 조심스럽게 안방 문을 밀어 연다. 화려한 옷차림의 조연우(26/ 세라 재단 대표의 친딸)가 소파 위에 거만하게 앉아 있다."},
        {"type": "dialogue", "speaker": "서아", "profile": "26/ 세라 재단 사생아", "tone": "", "line": "나 왔어, 연우야. 이거 먹으면서 쉬어."},
        {"type": "narration", "speaker": "서아", "text": "세라 재단 2세들 사이에는 서열이 있다. 사모님의 친딸 조연우와, 재단에서 일했던 청소부의 배에서 나온 사생아인 나. 아버지로부터 성씨조차 물려받지 못한 나는 완전히 찬밥 신세였다."},
        {"type": "action", "text": "연우, 트레이 위의 홍차를 한 모금 들이키더니 미간을 찌푸린다. 순간 퉤 하고 서아의 발밑을 향해 홍차를 뿜어버린다. 그대로 벌떡 일어나 서아의 뺨을 매섭게 후려친다."},
        {"type": "dialogue", "speaker": "연우", "profile": "26/ 세라 재단 친딸", "tone": "", "line": "장난해? 내 시중드는 게 몇 년 짼데. 차 우리는 거 하나 제대로 못 해? 아빠한테 말할까?"},
        {"type": "dialogue", "speaker": "서아", "profile": "26/ 세라 재단 사생아", "tone": "", "line": "미, 미안해. 말하지 말아줘. 다음엔 제대로 해볼게."},
        {"type": "dialogue", "speaker": "연우", "profile": "26/ 세라 재단 친딸", "tone": "", "line": "어머, 잠깐 멈춰봐. 그 목걸이 뭐야?"},
        {"type": "dialogue", "speaker": "서아", "profile": "26/ 세라 재단 사생아", "tone": "(다급하게 목걸이를 감싸 쥐며)", "line": "안 돼. 우리 엄마 유품이야. 건드리지 마."},
        {"type": "dialogue", "speaker": "연우", "profile": "26/ 세라 재단 친딸", "tone": "(비웃으며)", "line": "아직도 주제 파악이 안 됐구나? 우리 서아. 내가 달라면 줘야지, 뭐라는 거야."},
        {"type": "narration", "speaker": "서아", "text": "연우는 나의 모든 것을 짓밟았다. 나의 어머니. 나의 존엄. 나의 행복. 심지어는, 내 첫사랑까지도."},
    ]
    print(render_from_blocks(demo, episode_label="1"))
