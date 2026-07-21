# -*- coding: utf-8 -*-
"""작품 등록소 — 작품명·별칭 → 노션 페이지 매핑. data/notion_pages.json에 저장(재시작 무관).

실무자는 노션 페이지만 만들고 `[동기화] <작품> <링크>` 한 번 → 여기 등록됨.
이후 자동 폴러가 등록된 작품의 노션을 읽어 시트(빠른 캐시)에 반영. 시트는 실무자가 안 건드림.

저장 형식: { "<정식작품명>": {"page": "<32자리 page_id>", "aliases": ["별칭1", ...]} }
env NOTION_PAGES({이름:page_id})도 기본값으로 병합(파일이 우선).

(2026-07-16, 봇 합체 HANDOFF §3-2) co-writer-bot/storyboard-bot 두 버전을 병합.
storyboard 버전에는 `COWRITER_WORKS_PATH` env로 co-writer repo의 notion_pages.json을 가리키고,
`_cowriter_env_pages()`로 그 옆의 .env까지 따로 읽어 NOTION_PAGES를 병합하는 로직이 있었다 —
두 봇이 서로 다른 repo/프로세스로 떨어져 있을 때 "저쪽 봇이 등록한 작품"을 이쪽에서도 알아보기
위한 다리였음. 이제 두 봇이 이 repo 하나, `.env` 파일 하나를 공유하므로 그 다리 자체가
필요 없어졌다: `COWRITER_WORKS_PATH`는 이미 삭제됐고(.env 참고), `_cowriter_env_pages()`가
읽으려던 NOTION_PAGES는 `config.NOTION_PAGES`가 같은 .env에서 이미 읽어와 들고 있는 값과
동일하다 — 그대로 남겨뒀다면 같은 값을 파일에서 다시 파싱하는 죽은 코드가 됐을 것이므로
드롭했다. `_looks_like_bad_work()`/`_TRAILING_SUFFIXES` 가드는 두 버전 모두에 필요해 유지.
"""
from __future__ import annotations

import json
import re
import unicodedata

from .. import config

_PATH = config.BASE_DIR / "data" / "notion_pages.json"


def _load() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def all_works() -> dict:
    """{정식작품명: {page, aliases}} — 파일 + env NOTION_PAGES 병합(파일 우선)."""
    d = _load()
    known = set(d) | {a for v in d.values() for a in (v.get("aliases") or [])}
    for name, page in (config.NOTION_PAGES or {}).items():
        if name not in known:
            d[name] = {"page": page, "aliases": []}
            known.add(name)
    return d


def resolve(name: str) -> str | None:
    """이름/별칭 → 정식 작품명. 없으면 None."""
    name = (name or "").strip()
    d = all_works()
    if name in d:
        return name
    for w, v in d.items():
        if name in (v.get("aliases") or []):
            return w
    return None


def work_by_page(page_id: str) -> str | None:
    """page_id로 이미 등록된 정식 작품명 찾기. 없으면 None. (제목 바뀌어도 id로 식별)"""
    page_id = (page_id or "").replace("-", "")
    for w, v in all_works().items():
        if (v.get("page") or "").replace("-", "") == page_id:
            return w
    return None


_TRAILING_SUFFIXES = ("기획안", "기획서")


def sanitize(name: str) -> str:
    """구글 시트 탭명으로 못 쓰는 문자 제거 → 노션 제목을 안전한 작품명으로.
    (2026-07-15) 노션 페이지 제목을 그대로 정식명으로 쓰다 보니 중복 공백이나
    '…기획안/기획서' 같은 꼬리표가 그대로 등록돼 부르기 지저분했던 문제 —
    공백을 하나로 줄이고, 끝단어가 그 꼬리표면 떼어낸다."""
    s = re.sub(r"[\[\]:*?/\\]", " ", name or "").strip()
    s = re.sub(r"\s+", " ", s)
    for suf in _TRAILING_SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            stripped = s[: -len(suf)].strip()
            if stripped:
                s = stripped
            break
    return s


def page_of(name: str) -> str | None:
    w = resolve(name)
    return all_works().get(w, {}).get("page") if w else None


