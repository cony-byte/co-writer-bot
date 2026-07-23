# -*- coding: utf-8 -*-
"""인물 참조 시트(턴어라운드/설정화 — 정면·3/4·측면·뒷모습·표정·전신 등이 흰 배경에 여러 컷으로
배치된 한 장) → 각 컷을 자동으로 잘라낸다(★2026-07-22, 사용자 요청 "알아서 분할해서 저장").

방식: 흰 배경 위 '내용(비흰색) 덩어리'를 찾아 바운딩박스별로 크롭. 라인아트의 선 사이 틈은
팽창(dilate)으로 한 덩어리로 묶고, 캡션 글자·자잘한 얼룩은 최소 크기로 걸러낸다. 행 우선
(위→아래, 왼→오)으로 정렬해 반환하므로 보통 첫 컷이 좌상단(정면)이 된다.

★opencv는 봇 런타임에 없어서(face_grid도 동일 사유로 PIL 폴백) PIL+numpy로만 구현한다.
정확도(팽창량·임계값)는 시트 레이아웃마다 다를 수 있어 실제 시트로 검증 후 튜닝 필요."""
from __future__ import annotations

import io
import logging

log = logging.getLogger("storyboard-bot")

_PROC_W = 480   # 라벨링용 축소 폭(속도) — 박스는 원본 좌표로 되돌린다


def available() -> bool:
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:
        return False


def _dilate(mask, iters):
    """4-연결 이진 팽창(numpy 시프트-OR) — iters 픽셀만큼 확장해 라인아트 틈을 메운다."""
    m = mask
    for _ in range(iters):
        out = m.copy()
        out[:-1, :] |= m[1:, :]
        out[1:, :] |= m[:-1, :]
        out[:, :-1] |= m[:, 1:]
        out[:, 1:] |= m[:, :-1]
        m = out
    return m


def _label_components(mask):
    """8-연결 연결성분 라벨링(스택 기반 flood fill). 반환: [(x,y,w,h,area), ...] 바운딩박스."""
    import numpy as np
    H, W = mask.shape
    seen = np.zeros((H, W), dtype=bool)
    boxes = []
    idx = np.argwhere(mask)
    seenflat = seen
    for (sy, sx) in idx:
        if seenflat[sy, sx]:
            continue
        # flood fill
        stack = [(sy, sx)]
        seenflat[sy, sx] = True
        minx = maxx = sx
        miny = maxy = sy
        area = 0
        while stack:
            y, x = stack.pop()
            area += 1
            if x < minx: minx = x
            if x > maxx: maxx = x
            if y < miny: miny = y
            if y > maxy: maxy = y
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not seenflat[ny, nx]:
                        seenflat[ny, nx] = True
                        stack.append((ny, nx))
        boxes.append((minx, miny, maxx - minx + 1, maxy - miny + 1, area))
    return boxes


