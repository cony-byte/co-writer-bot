# -*- coding: utf-8 -*-
"""스토리보드 그리드(콘택트 시트) 합성.

샷 이미지들 + 번호·한글 캡션 → PNG 한 장. Pillow 필요(requirements.txt).
한글 폰트: macOS AppleSDGothicNeo 우선, 없으면 후보들 탐색.
"""
from __future__ import annotations

import io
import os

# 한글 글리프 있는 폰트 후보 (순서대로 탐색)
_FONT_CANDIDATES = [
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "/Library/Fonts/AppleGothic.ttf",
    "/Library/Fonts/NanumGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]


def available() -> tuple[bool, str]:
    """Pillow 설치 여부. (ok, 안내메시지)."""
    try:
        import PIL  # noqa: F401
        return True, ""
    except Exception:
        return False, "Pillow가 설치돼 있지 않아요. `pip install Pillow` 후 봇을 재시작하세요."


def _font(size: int):
    from PIL import ImageFont
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int, max_lines: int) -> list[str]:
    """한국어 포함 줄바꿈: 어절 우선, 넘치면 글자 단위. max_lines 초과분은 …로 자름."""
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return []
    def w(s):
        return draw.textlength(s, font=font)
    lines, cur = [], ""
    tokens = text.split(" ")
    for tok in tokens:
        cand = (cur + " " + tok).strip()
        if w(cand) <= max_w:
            cur = cand
            continue
        if cur:
            lines.append(cur)
            cur = ""
        # 어절 자체가 길면 글자 단위로 쪼갬
        piece = ""
        for ch in tok:
            if w(piece + ch) <= max_w:
                piece += ch
            else:
                if piece:
                    lines.append(piece)
                piece = ch
        cur = piece
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and w(last + "…") > max_w:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def build_grid(panels: list[tuple[bytes, int, str]], *, cols: int = 6, panel_w: int = 400,
               pad: int = 12, cap_lines: int = 3, bg=(18, 18, 20),
               fg=(236, 236, 236)) -> bytes:
    """panels = [(png_bytes, 번호, 캡션)] → 그리드 PNG bytes.
    각 셀 = 이미지(첫 이미지 비율로 통일) + 좌상단 번호칩 + 하단 캡션바."""
    from PIL import Image, ImageDraw

    imgs = []
    for data, n, cap in panels:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        imgs.append((im, n, cap))
    if not imgs:
        raise RuntimeError("합성할 이미지가 없습니다.")

    # 패널 이미지 크기: 첫 이미지 비율 유지, 폭=panel_w
    aw, ah = imgs[0][0].size
    panel_h = max(1, round(panel_w * ah / aw))

    cap_font = _font(20)
    num_font = _font(22)
    line_h = cap_font.getbbox("가")[3] + 6
    cap_h = cap_lines * line_h + 12          # 캡션바 높이
    cell_w = panel_w
    cell_h = panel_h + cap_h

    rows = (len(imgs) + cols - 1) // cols
    W = cols * cell_w + (cols + 1) * pad
    H = rows * cell_h + (rows + 1) * pad

    canvas = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(canvas)

    for i, (im, n, cap) in enumerate(imgs):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = pad + r * (cell_h + pad)
        canvas.paste(im.resize((panel_w, panel_h), Image.LANCZOS), (x, y))
        # 하단 캡션바
        by = y + panel_h
        draw.rectangle([x, by, x + cell_w, by + cap_h], fill=(0, 0, 0))
        lines = _wrap(draw, cap, cap_font, cell_w - 16, cap_lines)
        ty = by + 6
        for ln in lines:
            tw = draw.textlength(ln, font=cap_font)
            draw.text((x + (cell_w - tw) / 2, ty), ln, font=cap_font, fill=fg)
            ty += line_h
        # 좌상단 번호 칩
        label = f"{n:02d}"
        lw = draw.textlength(label, font=num_font)
        draw.rectangle([x, y, x + lw + 14, y + 30], fill=(0, 0, 0))
        draw.text((x + 7, y + 4), label, font=num_font, fill=(255, 255, 255))

    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()
