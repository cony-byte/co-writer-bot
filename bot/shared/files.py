"""shared/files.py -- attachment/text-decoding helpers unified from
co-writer-bot and storyboard-bot. See extraction report for per-function
canonical-body decisions.
"""
import json
import os
import re
import urllib.request

from bot import config
from bot.shared.slack_io import log

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".tiff")
_REF_SAVE_EXTS = (".png", ".jpg", ".jpeg", ".webp")     # openrouter_image._REF_EXTS와 동일해야 함

# --- _files_text --- canonical: cowriter (mostly cosmetic; cowriter also logs a warning on empty hwpx extraction (storyboard silently continues) -- kept for observability. Also: storyboard's HTML-login detection uses `.startswith(...)` where cowriter uses `.find(...) >= 0` (checks anywhere in first 200 chars, not just the start) -- cowriter's is marginally more lenient/robust, kept as canonical.)
def _files_text(event: dict) -> tuple[str, int]:
    """메시지에 붙은 스니펫/텍스트/.hwpx 파일 내용을 봇 토큰으로 내려받아 합친다.
    반환: (내용, blocked) — blocked>0이면 권한 부족으로 로그인 HTML만 받은 것."""
    out, blocked = [], 0
    for f in event.get("files") or []:
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        name = (f.get("name") or "").lower()
        ftype = (f.get("filetype") or "").lower()
        mt = (f.get("mimetype") or "").lower()
        # 이미지는 텍스트로 디코딩하면 깨진 글자만 나옴 → 여기선 건너뜀([참조]가 별도로 다룸)
        if mt.startswith("image/") or ftype in {"png", "jpg", "jpeg", "webp", "gif", "bmp", "heic", "tiff"} \
                or name.endswith(_IMG_EXTS):
            continue
        try:
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
        except Exception:
            log.exception("첨부 파일 다운로드 실패")
            blocked += 1
            continue
        if name.endswith(".hwpx") or ftype == "hwpx":          # 신형 한글 = ZIP+XML → 텍스트 추출
            txt = _hwpx_text(data)
            if txt:
                out.append(txt)
            else:
                log.warning("hwpx 본문 추출 실패(빈 결과)")
            continue
        # ★2026-07-23 실사고: zip(.docx/.pdf 등 압축 기반 바이너리 포함)은 어떤 인코딩으로도
        # 정상 디코딩이 안 되는데, _decode_text가 최후엔 errors="replace"로 강제 디코딩해
        # 압축 바이트가 깨진 문자열(예: "PK�Q�\\...")로 그대로 "첨부 참고 자료"에
        # 섞여 나갔다(CapCut draft zip 첨부 시 실측). hwpx처럼 알려진 텍스트 추출법이 없는
        # zip 계열은 안전하게 건너뛴다(이미지와 동일한 이유).
        if name.endswith((".zip", ".docx", ".pptx", ".xlsx")) or ftype == "zip" or data[:2] == b"PK":
            log.info("텍스트 추출 미지원 압축 파일 건너뜀(zip 계열): %s", name or ftype)
            continue
        body = _decode_text(data)
        if body.lstrip()[:200].lower().find("<!doctype html") >= 0 or body.lstrip().lower().startswith("<html"):
            log.warning("첨부 다운로드가 로그인 HTML 반환 — files:read 권한 필요")
            blocked += 1
            continue
        out.append(body)
    return "\n".join(out).strip(), blocked

# --- _image_files --- canonical: storyboard (REAL DIFF (HANDOFF correctly flagged this): cowriter returns 3-tuples (stem, ext, data), storyboard returns 4-tuples (stem, ext, data, url) -- the extra url lets a confirm-card recover the image after a restart. Adopted storyboard's superset. WARNING: cowriter's own (dead-code, see gap report) `_do_ref` unpacks this with `for stem, ext, data in imgs` (exactly 3) -- would raise ValueError if that dead code is ever revived against the shared 4-tuple version. Flagged, not silently fixed.)
def _image_files(event: dict) -> list[tuple[str, str, bytes, str]]:
    """첨부 이미지를 내려받아 [(원본파일명 stem, 확장자, bytes, 원본 url)]. 참조로 못 쓰는 형식(gif/heic 등)은 제외.
    url도 같이 들고 다니는 이유(2026-07-13): 확정 대기 상태가 재시작으로 날아가도, 확정 버튼
    자체에 심어둔 이 url로 이미지를 다시 받아 재요청 없이 복구할 수 있게 하기 위함."""
    out = []
    for f in event.get("files") or []:
        name = f.get("name") or ""
        ftype = (f.get("filetype") or "").lower()
        mt = (f.get("mimetype") or "").lower()
        ext = os.path.splitext(name)[1].lower()
        if not ext and ftype:
            ext = "." + ftype
        if ext == ".jpeg":
            ext = ".jpg"
        is_img = mt.startswith("image/") or ftype in {"png", "jpg", "jpeg", "webp"} or ext in _REF_SAVE_EXTS
        if not is_img or ext not in _REF_SAVE_EXTS:
            continue
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
        except Exception:
            log.exception("이미지 첨부 다운로드 실패")
            continue
        stem = os.path.splitext(os.path.basename(name))[0] or "ref"
        out.append((stem, ext, data, url))
    return out

