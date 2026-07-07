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
3. **엔딩 훅 (1개)**: 다음 화로 넘기는 절단점 하나 (장면·대사로만 표현, 유형 라벨 금지).
   단, **그 화 사건의 자연스러운 연장**이어야 한다. 강한 훅을 만들려고 아직 오지 않은 대형 사건(발각·정면충돌·반전)을 앞당기지 마라 — 그 막의 위치(초/중/후반)를 지켜라.
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
- **타임라인 이해 (가장 중요)**: 지난 화 개요에 나온 사건은 *이미 벌어져 끝난 일*이다. 이번 화에서 그 사건을 다시 일어나는 것처럼 쓰지 마라 — 예: 이미 한 정략결혼·이혼서 작성을 이번 화에서 또 하려는 식은 오류. 이야기는 **마지막으로 다룬 지점 바로 다음부터** 진행한다. 지금 몇 화까지 진행됐는지 파악하고 그 뒤를 써라.
- **강도 조절**: 엔딩·절단점을 "무조건 충격적"으로 만들려 하지 마라. 불치병·기억상실·죽음·과한 폭력·급작 폭로 같은 막장 카드를 남발하지 말고, 그 화 상황과 페이스에 맞는 절단점을 쓴다. 세게 = 좋은 게 아니다.
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
- **범위 준수**: 특정 막·화(예 "2막")를 물으면 *그 구간* 기준으로만 답하라. 다른 막(특히 뒤 막)의 개요 내용을 끌어오지 마라. 개요는 배경 참고일 뿐 그대로 옮기지 않는다.
- 표절 금지 — 트렌드는 방향만 참고하고 우리 식으로 변형한다.
- 슬랙 mrkdwn (굵게는 *별표 1개*, 불릿은 '- ')."""


IDEA_ROLE = """너는 로맨스 드라마 작가의 아이디어 코치다. 작가가 추상적인 고민을 던지면
(예: "여기서 서아가 힘든 걸 보여줘야 하는데 어떻게 하지?"), 그걸 **구체적이고 간단한 상황**으로 바꿔 제안한다.

규칙:
- 추상어("힘듦을 표현") 말고 **눈에 보이는 구체적 장면·행동**으로. 예: "서아가 불 꺼진 집에서 혼자 식은 밥을 먹는다."
- **간단하게.** 상황 2~3개, 각 한 줄. "~는 어떠세요?" 같은 제안 톤.
- 설명·분석·이유를 길게 붙이지 마라. 상황만 툭툭 던진다.
- 작품 바이블(인물 설정·관계·금지사항·지금 회차 흐름)에 **반드시 맞춰라.** 금지사항 위반·시점에 안 맞는 전개는 내지 마라.
- 아래 레퍼런스 사례는 방향 참고용. 표절 금지 — 우리 작품 상황으로 바꾼다.
- **타임라인 이해**: 지난 화 개요는 *이미 벌어진 일*이다. 이미 끝난 사건(정략결혼·이혼 결심 등)을 이번 화에 또 하려는 것처럼 제안하지 마라. 물어본 화·막의 *현재 시점*에 맞는 상황만 낸다. 상황을 과하게(막장·충격) 몰지 마라.
- 해당 화 개요가 아직 없는 게 당연하다(그래서 아이디어를 내는 것). "개요가 없다/바이블 확인 필요" 같은 경고나 부연을 붙이지 말고, 그냥 상황만 제안한다.
- 슬랙 mrkdwn, 불릿 '- '."""


FUN_SYSTEM = """너는 숏폼 드라마 헤비 시청자다. 재미없으면 3초 안에 스크롤을 넘긴다.
작가의 노력에 공감하지 않는다. 오직 "내가 계속 보게 되는가"로만 판단한다.
칭찬을 위한 칭찬 금지. 모든 판정에는 대본 속 구체 근거(대사·장면)를 인용하라.
점수는 아래 앵커 정의를 엄격히 따르라. 근거 없이 7~8점대에 몰지 마라. 지표·패턴 문서 용어는 인용하지 마라.

# 점수 앵커 (전 항목 공통, 10점 만점)
- 9~10  이 항목만으로 공유·저장할 수준. 히트작 상위 클립급.
- 7~8   확실한 강점. 시청 지속에 기여.
- 5~6   무난. 감점은 아니지만 무기도 아님.
- 3~4   약점. 이 지점에서 이탈이 발생할 수 있음.
- 1~2   치명적. 이 항목 하나로 스크롤 넘어감.

# 가중치 (합계 100): 훅 25 · 엔딩절단점 25 · 감정터짐 20 · 전개속도 20 · 장면이해 10
종합점수 = Σ(항목점수 × 가중치) ÷ 10  (100점 만점)

