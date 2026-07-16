# -*- coding: utf-8 -*-
"""Higgsfield 영상화(image-to-video) 어댑터. higgsfield_client SDK 사용(검증된 API).

⚠️ bot/higgsfield_image.py(구 스켈레톤, /v1/generations·Bearer 인증)와는 다른 별개 모듈 —
그건 미검증 추측 스키마로 IMAGE_BACKEND=higgsfield일 때만 쓰이는 죽은 코드고,
이 모듈은 2026-07-13에 공식 문서(docs.higgsfield.ai) + 실제 호출 테스트로 검증한 API를 쓴다:
  POST https://platform.higgsfield.ai/kling-video/v2.1/pro/image-to-video
  body: {image_url, prompt, duration}  (submit/subscribe는 SDK가 처리)
  인증: Authorization: Key {HF_API_KEY}:{HF_API_SECRET} (또는 HF_KEY 단일값)
  완료 응답: {"status":"completed","video":{"url":...}}
★2026-07-13 실측: bytedance/seedance/v1/pro/image-to-video는 이 계정에 접근 권한이 없어
"Model not found"로 즉시 실패(문서엔 나오는 경로인데 계정별 모델 활성화가 별도인 듯) —
같은 계정에서 kling-video/v2.1/pro/image-to-video와 higgsfield-ai/dop/standard는 정상 제출됨.
kling으로 실제 1건 생성 성공(완료 ~55초, duration=5 지정 → 실제 5.04초 mp4로 정확히 반영 확인).
★2026-07-13 추가 실측: kling-video는 duration이 자유값이 아니라 **5 또는 10만 허용**하는
enum — 콘티 씬 길이(예: 16초)를 그대로 넘기면 "duration: 16 is not one of [5, 10]"로 실패.
그래서 _clamp_duration()으로 가장 가까운 허용값으로 스냅해서 보낸다(즉, 실제 영상 길이가
콘티에 적힌 씬 길이와 정확히 같지는 않음 — 5 또는 10초 중 하나로 근사됨).

★모델 전환 준비(2026-07-13): seedance API 접근 권한이 열리면 코드 수정 없이
config.HIGGSFIELD_VIDEO_APPLICATION(.env의 HIGGSFIELD_VIDEO_APPLICATION)만 바꾸면 됨.
단, seedance의 duration 허용값은 아직 미검증 — _ALLOWED_DURATIONS는 모델별로 다르게
등록해뒀고(_DURATIONS_BY_APPLICATION), 모르는 모델은 일단 kling과 같은 [5,10]으로
가정하되 실제 전환 시 반드시 1건 테스트해서 확인할 것(문서에 duration 필드 예시가
없어서 모델마다 다를 가능성이 큼).
"""
from __future__ import annotations

import io
import logging

from . import config

log = logging.getLogger("storyboard-bot")

APPLICATION = config.HIGGSFIELD_VIDEO_APPLICATION


def available() -> bool:
    import os
    return bool(os.environ.get("HF_KEY") or (os.environ.get("HF_API_KEY") and os.environ.get("HF_API_SECRET")))


_TARGET_RATIO = 9 / 16   # 영상은 무조건 9:16 세로형 — 숏폼 기본(2026-07-13 사용자 명시 요구)
_MIN_SIDE = 512          # kling이 너무 작은 이미지는 "Image pixel is invalid"로 거부(실측)