# --- _decode_text --- canonical: cowriter (cosmetic-only (docstring))
def _decode_text(data: bytes) -> str:
    """첨부 텍스트 디코딩. 한글 .txt는 윈도우 저장 시 CP949/EUC-KR가 흔해
    UTF-8만 쓰면 다 깨진다 → BOM·UTF-8 → CP949 → UTF-16 순으로 시도."""
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr", "utf-16"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")   # 최후: 깨져도 최대한

# --- _hwpx_text --- canonical: cowriter (cosmetic-only (storyboard is a pure refactor, same logic))
def _hwpx_text(raw: bytes) -> str:
    """.hwpx(ZIP+XML, OWPML) → 본문 텍스트만. 표준 라이브러리만 사용(서식·표는 버림).
    본문은 Contents/section*.xml 의 <hp:t> 런에 있고 문단은 <hp:p>. 실패 시 빈 문자열."""
    import io
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception:
        return ""
    names = sorted(n for n in zf.namelist()
                   if re.match(r"Contents/section\d+\.xml$", n))
    chunks = []
    for n in names:
        try:
            xml = zf.read(n).decode("utf-8", "replace")
        except Exception:
            continue
        xml = re.sub(r"</(?:\w+:)?p>", "\n", xml)                       # 문단 끝 → 줄바꿈
        xml = re.sub(r"<(?:\w+:)?t>(.*?)</(?:\w+:)?t>", r"\1", xml, flags=re.S)  # 텍스트 런 언랩
        xml = re.sub(r"<[^>]+>", "", xml)                               # 나머지 태그 제거(줄바꿈 유지)
        for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
                     ("&quot;", '"'), ("&apos;", "'")):
            xml = xml.replace(a, b)
        lines = [ln.strip() for ln in xml.split("\n")]
        text = "\n".join(ln for ln in lines if ln)                      # 빈 줄 정리, 문단 유지
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()

# --- _parse_json_array --- canonical: storyboard+dep (REAL DIFF: storyboard adds a `_repair_json_quotes` retry when the first json.loads fails; cowriter just raises. Adopted storyboard's superset. Requires bringing the (storyboard-only, non-colliding) helper `_repair_json_quotes` into shared/files.py too, since dispatch_storyboard.py's `_parse_json_object` also depends on it.)
def _parse_json_array(text):
    t = str(text).strip()
    s, e = t.find("["), t.rfind("]")
    if s == -1 or e == -1 or e < s:
        raise ValueError("응답에서 JSON 배열([...])을 못 찾았어요. (콘티가 너무 길거나 시간초과일 수 있어요)")
    body = t[s:e + 1]
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return json.loads(_repair_json_quotes(body))

# --- _repair_json_quotes --- relocated from storyboard-bot/app.py (no cowriter equivalent); shared because both _parse_json_array (here) and dispatch_storyboard.py's _parse_json_object depend on it.
def _repair_json_quotes(s):
    """문자열 값 안에 이스케이프 안 된 큰따옴표(예: 대사 "…")를 복구.
    문자열 안의 " 는 바로 뒤 첫 비공백이 , : ] } (또는 끝)이면 정상 종료, 그 외면 내용 따옴표로 보고 이스케이프."""
    out, in_str, i, n = [], False, 0, len(s)
    while i < n:
        c = s[i]
        if not in_str:
            out.append(c)
            if c == '"':
                in_str = True
        else:
            if c == '\\' and i + 1 < n:
                out.append(c); out.append(s[i + 1]); i += 2; continue
            if c == '"':
                j = i + 1
                while j < n and s[j] in ' \t\r\n':
                    j += 1
                if j >= n or s[j] in ',:]}':
                    out.append(c); in_str = False
                else:
                    out.append('\\"')
            else:
                out.append(c)
        i += 1
    return ''.join(out)

