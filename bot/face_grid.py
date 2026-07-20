# -*- coding: utf-8 -*-
"""얼굴 자동 감지 + 빨간 3×3 격자 오버레이 (봇 내장, 자체완결).

영상화가 "입력 이미지가 실존 인물처럼 보인다"는 안전필터에 걸렸을 때, 그 컷 스틸의 얼굴을
빨간 격자로 덮어 실존 인물 오탐을 회피하기 위한 모듈. visual-pipeline/tools/face_grid의
독립 CLI 도구와 동일한 로직을 봇 런타임에서 바로 쓸 수 있게 옮긴 것.

핵심 함수 overlay_grid(png_bytes) -> png_bytes:
  얼굴을 자동 감지(애니풍 → 실사 정면 → 상단중앙 폴백)해 그 영역에 흰 배경 없이 빨간 격자
  선만 얹은 PNG bytes를 반환. opencv/numpy가 없으면 ImportError를 그대로 올리므로 호출부가
  try/except로 감싸 미설치 환경에서는 자동 발동을 건너뛰게 한다.
"""
from __future__ import annotations

import io
import logging
import os

log = logging.getLogger("storyboard-bot")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ANIME_CASCADE = os.path.join(_HERE, "lbpcascade_animeface.xml")

GRID_COLOR = (237, 28, 36)   # Figma 기본 빨강
GRID_WIDTH = 3
# 감지 박스는 이미 이마~턱을 포함하므로 아주 살짝만 넓힌다(가로/세로 배율).
# 크게 주면 세로 이미지에서 금방 좌우가 넘쳐 얼굴보다 훨씬 커진다.
PAD_X, PAD_Y = 0.03, 0.03


def detect_face_box(png_bytes: bytes):
    """(x, y, w, h) 반환. 얼굴 못 찾으면 상단 중앙 휴리스틱으로 폴백."""
    import cv2
    import numpy as np

    arr = np.frombuffer(png_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("이미지 디코드 실패")
    H, W = img.shape[:2]
    gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))

    candidates = []
    # 1) 애니풍 얼굴
    if os.path.exists(_ANIME_CASCADE):
        c = cv2.CascadeClassifier(_ANIME_CASCADE)
        candidates = c.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                        minSize=(int(W * 0.08), int(W * 0.08)))
    # 2) 실사 정면 얼굴 폴백
    if len(candidates) == 0:
        c = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        candidates = c.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4,
                                        minSize=(int(W * 0.08), int(W * 0.08)))

    if len(candidates) > 0:
        x, y, w, h = max(candidates, key=lambda r: r[2] * r[3])
        px, py = int(w * PAD_X), int(h * PAD_Y)
        x0 = max(0, x - px); y0 = max(0, y - py)
        x1 = min(W, x + w + px); y1 = min(H, y + h + py)
        return (x0, y0, x1 - x0, y1 - y0), True

    # 3) 폴백: 상단 중앙(세로 이미지에서 얼굴이 보통 위쪽)
    w = int(W * 0.65); h = int(H * 0.36)
    return ((W - w) // 2, int(H * 0.29), w, h), False


def overlay_grid(png_bytes: bytes) -> bytes:
    """얼굴 영역에 빨간 3×3 격자 선만 얹은 PNG bytes 반환(흰 배경 없음)."""
    from PIL import Image, ImageDraw

    box, detected = detect_face_box(png_bytes)
    log.info("face_grid 얼굴 %s: %s", "감지" if detected else "폴백", box)

    base = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    x, y, w, h = box
    x0, y0, x1, y1 = x, y, x + w, y + h
    color = GRID_COLOR + (255,)
    for i in range(4):
        gx = round(x0 + (x1 - x0) * i / 3)
        d.line([(gx, y0), (gx, y1)], fill=color, width=GRID_WIDTH)
        gy = round(y0 + (y1 - y0) * i / 3)
        d.line([(x0, gy), (x1, gy)], fill=color, width=GRID_WIDTH)

    out = io.BytesIO()
    Image.alpha_composite(base, overlay).convert("RGB").save(out, format="PNG")
    return out.getvalue()
