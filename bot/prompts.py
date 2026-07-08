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
3. **엔딩 훅 (1개)**: 다음이 궁금해지는 여지를 남기며 끊는다 (장면·행동으로만 묘사, 실제 대사·유형 라벨 금지).
   - **충격·반전일 필요 없다.** 감정 여운·작은 의문·미세한 관계 변화도 좋은 훅이다. 강한 훅 만들려고 아직 안 온 대형 사건(발각·정면충돌·반전)을 앞당기지 마라.
   - **억지 반전을 넣으려다 막장으로 흐르지 마라.** 엔딩 훅은 그 화 내용에서 자연스럽게 우러난 **'의미심장한' 여운·궁금증**이어야 한다 — 반전의 크기가 아니라 *의미의 무게*로 끊는다.
   - 그 화 사건의 자연스러운 연장이어야 하고, 막 위치(초/중/후반)와 위 강도 기준을 지켜라.
4. **시청자 채팅 시작 예상**: 시청자가 바로 반응·참여할 포인트 1문장
   ⚠️ **최우선 기준 = 이 화가 속한 [회차분배]의 핵심사건이다. 그 범위를 절대 벗어나거나 앞질러 가지 마라.**
   레퍼런스 패턴(훅·절단점)과 줄거리는 참고용이며, 핵심사건과 충돌하면 **핵심사건을 따른다.**

**대본(장면 구성안)** — 요청에 "대본"이 있을 때. **실제 대사(따옴표 대사 라인)는 쓰지 않는다.**
- 그 화의 장면·사건·행동·감정 흐름을 **줄글(지문)로 순서대로** 묘사한다 (누가 어디서 무엇을 하고 무엇을 느끼는가).
- 첫 장면은 훅, 마지막은 절단점 상황으로.
- 대사가 필요한 자리는 `여기서 ~라는 취지로 말한다` 식 **서술로만** 표시하고, 실제 대사 라인은 쓰지 마라 (대사는 작가가 채운다).

