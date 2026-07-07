# -*- coding: utf-8 -*-
"""시스템 프롬프트 조립.

캐싱 설계 (프롬프트 캐시는 접두사 일치):
  [고정] 역할 정의 + 패턴 요약 + 사내 템플릿  ← cache_control 브레이크포인트
  [가변] 이번 요청의 유사 사례 2~3편          ← 캐시 뒤에 배치
"""
import re

from . import reference, retrieval

ROLE = """너는 숏폼 로맨스 드라마 제작팀의 보조 작가다. 슬랙에서 작가와 협업한다.
목표: 작가가 3일에 1편 쓰던 기획안·대본을 1일 1편 이상으로 끌어올리는 것.
너는 초안 생산과 반복 수정 담당이고, 최종 판단은 항상 사람 작가가 한다.

## 산출물 세 종류

**기획안** — 요청에 "기획안"이 있거나 아이디어/키워드만 주어졌을 때:
1. 제목(가제) / 로그라인 1문장
2. story_type · 핵심 트로프 · 남주 유형 · 배경 (아래 태그 체계 사용)
3. 훅 설계: 첫 3초에 무슨 일이 일어나는가 1문장 + 훅 유형
4. 절단점 설계: 각 회차(클립)가 어떤 유형의 절단점으로 끝나는가
5. 회차 구성: 1~8화 각 1줄 (사건 + 절단점)
6. 근거: 참고한 패턴/사례 id

**회차 개요** — 요청에 "개요"가 있을 때 (특정 N화의 사건 설계도). 반드시 아래 4블록으로만 쓴다:
1. **제목**: 그 화를 한 줄로 압축한 제목
2. **사건 (정확히 2개)**: 각 항목을 `● 라벨: 한두 문장` 형식으로. 시간·인과 순서대로 나열.
3. **엔딩 훅 (1개)**: 다음 화로 넘기는 강한 절단점 하나 (장면·대사로만 표현, 유형 라벨 금지)
4. **시청자 채팅 시작 예상**: 시청자가 바로 반응·참여할 포인트 1문장
   ⚠️ **최우선 기준 = 이 화가 속한 [회차분배]의 핵심사건이다. 그 범위를 절대 벗어나거나 앞질러 가지 마라.**
   레퍼런스 패턴(훅·절단점)과 줄거리는 참고용이며, 핵심사건과 충돌하면 **핵심사건을 따른다.**

**대본** — 요청에 "대본"이 있거나 기획안(스레드 위쪽 포함)이 이미 있을 때:
- 화자 표기: ML(남주)/FL(여주)/SUP(조연)/NAR(나레이션) — 레퍼런스 정제 대본과 동일 체계
- 클립 1편 = 30~120초 분량 대사
- 첫 3초 훅 대사로 시작, 절단점 대사로 끝낸다
- 지문은 [ ] 안에 최소한으로

## 원칙
- 아래 '패턴 요약'이 실측 데이터 기반 SSOT다. 훅·절단점·트로프 선택의 근거로 삼고, 근거 없는 유행 추측을 하지 마라.
- 유사 사례는 참고용이다. 표절 금지 — 구조를 빌리고 상황·대사는 새로 만든다.
- 사내 템플릿이 주어지면 그 양식을 우선한다.
- 작가가 스레드에서 수정을 요청하면 전체 재생성이 아니라 해당 부분만 고쳐서 전체본을 다시 낸다.
- 슬랙 mrkdwn으로 출력한다 (굵게는 *별표 1개*, 헤더 대신 굵은 줄).
- **분석 라벨 금지**: 절단점·훅 유형 표기(예: "— 킬러 라인 정점 컷", "축출 선언 정점 컷")를 결과물 본문에 붙이지 마라. 그 효과는 장면과 대사로만 드러내고, 유형 이름은 쓰지 않는다.
"""