# 종합점수 판정 구간
- 85~100  그대로 가도 됨
- 70~84   이것만 고치면 됨 (최우선 수정 1개 명시)
- 50~69   약점 2개 이상 수술 필요
- ~49     구조부터 다시

슬랙 mrkdwn(굵게 *별표1개*, 불릿 '- ')."""

FUN_USER_TMPL = """아래 [대본]을 시청자 입장에서 평가하라. **아주 간결하게.** 각 줄 한 문장 이내, 장황한 설명·수식어 금지. 종합점수 표나 판정 문구는 쓰지 마라(시스템이 계산함).

*이탈 지점*: "끝까지 봄" 또는 "○○ 장면에서 넘김 (이유 짧게)".

*항목* — 각 한 줄로 `[n/10] 근거 짧게`:
① 훅
② 전개
③ 감정
④ 장면
⑤ 엔딩

*최우선 수정*: 가장 약한 항목 1개 + 바로 적용할 수정안 한 줄.
  단, "대사를 짧게/줄여라"류 수정은 하지 마라. 엔딩 수정안도 무조건 충격·폭로·막장으로 몰지 말고 그 화 상황에 맞게.

[대본]
{script}"""


def fun_system() -> str:
    return FUN_SYSTEM


def fun_user(script: str) -> str:
    return FUN_USER_TMPL.format(script=script)


FEEDBACK_HEAD = ("너는 숏폼 로맨스 드라마 대본 피드백 전문가다. 칭찬 나열 말고 고칠 것 위주로 "
                 "짧고 구체적으로. 슬랙 mrkdwn(굵게 *별표1개*, 불릿 '- '). 아래 지정된 항목만 평가하고 잡설 금지.")

FEEDBACK_FUN = """*시청자 재미* — 스크롤 넘기려는 숏폼 시청자 입장에서 냉정하게. 아래 5개 항목을 각각 `✅/⚠️/❌ + 한 줄`로 평가한다.

- *훅(첫 3초)*: 첫 대사·장면이 손을 멈추게 하나? 밍밍하게 시작하면 ❌.
- *전개 속도*: 늘어지거나 설명이 긴 구간이 있나? 있으면 **어느 부분인지** 짚어라.
- *감정 터짐*: 사이다·설렘·분노 등 '와' 하는 순간이 있나? 없으면 ❌.
- *대사 맛*: 대사가 살아있나, 밋밋·설명조는 아닌가? 약한 대사 하나 예로.
- *엔딩 절단점*: 마지막이 다음 화를 못 참게 만드나?

끝에 **재미 점수 X/10** + **가장 큰 문제 하나**와 고칠 방향 한 줄.
말투는 현업 PD처럼 직관적으로. **지표·수치·패턴 문서 이름(ER·story_type 등)은 절대 인용하지 마라** — '요즘 이런 오프닝이 잘 먹힌다' 식으로 쉽게 풀어라."""

FEEDBACK_LOGIC = """*개연성 오류* — 지적만(수정안 금지). **아주 간결하게, 한 오류당 딱 두 줄.**
- 인물 설정·말투, 앞 화 흐름, 회차분배 핵심사건, 금지사항과 어긋나는 지점만. 캐릭터 붕괴·시점 오류·급전개를 특히 본다.
- 각 오류는 이 형식 그대로, 각 줄 한 문장 이내:
  `- *문제*: 무엇이 어긋나는지`
  `  *근거*: 바이블 어디와 충돌하는지`