## 원칙
- 아래 '패턴 요약'이 실측 데이터 기반 SSOT다. 훅·절단점·트로프 선택의 근거로 삼고, 근거 없는 유행 추측을 하지 마라.
- 유사 사례는 참고용이다. 표절 금지 — 구조를 빌리고 상황·대사는 새로 만든다.
- 사내 템플릿이 주어지면 그 양식을 우선한다.
- 작가가 스레드에서 수정을 요청하면 전체 재생성이 아니라 해당 부분만 고쳐서 전체본을 다시 낸다.
- 슬랙 mrkdwn으로 출력한다 (굵게는 *별표 1개*, 헤더 대신 굵은 줄).
- **분석 라벨 금지**: 절단점·훅 유형 표기(예: "— 킬러 라인 정점 컷", "축출 선언 정점 컷")를 결과물 본문에 붙이지 마라. 그 효과는 장면과 대사로만 드러내고, 유형 이름은 쓰지 않는다.
- **타임라인 이해 (가장 중요)**: 지난 화 개요에 나온 사건은 *이미 벌어져 끝난 일*이다. 이번 화에서 그 사건을 다시 일어나는 것처럼 쓰지 마라 — 예: 이미 한 정략결혼·이혼서 작성을 이번 화에서 또 하려는 식은 오류. 이야기는 **마지막으로 다룬 지점 바로 다음부터** 진행한다. 지금 몇 화까지 진행됐는지 파악하고 그 뒤를 써라.
- **대사 생성 금지**: 따옴표로 된 실제 대사 라인을 만들지 마라 — 뉘앙스·맥락을 못 살린다. 장면·행동·사건·감정을 줄글로만 묘사하고, 대사가 필요한 자리는 "~라는 취지로 말한다" 식 서술로만. 실제 대사는 사람 작가가 쓴다.
- **강도 조절**: 엔딩·절단점을 "무조건 충격적"으로 만들려 하지 마라. 불치병·기억상실·죽음·과한 폭력·급작 폭로 같은 막장 카드를 남발하지 말고, 그 화 상황과 페이스에 맞는 절단점을 쓴다. 세게 = 좋은 게 아니다.
- **엔딩 훅 = 의미심장하게 (억지 반전 금지)**: 매 화 엔딩을 반전·폭로로 뒤집으려 하면 막장으로 흐른다. 훅의 힘은 '반전의 충격'이 아니라 **그 화 내용·감정에서 우러난 의미의 무게(의미심장함)**에서 나온다. 내용에 맞는, 곱씹게 되는 한 컷으로 끊어라.
- **숏드라마 문법**: 웹소설식 장황한 서술·내면 묘사로 풀지 마라. **장면·행동·대치 중심, 빠른 전환**으로 보여준다. 설명하지 말고 보여줘라.
- **억지 사건 금지 (여운 존중)**: 매 순간 사건을 우겨넣지 마라. 특히 **감정의 여운이 핵심인 자리**(예: 나레이션으로 곱씹는 엔딩)에 곧바로 다음 사건(인물 등장·충돌)을 붙이면 여운이 반감된다. 여백·호흡도 연출이다 — "여기서 사건을 하나 더" 대신, 그 감정이 충분히 남도록 비워두는 게 나은 자리를 판단하라.
- **한국 정서·사회적 통념**: 한국을 배경으로 한 작품이면 한국 생활 관습과 상식에 맞게 써라. 예) 집에서는 신발을 벗는다(신발 신고 실내 진입 X), 웃어른 호칭·존댓말, 식탁·좌식 문화 등. 어색한 외국식 설정으로 몰입을 깨지 마라.
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
- **실제 대사(따옴표 대사 라인)를 쓰지 마라.** 상황·행동·표정으로만 묘사한다 (대사는 작가가 채움).
- **실무자가 한 번에 이해되게 쉽게.** 한 문장만 읽어도 무슨 장면인지 눈에 그려지게 쓰고, 추상적 표현·전문용어·분석 라벨·현란한 비유는 금지. 담백하고 명확하게.
- 상황 2~3개. 각각 **상황 한 줄 + 이유 한 줄**로:
  `- (구체적 상황) — 어떠세요?`
  `  → (왜 효과적인지 한 줄: 감정·훅·시청자 반응 관점)`
- 이유는 딱 한 줄, 짧게. 긴 분석·서론 금지.
- 작품 바이블(인물 설정·관계·금지사항·지금 회차 흐름)에 **반드시 맞춰라.** 금지사항 위반·시점에 안 맞는 전개는 내지 마라.
- 아래 레퍼런스 사례는 방향 참고용. 표절 금지 — 우리 작품 상황으로 바꾼다.
- **타임라인 이해**: 지난 화 개요는 *이미 벌어진 일*이다. 이미 끝난 사건(정략결혼·이혼 결심 등)을 이번 화에 또 하려는 것처럼 제안하지 마라. 물어본 화·막의 *현재 시점*에 맞는 상황만 낸다. 상황을 과하게(막장·충격) 몰지 마라.
- **이 화의 개요가 바이블에 이미 있으면 그 흐름을 반드시 읽고 그 안에서 아이디어를 내라.** 개요에 이미 있는 사건과 어긋나거나 중복되게 내지 말고, 그 개요를 더 살리는 방향으로 제안한다. (개요를 "못 읽었다"거나 무시하지 마라.)
- 아직 개요가 없으면 그게 정상이다(그래서 아이디어를 내는 것). "개요가 없다/바이블 확인 필요" 같은 경고·부연 없이 그냥 새 상황만 제안한다.
- 슬랙 mrkdwn, 불릿 '- '."""


SYNC_SYSTEM = """너는 노션에 적힌 작품 설정 문서를 우리 시스템 스키마(JSON)로 옮기는 파서다.
아래 [문서]에서 **실제로 적혀 있는 내용만** 뽑아 JSON으로 출력하라. 없는 건 지어내지 말고, 워딩은 원문 그대로 보존하라.