# ★2026-07-21 작업1 — 라우터 컨텍스트/프롬프트용 별칭 테이블. _build_context가
# works.all_works_with_aliases()를 호출하는데 이 함수가 없어(all_names()도 없음) 항상
# 빈 dict로 폴백 → 라우터가 등록 작품/별칭을 아예 못 봐서 별칭 미해석 사고(저연프)가 났다.
def all_works_with_aliases() -> dict[str, list[str]]:
    """{정식작품명: [별칭...]} — 라우터 프롬프트 컨텍스트(registered_works)용."""
    return {canon: list(v.get("aliases") or []) for canon, v in all_works().items()}


def all_names() -> list[str]:
    """정식작품명 목록(구 호출부 호환)."""
    return list(all_works().keys())


_PARTICLE_SUFFIX_RE = re.compile(
    r"(으로|에서|부터|까지|이랑|이나|은|는|이|가|을|를|의|에|도|만|로|랑|나|와|과)$")


def _norm_match(s: str) -> str:
    """비교용 정규화 — NFC + 공백·괄호·꺾쇠·구두점 제거 + 소문자."""
    s = unicodedata.normalize("NFC", s or "")
    s = re.sub(r"[\s「」『』<>《》〈〉\[\]（）()·,.!?~…]", "", s)
    return s.strip().lower()


def _name_alias_pairs() -> list[tuple[str, str]]:
    """[(정식명, 매칭토큰)] — 정식명 + 각 별칭. 매칭토큰 긴 것부터(최장일치 우선)."""
    out: list[tuple[str, str]] = []
    for canon, v in all_works().items():
        out.append((canon, canon))
        for a in (v.get("aliases") or []):
            if a:
                out.append((canon, a))
    out.sort(key=lambda t: len(_norm_match(t[1])), reverse=True)
    return out


def resolve_work(token_or_text: str | None, channel: str, thread_ts: str) -> str | None:
    """어떤 표기(별칭/풀제목/괄호포함/부분)든 정식 작품명으로 통일하는 단일 진입점.
    우선순위: ①<꺾쇠>/[대괄호] 토큰 최장일치 → ②본문 최장일치(조사 제거·정규화) →
    ③conti_state tracked_work → ④스레드 이력 스캔(부모→사용자→봇 동기화/등록 확인) → ⑤None.
    성공 시 conti_state에 tracked_work 캐시(episode/human_final은 안 건드림) → 이후 결정적."""
    from .. import conti_state

    text = (token_or_text or "").strip()
    pairs = _name_alias_pairs()

    def _cache(canon: str) -> str:
        try:
            conti_state.set_tracked_work(thread_ts, canon)
        except Exception:
            pass
        return canon

    # ① <꺾쇠>/[대괄호] 토큰
    for m in re.finditer(r"[<\[]\s*([^<>\[\]]+?)\s*[>\]]", text):
        tok = _norm_match(m.group(1))
        if len(tok) < 1:
            continue
        for canon, alias in pairs:
            na = _norm_match(alias)
            if na and (na == tok or (len(na) >= 2 and na in tok)):
                return _cache(canon)

    # ② 본문 최장일치 (조사·괄호 무시). 토큰이 본문에 substring으로 들어오면 인정.
    ntext = _norm_match(text)
    if ntext:
        for canon, alias in pairs:   # 이미 긴 것부터 정렬
            na = _norm_match(alias)
            if len(na) >= 2 and na in ntext:
                return _cache(canon)

    # ③ 스레드에 이미 추적 중인 작품
    try:
        tracked = (conti_state.get_episode(thread_ts) or {}).get("work")
        tw = resolve(tracked) if tracked else None
        if tw:
            return tw
    except Exception:
        pass

    # ④ 스레드 이력 스캔 — 부모/사용자 메시지, 봇의 동기화·등록 확인 메시지에서 작품명 추출.
    #    봇 플레이스홀더("<작품명>")·진행("…중이에요") 메시지는 신뢰하지 않는다.
    try:
        from .slack_io import _thread_messages
        msgs = _thread_messages(channel, thread_ts) or []
        for m in msgs:
            c = m.get("content") or ""
            nc = _norm_match(c)
            if not nc or "작품명" in c:   # placeholder 제외
                continue
            for canon, alias in pairs:
                na = _norm_match(alias)
                if len(na) >= 2 and na in nc:
                    return _cache(canon)
    except Exception:
        pass

    return None


_BAD_WORK_TOKEN_RE = re.compile(r"^[@#!]|^[UBWC][A-Z0-9]{6,}(\||$)")   # <@U..>/<#C..|이름>/<!here> 등
_BAD_WORK_LITERALS = {"작품", "undefined", "none", "null"}


