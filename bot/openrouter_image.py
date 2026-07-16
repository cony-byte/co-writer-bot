# -*- coding: utf-8 -*-
"""OpenRouter Unified Image API 클라이언트 (표준 라이브러리만).

상세 콘티의 각 샷 프롬프트 → GPT 이미지(openai/gpt-image-2)로 9:16 스틸 생성.
  POST https://openrouter.ai/api/v1/images
  body : {model, prompt, aspect_ratio, n, input_references[]}
  resp : {"data":[{"b64_json": "...", "media_type":"image/png"}], "usage":{"cost":..}}
"""
from __future__ import annotations

import base64
import mimetypes
import json
import re
import threading
import unicodedata
import urllib.error
import urllib.request
import uuid

from . import config

# register_element가 load→modify→save를 락 없이 하면, 짧은 시간에 여러 등록 요청이 겹칠 때
# (예: 이미지 여러 장을 연달아 확정) 뒤에 저장한 쪽이 앞선 쪽을 덮어써서 등록이 조용히
# 유실되는 문제가 있었다(2026-07-14, 실사용 중 발견 — "연우(과거)" 등록이 사라짐).
_ELEMENTS_LOCK = threading.Lock()

_URL = "https://openrouter.ai/api/v1/images"
_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
_REF_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def chat(system: str, user: str, *, model: str | None = None, timeout: int = 240) -> str:
    """OpenRouter chat completions (HTTP) — 샷 분해 등 LLM 호출용.
    agent(claude CLI) 대신 써서 느림·동시호출 충돌을 피한다."""
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY 미설정")
    payload = {
        "model": model or getattr(config, "OPENROUTER_LLM_MODEL", "anthropic/claude-sonnet-4.5"),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    req = urllib.request.Request(
        _CHAT_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OpenRouter chat 오류 {e.code}: {e.read().decode('utf-8','replace')[:200]}") from e
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""


def vision_check(png: bytes, ref_urls: list[str], question: str, *,
                 model: str | None = None, timeout: int = 60) -> str:
    """★2026-07-15(자동주행 이미지/영상 일관성 후검사): chat()과 같은 OpenRouter chat/completions
    엔드포인트를 그대로 쓰되, 생성 이미지 1장 + 참조 이미지들을 멀티모달 content 배열(OpenAI/
    OpenRouter 공통 포맷)로 함께 넣는다 — 이 저장소에서 vision 호출은 이게 처음이라 새 클라
    이언트를 만들지 않고 기존 chat() 인프라(엔드포인트·인증)만 재사용한다. OPENROUTER_LLM_MODEL
    기본값(anthropic/claude-sonnet-4.5)은 vision을 지원하므로 별도 모델 설정 없이 그대로 쓴다."""
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY 미설정")
    content = [{"type": "text", "text": question},
              {"type": "image_url", "image_url": {"url": png_data_url(png)}}]
    for u in ref_urls:
        content.append({"type": "image_url", "image_url": {"url": u}})
    payload = {
        "model": model or getattr(config, "OPENROUTER_LLM_MODEL", "anthropic/claude-sonnet-4.5"),
        "messages": [{"role": "user", "content": content}],
    }
    req = urllib.request.Request(
        _CHAT_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OpenRouter vision 오류 {e.code}: {e.read().decode('utf-8','replace')[:200]}") from e
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""


def _nfc(s: str) -> str:
    """macOS 파일명은 한글을 NFD(자모분해)로 저장 → NFC(완성형)로 통일해 비교."""
    return unicodedata.normalize("NFC", s or "")


def available() -> bool:
    return bool(config.OPENROUTER_API_KEY)


def _canon_work(work: str | None) -> str | None:
    """별칭(예: '코니') → 정식 작품명('cony 테스트 작품')으로 정규화.
    elements.json/data/refs 트리는 work 문자열 그대로 폴더명으로 쓰는데, [참조] 명령만
    별칭→정식명 변환을 했고(2026-07-14, b91db20) 이 저수준 공용 함수들은 안 해서, 별칭으로
    부르면 정식명과 다른 폴더가 생겨 등록 정보·참조 이미지가 쪼개졌다("코니" 실측) — 여기서
    한 번에 정규화해 _ref_dirs/_elements_path/_element_data_url을 쓰는 모든 경로에 적용."""
    if not work:
        return work
    try:
        from .shared import works
        return works.resolve(work) or work
    except Exception:
        return work


def canon_work(work: str | None) -> str | None:
    """work 별칭 → 정식명. (공개 헬퍼 — app.py에서 경로를 직접 만들기 전에 호출)"""
    return _canon_work(work)


# ── 캐릭터 참조 이미지 (일관성) ────────────────────────────────
#   구조: <refs>/<작품>/<정본이름>.(png|jpg|jpeg|webp)
#   대본이 이름을 섞어 써도(강태혁/태혁) 하나의 파일로 매칭:
#     ① 정확일치 → ② aliases.json 별칭 → ③ 양방향 부분일치(한쪽이 다른 쪽을 포함, 2자↑)
#   aliases.json (선택, 작품 폴더에): {"태혁": ["강태혁","태혁오빠"], ...}  (키=파일명 정본)
def _ref_dirs(work: str | None) -> list:
    work = _canon_work(work)
    dirs = []
    if work:
        dirs.append(config.OPENROUTER_REFS_DIR / work)
    dirs.append(config.OPENROUTER_REFS_DIR)
    return dirs


def _aliases(work: str | None) -> dict:
    for d in _ref_dirs(work):
        p = d / "aliases.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
    return {}


# ── visual-pipeline fixed-images 공유(단일 소스) ────────────────
#   projects/<작품>/fixed-images/<인물>/<파일>. 작품명이 달라도(코니↔cony 테스트 작품)
#   폴더명/ work_name / 노션 page_id 로 브리지해 프로젝트 폴더를 찾는다.
def _vp_project_dir(work: str | None):
    root = getattr(config, "FIXED_IMAGES_ROOT", None)
    if not work or not root or not root.exists():
        return None
    wn = _nfc(work)
    projs = []
    for pj in root.glob("*/project.json"):
        try:
            meta = json.loads(pj.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        proj = meta.get("project") or {}
        names = {_nfc(pj.parent.name), _nfc(proj.get("work_name") or "")}
        page = (proj.get("notion_page_id") or "").replace("-", "")
        projs.append((pj.parent, names, page))
    for d, names, _ in projs:                       # ① 폴더명/work_name 직접 매칭
        if wn in names:
            return d
    try:                                            # ② 노션 page_id 브리지
        from .shared import works
        pid = (works.page_of(work) or "").replace("-", "")
    except Exception:
        pid = ""
    if pid:
        for d, _, page in projs:
            if page and page == pid:
                return d
    return None


def vp_project_dir(work: str | None):
    """이 작품의 visual-pipeline 프로젝트 루트(Path) — 없으면 None. (공개 헬퍼)"""
    return _vp_project_dir(work)


def vp_fixed_dir(work: str | None):
    """이 작품의 visual-pipeline fixed-images 폴더(Path) — 없으면 None. (쓰기용 공개 헬퍼)"""
    d = _vp_project_dir(work)
    return (d / "fixed-images") if d else None


def _first_image(pdir) -> "Path | None":
    if not pdir.is_dir():
        return None
    imgs = sorted((p for p in pdir.iterdir() if p.suffix.lower() in _REF_EXTS),
                 key=lambda p: p.stat().st_mtime)
    return imgs[0] if imgs else None


def _vp_person_images(work: str | None) -> dict:
    """{인물이름(NFC): 대표 이미지 Path} — visual-pipeline fixed-images/의 대표 이미지.
    ★생성시각(mtime) 기준 최초 파일을 대표로 고정 — 나중에 이미지가 추가돼도 대표가
    안 바뀜(project.json 규칙 "fixed 자동 덮어쓰기 금지"에 대응).

    ★2026-07-13: 폴더명을 표시 이름(예: "선우")으로 쓰면, elements.json에서 그 이름을
    바꾸는 순간(rename) fixed-images 폴더와의 연결이 끊긴다 — 그래서 등록된 엘리먼트는
    이제 폴더명을 **id**로 우선 찾는다(rename에 안전). id 폴더가 없는 옛 폴더(이름 그대로,
    아직 마이그레이션 안 된 것들)는 레거시 폴백으로 계속 지원."""
    fx = vp_fixed_dir(work)
    out = {}
    if not fx or not fx.exists():
        return out
    id_dirs_used = set()
    for e in load_elements(work):
        eid, display = e.get("id"), _nfc(e.get("display", ""))
        if not eid or not display or eid.startswith("file:"):
            continue
        img = _first_image(fx / eid)
        if img:
            out[display] = img
            id_dirs_used.add(eid)
    for pdir in fx.iterdir():                              # 레거시: 이름 그대로인 폴더
        if not pdir.is_dir() or pdir.name in id_dirs_used:
            continue
        name = _nfc(pdir.name)
        if name in out:
            continue
        img = _first_image(pdir)
        if img:
            out[name] = img
    return out


def registered_refs(work: str | None) -> list[str]:
    """그 작품에 등록된 참조 인물(정본 NFC) 목록 — data/refs + visual-pipeline fixed-images 통합."""
    out = set()
    for d in _ref_dirs(work):
        if d.exists():
            out |= {_nfc(p.stem) for p in d.iterdir() if p.suffix.lower() in _REF_EXTS}
    out |= set(_vp_person_images(work).keys())      # ← visual-pipeline 단일 소스 통합
    return sorted(out)


def resolve_ref_name(work: str | None, mention: str) -> str | None:
    """대본/콘티에 나온 이름(mention) → 등록된 정본 파일명(NFC). 못 찾으면 None."""
    mention = _nfc(mention)
    if not mention:
        return None
    stems = registered_refs(work)          # NFC
    aliases = _aliases(work)
    # ① 정확일치
    if mention in stems:
        return mention
    # ② 별칭 (NFC 정규화 비교)
    for canon, alts in aliases.items():
        names = {_nfc(canon)} | {_nfc(a) for a in (alts or [])}
        if mention in names and _nfc(canon) in stems:
            return _nfc(canon)
    # ③ 양방향 부분일치 (2자 이상, 가장 긴 정본 우선) — resolve_element와 동일하게, 한쪽에만
    # "(과거)" 같은 시간대 표기가 있으면 서로 다른 인물이니 부분일치 대상에서 제외(2026-07-14).
    mention_has_ts = _has_time_suffix(mention)
    for stem in sorted(stems, key=len, reverse=True):
        if _has_time_suffix(stem) != mention_has_ts:
            continue
        if len(stem) >= 2 and (stem in mention or mention in stem):
            return stem
    return None


def _data_url(p) -> str:
    mt = mimetypes.guess_type(str(p))[0] or "image/png"
    return f"data:{mt};base64," + base64.b64encode(p.read_bytes()).decode("ascii")


def _load_by_stem(work: str | None, stem: str) -> str | None:
    for d in _ref_dirs(work):                        # ① 로컬 data/refs
        for ext in _REF_EXTS:
            p = d / f"{stem}{ext}"
            if p.exists():
                return _data_url(p)
    vp = _vp_person_images(work).get(_nfc(stem))     # ② visual-pipeline fixed-images(단일 소스)
    if vp and vp.exists():
        return _data_url(vp)
    return None


def ref_data_url(work: str | None, name: str) -> str | None:
    """이름(별칭/부분이름 허용) → 참조 이미지 base64 data URL. 없으면 None."""
    stem = resolve_ref_name(work, name)
    return _load_by_stem(work, stem) if stem else None


def character_refs(work: str | None, names: list[str]) -> list[str]:
    """등장 인물 이름들 → 참조 data URL 리스트(하위호환: element_refs로 위임)."""
    return element_refs(work, names)


# ── 엘리먼트 레지스트리 (elements.json) ─────────────────────────
#   OpenRouter 방식: 엔진 엘리먼트가 아니라 "어떤 참조 이미지를 어느 컷에 붙일지"의 색인.
#   data/refs/<작품>/elements.json = {"elements":[{id,type,tag_name,display,file,aliases,status}]}
#   컷 프롬프트/캡션에 등장하는 등록 엘리먼트(인물·장소·소품·의상)의 참조 이미지를 input_references로 자동 첨부.
# ★2026-07-15: "의상도 레지스트리가 필요함"(사용자) — 소품/장소와 같은 방식으로 의상도 정식
# 타입으로 등록해 참조 이미지를 붙일 수 있게 함. shot_refs()가 이미 타입 무관하게 caption/prompt
# 텍스트에 등록 이름이 등장하는지로 매칭하므로(아래 shot_refs 참고) 이 타입만 추가하면 된다.
ELEMENT_TYPES = ("person", "place", "prop", "costume")


def _elements_path(work: str):
    return config.OPENROUTER_REFS_DIR / _canon_work(work) / "elements.json"


def load_elements(work: str | None) -> list[dict]:
    if not work:
        return []
    p = _elements_path(work)
    if not p.exists():
        return []
    try:
        return (json.loads(p.read_text(encoding="utf-8")) or {}).get("elements") or []
    except Exception:
        return []


def _save_elements(work: str, elems: list[dict]) -> None:
    p = _elements_path(work)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"elements": elems}, ensure_ascii=False, indent=1), encoding="utf-8")


def register_element(work: str, display: str, etype: str = "person",
                     filename: str | None = None, aliases: list[str] | None = None,
                     tag_name: str | None = None, clear_file: bool = False) -> dict:
    """엘리먼트 등록/갱신(display 기준 upsert). 반환: 엘리먼트 dict.
    load→modify→save를 락으로 감싸 동시 등록 요청이 서로 덮어쓰지 않게 한다(2026-07-14).

    clear_file: True면 기존 "file" 필드를 지운다 — visual-pipeline fixed-images 경로로 등록할 땐
    filename을 안 넘기는데, _element_data_url이 "file" 필드가 있으면(레거시 등록 당시 값이
    남아있으면) fixed-images보다 그 옛 로컬 파일을 항상 우선해버려서, fixed-images를 아무리
    새로 덮어써도 실제 생성엔 반영이 안 되는 버그가 있었다(2026-07-14, "선우" 실측)."""
    display = _nfc(display).strip()
    etype = etype if etype in ELEMENT_TYPES else "person"
    with _ELEMENTS_LOCK:
        elems = load_elements(work)
        cur = next((e for e in elems if _nfc(e.get("display", "")) == display), None)
        if cur is None:
            cur = {"id": uuid.uuid4().hex, "display": display, "aliases": [], "status": "confirmed"}
            elems.append(cur)
        cur["type"] = etype
        if filename:
            cur["file"] = filename
        elif clear_file:
            cur.pop("file", None)
        if tag_name:
            cur["tag_name"] = tag_name
        if aliases:
            cur["aliases"] = sorted(set((cur.get("aliases") or []) + [_nfc(a) for a in aliases]))
        _save_elements(work, elems)
        return cur


_GENDER_CARD_RE = re.compile(r"^(?:#{1,3}\s*)?([^\n(]{1,12}?)\s*\(([^)\n]*)\)\s*/", re.M)


def _fmt_gender_token(v: str) -> str | None:
    t = (v or "").strip()
    if t in ("남", "남자", "남성", "m", "M", "male", "Male", "♂"):
        return "male"
    if t in ("여", "여자", "여성", "f", "F", "female", "Female", "♀"):
        return "female"
    return None


def _notion_character_gender(work: str | None, name: str) -> str | None:
    """노션 캐릭터 카드 첫 줄 "이름 (성별, 나이) / ..."에서 성별만 뽑는다. app.py의
    _notion_character_visual_desc와 같은 카드 포맷을 쓰되, 순환 임포트를 피하려고 여기서
    독립적으로 조회한다. 카드/노션 설정이 없으면 None."""
    if not (work and config.NOTION_TOKEN):
        return None
    try:
        from .shared import works
        pid = works.page_of(work)
        if not pid:
            return None
        from .shared import notion_sync
        full = notion_sync.page_text(pid)
    except Exception:
        return None
    # ★2026-07-15: "민대표 있는데 못찾음" — app.py._notion_character_visual_desc와 동일한
    # 공백 유무 불일치 버그. 공백을 지우고 비교.
    name_n = re.sub(r"\s+", "", _nfc(name))
    for m in _GENDER_CARD_RE.finditer(full):
        if re.sub(r"\s+", "", _nfc(m.group(1).strip())) != name_n:
            continue
        parts = m.group(2).split(",")
        if parts:
            g = _fmt_gender_token(parts[0])
            if g:
                return g
    return None


def _infer_gender(work: str, e: dict) -> str | None:
    """이 엘리먼트의 성별("male"/"female") — 캐시된 값 우선, 없으면 노션 카드에서 추론.
    둘 다 없으면 None(호출자는 성별 무관 폴백으로 진행)."""
    if e.get("gender") in ("male", "female"):
        return e["gender"]
    return _notion_character_gender(work, e.get("display", ""))


def voice_for(work: str, name: str) -> str:
    """이 캐릭터(엘리먼트)에 고정 배정된 TTS 보이스 이름. 처음 불리면 성별에 맞는 보이스 풀
    (openrouter_tts.MALE_VOICES/FEMALE_VOICES)에서, 그 성별로 이미 등록된 캐릭터 수를 기준으로
    하나를 배정하고 영구 저장해서, 이후로는 항상 같은 목소리가 나오게 한다(2026-07-14, "인물마다
    목소리가 바뀌면 안 되잖아" 요청 — 얼굴 레퍼런스를 엘리먼트로 고정하는 것과 같은 패턴).

    ★2026-07-14: 처음엔 성별 상관없이 전체 30개 보이스를 등록 순서로 순환 배정했더니 남자
    캐릭터에 여성 보이스가 배정되는 등 "목소리가 성별이랑 매칭이 안 됨" 피드백 — 노션 캐릭터
    카드의 "(성별, 나이)"를 읽어 성별에 맞는 풀에서만 뽑도록 수정. 성별을 못 찾으면(카드
    없음 등) 예전처럼 전체 풀에서 배정(완전히 틀리는 것보단 절반 확률이라도 맞는 게 낫다)."""
    from . import openrouter_tts as tts
    e = resolve_element(work, name)
    if not e or not e.get("id") or e["id"].startswith("file:"):
        return tts.CHARACTER_VOICES[0]   # 레지스트리에 없는 옛 파일 기반 폴백 — 고정 배정 불가, 기본값
    if e.get("voice"):
        return e["voice"]
    gender = _infer_gender(work, e)
    pool = {"male": tts.MALE_VOICES, "female": tts.FEMALE_VOICES}.get(gender, tts.CHARACTER_VOICES)
    elems = load_elements(work)
    # 같은 풀(성별)로 이미 목소리가 배정된 캐릭터 수만큼 인덱스를 밀어서, 같은 성별끼리는
    # 서로 다른 목소리를 받게 한다. 아직 gender가 캐시 안 된(=이 함수를 아직 안 거친) 다른
    # 캐릭터는 계산에 넣지 않는다(그 캐릭터도 자기 차례에 voice_for가 불리면서 캐시된다 —
    # 매번 전체 캐릭터의 성별을 다시 추론하면 노션 API 호출이 캐릭터 수만큼 반복돼 느려짐).
    same_pool_voices = {x.get("voice") for x in elems
                       if x.get("type") == "person" and x.get("id") != e["id"]
                       and x.get("gender") == gender and x.get("voice")}
    remaining = [v for v in pool if v not in same_pool_voices]
    voice = remaining[0] if remaining else pool[len(same_pool_voices) % len(pool)]
    with _ELEMENTS_LOCK:
        elems = load_elements(work)
        for x in elems:
            if x.get("id") == e["id"]:
                x["voice"] = voice
                if gender:
                    x["gender"] = gender
                break
        _save_elements(work, elems)
    return voice


def set_voice(work: str, name: str, voice: str) -> str:
    """이 캐릭터의 TTS 보이스를 수동으로 재배정(마음에 안 들면 바꾸기, 2026-07-14). voice는
    openrouter_tts.VOICES 중 하나여야 함 — 아니면 ValueError. 반환: 실제로 저장된 보이스명."""
    from . import openrouter_tts as tts
    if voice not in tts.VOICES:
        raise ValueError(f"'{voice}'는 지원하는 보이스가 아니에요 — 가능한 값: {', '.join(tts.VOICES)}")
    e = resolve_element(work, name)
    if not e or not e.get("id") or e["id"].startswith("file:"):
        raise ValueError(f"'{name}'이 등록된 엘리먼트가 아니라 보이스를 고정할 수 없어요.")
    with _ELEMENTS_LOCK:
        elems = load_elements(work)
        for x in elems:
            if x.get("id") == e["id"]:
                x["voice"] = voice
                break
        _save_elements(work, elems)
    return voice


def _element_names(e: dict) -> set:
    names = {_nfc(e.get("display", "")), _nfc(e.get("tag_name", ""))}
    names |= {_nfc(a) for a in (e.get("aliases") or [])}
    return {n for n in names if n}


_TIME_SUFFIX_RE = re.compile(r"\([^)]*\)\s*$")


def _has_time_suffix(s: str) -> bool:
    """이름 끝에 "(과거)"/"(학창시절)"류 시간대 표기가 붙어있는지 — 이게 있고 없고는 서로 다른
    인물(현재/과거)이라는 뜻이라, 부분일치로 뭉개면 안 된다(아래 참고)."""
    return bool(_TIME_SUFFIX_RE.search(s))


def _place_category(name: str) -> str | None:
    """'숙소-화장실 앞' → '숙소'. '-' 없으면 None(계층 없는 평범한 이름)."""
    i = name.find("-")
    return name[:i].strip() if i > 0 else None


def resolve_element(work: str | None, mention: str) -> dict | None:
    """이름/별칭/tag → 엘리먼트 dict. 레지스트리 우선, 없으면 파일 기반 폴백(인물).

    ★2026-07-14: "태혁"(현재)이 부분일치(②)에서 "태혁(과거)" 엘리먼트에 걸려 플래시백용
    참조를 현재 시점 컷에도 잘못 쓰던 버그 — "태혁"이 "태혁(과거)"의 부분 문자열이라 길이
    조건만으로는 구분이 안 됐다. 한쪽에만 "(과거)" 같은 시간대 표기가 있으면 서로 다른
    인물(별개 레퍼런스)이므로 부분일치 대상에서 제외해, "태혁(과거)"라고 정확히 안 부르면
    현재 버전(등록 안 돼있으면 폴백 ③)으로만 가게 한다.

    ★2026-07-15: 장소를 '대분류-소분류'(예: '숙소-화장실 앞', '숙소-선우 방 안')로 등록하기
    시작하면서, 부분일치(②)가 그냥 "숙소"라는 대분류만 언급된 문장에도 걸려버리는 문제가
    생겼다 — 같은 대분류 아래 하위 장소가 여러 개면(예: 화장실 앞/선우 방 안 둘 다 "숙소" 포함)
    어느 쪽을 가리키는지 알 수 없는데, sorted 순서상 하나를 임의로 골라버리게 된다. 그래서
    mention이 정확히 어떤 하위 장소의 대분류 이름과 '완전히 같을' 때는(=소분류가 특정 안 됨)
    그 대분류를 가진 하위 장소가 2개 이상이면 부분일치를 건너뛴다 — 차라리 참조를 못 붙이는
    게 엉뚱한 하위 장소를 잘못 붙이는 것보다 안전하다."""
    mention = _nfc(mention).strip()
    if not mention:
        return None
    elems = load_elements(work)
    # ★2026-07-15 "민대표 이미 있었음"(실사용자 리포트) — 등록은 "민 대표"(공백 있음)로 돼있는데
    # 콘티/추출 LLM이 "민대표"(공백 없음)로 언급하면 exact-match(mention in _element_names(e))가
    # 정확히 그 공백 하나 때문에 실패해 "미등록"으로 잘못 뜬다. 인물/장소 이름에 실제로 의미
    # 있는 공백이 오는 경우가 드물어(대개 성+직함/성+이름 사이 관례적 공백), 공백 제거 버전으로도
    # 한 번 더 비교 — _notion_character_gender 등에서 이미 쓰던 것과 같은 방식(공백 스트립)을
    # 여기 핵심 매칭 함수에도 적용한다.
    mention_nospace = mention.replace(" ", "")
    for e in elems:                                   # ① 정확/별칭 일치(공백 무시 포함)
        names = _element_names(e)
        if mention in names or mention_nospace in {n.replace(" ", "") for n in names}:
            return e
    mention_has_ts = _has_time_suffix(mention)
    cat_counts: dict[str, int] = {}
    for e in elems:
        for nm in _element_names(e):
            cat = _place_category(nm)
            if cat:
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
    for e in sorted(elems, key=lambda x: len(_nfc(x.get("display", ""))), reverse=True):
        for nm in _element_names(e):                  # ② 부분일치(2자↑, 긴 이름 우선)
            if _has_time_suffix(nm) != mention_has_ts:
                continue                              # 현재/과거 등 시간대 다른 인물은 제외
            cat = _place_category(nm)
            if cat and mention == cat and cat_counts.get(cat, 0) > 1:
                continue                              # 대분류만 언급 + 하위 장소 여럿 → 애매하니 건너뜀
            if len(nm) >= 2 and (nm in mention or mention in nm):
                return e
    stem = resolve_ref_name(work, mention)            # ③ 폴백: 레지스트리에 없는 옛 파일(인물)
    if stem:
        return {"type": "person", "display": stem, "file": None, "id": "file:" + stem}
    return None


def _element_data_url(work: str | None, e: dict) -> str | None:
    f = e.get("file")
    if f and work:
        p = config.OPENROUTER_REFS_DIR / _canon_work(work) / f
        if p.exists():
            mt = mimetypes.guess_type(str(p))[0] or "image/png"
            return f"data:{mt};base64," + base64.b64encode(p.read_bytes()).decode("ascii")
    return _load_by_stem(work, _nfc(e.get("display", "")))   # file 미지정/부재 → stem으로


def png_data_url(png: bytes) -> str:
    """생성된 PNG bytes → data URL (직전 컷을 다음 컷의 참조로 체이닝할 때 씀, 2026-07-14)."""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def element_refs(work: str | None, mentions: list[str]) -> list[str]:
    """이름 목록 → 참조 data URL 리스트(엘리먼트 id 기준 중복 제거)."""
    out, seen = [], set()
    for m in mentions or []:
        e = resolve_element(work, m)
        if not e or e.get("id") in seen:
            continue
        seen.add(e.get("id"))
        u = _element_data_url(work, e)
        if u:
            out.append(u)
    return out


def _shot_mentions(work: str | None, shot: dict) -> list[str]:
    """한 컷에 붙일 참조 data URL: characters/places/props/elements 필드 + 프롬프트·캡션 텍스트에
    등장하는 등록 엘리먼트(장소·소품 포함) 전부. 장소·소품은 샷마다 있을 수도 없을 수도 있음(맥락
    판단은 LLM이 'places'/'props' 필드로 컷별로 표시 — storyboard_shots_system의 [등록된 장소/소품] 참고).
    소품은 콘티 표현이 매번 달라질 수 있어(대명사·별칭) LLM의 문맥 판단에 의존한다.

    ★2026-07-15: OTS/"위주(...걸침)" 컷은 3단계 LLM이 'focus_char'에 "위주"(선명한 메인 피사체)
    인물 이름을 넣어준다 — 이미지 생성 API가 참조 이미지 순서에 민감할 수 있어서(먼저 넣은
    참조가 더 선명/우세하게 나오는 경향), 'characters' 배열의 우연한 나열 순서와 무관하게
    focus_char로 지정된 인물의 참조를 mentions 맨 앞으로 옮긴다. focus_char이 없거나(null),
    mentions에 실제로 없는(3단계 LLM의 환각/오타) 이름이면 아무 것도 안 바꾸고 원래 순서 그대로
    둔다 — fail-safe, 절대 예외를 던지거나 깨진 리스트를 만들지 않는다."""
    mentions = (list(shot.get("characters") or []) + list(shot.get("places") or [])
                + list(shot.get("props") or []) + list(shot.get("elements") or []))
    tnorm = _nfc(f"{shot.get('prompt', '')} {shot.get('caption', '')}")
    for e in load_elements(work):
        for nm in _element_names(e):
            if len(nm) >= 2 and nm in tnorm:
                mentions.append(e.get("display") or nm)
                break
    focus_char = shot.get("focus_char")
    if focus_char:
        focus_n = _nfc(str(focus_char)).strip()
        match_idx = next(
            (i for i, m in enumerate(mentions) if _nfc(str(m)).strip() == focus_n), None
        )
        if match_idx is not None:
            focus_mention = mentions.pop(match_idx)
            mentions.insert(0, focus_mention)
            # ★2026-07-15(2차 강화): 순서만 앞으로 옮기는 걸로는 부족해서(다른 인물 참조가
            # 같이 들어가면 이미지 생성기가 순서와 무관하게 여러 얼굴에 주의를 분산시켜
            # focus_char이 아닌 인물이 여전히 선명하게 나오는 사고가 재현됨) — focus_char이
            # mentions 안에서 실제로 매칭됐을 때만(=3단계 LLM의 환각/오타가 아님이 확인된
            # 경우만) 그 인물이 아닌 다른 "person" 타입 참조는 아예 제외한다. 장소/소품
            # 참조(person이 아닌 타입)는 그대로 둔다. match_idx가 None이면(focus_char이
            # mentions에 없는 이름) 이 필터 블록 자체가 실행되지 않아 원래 동작(무필터)
            # 그대로 유지된다 — fail-safe.
            def _is_person(nm: str) -> bool:
                e = resolve_element(work, nm)
                if not e:
                    return False
                return e.get("type") == "person" or str(e.get("id", "")).startswith("file:")
            mentions = [
                m for m in mentions
                if _nfc(str(m)).strip() == focus_n or not _is_person(m)
            ]
    # ★2026-07-15: 얼굴 참조 사진에 찍힌 원래 옷차림이 별도 의상 참조보다 우선시되는 사고
    # 실측(사용자 리포트: "잠옷" 의상을 등록했는데 인물 참조 사진 속 정장 차림으로 계속 나옴)
    # — 참조 이미지 순서에 민감한 생성기 특성(위 focus_char 설명과 동일 근거)을 활용해,
    # costume 타입 참조를 person 타입보다 앞으로 옮겨 의상이 더 강하게 반영되게 한다.
    def _is_costume(nm: str) -> bool:
        e = resolve_element(work, nm)
        return bool(e) and e.get("type") == "costume"
    costume_first = sorted(range(len(mentions)), key=lambda i: 0 if _is_costume(mentions[i]) else 1)
    mentions = [mentions[i] for i in costume_first]
    return mentions


def shot_refs(work: str | None, shot: dict) -> list[str]:
    """_shot_mentions()의 이름들 → 참조 data URL 리스트(엘리먼트 id 기준 중복 제거)."""
    return element_refs(work, _shot_mentions(work, shot))


_ROLE_INSTRUCTIONS = {
    "person": "identity reference — use ONLY for this character's facial identity, facial "
              "proportions, hairstyle, hair color, skin tone, and approximate body proportions. "
              "Do NOT copy any clothing, accessories, pose, background, or lighting from this image "
              "— the outfit shown here must be completely ignored and replaced.",
    "costume": "outfit reference — use as the EXCLUSIVE source for all clothing on the character(s) "
               "wearing it: top, bottoms, materials, colors, fit, layers, shoes, and accessories. "
               "The character must wear the exact outfit shown here, not whatever clothing appears "
               "in any identity reference image.",
    "place": "location reference — use ONLY for the background/setting's architecture, layout, "
             "and color palette. Do not let it influence character identity or clothing.",
    "prop": "object reference — use ONLY for this prop's exact appearance (shape, color, material) "
            "where it appears in the scene.",
    "mood": "mood/lighting reference — use ONLY for this image's overall color grading, lighting "
            "tone, and atmosphere. Ignore any objects, shapes, architecture, or content shown in "
            "it — it exists purely to set the color/light mood, not to contribute visual elements.",
}


def shot_ref_entries(work: str | None, shot: dict) -> list[tuple[str, str]]:
    """(role, url) 순서쌍 — costume-first 순서 유지. role은 person/costume/place/prop."""
    out, seen = [], set()
    for m in _shot_mentions(work, shot):
        e = resolve_element(work, m)
        if not e or e.get("id") in seen:
            continue
        seen.add(e.get("id"))
        u = _element_data_url(work, e)
        if u:
            out.append((e.get("type") or "person", u))
    return out


def shot_costume_text_notes(work: str | None, shot: dict) -> list[tuple[str, str]]:
    """이미지 참조가 없는(description-only) 의상 멘션 — (표시이름, 설명) 리스트.
    ★2026-07-15: 참조 이미지가 없는 의상은 shot_refs/shot_ref_entries에서 아예 빠져버려서
    (_element_data_url이 falsy → element_refs가 건너뜀) 자동주행 vision 후검사가 그 의상의
    등장/일관성을 이미지 비교로 확인할 방법이 전혀 없었다(실측: "잠옷-A"로 등록된 의상이 컷마다
    다른 옷으로 나와도 후검사를 통과함 — person/place 참조만으로 'yes' 판정). 이미지가 없어도
    등록 시 적어둔 description은 있으니, 최소한 텍스트 설명 기준으로라도 vision 모델에게 검증
    근거를 주기 위한 함수."""
    out, seen = [], set()
    for m in _shot_mentions(work, shot):
        e = resolve_element(work, m)
        if not e or e.get("type") != "costume" or e.get("id") in seen:
            continue
        if _element_data_url(work, e):
            continue  # 이미지 참조가 있으면 이미 shot_ref_entries가 다룸 — 여기선 제외
        seen.add(e.get("id"))
        desc = (e.get("description") or "").strip()
        if desc:
            out.append((e.get("display") or "", desc))
    return out


def reference_priority_block(entries: list[tuple[str, str]]) -> str:
    """★2026-07-15: 참조 이미지가 여러 장(얼굴+의상 등) 섞이면 생성기가 역할 구분 없이 뒤섞어
    반영하는 문제(실측: 잠옷 의상을 등록해도 얼굴 참조 사진 속 옷차림으로 계속 나옴) — 참조
    순서 재배치(costume-first)만으론 부족해서, 프롬프트 텍스트에 각 참조 이미지가 정확히
    몇 번째이고 무슨 역할인지, 그 역할 밖의 정보는 무시하라고 명시적으로 선언한다(사용자 제공
    문구 기반). refs가 없으면 빈 문자열(생성 자체엔 영향 없음)."""
    if not entries:
        return ""
    lines = ["REFERENCE PRIORITY — each reference image below has ONE role only; "
             "ignore anything outside that role:"]
    for i, (role, _url) in enumerate(entries, 1):
        instr = _ROLE_INSTRUCTIONS.get(role, _ROLE_INSTRUCTIONS["prop"])
        lines.append(f"Reference image {i}: {instr}")
    return "\n".join(lines)


# gpt-image 계열은 "aspect_ratio" 문자열을 무시하므로 size로 직접 보내야 함(실측 2026-07-10).
# ★2026-07-14 재실측: size는 "16의 배수"면 임의 값을 받아준다(문서화 안 된 3종 고정이 아니었음
# — 최소 픽셀 예산만 있음, 640x1136은 통과·576x1024는 "below minimum pixel budget"으로 거부).
# 기존 "1024x1536"은 사실 9:16이 아니라 2:3(0.667) 비율이었음 — 720x1280이 정확히 9:16이고
# 최종 합본 캔버스(1080x1920)와 정비율(1.5배)이며 픽셀수 41%↓(비용도 약 33%↓, 실측 $0.0049→$0.0033).
_GPT_IMAGE_SIZES = {
    "1:1": "1024x1024", "9:16": "720x1280", "16:9": "1280x720",
    "2:3": "1024x1536", "3:2": "1536x1024",
}


def _size_for(aspect_ratio: str | None) -> str:
    """알려진 비율 문자열 → gpt-image size. 모르는 값은 세로(숏폼 기본)로 폴백."""
    return _GPT_IMAGE_SIZES.get(aspect_ratio or "", "1024x1536")


def generate(prompt: str, *, model: str | None = None, aspect_ratio: str | None = None,
             refs: list[str] | None = None, timeout: int | None = None,
             quality: str | None = None, moderation: str | None = None) -> tuple[bytes, float]:
    """이미지 1장 생성 → (PNG bytes, cost$). 실패 시 예외.

    refs: 참조 이미지 URL 리스트(캐릭터 일관성용, 선택 — input_references로 전달).
    quality: auto|low|medium|high. 미지정시 config.OPENROUTER_IMAGE_QUALITY(기본 low) —
    quality 생략하면 provider가 high로 갈 수 있어(장당 최대 10배+ 비용) 항상 명시한다.
    moderation: auto|low — 안전필터 강도. 미지정시 config.OPENROUTER_IMAGE_MODERATION(기본 low).
    aspect_ratio: "9:16"/"16:9" 등 — gpt-image는 aspect_ratio 필드를 무시하므로 size로 변환해 보낸다."""
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY 미설정 — 이미지 생성 불가")
    if timeout is None:
        timeout = config.OPENROUTER_IMG_TIMEOUT
    ar = aspect_ratio or config.OPENROUTER_IMAGE_ASPECT
    payload: dict = {
        "model": model or config.OPENROUTER_IMAGE_MODEL,
        "prompt": prompt,
        "size": _size_for(ar),
        "quality": quality or config.OPENROUTER_IMAGE_QUALITY,
        "moderation": moderation or config.OPENROUTER_IMAGE_MODERATION,
    }
    if refs:
        payload["input_references"] = [
            {"type": "image_url", "image_url": {"url": u}} for u in refs
        ]
    req = urllib.request.Request(
        _URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"OpenRouter 이미지 오류 {e.code}: {body}") from e
    items = data.get("data") or []
    if not items or not items[0].get("b64_json"):
        raise RuntimeError("이미지 응답이 비어 있음: " + json.dumps(data)[:200])
    png = base64.b64decode(items[0]["b64_json"])
    cost = float((data.get("usage") or {}).get("cost") or 0.0)
    return png, cost


def generate_mood_reference(mood_text: str, *, style_suffix: str | None = None,
                             aspect_ratio: str | None = None) -> tuple[bytes, float]:
    """씬의 '무드/조명:' 텍스트로 추상 색감/조명 참조 이미지 1장 생성 → (PNG bytes, cost$).

    사람·구체적 건축·오브젝트가 전혀 안 보이는 순수 색감/조명/분위기 스터디만 그리게 한다 —
    이 참조가 다른 참조(인물/장소/의상/소품)의 역할을 침범해 컷에 무관한 형태를 "새어들게"
    하면 안 되기 때문. style_suffix(=STILL_STYLE 등, 호출부가 넘겨줌)를 붙이면 이 무드
    참조 자체의 렌더링 스타일도 다른 스틸컷들과 맞춰져, 이 참조로 인해 스타일이 흔들리는
    걸 막는다. app.py를 import하지 않도록 aspect_ratio는 호출부에서 넘겨받는다."""
    prompt = (
        "Abstract atmospheric lighting and color-grade study — soft diffused light, color "
        "gradients, no people, no readable objects, no architecture, no text, capturing this "
        f"mood/tone: {mood_text}."
    )
    if style_suffix:
        prompt = f"{prompt} {style_suffix}"
    return generate(prompt, aspect_ratio=aspect_ratio, refs=None)
