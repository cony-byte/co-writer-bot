# -*- coding: utf-8 -*-
"""얼굴 자동 감지 + 빨간 5×5 격자 오버레이 (봇 내장, 자체완결).

영상화가 "입력 이미지가 실존 인물처럼 보인다"는 안전필터에 걸렸을 때, 그 컷 스틸의 얼굴을
빨간 격자로 덮어 실존 인물 오탐을 회피하기 위한 모듈. visual-pipeline/tools/face_grid의
독립 CLI 도구와 동일한 로직을 봇 런타임에서 바로 쓸 수 있게 옮긴 것.

핵심 함수 overlay_grid(png_bytes) -> png_bytes:
  프레임 안의 얼굴을 모두 자동 감지(애니풍 정면 + 실사 정면 + 실사 좌/우 옆모습)해, 감지된
  얼굴마다 흰 배경 없이 빨간 5×5 격자 선만 얹은 PNG bytes를 반환. 한 명도 못 찾으면 상단
  중앙 폴백 박스 하나에 격자를 얹는다. opencv/numpy가 없으면 ImportError를 그대로 올리므로
  호출부가 try/except로 감싸 미설치 환경에서는 자동 발동을 건너뛰게 한다.
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
GRID_CELLS = 5  # 5×5
# 감지 박스는 이미 이마~턱을 포함하므로 아주 살짝만 넓힌다(가로/세로 배율).
# 크게 주면 세로 이미지에서 금방 좌우가 넘쳐 얼굴보다 훨씬 커진다.
PAD_X, PAD_Y = 0.03, 0.03
# 같은 얼굴이 여러 캐스케이드(정면+옆모습 등)에 중복 감지될 때 병합할 IOU 임계값.
_MERGE_IOU = 0.3


def _iou(a, b) -> float:
    ax0, ay0, aw, ah = a; bx0, by0, bw, bh = b
    ax1, ay1 = ax0 + aw, ay0 + ah
    bx1, by1 = bx0 + bw, by0 + bh
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    if inter == 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _merge_boxes(boxes: list) -> list:
    """겹치는(IOU>_MERGE_IOU) 박스들을 하나로 합쳐(합집합 bounding box) 중복 감지를 줄인다."""
    merged: list = []
    for b in boxes:
        hit = next((i for i, m in enumerate(merged) if _iou(m, b) > _MERGE_IOU), None)
        if hit is None:
            merged.append(list(b))
            continue
        mx0, my0, mw, mh = merged[hit]
        bx0, by0, bw, bh = b
        x0 = min(mx0, bx0); y0 = min(my0, by0)
        x1 = max(mx0 + mw, bx0 + bw); y1 = max(my0 + mh, by0 + bh)
        merged[hit] = [x0, y0, x1 - x0, y1 - y0]
    return [tuple(m) for m in merged]


def detect_face_boxes(png_bytes: bytes):
    """프레임 안의 얼굴을 모두 감지해 [(x,y,w,h), ...] 반환(다중 인물 지원).
    애니풍 정면 + 실사 정면 + 실사 옆모습(좌/우 모두, 이미지 좌우반전으로 반대쪽도 커버)을
    합쳐 감지하고, 겹치는 중복 박스는 병합한다. 하나도 못 찾으면 상단 중앙 휴리스틱 박스
    하나를 반환(detected=False)."""
    import cv2
    import numpy as np

    arr = np.frombuffer(png_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("이미지 디코드 실패")
    H, W = img.shape[:2]
    gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    min_size = (int(W * 0.06), int(W * 0.06))

    raw: list = []

    def _detect(classifier, gray_img, min_neighbors, flip_x=False):
        cands = classifier.detectMultiScale(gray_img, scaleFactor=1.1,
                                            minNeighbors=min_neighbors, minSize=min_size)
        for (x, y, w, h) in cands:
            if flip_x:
                x = W - x - w
            raw.append((int(x), int(y), int(w), int(h)))

    # 1) 애니풍 정면
    if os.path.exists(_ANIME_CASCADE):
        _detect(cv2.CascadeClassifier(_ANIME_CASCADE), gray, 5)
    # 2) 실사 정면
    _detect(cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml"),
            gray, 4)
    # 3) 실사 옆모습 — 캐스케이드는 한쪽 방향만 잡으므로, 좌우반전 이미지에도 돌려 반대쪽도 커버
    profile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
    _detect(profile, gray, 4)
    _detect(profile, cv2.flip(gray, 1), 4, flip_x=True)

    boxes = _merge_boxes(raw)

    if boxes:
        padded = []
        for (x, y, w, h) in boxes:
            px, py = int(w * PAD_X), int(h * PAD_Y)
            x0 = max(0, x - px); y0 = max(0, y - py)
            x1 = min(W, x + w + px); y1 = min(H, y + h + py)
            padded.append((x0, y0, x1 - x0, y1 - y0))
        return padded, True

    # 폴백: 상단 중앙(세로 이미지에서 얼굴이 보통 위쪽)
    w = int(W * 0.65); h = int(H * 0.36)
    return [((W - w) // 2, int(H * 0.29), w, h)], False


def overlay_grid(png_bytes: bytes) -> bytes:
    """감지된 얼굴 영역마다(다중 인물 포함) 빨간 5×5 격자 선만 얹은 PNG bytes 반환
    (흰 배경 없음)."""
    from PIL import Image, ImageDraw

    boxes, detected = detect_face_boxes(png_bytes)
    log.info("face_grid 얼굴 %s (%d개): %s", "감지" if detected else "폴백", len(boxes), boxes)

    base = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    color = GRID_COLOR + (255,)
    for (x, y, w, h) in boxes:
        x0, y0, x1, y1 = x, y, x + w, y + h
        for i in range(GRID_CELLS + 1):
            gx = round(x0 + (x1 - x0) * i / GRID_CELLS)
            d.line([(gx, y0), (gx, y1)], fill=color, width=GRID_WIDTH)
            gy = round(y0 + (y1 - y0) * i / GRID_CELLS)
            d.line([(x0, gy), (x1, gy)], fill=color, width=GRID_WIDTH)

    out = io.BytesIO()
    Image.alpha_composite(base, overlay).convert("RGB").save(out, format="PNG")
    return out.getvalue()