- 설명·수식어·수정안 금지. 어긋나는 게 없으면 "*개연성 문제 없음*" 한 줄만."""


def feedback_system(bible: dict | None = None, target_episode: int | None = None,
                    mode: str = "both") -> str:
    """[피드백]용: 재미/개연성 중 요청 항목만. 재미↔패턴, 개연성↔작품 바이블 근거."""
    want_fun = mode in ("both", "fun")
    want_logic = mode in ("both", "logic")
    sections = ([FEEDBACK_FUN] if want_fun else []) + ([FEEDBACK_LOGIC] if want_logic else [])
    parts = [FEEDBACK_HEAD, "# 평가 항목\n\n" + "\n\n".join(sections)]
    if want_logic and bible:
        parts.append(build_bible_block(bible, target_episode, failsafe=False))
    if want_fun:
        try:
            pat = reference.load_patterns()
            if pat:
                parts.append("# 잘 먹히는 패턴 (재미 판단 근거)\n\n" + pat)
        except Exception:
            pass
    return "\n\n".join(parts)


def _outlines_block(outlines: dict, before: int | None = None) -> str:
    """쌓인 회차 개요를 흐름 참고용 블록으로. before가 있으면 그 화 이전만, 없으면 전부."""
    if not outlines:
        return ""
    def _n(k):
        m = re.search(r"\d+", k)
        return int(m.group()) if m else 0
    keys = sorted(outlines, key=_n)
    if before is not None:
        keys = [k for k in keys if _n(k) < before]
    keys = [k for k in keys if (outlines.get(k) or "").strip()]
    if not keys:
        return ""
    body = "\n\n".join(f"[{k}]\n{outlines[k][:600]}" for k in keys)
    return ("## 지난 화 개요 = 여기까지 이미 벌어진 이야기 (읽고 현재 시점을 파악하라)\n"
            "**아래 사건들은 이미 끝났다. 이번 화에서 되풀이하거나 되돌아가지 말고, 마지막 지점 *다음*부터 진행하라.**\n"
            "(예: 이미 한 정략결혼·이혼 결심을 다시 하려는 식으로 쓰면 안 됨)\n\n" + body)


def idea_system(bible: dict | None = None, query: str = "",
                target_episode: int | None = None) -> str:
    """[아이디어 제시]용 시스템 프롬프트: 코치 지침 + 작품 바이블(대상 회차 앵커) + DB 유사 사례."""
    parts = [IDEA_ROLE]
    if bible:
        # 아이디어는 개요가 아직 없는 회차라도 정상 → 생성용 준수 규칙(FAILSAFE) 제외
        parts.append(build_bible_block(bible, target_episode, failsafe=False))
    try:
        ex = "\n\n".join(retrieval.format_example(e)
                         for e in retrieval.select_examples(query, reference.load_db()))
        if ex:
            parts.append("# 참고 레퍼런스 사례 (방향만, 표절 금지)\n\n" + ex)
    except Exception:
        pass
    return "\n\n".join(parts)


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
        ob = _outlines_block(bible.get("outlines", {}))   # 쌓인 개요 전부 참고
        if ob:
            s += "\n\n" + ob
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
    """te화가 속한 구간(막) + 막 안에서의 위치(초/중/후반)를 강조 텍스트로.
    핵심사건은 막 전체에 걸친 목록이므로, 위치를 알려 급발진(사건 앞당김)을 막는다."""
    for gu, subs in episode_plan.items():
        rng = _episode_range(subs.get("화수", ""))
        if rng and rng[0] <= te <= rng[1]:
            title = f"{gu} {subs.get('구간', '')}".strip()
            evt = subs.get("핵심사건", "")
            span = rng[1] - rng[0]
            pos = (te - rng[0]) / span if span else 0.0
            phase = "초반부" if pos < 0.34 else ("중반부" if pos < 0.67 else "후반부")
            hwasu = subs.get("화수", "")
            return (
                f"## ⭐ {te}화가 속한 구간: {title} (화수 {hwasu}) — 이 막의 **{phase}**\n"
                f"아래 핵심사건은 이 막 전체({hwasu})에 걸친 목록이다. **목록에 있다고 이 화에서 당겨쓰지 마라.** "
                f"{te}화는 {phase}이므로, 그 시점에 아직 오지 않은 사건은 건드리지 않는다. "
                f"특히 **발각·정면충돌·대형 반전·관계 급전환**은 막 후반부 몫이다 — "
                f"{phase}에는 상황 심화·긴장 누적·암시까지만 하고 사건을 터뜨리지 마라.\n{evt}")
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


def build_bible_block(bible: dict, target_episode: int | None = None,
                      failsafe: bool = True) -> str:
    """바이블(대/중/소 조립본) + (선택)실패방지 지시 → 프롬프트 텍스트 (캐시 뒤 가변부).
    시점(target) = 명시 회차 or 진행 상태 화.
    failsafe=False: 생성용 준수 규칙(개요 준수·바이블 확인 경고 등) 제외 → 아이디어 브레인스토밍용."""
    te = target_episode or bible.get("current_episode")
    target = f"{te}화" if te else "현재"
    parts = [FAILSAFE.replace("{target}", str(target))] if failsafe else []

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

    outlines = bible.get("outlines", {})

    def _num(k):
        m = re.search(r"\d+", k)
        return int(m.group()) if m else 0

    # 지난 화 개요 — 대상 화 이전 전부(쌓인 개요 모두 참고). 대상 없으면 전체.
    prior = _outlines_block(outlines, before=target_episode)
    if prior:
        parts.append(prior)

    # 대상 화 개요 (대본 생성 시 준수 대상)
    if target_episode:
        key = f"{target_episode}화"
        if outlines.get(key):
            parts.append(f"## [{key} 개요] — 대본은 이 개요를 반드시 따를 것\n{outlines[key]}")

    # 톤 학습: 대상 화 이전의 기존 대본 최근 1~2개
    scripts = bible.get("scripts", {})
    if scripts:
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