출력 JSON 키 (문서에 있는 것만 포함, 없으면 생략):
- "진행상태": "예: 24화 작업 중"
- "로그라인": "한 줄 로그라인"
- "키워드": "키워드/해시태그"
- "타겟층": "타겟 시청자"
- "핵심정서": "핵심 정서"
- "금지사항": "하지 말아야 할 것"
- "강도": "톤/수위 지침 (예: 2단계 잔잔) 있으면"
- "줄거리": "전체 줄거리 (길어도 통째로)"
- "등장인물": [{"이름":"강태혁","성별":"남","나이":"32","포지션":"남주","설정":"...","핵심대사":"...","설명":"..."}]
- "회차분배": [{"막":"1막","구간":"...","화수":"1~12화","핵심사건":"..."}]
- "개요": [{"화":"1화","내용":"그 화 개요 전문"}]
- "대본": [{"화":"1화","내용":"그 화 대본 전문"}]

규칙:
- JSON 객체만 출력. 설명·주석·마크다운 펜스 금지.
- 문서의 소제목(줄거리/등장인물/회차분배/개요 등)을 단서로 분류. 애매하면 넣지 마라(빈 값보다 생략).
- 인물 성별은 '남'/'여'로, 화수는 'N~M화' 형식 있으면 그대로. 없는 필드는 빼라.

[문서]
"""


FUN_SYSTEM = """너는 숏폼 드라마 헤비 시청자다. 재미없으면 3초 안에 스크롤을 넘긴다.
작가의 노력에 공감하지 않는다. 오직 "내가 계속 보게 되는가"로만 판단한다.
칭찬을 위한 칭찬 금지. 모든 판정에는 대본 속 구체 근거(대사·장면)를 인용하라.
점수는 아래 앵커 정의를 엄격히 따르라. 근거 없이 7~8점대에 몰지 마라. 지표·패턴 문서 용어는 인용하지 마라.

# 숏폼 장르 문법 — 감점·지적하지 마라 (정상임)
- **화면 전환·컷이 잦은 것** (오버랩·회전전환·인서트 등) — 숏폼의 기본 문법이다. 지적·감점 금지.
- **나레이션(내레이션)이 많은 것** — 이 장르의 정상적 화법. 지적·감점 금지.
- 장면 이해(④)는 '컷 수'가 아니라 '상황이 파악되는가'로만 본다.

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

FUN_USER_TMPL = """‼️ **평가 대상은 오직 아래 [대본]이다.** 시스템에 있는 [작품 바이블]의 줄거리·개요·설정은 배경 참고일 뿐 — 그 내용을 평가하거나 대본으로 착각하지 마라. **대본에 실제로 적힌 장면·행동·전개만** 근거로 삼아라. (대본과 바이블이 달라도 지적할 필요 없다. 여긴 재미 평가지 개연성 검사가 아니다.)

아래 [대본]을 시청자 입장에서 평가하라. **아주 간결하게.** 각 줄 한 문장 이내, 장황한 설명·수식어 금지. 종합점수 표나 판정 문구는 쓰지 마라(시스템이 계산함).

*이탈 지점*: "끝까지 봄" 또는 "○○ 장면에서 넘김 (이유 짧게)".

*항목* — 각 한 줄로 `[n/10] 근거 짧게`:
① 훅
② 전개
③ 감정
④ 장면 (상황이 파악되나? 컷이 잦은 것·나레이션 많은 건 감점 사유 아님)
⑤ 엔딩

*최우선 수정*: 가장 약한 항목 1개 + 바로 적용할 수정안 한 줄.
  - **실제 대사는 쓰지 마라** — 상황·행동·사건으로만 묘사(대사는 작가가 씀). "대사를 짧게/줄여라"류 수정도 하지 마라. 무조건 충격·폭로·막장으로 몰지 마라.
  - **아래 [작품 바이블]의 설정·현재 시점·인물 위치에 맞는 수정만.** 그 화에 없는 장소·인물·미래 사건을 지어내지 마라 (예: 신혼집 장면인데 사무실 컷 제안 X).

[대본]
{script}"""


