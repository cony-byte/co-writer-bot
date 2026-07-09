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
import urllib.error
import urllib.request

from . import config

_URL = "https://openrouter.ai/api/v1/images"
_REF_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def available() -> bool:
    return bool(config.OPENROUTER_API_KEY)


# ── 캐릭터 참조 이미지 (일관성) ────────────────────────────────
def ref_data_url(work: str | None, name: str) -> str | None:
    """<refs>/<작품>/<인물>.(png|jpg|jpeg|webp) 를 찾아 base64 data URL로. 없으면 None."""
    if not name:
        return None
    dirs = []
    if work:
        dirs.append(config.OPENROUTER_REFS_DIR / work)
    dirs.append(config.OPENROUTER_REFS_DIR)          # 작품 폴더 없으면 공용 폴더도 탐색
    for d in dirs:
        for ext in _REF_EXTS:
            p = d / f"{name}{ext}"
            if p.exists():
                mt = mimetypes.guess_type(str(p))[0] or "image/png"
                b64 = base64.b64encode(p.read_bytes()).decode("ascii")
                return f"data:{mt};base64,{b64}"
    return None


def character_refs(work: str | None, names: list[str]) -> list[str]:
    """등장 인물 이름들 → 등록된 참조 이미지 data URL 리스트(있는 것만, 중복 제거)."""
    out, seen = [], set()
    for n in names:
        if n in seen:
            continue
        seen.add(n)
        u = ref_data_url(work, n)
        if u:
            out.append(u)
    return out


def registered_refs(work: str | None) -> list[str]:
    """그 작품에 등록된 참조 인물 이름 목록(파일명 기준). 상태 확인용."""
    d = config.OPENROUTER_REFS_DIR / work if work else config.OPENROUTER_REFS_DIR
    if not d.exists():
        return []
    return sorted({p.stem for p in d.iterdir() if p.suffix.lower() in _REF_EXTS})


def generate(prompt: str, *, model: str | None = None, aspect_ratio: str | None = None,
             refs: list[str] | None = None, timeout: int = 180) -> tuple[bytes, float]:
    """이미지 1장 생성 → (PNG bytes, cost$). 실패 시 예외.

    refs: 참조 이미지 URL 리스트(캐릭터 일관성용, 선택 — input_references로 전달)."""
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY 미설정 — 이미지 생성 불가")
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