def _looks_like_bad_work(work: str) -> bool:
    """멘션 토큰(<@U..> 등)이나 '작품' 플레이스홀더를 작품명으로 등록하지 않게(2026-07-13,
    sheet_bible.py의 동명 함수와 같은 방어 — 실제로 가짜 탭/매핑이 생겼던 문제)."""
    w = (work or "").strip()
    if not w or w in _BAD_WORK_LITERALS:
        return True
    return bool(_BAD_WORK_TOKEN_RE.match(w))


def register(work: str, page_id: str, aliases: list | None = None) -> None:
    """작품 등록/갱신 (페이지 매핑 + 선택 별칭).

    ★2026-07-20e: 같은 page_id가 이미 다른 정식명으로 등록돼 있는데 work가 그 정식명도
    별칭도 아니면(예: "저연프"로 불렀는데 실제로는 "저는 연프 출연진이 아닌데요 !"라는
    정식명으로 이미 등록된 페이지), 예전엔 무조건 d[work]에 새로 써서 같은 페이지를
    가리키는 정식 작품이 두 개로 쪼개졌다 — refs/fixed-images 폴더까지 작품명마다
    따로 생겨 등록한 참조가 전부 분산되는 실측 사고("김신우 등 7명 등록했는데 폴더가
    둘로 쪼개짐"). page_id가 이미 다른 이름으로 등록돼 있으면 그 기존 정식명 엔트리에
    work를 별칭으로만 추가하고, 새 최상위 엔트리는 만들지 않는다."""
    if _looks_like_bad_work(work):
        return
    d = _load()
    existing = work_by_page(page_id)
    if existing and existing != work:
        entry = d.get(existing) or {"page": page_id, "aliases": []}
        entry["page"] = page_id
        entry["aliases"] = sorted(set((entry.get("aliases") or []) + [work] + list(aliases or [])))
        d[existing] = entry
        _save(d)
        return
    entry = d.get(work) or {"page": "", "aliases": []}
    entry["page"] = page_id
    if aliases:
        entry["aliases"] = sorted(set((entry.get("aliases") or []) + list(aliases)))
    d[work] = entry
    _save(d)


def add_aliases(work: str, aliases: list) -> str | None:
    """기존 작품에 별칭 추가. 반환: 정식 작품명(없으면 None)."""
    w = resolve(work)
    if not w:
        return None
    d = _load()
    entry = d.get(w) or {"page": page_of(w) or "", "aliases": []}
    entry["aliases"] = sorted(set((entry.get("aliases") or []) + list(aliases)))
    d[w] = entry
    _save(d)
    return w


# ★2026-07-20: 작품 장르(실사화/2D 애니메이션) — 스틸컷/영상뿐 아니라 co-writer의 노션
# 동기화 등록 흐름(dispatch_cowriter._do_sync)에서도 참조해야 해서(순환 임포트 방지 —
# dispatch_cowriter.py/dispatch_storyboard.py는 서로 안 부르고 둘 다 이 shared 모듈만 부름)
# vocabulary·라벨을 여기 shared/works.py에 둔다. dispatch_storyboard.py는 이 값을 그대로
# 재노출(alias)해서 기존 호출부를 안 건드린다.
STYLE_LABELS = {"realistic": "실사풍(리얼리스틱 시네마틱)", "2d_anim": "2D 애니메이션"}

_STYLE_KEYWORDS = {
    # 2d_anim을 먼저 검사해야 한다 — "리얼" 계열과 겹치는 단어가 없어 순서 자체는 안전하지만,
    # 향후 프리셋이 늘어 겹치는 표현이 생기면 이 순서(구체적인 것 먼저)가 중요해진다.
    "2d_anim": ("2d 애니메이션", "2d애니메이션", "2d 애니", "2d애니", "애니메이션", "애니메",
               "애니풍", "카툰", "cartoon", "anime", "2d anim"),
    "realistic": ("실사", "세미리얼", "반실사", "사진풍", "리얼", "realistic", "reality"),
}


def parse_style_key(text: str) -> str | None:
    """자유 텍스트(슬랙 명령이든 노션 페이지 본문이든)에서 장르 키워드를 찾는다."""
    t = (text or "").lower()
    for key, keywords in _STYLE_KEYWORDS.items():
        if any(kw in t for kw in keywords):
            return key
    return None