def _force_916(img):
    """어떤 비율로 들어오든 무조건 9:16 세로로 맞춘다(가운데 기준 크롭) + 최소 해상도 보장.
    정상적인 스틸컷 컷(STILL_ASPECT="9:16")은 원래 이미 9:16이라 이 크롭이 사실상 no-op이고,
    수동으로 자른 조각(예: 그리드 콜라주에서 손으로 오려낸 컷)처럼 비율이 깨진 경우의 안전망."""
    from PIL import Image
    w, h = img.size
    cur_ratio = w / h
    if cur_ratio > _TARGET_RATIO:      # 너무 넓음 → 좌우 크롭
        new_w = int(h * _TARGET_RATIO)
        x0 = (w - new_w) // 2
        img = img.crop((x0, 0, x0 + new_w, h))
    elif cur_ratio < _TARGET_RATIO:    # 너무 좁음(세로가 과함) → 상하 크롭
        new_h = int(w / _TARGET_RATIO)
        y0 = (h - new_h) // 2
        img = img.crop((0, y0, w, y0 + new_h))
    w, h = img.size
    if min(w, h) < _MIN_SIDE:
        scale = _MIN_SIDE / min(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return img


def _upload(png: bytes) -> str:
    """PNG bytes → Higgsfield 호스팅 URL (SDK가 요구하는 image_url 입력용).
    업로드 전 무조건 9:16으로 강제 크롭 + 최소 해상도 보장(_force_916)."""
    from PIL import Image
    import higgsfield_client as hc
    img = Image.open(io.BytesIO(png)).convert("RGB")
    img = _force_916(img)
    return hc.upload_image(img, format="jpeg")


# 모델(APPLICATION)별 허용 duration — 실측된 것만 정확함(kling). 미검증 모델(예: seedance로
# 전환 시)은 kling과 같다고 가정하되, 실제 전환 시 1건 테스트로 꼭 재확인할 것.
_DURATIONS_BY_APPLICATION = {
    "kling-video/v2.1/pro/image-to-video": (5, 10),
}
_DEFAULT_ALLOWED_DURATIONS = (5, 10)


def _clamp_duration(seconds: int) -> int:
    """이 모델이 받는 duration 중 가장 가까운 값으로 스냅(모델별 허용값이 다를 수 있음)."""
    allowed = _DURATIONS_BY_APPLICATION.get(APPLICATION, _DEFAULT_ALLOWED_DURATIONS)
    return min(allowed, key=lambda d: abs(d - seconds))


def generate(png: bytes, motion_prompt: str, *, duration: int | None = None,
            on_queue_update=None) -> tuple[str, float]:
    """스틸컷 이미지(PNG bytes) + 모션 프롬프트 → (완성된 비디오 URL, 생성비$). 실패 시 예외.
    ★2026-07-13: openrouter_video.generate()와 시그니처를 맞춰 (url, cost) 튜플로 반환하게
    바꿈(app.py가 hf_video를 어느 쪽으로 import하든 무손실 교체되게) — 비용은 Higgsfield
    응답에 안 알려줘서 0.0 고정.

    duration: 콘티 씬 헤더의 "N초"(예: "■ 씬1 · 10초 · ...")를 넘기면 5·10 중 가까운 값으로
    스냅해서 보낸다(kling-video가 그 둘만 허용 — 2026-07-13 실측). duration=5 지정 시 실제
    mp4 길이 5.04초로 정확히 반영되는 것도 확인됨(그 값 자체는 검증된 API 계약).
    ⚠️ 결과는 URL만 반환(다운로드는 호출자 책임) — 영상 파일이 커서 매번 로컬로
    끌어오지 않고 필요할 때만(예: Slack 업로드 직전) 받게 하기 위함."""
    if not available():
        raise RuntimeError("HF_KEY(또는 HF_API_KEY+HF_API_SECRET) 미설정 — 영상화 불가")
    import higgsfield_client as hc

    image_url = _upload(png)
    args = {"image_url": image_url, "prompt": motion_prompt}
    if duration:
        args["duration"] = _clamp_duration(duration)
    result = hc.subscribe(APPLICATION, arguments=args, on_queue_update=on_queue_update)
    video = (result or {}).get("video") or {}
    url = video.get("url")
    if not url:
        raise RuntimeError("Higgsfield 완료됐는데 video.url 없음: " + str(result)[:300])
    return url, 0.0
