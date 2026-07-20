# -*- coding: utf-8 -*-
"""face_ref.py — 인물 등록 시 '얼굴 전용 레퍼런스' 자동 생성.

배경(실사용 사고): 업로드된 인물 고정 이미지가 상반신/전신 + 특징적 의상(흰 셔츠 등)을
포함하면, 스틸컷 생성 때 person 참조가 identity(얼굴)만이 아니라 의상까지 끌고 들어와
별도로 등록한 costume 참조가 무시되거나 섞인다. (openrouter_image.py의 참조 규칙이
"person = 얼굴 identity 전용 / costume = 의상의 배타적 소스"라고 명시해도, 참조 이미지
자체에 의상 정보가 강하면 모델이 새어 나감.)

해결: 인물 등록 파이프라인에 이 모듈을 끼워, 원본 대신 아래 조건의 얼굴 전용
레퍼런스를 person 참조로 저장한다.
  * 얼굴~어깨(버스트)만 크롭
  * 무지 중립 상의(회색 크루넥), 액세서리 제거
  * 정면 또는 반측면, 단색 배경
  * 원본은 <이름>_원본 으로 함께 보관(비교/롤백용)

3단계 우아한 성능저하(graceful degradation):
  1) 얼굴 감지 성공 + AI 중화 성공  → 크롭 → img2img 중화본 등록  (최선)
  2) AI 중화 실패(비용/필터/타임아웃) → 크롭본만 등록              (차선: 의상 노출 최소화)
  3) 얼굴 감지 실패                  → 원본 등록 + 경고 문구        (기존과 동일 + 안내)

환경변수:
  FACE_REF_ENABLED=1      기능 온오프 (기본 1)
  FACE_REF_NEUTRALIZE=1   AI 중화 단계 온오프 (기본 1; 0이면 크롭만)
  FACE_REF_QUALITY=low    중화 생성 품질 (openrouter quality — low ≈ $0.01 내외/장)
"""
from __future__ import annotations

import base64
import io
import logging
import os
from dataclasses import dataclass

log = logging.getLogger("storyboard-bot")

FACE_REF_ENABLED = os.environ.get("FACE_REF_ENABLED", "1") == "1"
FACE_REF_NEUTRALIZE = os.environ.get("FACE_REF_NEUTRALIZE", "1") == "1"
FACE_REF_QUALITY = os.environ.get("FACE_REF_QUALITY", "low")

_EXPAND_X = 0.55
_EXPAND_TOP = 0.45
_EXPAND_BOTTOM = 0.95

_NEUTRALIZE_PROMPT = (
    "Identity-preserving face reference sheet. Recreate EXACTLY the same person from the "
    "reference image — identical facial identity, face shape, eyes, nose, lips, skin tone, "
    "hairstyle and hair color. Bust shot only (head to shoulders). "
    "Wearing a plain light-gray crew-neck t-shirt with no logos, patterns, collars or buttons. "
    "Remove all accessories (no earrings, necklaces, glasses, piercings, hats). "
    "Neutral relaxed expression, facing front or slight three-quarter angle. "
    "Plain solid light background, soft even studio lighting, no props. "
    "Match the art style of the reference image (photo stays photoreal, illustration stays "
    "illustrated). This image is used purely as a FACIAL IDENTITY reference — clothing must "
    "carry zero distinctive information."
)


@dataclass
class FaceRefResult:
    png: bytes
    original: bytes
    detected: bool
    neutralized: bool
    cost: float = 0.0
    note: str = ""


