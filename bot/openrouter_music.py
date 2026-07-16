# -*- coding: utf-8 -*-
"""OpenRouter 배경음악 생성(Google Lyria 3) 어댑터 — 합본 배경음용.

★2026-07-15 재작업: 이 기능은 한 번 만들어졌다가("2026-07-14 신규") 나레이션 타이밍 문제와
별개로 "일단 배경음악 제거"라는 사용자 요청으로 완전히 삭제된 적이 있다(`git log --all --
oneline -- bot/openrouter_music.py` 참고). 이번엔 작품의 분위기(로그라인/감정)에 맞춰 자동으로
어울리는 곡을 추론하는 요구사항이 추가돼 재작업 — 아래 API 실측 내용은 이전 구현에서 그대로
가져온 것(재검증 없이 신뢰, 이전 세션에서 실제 호출로 확인됨):

★실측 확인(당시): 공식 문서에 전용 음악 엔드포인트가 없다 — TTS/영상과 달리 이 모델은
/api/v1/chat/completions를 audio 출력 모드로 써야 한다:
  POST https://openrouter.ai/api/v1/chat/completions
    body: {model: "google/lyria-3-clip-preview", messages:[{role:"user", content:<곡 설명>}],
           modalities:["audio"], stream: true}
  ⚠️ stream:false로 보내면 "Audio output requires stream: true"로 400 거부됨(실측).
  응답: SSE 스트림 — 각 청크의 choices[0].delta.audio.data에 base64 mp3 조각(전부 이어붙여야
  완전한 파일). 첫 청크는 delta.content에 "<instrumental>" 같은 태그 텍스트만 오고, 그 다음
  청크(들)에 실제 audio.data가 옴. 27초 클립 실측 성공(모델명이 "clip"이라도 duration은
  프롬프트 뉘앙스에 따라 달라지는 듯 — 정확한 길이 제어 파라미터는 문서에 없음).

이번 기능은 기본 OFF(config.OPENROUTER_MUSIC_ENABLED, env SB_MUSIC_ENABLED="false")다 — 다시
켜기 전까지는 합본 파이프라인 동작이 오늘과 완전히 동일하다."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from . import config

_URL = "https://openrouter.ai/api/v1/chat/completions"


def available() -> bool:
    return bool(config.OPENROUTER_API_KEY) and config.OPENROUTER_MUSIC_ENABLED


def build_mood_prompt(mood_hint: str) -> str:
    """작품 분위기 힌트(app.py의 _work_mood_hint() 결과 — 로그라인/감정 텍스트)를 Lyria 3용
    영어 배경음악 설명 문장으로 감싼다. mood_hint가 비어 있으면 장르 불문 범용 인스트루멘탈
    배경음악 문구로 폴백(호출자가 프롬프트 추론에 실패해도 완전히 막히지 않게).
    이 모듈은 app.py를 import하지 않는다 — 이 repo의 기존 관례상 app.py가 top-level
    오케스트레이터로 bot/*를 import하는 방향만 있고 반대 방향(bot/* → app.py)은 없어서,
    분위기 텍스트는 app.py 쪽에서 미리 뽑아 문자열로 넘겨받는다."""
    hint = (mood_hint or "").strip()
    if hint:
        return (f"Instrumental background music for a short-form Korean drama. "
                f"Story tone/mood: {hint}. Match the instrumentation, tempo, and atmosphere to "
                f"this tone. No vocals, instrumental only, seamless loopable feel.")
    return ("Calm, emotionally neutral instrumental background music for a short-form drama. "
            "Soft, understated, no vocals, instrumental only, seamless loopable feel.")


def generate(prompt: str, *, model: str | None = None, timeout: int | None = None) -> bytes:
    """텍스트 설명 → 배경음악(mp3 bytes). 실패 시 예외.
    prompt 예: "calm emotional K-drama instrumental, soft piano, no vocals, gentle and warm"."""
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY 미설정 — 배경음악 생성 불가")
    payload = {
        "model": model or config.OPENROUTER_MUSIC_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["audio"],
        "stream": True,
    }
    req = urllib.request.Request(
        _URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                "Content-Type": "application/json"},
        method="POST")
    b64_parts = []
    try:
        with urllib.request.urlopen(
                req, timeout=timeout or config.OPENROUTER_MUSIC_TIMEOUT) as r:
            for raw_line in r:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                obj = json.loads(data)
                audio = (obj.get("choices") or [{}])[0].get("delta", {}).get("audio")
                if audio and audio.get("data"):
                    b64_parts.append(audio["data"])
    except urllib.error.HTTPError as e:
        body_s = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"OpenRouter 음악 생성 오류 {e.code}: {body_s}") from e
    if not b64_parts:
        raise RuntimeError("배경음악 생성 응답에 audio 데이터가 없음")
    return base64.b64decode("".join(b64_parts))