def _split_by_caption_columns(keep, raw_boxes, pw, ph):
    """★2026-07-23 실측(사용자 시트) — 같은 행의 인접 패널끼리 그림 자체가 거의 안 떨어져 있어
    (여백이 거의/전혀 없음) 팽창 전에도 이미 하나의 블롭으로 붙어있는 시트가 있었다(예: 정면·
    3/4·측면 세 패널이 통째로 한 블롭). 반면 그 밑의 캡션 라벨(예: "[정면 (FRONT)]")은 컬럼별로
    실제 공백을 두고 떨어져 있으므로, 큰 블롭 바로 아래 캡션 밴드에 있는 작은 텍스트 조각들의
    x 군집 수로 그 블롭이 실제로 몇 컬럼인지 추정해 세로로 다시 나눈다. 캡션이 없거나 군집이
    1개뿐이면(예: 전신처럼 원래 패널 하나) 그대로 둔다."""
    cap_h_max = ph * 0.05
    gap_thresh = ph * 0.02
    out = []
    for (x, y, w, h, a) in keep:
        # ★2026-07-23: 캡션이 패널과 거의 안 떨어져 있으면(관찰상 1px 수준) 팽창된 keep
        # 박스 안에 캡션 자체가 이미 통째로 흡수돼 있다 — "박스 바로 밑"이 아니라 "박스
        # 아래쪽 20% 영역"에서 팽창 전(raw) 작은 조각을 찾는다(흡수됐든 안 됐든 다 잡힘).
        band_top, band_bot = y + h * 0.80, y + h + ph * 0.05
        frags = sorted(
            (fb for fb in raw_boxes
             if fb[3] <= cap_h_max and band_top <= fb[1] + fb[3] / 2 <= band_bot
             and fb[0] >= x - 5 and fb[0] + fb[2] <= x + w + 5),
            key=lambda f: f[0])
        if not frags:
            out.append((x, y, w, h, a))
            continue
        clusters = [[frags[0]]]
        for f in frags[1:]:
            prev = clusters[-1][-1]
            if f[0] - (prev[0] + prev[2]) > gap_thresh:
                clusters.append([f])
            else:
                clusters[-1].append(f)
        if len(clusters) < 2:
            out.append((x, y, w, h, a))
            continue
        centers = [sum(f[0] + f[2] / 2 for f in c) / len(c) for c in clusters]
        bounds = ([x] + [(centers[i] + centers[i + 1]) / 2 for i in range(len(centers) - 1)]
                  + [x + w])
        for i in range(len(clusters)):
            sx, ex = int(bounds[i]), int(bounds[i + 1])
            out.append((sx, y, ex - sx, h, max(1, a // len(clusters))))
    return out


def split_panels(data: bytes, *, min_area_frac: float = 0.010, pad: int = 10) -> list[bytes]:
    """참조 시트 bytes → 각 컷 PNG bytes 리스트(행 우선 정렬). 컷을 2개 미만으로밖에 못 찾으면
    빈 리스트 반환(호출자는 '시트가 아님'으로 보고 통짜 등록으로 폴백)."""
    import numpy as np
    from PIL import Image

    try:
        full = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return []
    FW, FH = full.size
    scale = FW / _PROC_W if FW > _PROC_W else 1.0
    pw, ph = int(FW / scale), int(FH / scale)
    small = full.resize((pw, ph), Image.BILINEAR)
    g = np.asarray(small.convert("L"))
    mask = g < 245                                  # 비흰색 = 내용
    raw_boxes = _label_components(mask)             # 팽창 전(캡션 텍스트 조각 탐지용)
    # 라인아트 틈 메우기: 축소 이미지 크기에 비례한 픽셀만큼 팽창.
    # ★2026-07-23 실측(사용자 시트) — 기존 pw//60(약 8px)은 이 시트에서 행 사이·컬럼 사이
    # 공백까지 다 메워버려 6개 패널이 통째로 1개 블롭으로 뭉쳤다(6번째 실측 시트에서 발견).
    # 패널 안 선 끊김만 메우면 되므로 훨씬 작은 값으로 낮춘다 — 행/컬럼 사이 여백은 그대로
    # 살아있어야 _split_by_caption_columns 이전에 이미 과도하게 뭉치지 않는다.
    dilated = _dilate(mask, max(1, pw // 240))
    boxes = _label_components(dilated)
    min_area = pw * ph * min_area_frac
    keep = [b for b in boxes
            if b[4] >= min_area and b[2] >= pw * 0.04 and b[3] >= ph * 0.04]
    if len(keep) < 2:
        return []
    keep = _split_by_caption_columns(keep, raw_boxes, pw, ph)
    band = max(1.0, ph * 0.18)
    keep.sort(key=lambda b: (round(b[1] / band), b[0]))
    crops = []
    for (x, y, w, h, _a) in keep:
        # 원본 좌표로 환산 + 패딩
        x0 = max(0, int(x * scale) - pad)
        y0 = max(0, int(y * scale) - pad)
        x1 = min(FW, int((x + w) * scale) + pad)
        y1 = min(FH, int((y + h) * scale) + pad)
        buf = io.BytesIO()
        full.crop((x0, y0, x1, y1)).save(buf, format="PNG")
        crops.append(buf.getvalue())
    return crops
