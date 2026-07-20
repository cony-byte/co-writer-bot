# -*- coding: utf-8 -*-
"""피그마 브릿지 — 2026-07-20 신규, 2026-07-20b 되돌리기 경로 추가.

영상화가 안전필터에 걸린 스틸컷을, 실무자가 직접 손볼 수 있게 피그마로 넘기고, 손본 결과를
다시 봇으로 되돌려 그 컷의 스틸컷 파일에 반영하는 기능의 봇 쪽 절반.

★핵심 제약: 피그마 REST API(https://api.figma.com)는 파일을 "읽는" 용도뿐이다 — 파일에 이미지
노드를 추가/변경하는 쓰기 엔드포인트가 없다(피그마 앱 안에서 플러그인으로만 캔버스를 편집할 수
있다). 그래서 이 모듈은 캔버스를 직접 건드리지 않는다. 대신 로컬 큐 3단계로 사람 손을 거친다:

  pending/  — enqueue()가 스틸컷을 올려두는 곳. 피그마 플러그인이 GET /pending으로 가져가
              캔버스에 얹은 뒤 POST /ack/<id>로 확인하면 sent/로 옮긴다(메타는 보존 — 나중에
              되돌아올 때 어느 컷 것인지 알아야 하므로 삭제하지 않는다).
  sent/     — 캔버스에 이미 올라간 것. 사용자가 편집 후 플러그인에서 "봇으로 보내기"를 누르면
              POST /return/<id>로 편집본이 오고, sent/의 메타를 그대로 붙여 returned/로 옮긴다.
  returned/ — 봇이 아직 처리 안 한, 사람이 되돌린 편집본. get_and_clear_returned()가 이걸
              소비하면서 비운다 — dispatch_storyboard.py가 백그라운드 폴러로 주기적으로 불러서
              그 컷의 원본 스틸컷 파일(meta["still_path"])을 편집본으로 덮어쓰고 슬랙에 알린다.

봇 프로세스와 피그마 데스크톱 앱이 이 포트에 서로 접근 가능한 위치(보통 같은 머신)에 있어야
동작한다 — 다른 머신이면 SB_FIGMA_BRIDGE_PORT를 터널링하거나 포트를 열어줘야 한다.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config

log = logging.getLogger("storyboard-bot")

_RETURN_POLL_SEC = 5


def _sub_dir(name: str) -> Path:
    d = Path(config.FIGMA_QUEUE_DIR) / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def enqueue(png_bytes: bytes, meta: dict) -> str:
    """스틸컷 PNG 하나를 pending/ 큐에 올린다. 호출부는 이미 메모리에 들고 있는 PNG 바이트를
    그대로 넘긴다(디스크 경로가 아님 — 컷은 확정 저장 전엔 파일로 존재하지 않을 수 있다).
    반환: item id(uuid). meta에는 되돌아온 편집본을 나중에 그 컷 파일에 반영하기 위해
    still_path(그 컷이 실제로 읽는 로컬 PNG 경로, 없으면 되돌리기 반영을 건너뜀)와, 슬랙에
    결과를 알리기 위한 channel/thread_ts를 담아둬야 한다. 예:
    {"work": "코니", "scene_num": 3, "cut_num": 5, "reason": "실존인물 안전필터",
     "still_path": "/…/cut5.png", "channel": "C123", "thread_ts": "170…"}."""
    item_id = uuid.uuid4().hex
    d = _sub_dir("pending")
    (d / f"{item_id}.png").write_bytes(png_bytes)
    (d / f"{item_id}.json").write_text(json.dumps({**meta, "id": item_id}, ensure_ascii=False),
                                       encoding="utf-8")
    log.info(f"피그마 큐에 추가: {item_id} ({meta})")
    return item_id


def _items_in(dirname: str) -> list[dict]:
    d = _sub_dir(dirname)
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


def _pending_items() -> list[dict]:
    return _items_in("pending")


def _move(item_id: str, src: str, dst: str, new_png_bytes: bytes | None = None) -> dict | None:
    """src/{id}.(png|json)을 dst/로 옮긴다. new_png_bytes가 있으면 png 내용만 그걸로 바꿔서
    옮긴다(되돌아온 편집본이 원본과 다른 이미지이므로). 반환: 옮긴 meta dict, 없으면 None."""
    src_dir, dst_dir = _sub_dir(src), _sub_dir(dst)
    meta_path = src_dir / f"{item_id}.json"
    png_path = src_dir / f"{item_id}.png"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    (dst_dir / f"{item_id}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (dst_dir / f"{item_id}.png").write_bytes(
        new_png_bytes if new_png_bytes is not None else png_path.read_bytes())
    meta_path.unlink(missing_ok=True)
    png_path.unlink(missing_ok=True)
    return meta


def _ack(item_id: str) -> bool:
    """피그마 플러그인이 캔버스 삽입을 마치면 호출 — pending → sent로 옮긴다(삭제하지 않음,
    나중에 되돌아올 때 meta가 필요하므로)."""
    return _move(item_id, "pending", "sent") is not None


def _return(item_id: str, image_b64: str) -> bool:
    """플러그인의 "봇으로 보내기"가 호출 — sent에 남아있던 meta를 그대로 유지한 채, 편집된
    이미지로 returned/에 옮긴다. sent/에 없는 id(예: ack 안 하고 바로 되돌리기 시도)는 실패."""
    try:
        png_bytes = base64.b64decode(image_b64)
    except Exception:
        log.exception(f"피그마에서 되돌아온 이미지 디코딩 실패: {item_id}")
        return False
    return _move(item_id, "sent", "returned", new_png_bytes=png_bytes) is not None


def get_and_clear_returned() -> list[dict]:
    """returned/에 쌓인, 아직 처리 안 한 편집본을 전부 가져오면서 큐를 비운다(소비형).
    각 항목은 enqueue() 때 넣은 meta 전체 + image_bytes(디코딩된 원본 바이트)를 담는다."""
    d = _sub_dir("returned")
    out = []
    for meta_path in sorted(d.glob("*.json")):
        png_path = meta_path.with_suffix(".png")
        if not png_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            image_bytes = png_path.read_bytes()
        except Exception:
            log.exception(f"되돌아온 스틸컷 읽기 실패, 건너뜀: {meta_path}")
            continue
        out.append({**meta, "image_bytes": image_bytes})
        meta_path.unlink(missing_ok=True)
        png_path.unlink(missing_ok=True)
    return out


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
        elif self.path.startswith("/return/"):
            item_id = self.path[len("/return/"):]
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._send_json(400, {"error": "invalid body"})
                return
            image_b64 = body.get("image_b64")
            if not image_b64:
                self._send_json(400, {"error": "image_b64 required"})
                return
            ok = _return(item_id, image_b64)
            self._send_json(200 if ok else 404, {"ok": ok})
        else:
            self._send_json(404, {"error": "not found"})


_server: ThreadingHTTPServer | None = None
_return_poll_thread: threading.Thread | None = None


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


def start_return_poller(on_returned) -> None:
    """config.FIGMA_BRIDGE_ENABLED일 때 dispatch_storyboard.py가 기동 시 1회 호출 — 몇 초마다
    returned/를 확인해 새로 되돌아온 편집본이 있으면 on_returned(meta_with_image_bytes)를
    호출한다(그 컷 파일 덮어쓰기 + 슬랙 알림은 호출자 책임 — 이 모듈은 Slack을 모른다).
    실패한 개별 항목이 전체 폴링 루프를 죽이지 않게 항목 단위로 예외를 삼킨다."""
    global _return_poll_thread
    if _return_poll_thread is not None or not config.FIGMA_BRIDGE_ENABLED:
        return

    def _loop():
        while True:
            try:
                for item in get_and_clear_returned():
                    try:
                        on_returned(item)
                    except Exception:
                        log.exception(f"피그마에서 되돌아온 스틸컷 처리 실패: {item.get('id')}")
            except Exception:
                log.exception("피그마 되돌리기 폴링 루프 오류")
            time.sleep(_RETURN_POLL_SEC)

    _return_poll_thread = threading.Thread(target=_loop, daemon=True)
    _return_poll_thread.start()
    log.info("피그마 되돌리기 폴러 시작")