# ★2026-07-21 버그 리포트("어제 등록한 캐릭터 그림체가 애니풍으로 바뀜") — parse_style_key가
# _do_sync(신규 작품 노션 등록)에서 노션 페이지 "본문 전체"(대본/바이블 원문)를 대상으로
# 호출되고 있었다. _STYLE_KEYWORDS의 "애니메"/"애니풍"/"카툰"/"리얼" 같은 키워드는 사용자가
# 명시적으로 타이핑하는 `[스타일] <작품> ...` 짧은 명령문에서는 안전하지만, 수십~수백 줄짜리
# 자유 서술 대본/바이블 안에서는 흔히 등장하는 일반 단어의 부분 문자열로 우연히 매치될 수 있다
# (예: 지문에 "애니메이션풍 회상 장면"이나 "리얼한 감정 연기" 같은 묘사가 한 줄만 있어도 작품
# 전체의 화풍이 그 키워드로 조용히 확정돼버림 — 사용자는 그런 명령을 내린 적이 없다). 신규
# 등록 시 본문에서 장르를 "추정"하는 용도로는, 명시적으로 "장르:"/"스타일:"/"화풍:" 같은 라벨이
# 붙은 줄에서만 키워드를 찾도록 좁혀 오탐을 없앤다. 라벨 줄이 없으면 None을 반환해 호출부가
# mark_genre_required로 폴백(사용자에게 명시적으로 물어봄)하게 한다.
_STYLE_LABEL_LINE_RE = re.compile(r"(장르|스타일|화풍|genre|style)\s*[:：]")


def parse_style_key_labeled(text: str) -> str | None:
    """자유 서술 문서(노션 페이지 본문 등)에서는 "장르:"/"스타일:"/"화풍:" 라벨이 붙은 줄에서만
    장르 키워드를 찾는다 — 본문 중 우연히 등장하는 키워드 부분 문자열로 오탐하지 않기 위함.
    사용자가 직접 치는 `[스타일] <작품> <스타일명>` 같은 짧은 명령문에는 parse_style_key를
    그대로 쓴다(그 경로는 문맥이 이미 "스타일 지정"으로 명확하므로 라벨이 불필요)."""
    for line in (text or "").splitlines():
        if _STYLE_LABEL_LINE_RE.search(line):
            key = parse_style_key(line)
            if key:
                return key
    return None


def get_style(work: str) -> str | None:
    """★2026-07-20: 작품마다 스틸컷/영상 그림체(예: realistic/2d_anim)를 다르게 쓰고 싶다는
    요청 — 등록된 style_key를 반환(없으면 None → 호출자가 기본 스타일을 쓴다)."""
    w = resolve(work) or work
    d = _load()
    return (d.get(w) or {}).get("style")


def set_style(work: str, style_key: str) -> str | None:
    """작품에 그림체를 등록/변경. 반환: 정식 작품명(작품을 못 찾으면 None). 이 작품이
    genre_required로 표시돼 있었으면(신규 등록 때 장르를 못 찾아 필수로 걸어둔 상태)
    실제로 지정됐으니 그 표시를 해제한다."""
    w = resolve(work)
    if not w:
        return None
    d = _load()
    entry = d.get(w) or {"page": page_of(w) or "", "aliases": []}
    entry["style"] = style_key
    entry.pop("genre_required", None)
    d[w] = entry
    _save(d)
    return w


def mark_genre_required(work: str) -> None:
    """★2026-07-20 "노션에도 필수로 추가" — 노션 링크로 신규 등록되는 작품인데 페이지
    본문에서 장르를 못 찾았을 때만 dispatch_cowriter._do_sync가 호출한다. 이미 등록된
    작품(이 기능이 생기기 전부터 있던 작품 포함)은 이 함수가 호출되지 않으므로 기존 작품의
    스틸컷/영상 생성은 전혀 영향받지 않는다 — "새로 등록되는데 장르를 못 찾은 경우"에만
    좁혀 강제하기 위한 장치."""
    w = resolve(work) or work
    d = _load()
    entry = d.get(w) or {"page": page_of(w) or "", "aliases": []}
    entry["genre_required"] = True
    d[w] = entry
    _save(d)


def genre_required(work: str) -> bool:
    """스틸컷/영상/자동주행 진입부가 생성 전에 확인 — True면 아직 장르 미지정으로 막아야
    한다(set_style이 호출되는 순간 자동으로 해제됨)."""
    w = resolve(work) or work
    d = _load()
    return bool((d.get(w) or {}).get("genre_required"))