def crop_face_bust(png_bytes: bytes) -> tuple[bytes, bool]:
    """얼굴~어깨 크롭 PNG 반환. (bytes, 감지성공여부). 감지 실패면 원본 그대로."""
    from PIL import Image
    from . import face_grid

    (x, y, w, h), detected = face_grid.detect_face_box(png_bytes)
    if not detected:
        return png_bytes, False

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    W, H = img.size
    x0 = max(0, int(x - w * _EXPAND_X))
    x1 = min(W, int(x + w * (1 + _EXPAND_X)))
    y0 = max(0, int(y - h * _EXPAND_TOP))
    y1 = min(H, int(y + h * (1 + _EXPAND_BOTTOM)))

    bw, bh = x1 - x0, y1 - y0
    if bw < bh:
        need = min(bh - bw, x0 + (W - x1))
        x0 = max(0, x0 - need // 2)
        x1 = min(W, x1 + need // 2)

    out = io.BytesIO()
    img.crop((x0, y0, x1, y1)).save(out, format="PNG")
    return out.getvalue(), True


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def neutralize(face_png: bytes) -> tuple[bytes, float]:
    """(중화된 PNG, 비용$). 실패 시 예외 — 호출부에서 크롭본으로 폴백."""
    from . import openrouter_image
    return openrouter_image.generate(
        _NEUTRALIZE_PROMPT,
        refs=[_data_url(face_png)],
        aspect_ratio="1:1",
        quality=FACE_REF_QUALITY,
    )


def make_face_reference(original_png: bytes) -> FaceRefResult:
    """인물 참조 이미지 1장을 얼굴 전용 레퍼런스로 변환. 어떤 경우에도 예외를
    올리지 않고 쓸 수 있는 이미지를 반환한다(최악 = 원본 + 경고)."""
    if not FACE_REF_ENABLED:
        return FaceRefResult(
            original_png,
            original_png,
            False,
            False,
            note="(얼굴 전용 레퍼런스 기능 꺼짐 — 원본 그대로 등록)",
        )

    try:
        cropped, detected = crop_face_bust(original_png)
    except Exception as e:
        log.warning("face_ref 크롭 실패(%s) — 원본 등록", e)
        return FaceRefResult(
            original_png,
            original_png,
            False,
            False,
            note="⚠️ 얼굴 감지를 못 해서 원본을 그대로 등록했어요 — 의상 특징이 강한 "
                 "이미지라면 스틸컷에서 의상 참조가 밀릴 수 있어요. 얼굴 위주 사진으로 "
                 "다시 등록하는 걸 권해요.",
        )

    if not detected:
        return FaceRefResult(
            original_png,
            original_png,
            False,
            False,
            note="⚠️ 이 이미지에서 얼굴을 못 찾았어요 — 원본 그대로 등록했어요. "
                 "정면/반측면 얼굴이 크게 나온 사진이면 자동으로 얼굴 전용 레퍼런스를 만들어드려요.",
        )

    if FACE_REF_NEUTRALIZE:
        try:
            neutral, cost = neutralize(cropped)
            return FaceRefResult(
                neutral,
                original_png,
                True,
                True,
                cost,
                note=f"🪞 얼굴 전용 레퍼런스를 자동 생성해 등록했어요 "
                     f"(얼굴~어깨 크롭 · 무지 상의 · 액세서리 제거, ~${cost:.3f}). "
                     f"원본은 `_원본`으로 함께 보관돼요 — 의상은 의상 참조가 온전히 담당해요.",
            )
        except Exception as e:
            log.warning("face_ref 중화 실패(%s) — 크롭본으로 폴백", e)

    return FaceRefResult(
        cropped,
        original_png,
        True,
        False,
        note="✂️ 얼굴~어깨로 크롭해서 등록했어요 (AI 중화는 건너뜀). "
             "원본은 `_원본`으로 함께 보관돼요.",
    )


def process_for_registration(kind: str, name: str, png: bytes) -> tuple[dict[str, bytes], str]:
    """등록 저장 직전에 호출.
    반환: ({저장할 파일명 접미사: bytes}, 사용자 안내 문구).
      인물   → {"": 얼굴레퍼런스, "_원본": 원본}
      그 외  → {"": 원본} (의상/장소/소품은 손대지 않음)"""
    if kind != "인물":
        return {"": png}, ""
    r = make_face_reference(png)
    files = {"": r.png}
    if r.png is not r.original:
        files["_원본"] = r.original
    log.info(
        "face_ref[%s]: detected=%s neutralized=%s cost=$%.3f",
        name,
        r.detected,
        r.neutralized,
        r.cost,
    )
    return files, r.note
