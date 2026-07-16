# -*- coding: utf-8 -*-
"""Higgsfield 이미지 어댑터 (선택 백엔드). 표준 라이브러리만.

OpenRouter는 그대로 두고, config.IMAGE_BACKEND=="higgsfield" 일 때 _do_images가 이 모듈의
generate()를 openrouter_image.generate() 대신 호출한다(동일 시그니처 → 무손실 스왑).

문서 기준(2026-07, apidog/higgsfield.ai):
  POST {BASE}/v1/generations  {task:"text-to-image", model, prompt, width, height, ...}
    → 202 + {id} (비동기)
  GET  {BASE}/v1/generations/{id}  → {status, ...output url}
  인증: Authorization: Bearer <HIGGSFIELD_API_KEY>

⚠️ 확인 필요(계정/모델별로 필드가 다를 수 있음 — 실제 키로 1회 검증 후 확정):
  - Soul(캐릭터 일관성)·reference_image_urls 필드명/포맷
  - 모델명(gpt-image / flux / ...): config.HIGGSFIELD_IMAGE_MODEL
  - 응답 완료 시 이미지가 URL인지 base64인지 (아래 _extract_image가 둘 다 시도)
  - key+secret 쌍을 쓰는 계정이면 헤더 추가(config.HIGGSFIELD_SECRET)
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request

from . import config

# 9:16 / 16:9 등 → width,height (기본 세로 숏폼). 필요시 조정.
_ASPECT_WH = {
    "9:16": (768, 1344), "16:9": (1344, 768),
    "1:1": (1024, 1024), "3:4": (896, 1152), "4:3": (1152, 896),
}


def available() -> bool:
    return bool(config.HIGGSFIELD_API_KEY)


def _headers() -> dict:
    h = {"Authorization": f"Bearer {config.HIGGSFIELD_API_KEY}",
         "Content-Type": "application/json"}
    if config.HIGGSFIELD_SECRET:                       # 일부 계정: key+secret 쌍
        h["hf-secret"] = config.HIGGSFIELD_SECRET
    return h


def _req(method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(config.HIGGSFIELD_BASE_URL + path, data=data,
                                 headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_s = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Higgsfield {method} {path} 오류 {e.code}: {body_s}") from e


def _extract_image(obj: dict) -> bytes | None:
    """완료 응답에서 이미지 회수: ①결과 URL 다운로드 ②base64 직접. 스키마 흔들려도 최대한 훑음."""
    # 흔한 위치 후보들을 훑는다
    def _walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                lk = str(k).lower()
                if isinstance(v, str):
                    if v.startswith("http") and any(t in lk for t in ("url", "image", "output", "result")):
                        return ("url", v)
                    if lk in ("b64_json", "base64", "image_base64") and len(v) > 100:
                        return ("b64", v)
                r = _walk(v)
                if r:
                    return r
        elif isinstance(o, list):
            for it in o:
                r = _walk(it)
                if r:
                    return r
        return None

    hit = _walk(obj)
    if not hit:
        return None
    kind, val = hit
    if kind == "b64":
        return base64.b64decode(val)
    with urllib.request.urlopen(val, timeout=120) as r:   # URL 다운로드
        return r.read()


def generate(prompt: str, *, model: str | None = None, aspect_ratio: str | None = None,
             refs: list[str] | None = None, timeout: int | None = None) -> tuple[bytes, float]:
    """이미지 1장 생성 → (PNG bytes, cost$). openrouter_image.generate와 동일 시그니처.

    refs: 참조 이미지(캐릭터 일관성). ⚠️ Higgsfield는 보통 '호스팅된 URL'을 기대 —
    storyboard-bot의 refs는 base64 data URL이라 그대로는 안 맞을 수 있음(아래 TODO)."""
    if not config.HIGGSFIELD_API_KEY:
        raise RuntimeError("HIGGSFIELD_API_KEY 미설정 — Higgsfield 백엔드 사용 불가")
    ar = aspect_ratio or config.OPENROUTER_PANEL_ASPECT
    w, h = _ASPECT_WH.get(ar, (1024, 1024))
    payload: dict = {
        "task": "text-to-image",
        "model": model or config.HIGGSFIELD_IMAGE_MODEL,
        "prompt": prompt,
        "width": w, "height": h,
    }
    if refs:
        # TODO(검증): 계정 스키마에 맞춰 필드명 확정. Soul 모드면 reference_image_urls / soul_id.
        #   refs가 data URL이면 Higgsfield가 거부할 수 있음 → 사전 업로드 후 URL로 넘겨야 할 수도.
        payload["reference_image_urls"] = refs

    submit = _req("POST", "/v1/generations", payload, timeout=60)
    gen_id = submit.get("id") or submit.get("generation_id") or (submit.get("data") or {}).get("id")
    if not gen_id:
        # 동기 응답으로 이미지가 바로 올 수도 있음
        img = _extract_image(submit)
        if img:
            return img, float((submit.get("usage") or {}).get("cost") or 0.0)
        raise RuntimeError("Higgsfield 응답에 job id도 이미지도 없음: " + json.dumps(submit)[:200])

    deadline = (timeout or config.HIGGSFIELD_IMG_TIMEOUT)
    waited = 0
    while waited < deadline:
        time.sleep(config.HIGGSFIELD_POLL_INTERVAL)
        waited += config.HIGGSFIELD_POLL_INTERVAL
        st = _req("GET", f"/v1/generations/{gen_id}", None, timeout=30)
        status = str(st.get("status") or st.get("state") or "").lower()
        if status in ("completed", "succeeded", "success", "done", "finished"):
            img = _extract_image(st)
            if not img:
                raise RuntimeError("Higgsfield 완료됐는데 이미지 회수 실패: " + json.dumps(st)[:200])
            return img, float((st.get("usage") or {}).get("cost") or 0.0)
        if status in ("failed", "error", "canceled", "cancelled"):
            raise RuntimeError("Higgsfield 생성 실패: " + json.dumps(st)[:200])
    raise RuntimeError(f"Higgsfield 폴링 시간초과({deadline}s) — job {gen_id}")
