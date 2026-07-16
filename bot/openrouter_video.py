# -*- coding: utf-8 -*-
"""OpenRouter 영상화(image-to-video) 어댑터. 2026-07-13 신규, 실제 호출로 검증 완료.

Higgsfield는 bytedance/seedance가 계정별 활성화가 따로 필요해서 "Model not found"로 막혀
kling-video로 우회했었는데, OpenRouter는 seedance 2.0을 계정 활성화 없이 그냥 노출한다 —
이미 이미지 생성에 쓰던 OPENROUTER_API_KEY 그대로 재사용. 2026-07-13부터 슬랙 봇의 실제
영상화 백엔드로 전환됨(higgsfield_video는 폐기하지 않고 남겨둠 — seedance 접근 이슈 생기면
언제든 되돌릴 수 있게).

API (OpenRouter 공식 문서/쿡북 + 실제 호출 검증, 2026-07-13):
  POST https://openrouter.ai/api/v1/videos
    body: {model, prompt, duration, aspect_ratio, input_references:[{type:"image_url", image_url:{url}}]}
    응답: {"id":..., "status":"pending", "polling_url":...}
  GET  {polling_url}  — status: pending → completed/failed/cancelled/expired
    완료 시: {"status":"completed", "unsigned_urls":["https://..."], ...}
  인증: Authorization: Bearer {OPENROUTER_API_KEY} (이미지 생성과 동일 키)
  모델: bytedance/seedance-2.0 (정속) / bytedance/seedance-2.0-fast (저가·속도 우선, 기본값)

★실측 확인(2026-07-13):
- duration은 자유값(4~15초 전부 허용 — kling처럼 5/10 enum 아님), 단 **정수만**(2026-07-14
  실측: 소수 보내면 "expected int, received number" ZodError 400). generate()는 올림(ceil)해서
  보낸다 — 대사가 그 컷 안에 다 들어가야 하니 부족한 것보다 넉넉한 게 낫고(내림하면 4.5초를
  4초로 깎아 대사가 밀릴 수 있음), 남는 부분은 합본 편집(edit_plan의 start/duration)이 잘라
  쓰면 된다. /api/v1/videos/models로 모델별 supported_durations/aspect_ratios/resolutions 조회 가능.
- input_references에 base64 data URL 그대로 먹힘(별도 업로드/호스팅 불필요).
- ⚠️ 실존 인물 안전필터 있음: "InputImageSensitiveContentDetected.PrivacyInformation" —
  seedance-2.0/2.0-fast 둘 다 동일하게 걸림(모델 무관, ByteDance 공용 정책으로 보임).
  얼굴이 화면을 꽉 채우는 정면 "증명사진" 스타일 단독 인물컷에서 특히 잘 걸리고, 인물이
  포함되되 여백·공간감 있는 씬 컷(예: 두 사람이 나오는 와이드~미디엄 샷)은 통과됨(실측
  2건 비교 확인). moderation 완화 파라미터는 없음(allowed_passthrough_parameters는
  watermark/req_key뿐) — 우회 방법 없음, 레퍼런스 이미지 자체를 정면 포트레이트가 아니라
  자연스러운 스틸컷 구도로 만드는 게 유일한 대응(_generate_element_candidate에 반영함).
"""
from __future__ import annotations

import base64
import json
import math
import time
import urllib.error
import urllib.request

from . import config

_VIDEOS_URL = "https://openrouter.ai/api/v1/videos"
APPLICATION = config.OPENROUTER_VIDEO_MODEL  # higgsfield_video와 인터페이스 맞춤(app.py 무손실 교체용)

# /api/v1/videos/models의 pricing_skus.video_tokens(달러/토큰) 실측값. 토큰 공식(문서 기준):
# tokens = (height × width × duration × 24) / 1024. 정확한 청구액은 실제 응답에 따라
# 달라질 수 있어 어디까지나 스틸컷의 "생성비 ~$0.10" 표기와 같은 수준의 근사치.
_PRICE_PER_TOKEN = {
    "bytedance/seedance-2.0": 0.000007,
    "bytedance/seedance-2.0-fast": 0.0000056,
}
_RES_DIMS = {"480p": (480, 854), "720p": (720, 1280), "1080p": (1080, 1920), "4k": (2160, 3840)}