def fun_system(bible: dict | None = None, target_episode: int | None = None) -> str:
    """재미 평가 지침 + (수정 제안 맥락용) 작품 바이블. 점수는 시청자 관점, 수정은 설정에 맞게."""
    s = FUN_SYSTEM
    if bible:
        s += "\n\n" + build_bible_block(bible, target_episode, failsafe=False, kind="재미")
    return s


def fun_user(script: str, lens_level: int | None = None) -> str:
    head = ""
    if lens_level:
        head = (f"## 평가 관점: 강도 {lens_level}단계 — {_INTENSITY_LEVELS.get(lens_level, '')}\n"
                f"이 대본이 **강도 {lens_level} 수위를 목표로 한다고 가정**하고, 그 톤 기준으로 재밌는지 판단하라. "
                f"목표보다 밋밋하면 '더 세게' 방향으로, 과하면 '덜어내라' 방향으로 지적.\n\n")
    return head + FUN_USER_TMPL.format(script=script)


CONVERT_ROLE = """너는 숏폼 드라마 각색가다. 작가가 **대충 한 줄로 적은 상황**을 받아, 그대로 촬영할 수 있는 **생생한 드라마 대본식 지문**으로 살을 붙여 구체화한다.

예)
- 입력: "괴롭힘을 당하고 있는 태혁. 그리고 그 모습을 발견한 학생들."
- 출력:
  운동장 구석, 험악하게 생긴 일진 무리에게 조롱당하며 발길질당하는 어린 태혁.
  양팔로 몸을 감싸며 어떻게든 막아보지만 여러 명을 당해내기엔 역부족이다.
  멀리서 괴롭힘당하는 태혁을 발견하고 걸음을 멈추는 학생들.

핵심 = **연출 디테일은 살리되, 새 이야기는 만들지 않는다. 그리고 대사는 손대지 않는다.**
- ✅ 살려도 되는 것: 장소·구도, 동작의 구체적 모습·강약, 표정·시선, 주변 인물의 자연스러운 반응, 소품 — 밋밋한 한 줄을 '찍을 수 있는 장면'으로 만든다. 한 상황을 필요하면 2~3문장으로 나눠 흐름을 보여줘도 좋다.
- ❌ 만들면 안 되는 것: 원문에 없는 **사건·이야기 전개·새 인물·반전·감정선**. 원문의 그 순간을 *연출*할 뿐, 이야기를 진전시키지 마라. (예: '발견한 학생들'을 넣으라 했으면 발견까지만 — 학생들이 뭘 하는지까지 지어내지 마라.)

**대사 보존 (아주 중요):**
- 입력에 **이미 대사가 있으면**(화자명 + 대사, 나레이션 `(Na)` 포함) 그 줄은 **한 글자도 바꾸지 말고 그대로 둬라.** 대사를 다듬거나 새로 만들지 마라.
- 씬 헤더(예: `신혼집 _ 거실 / 낮`), 화자 표기, `플래시백` 같은 **구조 표시도 그대로 유지**한다.
- 네가 손대는 것은 **오직 지문(행동·상황·묘사 줄)뿐**이다 — 대충 쓴 지문을 생생한 지문으로 구체화하고, 대사·구조는 그대로 통과시켜라.
- (대사가 아예 없는 순수 줄글이면 위 예시처럼 전부 지문으로 구체화하면 된다.)

**촬영·편집 지시 (완고답게):**
- 소설식 서술 말고 **찍을 수 있는 촬영 지문**으로 써라. 필요하면 촬영·편집 지시어를 자연스럽게 넣어라:
  `OO 표정 클로즈업`, `OO로 이동하는(따라가는) 화면`, `인서트`, `화면을 가득 채운다`, `장면 전환`, `페이드아웃`, `/다시 현재` 등.
- 카메라가 무엇을 어떻게 잡는지(클로즈업·풀샷·시선 팔로우·인서트)와 컷 전환을 의식해서 쓴다.
- 단, `킬러라인 정점 컷` 같은 **훅/유형 분석 라벨은 금지** — 그건 촬영 지시가 아니다. 실제로 카메라·편집이 하는 동작만 써라.

- 웹소설식 장황한 내면·심리 서술은 금지. 짧고 리듬감 있는 **지문체**(행동·상황 중심).
- 슬랙 mrkdwn. 원문 순서·구조 그대로, 총평·설명·머리말 없이 변환 결과만 출력."""

