#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""내부 뷰어 빌드 — reference_db.json + filters.json → 뷰어 HTML.

  python3 reference/build_viewer.py                      # 평문 (개발용) → reference/viewer.html
  python3 reference/build_viewer.py --password 'xxx'     # 암호화 배포 → docs/index.html
  STORY_VIEWER_PW=xxx python3 reference/build_viewer.py --deploy   # 환경변수로 비번 (히스토리에 안 남김)

암호화: PBKDF2-HMAC-SHA256(200k) → AES-256-GCM. 뷰어 JS의 Web Crypto와 대칭.
배포 전 필독: repo가 public이면 reference/*.json 원본이 그대로 노출되어 게이트가 무의미하다.
암호화 배포는 repo를 private으로 전환한 뒤 할 것 (docs/index.html만 ciphertext).
"""
import argparse
import base64
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "reference_db.json")
FILTERS = os.path.join(BASE, "filters.json")
TEMPLATE = os.path.join(BASE, "viewer_template.html")
PLAIN_OUT = os.path.join(BASE, "viewer.html")
# Cloudflare Pages 배포 디렉터리 — 이 폴더가 pages.dev 루트. index.html이 내부 뷰어.
# docs/(GitHub Pages 공개 뷰어)와 완전 분리. 접근 제어는 Cloudflare Access(문 앞 인증)가 담당.
DEPLOY_OUT = os.path.join(os.path.dirname(BASE), "cloudflare", "index.html")
MARKER = "/*__PAYLOAD__*/null"
ITER = 200_000


BL_IDS = {"7544952986594741559", "7644648326046043406"}  # 실사 BL(게이물) — genre 컬럼 없어 수동
GL_IDS = {"7644620832018418960"}                          # 실사 GL


def orient_of(rec):
    """장르(성향): BL / GL / 남녀. genre 컬럼(bl/gl) 우선, 없으면 수동 세트, 기본 남녀."""
    g = (rec.get("genre") or "").lower()
    if g == "bl" or rec["id"] in BL_IDS:
        return "BL"
    if g == "gl" or rec["id"] in GL_IDS:
        return "GL"
    return "남녀"


def trim(rec):
    """뷰어에 필요한 필드만 (전체 원문·legacy_tags 등 제외해 페이로드 축소)."""
    return {
        "id": rec["id"], "url": rec.get("url", ""), "author": rec.get("author", ""),
        "desc": rec.get("desc", ""), "rank": rec.get("rank"),
        "crawl_date": rec.get("crawl_date", ""), "publish_dt": rec.get("publish_dt", ""),
        "region": rec.get("region", "서양"),
        "make": "AI" if rec.get("content_type") == "ai_generated" else "실사",
        "orient": orient_of(rec),
        "platform": "유튜브" if rec.get("platform") == "youtube" else "틱톡",
        "genre": rec.get("genre", ""),
        "metrics": {k: rec["metrics"].get(k) for k in
                    ("er", "save_rate", "views", "likes", "shares", "dur", "cut_count", "avg_cut")},
        "transcript_form": rec.get("transcript_form"),
        "transcript_raw": (rec.get("transcript_raw") or "")[:1],  # 존재 여부만 (원문은 뷰어에 불필요)
        "script": rec.get("script") or [],
        "script_len": sum(len(l.get("line", "")) for l in (rec.get("script") or [])),
        "scenes": rec.get("scenes") or [],
        "hook_desc": rec.get("hook_desc") or "",
        "context": rec.get("context") or "",
        "cats": rec.get("cats") or {},
        "tags": rec.get("tags") or {},
        "tag_confidence": rec.get("tag_confidence"),
        "tag_notes": rec.get("tag_notes") or "",
        "needs_review": bool(rec.get("needs_review")),
    }


def build_payload():
    db = [trim(r) for r in json.load(open(DB, encoding="utf-8"))]
    filters = json.load(open(FILTERS, encoding="utf-8"))
    return {"data": db, "filters": filters}


def encrypt(payload, password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from hashlib import pbkdf2_hmac
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = pbkdf2_hmac("sha256", password.encode(), salt, ITER, 32)
    pt = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ct = AESGCM(key).encrypt(iv, pt, None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"enc": {"salt": b64(salt), "iv": b64(iv), "ct": b64(ct), "iter": ITER}}


def _tpl():
    tpl = open(TEMPLATE, encoding="utf-8").read()
    assert MARKER in tpl, "viewer_template.html에 /*__PAYLOAD__*/null 마커가 없음"
    return tpl


def write_baked(payload_obj, out):
    """페이로드를 HTML에 구워넣기 (로컬 file:// 미리보기·AES 단일파일용)."""
    html = _tpl().replace(MARKER, json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":")))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)


def write_split(data_obj, html_out, data_out):
    """데이터 분리형 — HTML은 껍데기(PAYLOAD=null→data.json fetch), 데이터는 별도 파일.
    갱신 시 data.json만 바꾸면 되고, Cloudflare가 push마다 재빌드하면 로컬 빌드도 불필요."""
    os.makedirs(os.path.dirname(html_out), exist_ok=True)
    with open(html_out, "w", encoding="utf-8") as f:
        f.write(_tpl())  # 마커 그대로 → 런타임에 ./data.json fetch
    with open(data_out, "w", encoding="utf-8") as f:
        json.dump(data_obj, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--password", default=os.environ.get("STORY_VIEWER_PW", ""),
                    help="AES 암호화 비밀번호(선택적 이중잠금). 환경변수 STORY_VIEWER_PW도 가능")
    ap.add_argument("--deploy", action="store_true",
                    help="Cloudflare 배포 빌드(데이터 분리형) → cloudflare/index.html + data.json")
    a = ap.parse_args()

    payload = build_payload()
    n = len(payload["data"])
    root = os.path.dirname(BASE)
    data_out = os.path.join(os.path.dirname(DEPLOY_OUT), "data.json")

    if a.deploy:
        # 데이터 분리형: 데이터만 바꿔도 사이트가 갱신됨. Access가 접근 제어.
        data_obj = encrypt(payload, a.password) if a.password else {"plain": payload}
        write_split(data_obj, DEPLOY_OUT, data_out)
        mode = f"AES-256-GCM(PBKDF2 {ITER})" if a.password else "평문"
        print(f"Cloudflare 배포 빌드({mode}): {n}편 → "
              f"{os.path.relpath(DEPLOY_OUT, root)} + {os.path.relpath(data_out, root)}")
        print("→ 갱신: data.json만 재생성·push. 접근 제어는 Cloudflare Access. cloudflare/README.md 참고.")
    elif a.password:
        # 로컬 단일파일 AES (분리 안 함)
        write_baked(encrypt(payload, a.password), PLAIN_OUT)
        print(f"로컬 암호화 빌드: {n}편 → {os.path.relpath(PLAIN_OUT, root)} (AES-256-GCM)")
    else:
        # 로컬 미리보기 (file://로 바로 열림)
        write_baked({"plain": payload}, PLAIN_OUT)
        print(f"평문 빌드(개발용): {n}편 → {os.path.relpath(PLAIN_OUT, root)} — 로컬 미리보기용")


if __name__ == "__main__":
    main()