# PART D — 생성 시 자동 적용되는 실패 방지 지시. 바이블을 근거로 생성 시점에 스스로 적용.
FAILSAFE = """## 작품 바이블 준수 (실패 방지 — 반드시 지킬 것)

아래 [작품 바이블]은 이 작품의 확정 설정이다. **이 작품의 정보만** 쓰며(다른 작품과 섞지 마라), 금지사항·등장인물 설정·줄거리·회차분배·개요를 **종합**해 판단한다. **{target} 시점** 기준으로 다음을 스스로 점검해 어긋나지 않게 한다. (시점은 요청에 회차가 있으면 그 회차, 없으면 진행 상태 화 기준)

1. **금지사항 절대 준수** — [금지사항]에 적힌 것은 **어떤 경우에도** 위반하지 마라. (예: 특정 인물 간 신체 폭력 금지 등)
2. **시점에 안 맞는 캐릭터 차단** — 등장인물·회차분배에서 각 인물의 등장/활동 구간·포지션을 보고, 지금 시점에 없거나 회상으로만 나올 인물을 현재 장면에 넣지 마라.
3. **캐릭터 붕괴 차단** — 인물의 포지션·설정·핵심대사(말투)를 지켜라. 특정 화까지의 태도(예: 냉대)가 바이블에 있으면 그 전환 시점을 지킨다.
4. **급전개/무전개 차단** — 줄거리·회차분배상 "이 시점엔 아직 일어나지 않을 사건"을 미리 터뜨리지 마라.
5. **개요 준수(대본일 때)** — 이 화의 [개요]가 있으면 그 사건·순서·엔딩을 **반드시** 따른다. 개요에 없는 전개를 지어내지 마라.
6. **참조 규칙** — 회차분배는 줄거리를, 대본은 그 화 개요를 근거로 한다.
7. **톤·문법** — 아래 기존 화 대본의 나레이션·대사 문법(화자 표기·문체·호흡)을 그대로 따라라.

확신이 안 서는 지점은 지어내지 말고 본문에 `⚠️ 바이블 확인 필요: …`로 표시하라."""


TREND_ROLE = """너는 숏폼 로맨스 드라마 트렌드 분석가다. 아래 [측정 데이터]는 실제 성과 지표를 집계한 것이다.
이걸 근거로만 삼고, 지표 수치·표본수(n)·스냅샷 날짜·'성과지수' 같은 용어는 **결과에 절대 나열하지 마라.**

원칙:
- 짧고 쉽게, 핵심만. 전체 8줄 이내.
- 먼저 *요즘 뜨는 것* 을 2~3개로 콕 집어준다 — 스토리 결·키워드 중심의 쉬운 말로 (숫자 금지).
- 작품 정보가 주어지면, 그 작품에 맞는 구체적 아이디어를 1~2개 **개요 수준(각 한두 줄)** 으로 제안한다.
- 표절 금지 — 트렌드는 방향만 참고하고 우리 식으로 변형한다.
- 슬랙 mrkdwn (굵게는 *별표 1개*, 불릿은 '- ')."""


def trend_system(bible: dict | None = None) -> str:
    """트렌드/아이디어 요약용 시스템 프롬프트. 작품 바이블이 있으면 맞춤 아이디어 근거로 첨부."""
    s = TREND_ROLE
    if bible:
        bits = [f"제목: {bible.get('title')}"]
        for k, lbl in (("logline", "로그라인"), ("target", "타겟"),
                       ("emotion", "핵심정서"), ("keyword", "키워드")):
            if bible.get(k):
                bits.append(f"{lbl}: {bible[k]}")
        if bible.get("plot"):
            bits.append(f"줄거리: {bible['plot'][:300]}")
        s += "\n\n# 이 작품 정보 (아이디어는 이 작품에 맞춰라)\n" + "\n".join(bits)
    return s


def _episode_range(hwasu: str) -> tuple[int, int] | None:
    """'1~12화' → (1,12), '24화' → (24,24)."""
    nums = re.findall(r"\d+", hwasu or "")
    if len(nums) >= 2:
        return int(nums[0]), int(nums[1])
    if len(nums) == 1:
        return int(nums[0]), int(nums[0])
    return None


def _current_arc(episode_plan: dict, te: int) -> str:
    """te화가 속한 구간(막)을 회차분배 화수 범위로 찾아 강조 텍스트로."""
    for gu, subs in episode_plan.items():
        rng = _episode_range(subs.get("화수", ""))
        if rng and rng[0] <= te <= rng[1]:
            title = f"{gu} {subs.get('구간', '')}".strip()
            evt = subs.get("핵심사건", "")
            return (f"## ⭐ {te}화가 속한 구간: {title} (화수 {subs.get('화수', '')})\n"
                    f"이 화는 이 구간의 흐름 안에 있다. 아래 핵심사건 범위를 벗어나거나 "
                    f"앞질러 가지 마라:\n{evt}")
    return ""


def _fmt_gender(v: str) -> str:
    """성별 축약값을 명시적으로. '남'→'남성', '여'→'여성'."""
    t = (v or "").strip()
    if t in ("남", "남자", "남성", "m", "M", "male", "Male", "♂"):
        return "남성"
    if t in ("여", "여자", "여성", "f", "F", "female", "Female", "♀"):
        return "여성"
    return t


def _fmt_age(v: str) -> str:
    """나이가 숫자만이면 '세'를 붙여 명시. '32'→'32세', '20대 후반'→그대로."""
    t = (v or "").strip()
    return f"{t}세" if t.isdigit() else t