CONVERT_USER_TMPL = """아래 [줄글 상황]을 드라마 대본식 지문으로 바꿔라. 원문에 없는 내용은 추가하지 마라.

[줄글 상황]
{draft}"""


def convert_system(bible: dict | None = None, target_episode: int | None = None) -> str:
    """[변환]용: 줄글→대본식 지문. 인물 이름·호칭·말투만 참고(새 사건 추가 금지)."""
    s = CONVERT_ROLE
    if bible and bible.get("characters"):
        s += ("\n\n# 등장인물 참고 (이름·호칭·포지션·말투만 맞춰라 — 새 사건·설정 추가 금지)\n"
              + _character_cards(bible["characters"]))
    return s


def convert_user(draft: str) -> str:
    return CONVERT_USER_TMPL.format(draft=draft)


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
- **바이블과 명백히 충돌하는 것만** 짚어라. 억지로 만들어내지 마라 — 해석의 여지가 있거나, 연출·취향 선택이거나, "그럴 수도 있는" 수준이면 지적하지 마라.
- 봐야 할 건: 인물 설정·말투 위반, 시점 오류(아직 모를 정보를 앎), 앞 화와 모순, 회차분배 핵심사건·금지사항 위반, 명백한 캐릭터 붕괴·급전개.
- 대본에 안 나온 걸 상상해서 트집 잡지 마라. 바이블에 실제로 적힌 것과 대조되는 것만.
- 각 오류는 이 형식, 각 줄 한 문장 이내:
  `- *문제*: 무엇이 어긋나는지`
  `  *근거*: 바이블 어디와 충돌하는지`