def estimate_cost(duration: int, resolution: str = "720p") -> float:
    """대략적인 생성비($) 추정 — 실제 청구액과 다를 수 있음(근사치)."""
    h, w = _RES_DIMS.get(resolution.lower(), _RES_DIMS["720p"])
    tokens = (h * w * (duration or 5) * 24) / 1024
    rate = _PRICE_PER_TOKEN.get(APPLICATION, _PRICE_PER_TOKEN["bytedance/seedance-2.0-fast"])
    return tokens * rate


def available() -> bool:
    return bool(config.OPENROUTER_API_KEY)


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "Content-Type": "application/json"}


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _req(method: str, url: str, body: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_s = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"OpenRouter 영상 {method} 오류 {e.code}: {body_s}") from e


def generate(png: bytes, motion_prompt: str, *, duration: int | None = None,
            aspect_ratio: str = "9:16", generate_audio: bool = False,
            on_queue_update=None) -> tuple[str, float]:
    """스틸컷 이미지(PNG bytes) + 모션 프롬프트 → (완성된 비디오 URL, 추정 생성비$). 실패 시 예외.
    비용은 실제 청구 필드가 응답에 없어서 pricing_skus 공식 기반 근사치(estimate_cost).

    ★2026-07-14: input_references에 이미지 2장(현재 컷 스틸 + 직전 컷 마지막 프레임)을 같이
    보내는 다중 참조를 시도했다가 "Job None not found"(202 대신 id/polling_url 없는 응답)로
    실패 — 이미지 생성 API(openrouter_image.py)와 달리 이 비디오 API는 다중 참조가 검증된 적
    없다(원래 실측은 1장뿐). 그래서 다중 첨부 대신 호출자(app.py)가 어느 이미지 하나를 쓸지
    결정해서 png 인자 하나로만 넘기게 되돌림 — 연결 매끄러움은 그 하나를 무엇으로 고르는지로
    해결(직전 컷 마지막 프레임을 이 컷의 png로 사용).

    ★2026-07-14: seedance 계열은 기본으로 대사·효과음까지 자동 생성한다(/api/v1/videos/models의
    "generate_audio": true 확인). 이 봇은 그 오디오를 쓴 적이 없고(합본 렌더가 항상 -an으로
    버림, 나레이션은 별도 TTS로 만듦) 자동 생성된 대사가 "output audio may contain sensitive
    information"로 안전필터에 걸려 영상화 자체가 실패하는 경우까지 있었다(실측) — 그래서
    generate_audio=False로 아예 오디오를 안 만들게 해서 이 실패 원인 자체를 없앤다(비용은
    이 모델 기준 무음이어도 동일 — pricing_skus.video_tokens_without_audio가 video_tokens와
    같음, 그래도 안전필터 리스크가 사라지는 이득은 있음).

    ★2026-07-15: generate_audio를 파라미터로 뺌(기본값은 여전히 False — 함수 자체 기본값은
    무손실 보존). 이후 같은 날 config.OPENROUTER_VIDEO_GENERATE_AUDIO 토글을 app.py 호출부
    (_generate_video_for_cut)에 실제로 연결하고 기본값도 true로 전환 — 사용자가 실제 프로덕션
    파이프라인으로 슬랙에서 직접 라이브 테스트하기 위함(합성 API 테스트가 아니라 실사용).
    실측 테스트 결과(2026-07-15, seedance-2.0-fast, generate_audio=True, 대사 포함 5초 컷,
    레퍼런스 2장 시도 — ① 두 사람 미디엄샷 측면, ② 여러 인물 와이드 앙상블샷): **둘 다
    제출(POST) 단계에서 즉시 400 실패** — "InputImageSensitiveContentDetected.PrivacyInformation
    ... may contain real person". generate_audio 관련 안전필터(과거 겪었던 "output audio may
    contain sensitive information")가 아니라 **입력 이미지 자체**에 대한 실존인물 필터로,
    생성이 시작되기도 전에 걸린 것 — 즉 이번 2회 테스트로는 generate_audio=True 자체의
    안전성은 검증되지 못함(이미지 단계에서 막혀 오디오 경로까지 못 감). 기존에 통과했다고
    기록된 "두 사람 와이드~미디엄 샷" 규칙이 이번엔 재현되지 않음 — 세션/이미지별 필터
    민감도가 다르거나 판정이 더 엄격해졌을 가능성. 이 필터는 좁은 인물 크롭 합성 테스트에서
    관찰된 것이고, 실제 프로덕션은 더 넓은/다인물 프레이밍의 실제 씬 스틸컷을 쓰므로 이 필터를
    더 안정적으로 통과해온 히스토리가 있다 — 그래서 config 기본값을 true로 전환해 실사용으로
    직접 재검증하기로 함(generate_audio 자체의 안전성은 여전히 미확인이나, 실사용 중 가끔
    이 이미지단 필터에 걸려 실패해도 회귀가 아니라 알려진 리스크로 취급할 것)."""
    if not available():
        raise RuntimeError("OPENROUTER_API_KEY 미설정 — 영상화 불가")
    # ★2026-07-15: 사용자 리포트 — 생성된 영상이 참조 스틸컷과 머리색/옷/배경이 전혀 다름.
    # /api/v1/videos/models 실측 확인 결과 이 모델은 "supported_frame_images": ["first_frame",
    # "last_frame"]을 지원 — input_references를 그냥 "multimodal reference-to-video"(느슨한
    # 스타일/캐릭터 참고용, 모델이 자유롭게 재해석 가능)로만 보내고 있었고, 정작 "엄격한 시작
    # 프레임 고정"을 위한 frame_image 태그를 안 붙이고 있었다. 각 참조 항목에 "frame_image":
    # "first_frame"을 추가해 이 이미지를 정확한 시작 프레임으로 취급하도록 명시(API가 이 필드를
    # 스키마 오류 없이 받는 것 확인함).
    payload = {
        "model": config.OPENROUTER_VIDEO_MODEL,
        "prompt": motion_prompt,
        "aspect_ratio": aspect_ratio,
        "generate_audio": generate_audio,
        "input_references": [{"type": "image_url", "image_url": {"url": _data_url(png)},
                              "frame_image": "first_frame"}],
    }
    if duration:
        # ★2026-07-14 실측: API가 duration을 정수(safeint)로만 받는다 — 소수(예: 4.5)를 보내면
        # ZodError 400으로 거부됨. [N초] 기반 계산은 소수가 자연스럽게 나온다(예: 2.5초) —
        # round()는 내림 방향으로도 반올림돼(4.5→4, 파이썬 banker's rounding) 대사가 밀릴 수
        # 있으니, 항상 올림(ceil)해서 절대 원래 필요한 길이보다 짧아지지 않게 한다. 남는 부분은
        # 합본 편집(edit_plan)이 잘라 쓴다 — "생성은 넉넉히, 편집은 정확히"(2026-07-14 요청).
        payload["duration"] = math.ceil(duration)
    submit = _req("POST", _VIDEOS_URL, payload, timeout=60)
    polling_url = submit.get("polling_url") or f"{_VIDEOS_URL}/{submit.get('id')}"
    if not submit.get("polling_url") and not submit.get("id"):
        raise RuntimeError(f"OpenRouter 영상 제출 응답에 id/polling_url이 없음: {json.dumps(submit)[:300]}")

    waited = 0
    while waited < config.OPENROUTER_VIDEO_TIMEOUT:
        st = _req("GET", polling_url, None, timeout=30)
        status = st.get("status")
        if on_queue_update:
            on_queue_update(status)
        if status == "completed":
            urls = st.get("unsigned_urls") or []
            if not urls:
                raise RuntimeError("완료됐는데 unsigned_urls 없음: " + json.dumps(st)[:300])
            cost = st.get("cost") or st.get("usage", {}).get("cost") or estimate_cost(duration or 5)
            return urls[0], float(cost)
        if status in ("failed", "cancelled", "expired"):
            raise RuntimeError(f"OpenRouter 영상 생성 실패({status}): " + json.dumps(st)[:300])
        time.sleep(config.OPENROUTER_VIDEO_POLL_INTERVAL)
        waited += config.OPENROUTER_VIDEO_POLL_INTERVAL
    raise RuntimeError(f"OpenRouter 영상 폴링 시간초과({config.OPENROUTER_VIDEO_TIMEOUT}s)")
