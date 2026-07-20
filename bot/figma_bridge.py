# -*- coding: utf-8 -*-
"""피그마 브릿지 — 2026-07-20 신규.

영상화가 안전필터에 걸린 스틸컷을, 실무자가 직접 손볼 수 있게 피그마로 넘기는 기능의 봇 쪽 절반.

★핵심 제약: 피그마 REST API(https://api.figma.com)는 파일을 "읽는" 용도뿐이다 — 파일에 이미지
노드를 추가하는 쓰기 엔드포인트가 없다(피그마 앱 안에서 플러그인으로만 캔버스를 편집할 수 있다).
그래서 이 모듈은 이미지를 피그마에 직접 밀어 넣지 않는다. 대신:
1. enqueue()가 이미지+메타데이터를 로컬 큐 디렉터리(config.FIGMA_QUEUE_DIR)에 쌓아두고,
2. 이 모듈이 띄우는 작은 HTTP 서버(127.0.0.1:config.FIGMA_BRIDGE_PORT)가 그 큐를 JSON으로 노출하면,
3. 사용자가 피그마에 설치한 동반 플러그인(레포의 figma-plugin/co-writer-bridge/)이 그 서버를
   폴링해서 실제로 캔버스에 이미지를 얹는다.
봇 프로세스와 피그마 데스크톱 앱이 이 포트에 서로 접근 가능한 위치(보통 같은 머신)에 있어야
동작한다 — 다른 머신이면 SB_FIGMA_BRIDGE_PORT를 터널링하거나 포트를 열어줘야 한다.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config

log = logging.getLogger("storyboard-bot")


def _queue_dir() -> Path:
    d = Path(config.FIGMA_QUEUE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def enqueue(image_path: str, meta: dict) -> str:
    """스틸컷 PNG 하나를 큐에 올린다. 반환: item id(uuid). meta 예:
    {"work": "코니", "scene_num": 3, "cut_num": 5, "reason": "실존인물 안전필터"}."""
    item_id = uuid.uuid4().hex
    d = _queue_dir()
    png_bytes = Path(image_path).read_bytes()
    (d / f"{item_id}.png").write_bytes(png_bytes)
    (d / f"{item_id}.json").write_text(json.dumps({**meta, "id": item_id}, ensure_ascii=False),
                                       encoding="utf-8")
    log.info(f"피그마 큐에 추가: {item_id} ({meta})")
    return item_id


def _pending_items() -> list[dict]:
    d = _queue_dir()
    items = []
    for meta_path in sorted(d.glob("*.json")):
        png_path = meta_path.with_suffix(".png")
        if not png_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            log.exception(f"피그마 큐 메타 파싱 실패, 건너뜀: {meta_path}")
            continue
        image_b64 = base64.b64encode(png_path.read_bytes()).decode("ascii")
        items.append({**meta, "image_b64": image_b64})
    return items


def _ack(item_id: str) -> bool:
    d = _queue_dir()
    found = False
    for suffix in (".png", ".json"):
        p = d / f"{item_id}{suffix}"
        if p.exists():
            p.unlink()
            found = True
    return found


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):   # BaseHTTPRequestHandler는 기본으로 stderr에 찍음 — 로거로 통일
        log.info("figma_bridge http: " + fmt, *args)

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 피그마 플러그인 iframe(UI)이 로컬 파일/피그마 도메인에서 fetch하므로 CORS 허용.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):   # CORS preflight
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/pending":
            try:
                self._send_json(200, {"items": _pending_items()})
            except Exception:
                log.exception("피그마 큐 조회 실패")
                self._send_json(500, {"error": "internal error"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.startswith("/ack/"):
            item_id = self.path[len("/ack/"):]
            ok = _ack(item_id)
            self._send_json(200 if ok else 404, {"ok": ok})
        else:
            self._send_json(404, {"error": "not found"})


_server: ThreadingHTTPServer | None = None


def start_server() -> None:
    """config.FIGMA_BRIDGE_ENABLED일 때 app.py가 기동 시 1회 호출 — 백그라운드 스레드로
    로컬 HTTP 서버를 띄운다. 이미 떠 있으면 아무것도 안 한다."""
    global _server
    if _server is not None or not config.FIGMA_BRIDGE_ENABLED:
        return
    _server = ThreadingHTTPServer(("127.0.0.1", config.FIGMA_BRIDGE_PORT), _Handler)
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    log.info(f"피그마 브릿지 서버 시작: http://127.0.0.1:{config.FIGMA_BRIDGE_PORT} "
            f"(큐: {config.FIGMA_QUEUE_DIR})")
