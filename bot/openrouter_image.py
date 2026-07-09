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
import unicodedata
import urllib.error
import urllib.request

from . import config

_URL = "https://openrouter.ai/api/v1/images"
_REF_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def _nfc(s: str) -> str:
    """macOS 파일명은 한글을 NFD(자모분해)로 저장 → NFC(완성형)로 통일해 비교."""
    return unicodedata.normalize("NFC", s or "")


def available() -> bool:
    return bool(config.OPENROUTER_API_KEY)


# ── 캐릭터 참조 이미지 (일관성) ────────────────────────────────
#   구조: <refs>/<작품>/<정본이름>.(png|jpg|jpeg|webp)
#   대본이 이름을 섞어 써도(강태혁/태혁) 하나의 파일로 매칭:
#     ① 정확일치 → ② aliases.json 별칭 → ③ 양방향 부분일치(한쪽이 다른 쪽을 포함, 2자↑)
#   aliases.json (선택, 작품 폴더에): {"태혁": ["강태혁","태혁오빠"], ...}  (키=파일명 정본)
def _ref_dirs(work: str | None) -> list:
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


def registered_refs(work: str | None) -> list[str]:
    """그 작품에 등록된 참조 인물(파일명 정본, NFC) 목록. 상태 확인용."""
    out = set()
    for d in _ref_dirs(work):
        if d.exists():
            out |= {_nfc(p.stem) for p in d.iterdir() if p.suffix.lower() in _REF_EXTS}
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
    # ③ 양방향 부분일치 (2자 이상, 가장 긴 정본 우선)
    for stem in sorted(stems, key=len, reverse=True):
        if len(stem) >= 2 and (stem in mention or mention in stem):
            return stem
    return None


def _load_by_stem(work: str | None, stem: str) -> str | None:
    for d in _ref_dirs(work):
        for ext in _REF_EXTS:
            p = d / f"{stem}{ext}"
            if p.exists():
                mt = mimetypes.guess_type(str(p))[0] or "image/png"
                b64 = base64.b64encode(p.read_bytes()).decode("ascii")
                return f"data:{mt};base64,{b64}"
    return None


def ref_data_url(work: str | None, name: str) -> str | None:
    """이름(별칭/부분이름 허용) → 참조 이미지 base64 data URL. 없으면 None."""
    stem = resolve_ref_name(work, name)
    return _load_by_stem(work, stem) if stem else None


def character_refs(work: str | None, names: list[str]) -> list[str]:
    """등장 인물 이름들 → 참조 data URL 리스트(정본 기준 중복 제거, 있는 것만)."""
    out, seen = [], set()
    for n in names:
        stem = resolve_ref_name(work, n)
        if not stem or stem in seen:
            continue
        seen.add(stem)
        u = _load_by_stem(work, stem)
        if u:
            out.append(u)
    return out


def generate(prompt: str, *, model: str | None = None, aspect_ratio: str | None = None,
             refs: list[str] | None = None, timeout: int | None = None) -> tuple[bytes, float]:
    """이미지 1장 생성 → (PNG bytes, cost$). 실패 시 예외.

    refs: 참조 이미지 URL 리스트(캐릭터 일관성용, 선택 — input_references로 전달)."""
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY 미설정 — 이미지 생성 불가")
    if timeout is None:
        timeout = config.OPENROUTER_IMG_TIMEOUT
    payload: dict = {
        "model": model or config.OPENROUTER_IMAGE_MODEL,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio or config.OPENROUTER_IMAGE_ASPECT,
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
