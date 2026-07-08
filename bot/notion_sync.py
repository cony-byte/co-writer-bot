# -*- coding: utf-8 -*-
"""노션 페이지 → 평문 텍스트. [동기화]가 읽기전용 통합 토큰으로 기획안 페이지를 직접 읽어,
기존 SYNC 파서(LLM)가 스키마 JSON으로 정리하도록 넘긴다. 표준 라이브러리만 사용.

블록을 문서 순서대로 순회하며 제목·문단·목록·토글·인용·표·코드(대본)를 텍스트로 펼친다.
자식 블록은 재귀 수집(페이지네이션 포함). 이미지 등 텍스트 없는 블록은 건너뛴다.
"""
from __future__ import annotations

import json
import urllib.request

from . import config

_API = "https://api.notion.com/v1/"
_VER = "2022-06-28"


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(
        _API + path,
        headers={"Authorization": f"Bearer {token}", "Notion-Version": _VER},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _rt(arr) -> str:
    """rich_text 배열 → 평문."""
    return "".join(t.get("plain_text", "") for t in (arr or []))


def _children(block_id: str, token: str) -> list[dict]:
    """블록의 자식 전부 (100개 페이지네이션)."""
    out, cur = [], None
    while True:
        q = f"blocks/{block_id}/children?page_size=100"
        if cur:
            q += f"&start_cursor={cur}"
        d = _get(q, token)
        out += d.get("results", [])
        if d.get("has_more"):
            cur = d.get("next_cursor")
        else:
            break
    return out


def _render(blocks: list[dict], token: str) -> str:
    lines: list[str] = []
    for b in blocks:
        t = b.get("type")
        data = b.get(t) or {}
        txt = _rt(data.get("rich_text"))
        if t in ("heading_1", "heading_2", "heading_3"):
            lines.append("\n" + "#" * int(t[-1]) + " " + txt)
        elif t == "paragraph":
            if txt:
                lines.append(txt)
        elif t == "bulleted_list_item":
            lines.append("- " + txt)
        elif t == "numbered_list_item":
            lines.append("- " + txt)
        elif t == "to_do":
            lines.append("- " + txt)
        elif t == "toggle":
            if txt:
                lines.append("\n### " + txt)     # 토글 제목(줄거리/회차 분배) = 소제목처럼
        elif t == "quote":
            lines.append("> " + txt)
        elif t == "callout":
            if txt:
                lines.append(txt)
        elif t == "code":
            if txt:
                lines.append(txt)                 # 코드블록 내용(대본)
        elif t == "table_row":
            cells = data.get("cells") or []
            lines.append(" | ".join(_rt(c) for c in cells))
        elif t == "child_page":
            lines.append("\n# " + (data.get("title") or ""))
        # table/column_list/column/divider/image 등은 텍스트 없음 → 자식만 재귀
        else:
            if txt:
                lines.append(txt)
        if b.get("has_children"):
            child = _render(_children(b["id"], token), token)
            if child.strip():
                lines.append(child)
    return "\n".join(lines)


def page_text(page_id: str, token: str | None = None) -> str:
    """페이지 전체를 평문 텍스트로. 토큰 없으면 config.NOTION_TOKEN 사용."""
    token = token or config.NOTION_TOKEN
    if not token:
        raise RuntimeError("NOTION_TOKEN 미설정")
    return _render(_children(page_id, token), token).strip()


def page_last_edited(page_id: str, token: str | None = None) -> str:
    """페이지 마지막 수정 시각(ISO). 변경 감지용 — 싼 단일 호출."""
    token = token or config.NOTION_TOKEN
    if not token:
        raise RuntimeError("NOTION_TOKEN 미설정")
    return (_get(f"pages/{page_id}", token) or {}).get("last_edited_time", "")
