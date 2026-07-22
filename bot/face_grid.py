# -*- coding: utf-8 -*-
"""얼굴 자동 감지 + 빨간 불투명 박스 오버레이 (봇 내장, 자체완결).

영상화가 "입력 이미지가 실존 인물처럼 보인다"는 안전필터에 걸렸을 때, 그 컷 스틸의 얼굴을
빨간 박스로 완전히 덮어 실존 인물 오탐을 회피하기 위한 모듈.

핵심 함수 overlay_grid(png_bytes) -> png_bytes:
  프레임 안의 사람마다(다중 인물 지원) 얼굴 영역에 불투명 빨간 박스를 얹은 PNG bytes를 반환.
  한 명도 못 찾으면 상단 중앙 폴백 박스 하나를 얹는다.

★2026-07-22: 기존엔 OpenCV Haar cascade(정면/옆모습 캐스케이드)로 얼굴을 직접 찾았는데,
실측 검증(다중 인물 스틸컷 직접 영상화 호출)에서 옆모습·조명이 있는 실제 프로덕션 스틸컷의
한쪽 인물을 계속 놓치는 게 확인됨(캐스케이드가 얼굴이 아닌 손/자켓에 오탐도 냄) — 그 상태로는
박스를 아무리 불투명하게 칠해도 놓친 얼굴이 그대로 노출돼 필터를 못 피함. YOLOv8(person
클래스, ultralytics)로 교체해 사람 전신 박스를 훨씬 안정적으로 잡고(포즈/각도에 덜 민감），
그 박스 상단 일부를 얼굴 영역으로 근사해 덮는 방식으로 바꿈 — 같은 테스트 이미지로 재검증해
안전필터를 통과함을 확인함.
"""
from __future__ import annotations

import io
import logging
import os

log = logging.getLogger("storyboard-bot")

_HERE = os.path.dirname(os.path.abspath(__file__))
_YOLO_WEIGHTS = os.path.join(_HERE, "models", "yolov8n.pt")

GRID_COLOR = (237, 28, 36)   # Figma 기본 빨강
# 사람 박스 높이 중 상단 몇 %를 "얼굴 영역"으로 근사해서 덮을지(머리~턱 정도를 넉넉히 포함).
FACE_HEIGHT_RATIO = 0.30
# 감지 박스를 아주 살짝 넓혀서 여백 없이 얼굴이 딱 붙어 노출되는 걸 방지.
PAD_X, PAD_Y = 0.05, 0.05
_PERSON_CLASS = 0
_CONF_THRESHOLD = 0.4

_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        from ultralytics import YOLO
        _MODEL = YOLO(_YOLO_WEIGHTS)
    return _MODEL


def detect_face_boxes(png_bytes: bytes):
    """프레임 안의 사람마다(다중 인물 지원) 얼굴 영역 [(x,y,w,h), ...] 반환.
    YOLOv8로 사람 전신 박스를 감지해, 그 상단 FACE_HEIGHT_RATIO만큼을 얼굴 영역으로 근사한다.
    한 명도 못 찾으면 상단 중앙 휴리스틱 박스 하나를 반환(detected=False)."""
    import numpy as np
    import cv2

    arr = np.frombuffer(png_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("이미지 디코드 실패")
    H, W = img.shape[:2]

    res = _model()(img, verbose=False)[0]
    boxes = []
    for b in res.boxes:
        if int(b.cls) != _PERSON_CLASS or float(b.conf) < _CONF_THRESHOLD:
            continue
        x1, y1, x2, y2 = b.xyxy[0].tolist()
        h = y2 - y1
        fy1 = y1
        fy0 = fy1 - h * PAD_Y
        fy1 = fy1 + h * FACE_HEIGHT_RATIO
        w = x2 - x1
        px = w * PAD_X
        x0 = max(0.0, x1 - px); x1p = min(float(W), x2 + px)
        y0 = max(0.0, fy0); y1p = min(float(H), fy1)
        boxes.append((int(x0), int(y0), int(x1p - x0), int(y1p - y0)))

    if boxes:
        log.info("face_grid(YOLO) 사람 %d명 감지: %s", len(boxes), boxes)
        return boxes, True

    log.info("face_grid(YOLO) 사람 감지 실패 — 상단 중앙 폴백")
    w = int(W * 0.65); h = int(H * 0.36)
    return [((W - w) // 2, int(H * 0.29), w, h)], False


def detect_face_box(png_bytes: bytes):
    """구버전 단일-박스 호출부 호환용: 가장 큰 얼굴 영역 하나만 (x,y,w,h) 형태로 반환."""
    boxes, detected = detect_face_boxes(png_bytes)
    box = max(boxes, key=lambda b: b[2] * b[3])
    return box, detected


def overlay_grid(png_bytes: bytes) -> bytes:
    """감지된 얼굴 영역마다(다중 인물 포함) 빨간 불투명 박스로 완전히 덮은 PNG bytes 반환."""
    from PIL import Image, ImageDraw

    boxes, detected = detect_face_boxes(png_bytes)

    base = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    color = GRID_COLOR + (255,)
    for (x, y, w, h) in boxes:
        d.rectangle([x, y, x + w, y + h], fill=color)

    out = io.BytesIO()
    Image.alpha_composite(base, overlay).convert("RGB").save(out, format="PNG")
    return out.getvalue()