def _character_cards(characters: dict) -> str:
    """{이름: {소분류: 내용}} → 인물 카드들. 성별·나이는 명시적 값으로 풀어서 조립."""
    from .sheet_bible import CHAR_SUBS
    cards = []
    for name, subs in characters.items():
        tags = []
        if subs.get("포지션"):
            tags.append(subs["포지션"])
        if subs.get("성별"):
            tags.append(_fmt_gender(subs["성별"]))
        if subs.get("나이"):
            tags.append(_fmt_age(subs["나이"]))
        head = f"{name}" + (f" — {' · '.join(tags)}" if tags else "")
        lines = [f"■ {head}"]
        for k in CHAR_SUBS:
            if k in ("나이", "성별", "포지션"):
                continue
            if subs.get(k):
                lines.append(f"  - {k}: {subs[k]}")
        # 스키마 밖 소분류도 흘리지 않고 표시
        for k, v in subs.items():
            if k not in CHAR_SUBS and v:
                lines.append(f"  - {k}: {v}")
        cards.append("\n".join(lines))
    return "\n\n".join(cards)


def build_bible_block(bible: dict, target_episode: int | None = None) -> str:
    """바이블(대/중/소 조립본) + 실패방지 지시 → 프롬프트 텍스트 (캐시 뒤 가변부).
    시점(target) = 명시 회차 or 진행 상태 화."""
    te = target_episode or bible.get("current_episode")
    target = f"{te}화" if te else "현재"
    parts = [FAILSAFE.replace("{target}", str(target))]

    head = f"\n# [작품 바이블] {bible.get('title')}"
    if bible.get("status_raw"):
        head += f"  ·  진행 상태: {bible['status_raw']}"
    parts.append(head)
    if bible.get("stale"):
        parts.append("⚠️ (시트를 못 읽어 이전 캐시 기준입니다. 최신이 아닐 수 있음.)")

    if bible.get("forbidden"):
        parts.append(f"## ⛔ 금지사항 (절대 위반 금지)\n{bible['forbidden']}")
    if bible.get("logline"):
        parts.append(f"## 로그라인\n{bible['logline']}")
    if bible.get("keyword"):
        parts.append(f"## 키워드\n{bible['keyword']}")
    if bible.get("target"):
        parts.append(f"## 타겟층\n{bible['target']}")
    if bible.get("emotion"):
        parts.append(f"## 핵심정서\n{bible['emotion']}")
    if bible.get("characters"):
        parts.append("## 등장인물\n" + _character_cards(bible["characters"]))
    if bible.get("plot"):
        parts.append(f"## 줄거리(참고)\n{bible['plot']}")
    if bible.get("episode_plan"):
        lines = []
        for gu, subs in bible["episode_plan"].items():
            bits = " / ".join(f"{k}: {v}" for k, v in subs.items())
            lines.append(f"- {gu} — {bits}")
        parts.append("## 회차분배(참고 · 줄거리 근거)\n" + "\n".join(lines))
        # te화가 어느 구간인지 찾아 그 막의 핵심사건을 준수 대상으로 강조
        if te:
            arc = _current_arc(bible["episode_plan"], te)
            if arc:
                parts.append(arc)

    # 대상 화 개요 (대본 생성 시 준수 대상)
    outlines = bible.get("outlines", {})
    if target_episode:
        key = f"{target_episode}화"
        if outlines.get(key):
            parts.append(f"## [{key} 개요] — 대본은 이 개요를 반드시 따를 것\n{outlines[key]}")

    # 톤 학습: 대상 화 이전의 기존 대본 최근 1~2개
    scripts = bible.get("scripts", {})
    if scripts:
        def _num(k):
            m = re.search(r"\d+", k)
            return int(m.group()) if m else 0
        keys = sorted(scripts, key=_num)
        if target_episode:
            keys = [k for k in keys if _num(k) < target_episode] or keys
        picks = keys[-2:]
        tone = "\n\n".join(f"[{k} 대본 발췌]\n{scripts[k][:900]}" for k in picks)
        if tone:
            parts.append("## 기존 화 대본 (톤·문법 학습용)\n" + tone)

    return "\n\n".join(parts)


def system_blocks(query: str, bible: dict | None = None,
                  target_episode: int | None = None) -> list[dict]:
    patterns = reference.load_patterns()
    templates = reference.load_templates()

    stable = ROLE
    if patterns:
        stable += "\n\n# 패턴 요약 (레퍼런스 DB v5 실측)\n\n" + patterns
    if templates:
        stable += "\n\n# 사내 템플릿 (이 양식 우선)\n\n" + templates

    examples = "\n\n".join(
        retrieval.format_example(e)
        for e in retrieval.select_examples(query, reference.load_db())
    )

    blocks = [
        # 고정부(작품 무관) — 프롬프트 캐시 브레이크포인트
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
    ]
    if bible:  # 가변부: 작품 바이블(작품·화별로 변함)
        blocks.append({"type": "text", "text": build_bible_block(bible, target_episode)})
    blocks.append({"type": "text", "text": "# 이번 요청 유사 사례\n\n" + examples})
    return blocks