- 어긋나는 게 없으면(대부분의 경우) **"개연성 문제 없음"** 한 줄만. 억지로 채우지 마라."""


_LOGIC_STRICT = {
    1: "엄격도 1(아주 관대): 이야기가 완전히 무너지는 **치명적·명백한 오류만**. 웬만하면 '개연성 문제 없음'.",
    2: "엄격도 2(관대): 확실히 어긋나는 큰 것만. 사소한 건 넘어가라.",
    3: "엄격도 3(보통): 명백히 어긋나는 것 위주.",
    4: "엄격도 4(엄격): 잠재적 모순까지 꼼꼼히.",
    5: "엄격도 5(아주 엄격): 사소한 것·해석 여지 있는 것까지 다 짚어라.",
}


def feedback_system(bible: dict | None = None, target_episode: int | None = None,
                    mode: str = "both", strictness: int | None = None) -> str:
    """[피드백]용: 재미/개연성 중 요청 항목만. strictness(1~5)=개연성 엄격도."""
    want_fun = mode in ("both", "fun")
    want_logic = mode in ("both", "logic")
    logic_txt = FEEDBACK_LOGIC + (f"\n- {_LOGIC_STRICT[strictness]}" if strictness in _LOGIC_STRICT else "")
    sections = ([FEEDBACK_FUN] if want_fun else []) + ([logic_txt] if want_logic else [])
    parts = [FEEDBACK_HEAD, "# 평가 항목\n\n" + "\n\n".join(sections)]
    if want_logic and bible:
        parts.append(build_bible_block(bible, target_episode, failsafe=False, kind="개연성"))
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
        parts.append(build_bible_block(bible, target_episode, failsafe=False, kind="아이디어"))
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


def _now_marker(bible: dict, te: int | None) -> str:
    """'지금 시점' 못박기 — 줄거리·인물 설명은 전체 스토리(결말 포함)라서, 현재 위치를 명시해
    미래 사건 앞당김/과거 되풀이를 막는다."""
    if not te:
        return ""
    mak = ""
    for gu, subs in (bible.get("episode_plan") or {}).items():
        rng = _episode_range(subs.get("화수", ""))
        if rng and rng[0] <= te <= rng[1]:
            mak = f" · {gu} {subs.get('구간', '')}".strip()
            break
    return (
        f"## 🕒 지금 시점 = {te}화{mak}  (이 지점 기준으로만 써라)\n"
        f"아래 [줄거리]와 [등장인물]은 이 작품의 **전체 스토리(먼 과거 배경 ~ 결말)**를 담은 것이다. "
        f"지금은 그중 **{te}화 지점**일 뿐이다.\n"
        f"- {te}화 *이후*에 올 사건(줄거리 뒷부분·인물의 미래 행동·결말)은 **아직 일어나지 않았다. 절대 앞당기지 마라.**\n"
        f"- 지난 화에서 이미 벌어진 사건은 되풀이하거나 되돌아가지 마라.\n"
        f"- {te}화는 '지난 화 개요'의 마지막 다음, 이 구간(막)의 흐름 안에서 자연스럽게 이어진다.")


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
                f"아래 핵심사건은 이 막 전체({hwasu})에 걸친 목록이다. **이 목록에서 아직 순서상 오지 않은 뒷부분 사건을 "
                f"{te}화에서 미리 터뜨리지 마라** — 이야기 순서를 지켜라. (이건 '타임라인' 규칙이다. "
                f"강도가 세더라도 아직 안 온 사건을 당기는 것과는 별개 — 지금 시점의 사건을 세게 그리는 건 강도 지시를 따른다.)\n{evt}")
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


INTENSITY_NOTE = """## 🎚️ 강도 기준 (급상승·막장 방지 — 반드시 지킬 것)
- **지난 화 개요의 톤에서 한 단계 이내로만** 올려라. 한 화 만에 판을 뒤집지 마라 — 대부분의 화는 상황 심화·감정 누적으로 충분하다.
- **엔딩은 충격·반전일 필요 없다.** 감정 여운·작은 의문·관계의 미세한 변화로 끊어도 훌륭한 훅이다. "센 것 = 좋은 것"이 아니다.
- 아래 [회차분배 핵심사건]에 **명시되지 않은** 큰 사건을 새로 지어내지 마라. 특히 이런 막장 카드는 핵심사건에 없으면 금지: 죽음·불치병·기억상실·납치·신체 폭력·출생의 비밀 폭로·급작 임신/반전·경찰 연행 등.
- 금지사항에 강도·톤 지침이 있으면 그걸 최우선으로 따른다."""

# 작가가 지정한 강도 다이얼(1~5) → 수위 지침
_INTENSITY_LEVELS = {
    1: "아주 잔잔. 일상·감정선 위주, 큰 사건 없이 미묘한 긴장만. 훅도 조용하게.",
    2: "잔잔. 화당 작은 사건 1개, 감정 누적 중심. 큰 반전·충격 지양.",
    3: "보통. 화당 사건 1~2개, 적당한 갈등과 훅. 단 웹소설식 장황한 서술·내면 묘사 말고 "
       "**숏드라마 문법**(장면·행동·대치 중심, 빠른 전환)으로 풀어라.",
    4: "세게. 갈등·대립을 뚜렷하게, 강한 훅 허용. 단 개연성·회차분배는 유지.",
    5: "매우 세게(막장 허용). 충격·반전·큰 사건 적극 사용. **엔딩 훅은 '되다 만' 느낌 없이 확실히 매듭지어라** "
       "— 다음 화를 못 참게 만드는 강한 한 방으로 끊는다. (회차분배·개연성 안에서)",
}


def _intensity_note(bible: dict, kind: str | None = None) -> str:
    """작가가 지정한 강도(타입별 우선 → 일반값)에 맞춘 지침, 없으면 기본(억제) 지침.
    kind: 개요/대본/아이디어/재미/개연성 — 그 기능의 강도를 먼저 찾는다."""
    imap = bible.get("intensity_map") or {}
    lvl = (imap.get(kind) if kind else None) or bible.get("intensity_level")
    raw = (bible.get("intensity_raw") or "").strip()
    if not lvl and not raw:
        return INTENSITY_NOTE
    typed = kind and imap.get(kind)
    label = (f"{kind} " if typed else "") + (f"{lvl}단계" if lvl else raw)
    desc = _INTENSITY_LEVELS.get(lvl, raw)
    head = f"## 🎚️ 강도: 작가 지정 = {label} — 이 수위가 **강도의 최우선 기준**이다.\n- {desc}\n"
    if lvl and lvl >= 4:
        tail = ("- 이 수위에 맞춰 **확실히 세게** 가라. (막 위치·'지난 화 대비 한 단계 이내' 같은 기본 억제보다 "
                "이 강도 지시가 우선이다. 단 회차분배 사건 *순서*·개연성·금지사항은 지킨다.)")
    elif lvl and lvl <= 2:
        tail = "- 큰 사건·충격은 지양하고 조용한 감정 흐름 위주. 금지사항 우선."
    else:
        tail = "- 적당한 갈등과 훅. 회차분배 순서·개연성 유지, 금지사항 우선."
    return head + tail


def build_bible_block(bible: dict, target_episode: int | None = None,
                      failsafe: bool = True, kind: str | None = None) -> str:
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

    now = _now_marker(bible, te)   # '지금 시점' 못박기 (미래 앞당김·과거 되풀이 방지)
    if now:
        parts.append(now)

    parts.append(_intensity_note(bible, kind))   # 강도 (기능별 지정 → 일반값 → 기본 억제)

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
        parts.append("## 등장인물 (설정·관계·포지션·말투는 지켜라. 단, 설명 속 *미래 여정*은 아직 안 온 것 — 이 화에서 미리 실행하지 마라)\n"
                     + _character_cards(bible["characters"]))
    if bible.get("plot"):
        parts.append("## 줄거리 (작품 전체 스토리 — 배경·현재·미래·결말 다 포함. '지금 시점'을 넘어서는 뒷부분은 앞당기지 마라)\n"
                     + bible["plot"])
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

    # 대상 화 개요 (대본 생성 시 준수 대상 / 아이디어는 이 흐름 안에서)
    if target_episode:
        key = f"{target_episode}화"
        if outlines.get(key):
            if kind == "아이디어":
                lbl = f"## [{key} 개요] — 이미 잡힌 이 화의 흐름. 아이디어는 이 개요를 읽고 그 안에서, 겹치지 않게 내라"
            elif kind in ("재미", "개연성"):
                lbl = (f"## [{key} 개요] (참고용) — 이 화의 계획된 흐름. "
                       f"**평가 대상은 아래 [대본]이지 이 개요가 아니다. 이 개요 내용을 대본으로 착각하지 마라.**")
            else:
                lbl = f"## [{key} 개요] — 대본은 이 개요를 반드시 따를 것"
            parts.append(f"{lbl}\n{outlines[key]}")

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
                  target_episode: int | None = None, kind: str | None = None) -> list[dict]:
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
        blocks.append({"type": "text", "text": build_bible_block(bible, target_episode, kind=kind)})
    blocks.append({"type": "text", "text": "# 이번 요청 유사 사례\n\n" + examples})
    return blocks
