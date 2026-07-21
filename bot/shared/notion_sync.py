# -*- coding: utf-8 -*-
"""노션 페이지 → 평문 텍스트. [동기화]가 읽기전용 통합 토큰으로 기획안 페이지를 직접 읽어,
기존 SYNC 파서(LLM)가 스키마 JSON으로 정리하도록 넘긴다. 표준 라이브러리만 사용.

블록을 문서 순서대로 순회하며 제목·문단·목록·토글·인용·표·코드(대본)를 텍스트로 펼친다.
자식 블록은 재귀 수집(페이지네이션 포함). 이미지 등 텍스트 없는 블록은 건너뛴다.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request

from .. import config

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


_SECTION_LABELS = {
    "진행상태", "로그라인", "키워드", "타겟층", "핵심정서", "금지사항",
    "강도", "줄거리", "등장인물", "인물", "캐릭터", "회차분배",
    "회차 분배", "개요", "대본", "상세콘티", "상세 콘티", "콘티",
}


def _paragraph_is_section_label(data: dict, txt: str) -> bool:
    """노션에서 제목 블록 대신 '굵은 문단'으로 만든 섹션명도 헤딩으로 보존.

    실사용 페이지는 `heading_2`가 아니라 굵은 paragraph로 `줄거리`, `등장인물`을
    적는 경우가 많다. 평문 변환 때 annotations를 버리면 구조가 사라져 SYNC 파서가
    문서 전체를 한 덩어리로 보게 되므로, 짧은 굵은 문단/대표 라벨은 소제목으로 승격한다.
    """
    clean = txt.strip().strip(":：").strip()
    if not clean or len(clean) > 40:
        return False
    if clean in _SECTION_LABELS:
        return True
    rich = data.get("rich_text") or []
    visible = [r for r in rich if (r.get("plain_text") or "").strip()]
    return bool(visible) and all((r.get("annotations") or {}).get("bold") for r in visible)


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
                if _paragraph_is_section_label(data, txt):
                    lines.append("\n## " + txt.strip().strip(":：").strip())
                else:
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


_FILE_TYPES = ("file", "pdf")


def _walk_files(blocks: list[dict], token: str, out: list[dict]) -> None:
    for b in blocks:
        t = b.get("type")
        if t in _FILE_TYPES:
            data = b.get(t) or {}
            url = (data.get("file") or {}).get("url") or (data.get("external") or {}).get("url")
            name = data.get("name") or _rt(data.get("caption")) or "attachment"
            if url:
                out.append({"name": name, "url": url, "type": t})
        if b.get("has_children"):
            _walk_files(_children(b["id"], token), token, out)


def list_files(page_id: str, token: str | None = None) -> list[dict]:
    """페이지(하위 블록 전체 재귀) 안에 첨부된 file/pdf 블록 목록. [{name, url, type}].
    url은 Notion이 주는 그대로(대개 AWS 프리사인 — 별도 인증 헤더 없이 다운로드 가능,
    다만 시간 제한이 있어 받는 즉시 써야 함)."""
    token = token or config.NOTION_TOKEN
    if not token:
        raise RuntimeError("NOTION_TOKEN 미설정")
    out: list[dict] = []
    _walk_files(_children(page_id, token), token, out)
    return out


def download_file(url: str, timeout: int = 60) -> bytes:
    """file/pdf 블록의 url 다운로드(원본 bytes)."""
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def page_text(page_id: str, token: str | None = None) -> str:
    """페이지 전체를 평문 텍스트로. 토큰 없으면 config.NOTION_TOKEN 사용."""
    token = token or config.NOTION_TOKEN
    if not token:
        raise RuntimeError("NOTION_TOKEN 미설정")
    return _render(_children(page_id, token), token).strip()


# ★2026-07-20 "대본 N화"뿐 아니라 "N화 대본" 어순도 인식 — 사용자가 노션에서 토글 제목을
# "1화 대본"으로 쓰는 경우가 흔한데(스크린샷 실측), 예전엔 "대본 N화"만 잡아서 이 토글이
# 대본 섹션으로 인식되지 않아 "N화:" 콜론 헤딩 폴백(②)에만 의존해 취약했다.
_EP_SCRIPT_HEAD = re.compile(r"^\s*#*\s*(?:대본\s*(\d+)\s*화|(\d+)\s*화\s*대본)\b.*$", re.M)
_EP_COLON_HEAD = re.compile(r"^\s*#{1,3}\s*.*?\b(\d+)\s*화\s*:.*$", re.M)
_ANY_HEAD_LINE = re.compile(r"^\s*#{1,3}\s.*$", re.M)


def parse_episode_scripts(full_text: str) -> dict[str, str]:
    """노션 페이지 텍스트에서 화별 대본을 전부 추출(co-writer-bot과 동일 로직, 2026-07-13).
    ① '## 대본 N화' 헤딩(다음 '대본 M화' 전까지) — 봇이 자동 push한 페이지.
    ② '### N화:' 헤딩 — 손으로 정리한 기획안 페이지. 이 헤딩과 다음 'M화:' 헤딩 사이에
       '연출 레퍼런스'·'콘티'·'스토리보드' 같은 무관한 소제목·토글이 섞여 있는 경우가 많아서,
       그 구간의 **마지막 소제목 줄 다음부터**를 실제 대본 본문으로 본다."""
    if not full_text:
        return {}
    out: dict[str, str] = {}
    heads1 = list(_EP_SCRIPT_HEAD.finditer(full_text))
    if heads1:
        for i, m in enumerate(heads1):
            end = heads1[i + 1].start() if i + 1 < len(heads1) else len(full_text)
            ep = m.group(1) or m.group(2)   # "대본 N화" 또는 "N화 대본" 어느 쪽이든
            out[f"{ep}화"] = full_text[m.start():end].strip()
        return out
    heads2 = list(_EP_COLON_HEAD.finditer(full_text))
    for i, m in enumerate(heads2):
        end = heads2[i + 1].start() if i + 1 < len(heads2) else len(full_text)
        window = full_text[m.end():end]
        inner = list(_ANY_HEAD_LINE.finditer(window))
        if inner:
            window = window[inner[-1].end():]
        text = window.strip()
        if text:
            out[f"{m.group(1)}화"] = text
    return out


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


def append_markdown(page_id: str, md: str, token: str | None = None,
                    after: str | None = None) -> None:
    """마크다운을 노션 블록으로 변환해 페이지 하위에 append. 100블록씩 나눠 PATCH.
    after: 그 블록 '바로 뒤'에 삽입(첫 청크만). 권한/연결 부족 시 HTTPError 그대로 올림."""
    token = token or config.NOTION_TOKEN
    if not token:
        raise RuntimeError("NOTION_TOKEN 미설정")
    blocks = _md_to_blocks(md)
    for i in range(0, len(blocks), 100):
        chunk = blocks[i:i + 100]
        body: dict = {"children": chunk}
        if after and i == 0:
            body["after"] = after
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{_API}blocks/{page_id}/children", data=data, method="PATCH",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": _VER,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()


def upsert_section(page_id: str, heading: str, body_md: str, token: str | None = None) -> None:
    """'## <heading>' 섹션을 업서트. 같은 제목 heading_2가 있으면 그 아래 내용만 교체,
    완전히 새 섹션이면 같은 종류(개요/대본 등 첫 단어가 같은) 마지막 섹션 바로 뒤에 삽입
    (없으면 페이지 끝). '개요 4화'가 '대본 3화' 뒤에 뜬금없이 붙지 않고 다른 개요들 옆에 모이게.
    (2026-07-16, 봇 합체 HANDOFF §3-2/§3-5 리스크 6: storyboard-bot 버전은 새 섹션을 항상
    페이지 끝에만 추가하는 단순 버전이었는데, 그걸 그대로 채택하면 노션 문서가 지저분해지는
    퇴행이라 co-writer-bot의 이 "같은 접두어끼리 묶어 삽입" 로직을 그대로 가져왔다 — 시그니처가
    두 버전 다 동일해 호출부 수정 없이 그대로 교체 가능했음.)"""
    import re
    token = token or config.NOTION_TOKEN
    if not token:
        raise RuntimeError("NOTION_TOKEN 미설정")
    norm = lambda s: re.sub(r"\s+", "", s or "").lower()
    tgt = norm(heading)
    children = _children(page_id, token)
    hidx = None
    for i, b in enumerate(children):
        if b.get("type") == "heading_2" and norm(_rt((b.get("heading_2") or {}).get("rich_text"))) == tgt:
            hidx = i
            break
    if hidx is None:                                  # 완전히 새 섹션
        prefix_m = re.match(r"^(\S+)\s", heading)      # 예: '개요 4화' → '개요'
        after_id = None
        if prefix_m:
            pfx = prefix_m.group(1)
            last_h2_idx = None
            for i, b in enumerate(children):
                if (b.get("type") == "heading_2"
                        and _rt((b.get("heading_2") or {}).get("rich_text")).strip().startswith(pfx)):
                    last_h2_idx = i
            if last_h2_idx is not None:                # 그 섹션(다음 heading_2 전까지)의 마지막 블록 뒤에
                j = last_h2_idx + 1
                while j < len(children) and children[j].get("type") != "heading_2":
                    j += 1
                after_id = children[j - 1]["id"]
        append_markdown(page_id, f"## {heading}\n{body_md}", token, after=after_id)
        return
    hid = children[hidx]["id"]                         # 있으면 그 heading 아래~다음 heading_2 전까지 비우고 교체
    for b in children[hidx + 1:]:
        if b.get("type") == "heading_2":
            break
        try:
            _patch(f"blocks/{b['id']}", {"archived": True}, token)
        except Exception:
            log.warning("섹션 블록 archive 실패: %s", b.get("id"))
    append_markdown(page_id, body_md, token, after=hid)


def _flatten(page_id: str, token: str) -> list[dict]:
    """페이지 전체를 문서 순서(pre-order)로 평탄화 — 중첩된 toggle/컬럼 등 안에 들어있는 블록도
    전부 포함한다. ★2026-07-15, "노션에 있는 상세 콘티를 못 읽는다" 버그: 기존엔
    _episode_section_bounds/find_conti_toggle_for_episode/find_authored_conti_for_episode가
    _children(page_id)로 얻은 **최상위 블록만** 훑어서, 화 헤딩이나 콘티 토글이 다른 토글 안에
    중첩돼 있으면(예: "3화" 토글 안에 "상세 콘티" 토글이 들어있는 구조) 아예 못 찾고 조용히
    None을 반환했다. 이 함수로 페이지 트리 전체를 한 번 펼쳐서 그 문제를 없앤다."""
    out: list[dict] = []

    def walk(blocks: list[dict]) -> None:
        for b in blocks:
            out.append(b)
            if b.get("has_children"):
                walk(_children(b["id"], token))

    walk(_children(page_id, token))
    return out


def _episode_section_bounds(children: list[dict], episode: int):
    """children(페이지 최상위 블록)에서 '{episode}화' 헤딩 위치(ep_start)와 그 섹션이 끝나는
    다음 화 헤딩 위치(ep_end, 없으면 len(children))를 찾는다. ep_start가 None이면 그 화 헤딩이
    페이지에 없다는 뜻(호출부에서 안전 폴백 처리).
    콜론 유무 둘 다 인정("1화:" 도, "개요 4화"/"대본 4화"처럼 콜론 없는 것도) — 콜론을 필수로
    요구하면 "N화:" 포맷이 아닌 페이지(예: cony 테스트 작품)에서 아예 못 찾아 매번 새 토글이
    중복 추가되는 버그가 있었음(2026-07-13). "1~3화 개요 및 대본"처럼 여러 화를 묶은 범위
    헤딩은 제외(2026-07-13 — 이걸 "3화" 매치로 잘못 집어서 실제 "3화:" 섹션을 못 찾던 버그가
    있었음). 같은 화를 가리키는 헤딩이 여러 개(개요/대본 분리 등)면 더 뒤에 나온 걸 우선."""
    import re

    def _block_text(b: dict) -> str:
        t = b.get("type", "")
        return _rt((b.get(t) or {}).get("rich_text")) if t else ""

    ep_re = re.compile(rf"\b{episode}\s*화\b")
    any_ep_re = re.compile(r"\b\d+\s*화\b")
    range_re = re.compile(r"\d+\s*[~\-]\s*\d+\s*화")
    conti_re = re.compile(r"콘티|스토리보드")
    # heading 블록뿐 아니라 toggle 제목으로 화를 구분하는 페이지도 있어서(2026-07-15) toggle도
    # 화 경계 후보로 인정한다 — "3화" 자체가 heading이 아니라 toggle 제목인 경우를 놓치던 버그.
    # 단, "상세 콘티 (N화)"/"콘티(러프 ver.)_N화" 같은 콘티 토글은 제목에 "N화"가 들어있어도
    # 화 섹션의 경계가 아니라 그 섹션의 '내용'이다 — 이걸 경계 후보로 잘못 인정하면(2026-07-15
    # toggle 인정 이후 발견), 이미 콘티 토글이 있는 상태에서 다음 저장 때 matches[-1]("가장
    # 나중 것") 이 콘티 토글 자신을 ep_start로 잡아버려서, tog_idx 탐색 구간(ep_start+1~ep_end)이
    # 그 콘티 토글 바로 다음부터 시작 → 기존 토글을 절대 못 찾고 매번 옆에 새 토글만 추가하는
    # (덮어쓰기가 안 되고 중복만 쌓이는) 버그의 원인이었다. 콘티/스토리보드 토글은 경계 후보에서 제외.
    headings = [(i, _block_text(b)) for i, b in enumerate(children)
                if (b.get("type", "").startswith("heading") or b.get("type") == "toggle")
                and not (b.get("type") == "toggle" and conti_re.search(_block_text(b)))]
    matches = [i for i, txt in headings if ep_re.search(txt) and not range_re.search(txt)]
    if not matches:
        return None, None
    ep_start = matches[-1]
    ep_end = next((j for j, txt in headings if j > ep_start and any_ep_re.search(txt)), len(children))
    return ep_start, ep_end


def _find_episode_container(page_id: str, token: str, episode: int):
    """'{episode}화' 섹션이 실제로 들어있는 컨테이너(페이지 자신 또는 중첩된 toggle 등)를 찾는다.
    ★2026-07-15: upsert_conti_toggle_for_episode가 _children(page_id)만 훑어서(최상위만),
    화 섹션 전체가 다른 토글 안에 중첩돼 있으면 ep_start를 못 찾아 항상 '헤딩 없음' 폴백을 타
    페이지 최상위 끝에 새 콘티 토글을 매번 추가하기만 하고 기존(중첩된) 토글은 그대로 남아
    "덮어쓰기가 안 된다"는 버그가 있었다. Notion API는 PATCH의 'after'가 반드시 삽입할 위치와
    같은 부모의 형제 블록이어야 하므로(다른 부모 안 블록 id를 top-level PATCH의 after로 못 씀),
    _flatten()처럼 전체를 평탄화만 하면(인덱스가 형제 관계를 보장 못 함) archive+재삽입 위치가
    깨진다. 그래서 각 컨테이너의 '직계 자식'만으로 _episode_section_bounds를 검사하며 트리를
    내려가, 실제로 화 헤딩/토글이 형제로 들어있는 컨테이너 자체를 찾는다 — 그 컨테이너의
    children/ep_start/ep_end는 전부 같은 부모의 형제 목록이므로 기존 archive+재삽입 로직을
    그대로 쓸 수 있고, PATCH만 blocks/{container_id}/children으로 하면 된다.
    반환: (container_id, container의 children, ep_start, ep_end). 페이지 전체 어디서도
    못 찾으면 None."""
    kids = _children(page_id, token)
    ep_start, ep_end = _episode_section_bounds(kids, episode)
    if ep_start is not None:
        return page_id, kids, ep_start, ep_end
    for b in kids:
        if b.get("has_children"):
            found = _find_episode_container(b["id"], token, episode)
            if found:
                return found
    return None


def find_conti_toggle_for_episode(page_id: str, episode: int, token: str | None = None) -> str | None:
    """그 화의 '상세 콘티 (N화)' 토글 블록 안에 든 문단들을 줄바꿈으로 합쳐 반환. 없으면 None."""
    import re
    token = token or config.NOTION_TOKEN
    if not token:
        raise RuntimeError("NOTION_TOKEN 미설정")

    def _block_text(b: dict) -> str:
        t = b.get("type", "")
        return _rt((b.get(t) or {}).get("rich_text")) if t else ""

    conti_heading = f"상세 콘티 ({episode}화)"
    norm = lambda s: re.sub(r"\s+", "", s or "")
    scene_hdr_re = re.compile(r"■\s*씬\s*\d+")
    flat = _flatten(page_id, token)  # 다른 토글 안에 중첩돼 있어도 찾도록 전체 트리 평탄화(2026-07-15)
    candidates = [b for b in flat
                  if b.get("type") == "toggle" and norm(_block_text(b)) == norm(conti_heading)]
    if not candidates:
        return None

    # 2026-07-21: 같은 이름("상세 콘티 (N화)")의 토글이 페이지에 두 개 이상 존재할 수 있다
    # (예: 예전에 검증 없이 저장된 거절/오류 텍스트가 잔재로 남은 경우). 첫 매치를 무조건
    # 쓰면 잔재를 집어버릴 수 있으므로, 후보가 여럿이면 (1) 페이지 최상위 직속 토글을
    # (2) 씬 헤더(■ 씬N)가 실제로 들어있는 것을 (3) 가장 최근 수정된 것을 우선한다.
    def _rank(b: dict):
        kids = _children(b["id"], token) if b.get("has_children") else []
        parts = [_block_text(k) for k in kids if k.get("type") == "paragraph"]
        text = "\n".join(parts)
        is_top_level = b.get("parent", {}).get("type") == "page_id"
        has_scene_hdr = bool(scene_hdr_re.search(text))
        return (is_top_level, has_scene_hdr, b.get("last_edited_time", ""), text)

    ranked = sorted((_rank(b) for b in candidates), reverse=True)
    best_text = ranked[0][3]
    return best_text if best_text else None


def find_authored_conti_for_episode(page_id: str, episode: int, token: str | None = None) -> str | None:
    """봇이 만든 정확한 '상세 콘티 (N화)' 토글이 없을 때(2026-07-13) — 실무자가 노션에 직접
    쓴 상세 콘티도 인식하기 위한 폴백. 그 화 섹션 안에서 세 가지 형태를 다 찾는다:
    ① 제목에 '콘티'/'스토리보드'가 든 토글/헤딩(예: '콘티(러프 ver.)_1화') 안의 내용,
    ② 토글/헤딩 없이 그냥 code/paragraph 블록 하나가 "N화 콘티"로 시작하는 경우(실측 확인,
    날혐남 3화 — 실무자가 별도 컨테이너 없이 바로 그렇게 씀) 그 블록 전체.
    씬 구분은 '■ 씬N'뿐 아니라 실무자가 쓰는 'N. 제목' 번호 목록 형식도 인정. 후보가 여럿이면
    섹션에서 가장 나중에 나온 것(보통 최신/최종본)을 쓴다. 없으면 None."""
    import re
    token = token or config.NOTION_TOKEN
    if not token:
        raise RuntimeError("NOTION_TOKEN 미설정")

    def _block_text(b: dict) -> str:
        t = b.get("type", "")
        return _rt((b.get(t) or {}).get("rich_text")) if t else ""

    title_re = re.compile(r"콘티|스토리보드")
    bare_start_re = re.compile(r"^\s*\d*\s*화?\s*콘티")
    # 다른 토글 안에 중첩돼 있어도 찾도록 전체 트리 평탄화(2026-07-15, "이미 있는데 못 읽는다" 버그).
    children = _flatten(page_id, token)
    ep_start, ep_end = _episode_section_bounds(children, episode)
    if ep_start is None:
        return None

    candidates = []
    for i in range(ep_start + 1, ep_end):
        b = children[i]
        t = b.get("type", "")
        txt = _block_text(b)
        if t == "toggle" and title_re.search(txt):
            kids = _children(b["id"], token) if b.get("has_children") else []
            content = "\n".join(_block_text(k) for k in kids
                                 if k.get("type") in ("paragraph", "code", "bulleted_list_item",
                                                       "numbered_list_item", "quote", "toggle"))
        elif t.startswith("heading") and title_re.search(txt):
            end2 = next((j for j in range(i + 1, ep_end)
                         if children[j].get("type", "").startswith("heading")), ep_end)
            content = "\n".join(_block_text(children[j]) for j in range(i + 1, end2))
        elif t in ("code", "paragraph") and bare_start_re.match(txt):
            content = txt   # 컨테이너 없이 블록 자체가 "N화 콘티"로 시작 — 그 블록 전체가 내용
        else:
            continue
        # ★2026-07-15: 예전엔 "씬N"/"N. " 패턴이 없으면 이미 찾은 콘티도 통째로 버렸다(scene_re
        # 게이트) — 실무자가 다른 씬 구분 표기(예: "Scene 3", 그냥 프로즈)를 쓰면 콘티가 분명히
        # 있는데도 조용히 None이 되는 원인이었다. 내용이 비어있지만 않으면 그대로 채택한다.
        if content.strip():
            candidates.append(content)
    return candidates[-1] if candidates else None


def _conti_toggle_children(text: str) -> list:
    """콘티 텍스트를 줄 단위 문단 블록으로 — code 블록은 줄바꿈이 안 돼(고정폭·가로스크롤)
    실측으로 확인(2026-07-13), 그래서 문단(paragraph)으로 바꿔 화면 너비에 맞게 자동
    줄바꿈되게 한다. 빈 줄도 빈 문단으로 그대로 살려 씬 사이 간격을 유지."""
    return [{"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": _rich(line) if line.strip() else []}}
            for line in (text or "").split("\n")]


def upsert_conti_toggle_for_episode(page_id: str, episode: int, text: str, token: str | None = None) -> None:
    """상세 콘티를 그 '{episode}화' 섹션 바로 아래에 '상세 콘티 (N화)' 토글로 upsert — 토글
    안에 줄 단위 문단으로 들어가 접었다 펼 수 있고, 펼치면 클릭 없이 화면 너비로 줄바꿈되며
    바로 읽힌다. 이미 그 화 밑에 토글이 있으면 통째로 교체. 화 헤딩을 못 찾으면 페이지 끝에 추가."""
    import re
    token = token or config.NOTION_TOKEN
    if not token:
        raise RuntimeError("NOTION_TOKEN 미설정")

    def _block_text(b: dict) -> str:
        t = b.get("type", "")
        return _rt((b.get(t) or {}).get("rich_text")) if t else ""

    conti_heading = f"상세 콘티 ({episode}화)"
    norm = lambda s: re.sub(r"\s+", "", s or "")
    # ★2026-07-15: 화 섹션 전체가 다른 토글 안에 중첩돼 있으면 top-level _children()만으론
    # 못 찾아 매번 새 토글을 페이지 최상위에 추가하기만 하던 버그 — 실제로 화 섹션이 들어있는
    # 컨테이너(페이지 자신 또는 중첩 toggle)를 찾아 그 컨테이너 기준으로 upsert한다.
    found = _find_episode_container(page_id, token, episode)
    if found:
        container_id, children, ep_start, ep_end = found
    else:
        container_id, children, ep_start, ep_end = page_id, _children(page_id, token), None, None
    all_lines = _conti_toggle_children(text)
    toggle_block = {"object": "block", "type": "toggle",
                     "toggle": {"rich_text": _rich(conti_heading), "children": all_lines[:98]}}
    rest = all_lines[98:]   # children 100개 제한 — 첫 98줄만 생성과 함께, 나머지는 뒤이어 append

    tog_idx = None
    if ep_start is not None:
        tog_idx = next((k for k in range(ep_start + 1, ep_end)
                         if children[k].get("type") == "toggle"
                         and norm(_block_text(children[k])) == norm(conti_heading)), None)
    body: dict = {"children": [toggle_block]}
    if tog_idx is not None:                            # 이미 있음 — 그 토글 자체를 archive하고 같은 자리에 재삽입
        prev_id = children[tog_idx - 1]["id"] if tog_idx > 0 else None
        try:
            _patch(f"blocks/{children[tog_idx]['id']}", {"archived": True}, token)
        except Exception:
            log.warning("콘티 토글 archive 실패: %s", children[tog_idx].get("id"))
        if prev_id:
            body["after"] = prev_id
    elif ep_start is not None:                         # 없음 — 그 화 섹션 맨 끝에 새로 삽입
        body["after"] = children[ep_end - 1]["id"]
    # ep_start가 None이면(그 화 헤딩 자체가 없음) after 없이 컨테이너(보통 페이지) 맨 끝에 추가(안전 폴백)
    result = _patch(f"blocks/{container_id}/children", body, token)
    if rest:                                           # 100개 넘는 줄은 새 토글 안에 이어서 append
        toggle_id = result["results"][0]["id"]
        for i in range(0, len(rest), 100):
            _patch(f"blocks/{toggle_id}/children", {"children": rest[i:i + 100]}, token)
