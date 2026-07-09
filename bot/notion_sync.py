# -*- coding: utf-8 -*-
"""노션 페이지 → 평문 텍스트. [동기화]가 읽기전용 통합 토큰으로 기획안 페이지를 직접 읽어,
기존 SYNC 파서(LLM)가 스키마 JSON으로 정리하도록 넘긴다. 표준 라이브러리만 사용.

블록을 문서 순서대로 순회하며 제목·문단·목록·토글·인용·표·코드(대본)를 텍스트로 펼친다.
자식 블록은 재귀 수집(페이지네이션 포함). 이미지 등 텍스트 없는 블록은 건너뛴다.
"""
from __future__ import annotations

import json
import logging
import urllib.request

from . import config

log = logging.getLogger("co-writer")

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


def _post(path: str, body: dict, token: str) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        _API + path, data=data, method="POST",
        headers={"Authorization": f"Bearer {token}", "Notion-Version": _VER,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _page_title(page: dict) -> str:
    """페이지 객체 → 제목(작품명). properties 중 type=='title' 인 것의 평문."""
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return _rt(prop.get("title")).strip()
    return ""


def page_title(page_id: str, token: str | None = None) -> str:
    """페이지 제목 = 작품명 후보. 읽기 성공 = 통합(MCP) 연결됨 확인도 겸함."""
    token = token or config.NOTION_TOKEN
    if not token:
        raise RuntimeError("NOTION_TOKEN 미설정")
    return _page_title(_get(f"pages/{page_id}", token) or {})


def search_pages(token: str | None = None) -> list[dict]:
    """통합(토큰)에 연결·공유된 모든 페이지 → [{id, title, last_edited}].
    실무자가 노션 페이지를 통합에 연결하면 여기 잡힘 = 자동 작품 후보.
    (DB·아카이브·제목 없는 페이지는 제외)"""
    token = token or config.NOTION_TOKEN
    if not token:
        raise RuntimeError("NOTION_TOKEN 미설정")
    out, cur = [], None
    while True:
        body = {"filter": {"property": "object", "value": "page"}, "page_size": 100}
        if cur:
            body["start_cursor"] = cur
        d = _post("search", body, token)
        for pg in d.get("results", []):
            if pg.get("archived") or pg.get("in_trash"):
                continue
            title = _page_title(pg)
            if not title:
                continue
            out.append({"id": (pg.get("id") or "").replace("-", ""),
                        "title": title, "last_edited": pg.get("last_edited_time", "")})
        if d.get("has_more"):
            cur = d.get("next_cursor")
        else:
            break
    return out


def page_last_edited(page_id: str, token: str | None = None) -> str:
    """페이지 마지막 수정 시각(ISO). 변경 감지용 — 싼 단일 호출."""
    token = token or config.NOTION_TOKEN
    if not token:
        raise RuntimeError("NOTION_TOKEN 미설정")
    return (_get(f"pages/{page_id}", token) or {}).get("last_edited_time", "")


# ── 쓰기: 페이지에 마크다운을 노션 블록으로 append ─────────────────────────

def extract_page_id(url_or_id: str) -> str | None:
    """노션 URL/ID에서 32자리 페이지 id 추출 (대시 유무·타이틀 슬러그 무관)."""
    import re
    m = re.search(r"([0-9a-fA-F]{32})", (url_or_id or "").replace("-", ""))
    return m.group(1) if m else None


def _rich(text: str) -> list:
    """**볼드** 파싱 → rich_text 배열. 세그먼트당 2000자 제한 청킹."""
    import re
    out = []
    for i, seg in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if not seg:
            continue
        bold = (i % 2 == 1)
        for j in range(0, len(seg), 1900):
            out.append({"type": "text", "text": {"content": seg[j:j + 1900]},
                        "annotations": {"bold": bold}})
    return out or [{"type": "text", "text": {"content": " "}}]


def _md_to_blocks(md: str) -> list:
    """간단 마크다운 → 노션 블록. ## 제목·- 불릿·나머지 문단. (기획안 구조용)"""
    blocks = []
    for line in (md or "").split("\n"):
        s = line.rstrip()
        if not s.strip():
            continue
        if s.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3",
                           "heading_3": {"rich_text": _rich(s[4:])}})
        elif s.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                           "heading_2": {"rich_text": _rich(s[3:])}})
        elif s.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1",
                           "heading_1": {"rich_text": _rich(s[2:])}})
        elif s.lstrip()[:2] in ("- ", "* "):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _rich(s.lstrip()[2:])}})
        else:
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _rich(s)}})
    return blocks


def _patch(path: str, body: dict, token: str) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        _API + path, data=data, method="PATCH",
        headers={"Authorization": f"Bearer {token}", "Notion-Version": _VER,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def clear_children(page_id: str, token: str | None = None) -> None:
    """페이지의 최상위 블록을 전부 archive(비우기). 일부 실패해도 계속."""
    token = token or config.NOTION_TOKEN
    ids = [b["id"] for b in _children(page_id, token)]
    for bid in ids:
        try:
            _patch(f"blocks/{bid}", {"archived": True}, token)
        except Exception:
            log.warning("블록 archive 실패: %s", bid)


def replace_markdown(page_id: str, md: str, token: str | None = None) -> None:
    """페이지를 비우고 마크다운을 새로 기록 — 반복 수정 시 중복 없이 최신본으로 교체."""
    clear_children(page_id, token)
    append_markdown(page_id, md, token)


def replace_from_section(page_id: str, new_md: str, section_idx: int, token: str | None = None) -> None:
    """section_idx번째 '## 섹션' heading부터 페이지 끝까지 비우고, new_md의 그 섹션부터 새로 기록.
    앞 섹션(안 바뀐)은 건드리지 않음. (heading_2 = 기획안 5섹션 마커)"""
    import re
    token = token or config.NOTION_TOKEN
    children = _children(page_id, token)
    h2 = [i for i, b in enumerate(children) if b.get("type") == "heading_2"]
    if section_idx < len(h2):                       # 그 섹션 heading부터 끝까지 archive
        for b in children[h2[section_idx]:]:
            try:
                _patch(f"blocks/{b['id']}", {"archived": True}, token)
            except Exception:
                log.warning("블록 archive 실패: %s", b.get("id"))
    parts = [p for p in re.split(r"(?m)(?=^##\s)", new_md) if p.strip()]
    tail = "\n".join(parts[section_idx:]) if section_idx < len(parts) else ""
    if tail:
        append_markdown(page_id, tail, token)


def append_markdown(page_id: str, md: str, token: str | None = None) -> None:
    """마크다운을 노션 블록으로 변환해 페이지 하위에 append. 100블록씩 나눠 PATCH.
    권한/연결 부족 시 HTTPError를 그대로 올림(호출부에서 안내)."""
    token = token or config.NOTION_TOKEN
    if not token:
        raise RuntimeError("NOTION_TOKEN 미설정")
    blocks = _md_to_blocks(md)
    for i in range(0, len(blocks), 100):
        chunk = blocks[i:i + 100]
        data = json.dumps({"children": chunk}).encode("utf-8")
        req = urllib.request.Request(
            f"{_API}blocks/{page_id}/children", data=data, method="PATCH",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": _VER,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
