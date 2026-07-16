# -*- coding: utf-8 -*-
"""OpenRouter TTS(/api/v1/audio/speech) 어댑터 — 합본 나레이션용(2026-07-14 신규).

한국어 지원이 확인되고 언어 커버리지가 가장 넓은 google/gemini-3.1-flash-tts-preview를 기본
모델로 쓴다(사용자 선택, 2026-07-14). OpenAI Audio Speech API와 호환되는 형식:
  POST https://openrouter.ai/api/v1/audio/speech
    body: {model, input, voice, response_format}
    응답: JSON이 아니라 raw 오디오 바이트 스트림(mp3/pcm) — 다른 OpenRouter 엔드포인트와
    다르게 이 엔드포인트만 바이너리를 그대로 돌려준다.
  인증: Authorization: Bearer {OPENROUTER_API_KEY} (이미지/영상 생성과 동일 키)

★2026-07-14 실측: 이 모델은 response_format="mp3"를 거부한다 — "Gemini TTS only supports
response_format=\"pcm\"". 그래서 항상 pcm(24kHz/16-bit mono, 헤더 없는 raw PCM)으로 받고,
필요하면 synthesize_mp3()로 ffmpeg 변환까지 해서 내보낸다. voice="Kore"(Google Gemini 네이티브
TTS 라인업의 흔한 기본 보이스명)는 실제 호출로 확인됨(거부 없이 정상 응답)."""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request

from . import config

_TTS_URL = "https://openrouter.ai/api/v1/audio/speech"

# Gemini 네이티브 TTS의 전체 보이스 로스터(30개, 2026-07-14 조사) — 캐릭터별 목소리를 이 중
# 하나로 고정 배정할 때 쓴다(openrouter_image.voice_for). "Kore"는 기본 나레이션 보이스로 계속
# 쓰고, 캐릭터별 배정은 이 목록에서 Kore를 뺀 나머지를 순환시킨다(같은 목소리가 나레이션과
# 겹치지 않게).
VOICES = ("Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
         "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina",
         "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar", "Alnilam", "Schedar",
         "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia",
         "Sadaltager", "Sulafat")
# ★2026-07-14, 실사용 피드백("목소리가 성별이랑 매칭이 안 됨"): 성별 구분 없이 등록 순서로만
# 30개를 순환 배정했더니 남자 캐릭터에 여성 보이스가 배정되는 등 명백히 어긋났다 — 각 보이스의
# 성별(GeminiTTS 공식 보이스 라이브러리 기준, gemini-tts.com/voices)을 라벨링해서 캐릭터
# 성별에 맞는 풀에서만 뽑게 한다.
VOICE_GENDER = {
    "Zephyr": "female", "Puck": "male", "Charon": "male", "Kore": "female",
    "Fenrir": "male", "Leda": "female", "Orus": "male", "Aoede": "female",
    "Callirrhoe": "female", "Autonoe": "female", "Enceladus": "male", "Iapetus": "male",
    "Umbriel": "male", "Algieba": "male", "Despina": "female", "Erinome": "female",
    "Algenib": "male", "Rasalgethi": "male", "Laomedeia": "female", "Achernar": "female",
    "Alnilam": "male", "Schedar": "male", "Gacrux": "female", "Pulcherrima": "male",
    "Achird": "male", "Zubenelgenubi": "male", "Vindemiatrix": "female", "Sadachbia": "male",
    "Sadaltager": "male", "Sulafat": "female",
}
# 지문/상황 설명 나레이션(대사가 아닌 구간) 전용 고정 보이스 — 캐릭터별 배정 풀에서 빼서
# 등록 인물이 늘어나도 나레이터 목소리와 절대 안 겹치게 한다(2026-07-14).
NARRATION_VOICE = "Kore"
CHARACTER_VOICES = tuple(v for v in VOICES if v != NARRATION_VOICE)
MALE_VOICES = tuple(v for v in CHARACTER_VOICES if VOICE_GENDER.get(v) == "male")
FEMALE_VOICES = tuple(v for v in CHARACTER_VOICES if VOICE_GENDER.get(v) == "female")
PCM_RATE = 24000   # 이 모델의 pcm 출력 고정 사양(24kHz/16-bit mono) — 응답에 별도 헤더가 없어
                    # 재생/변환 시 이 값을 직접 알려줘야 한다(ffmpeg -f s16le -ar 24000 -ac 1).


def available() -> bool:
    return bool(config.OPENROUTER_API_KEY)


def synthesize(text: str, *, voice: str | None = None, speed: float = 1.0) -> bytes:
    """텍스트 → 합성 음성(raw PCM bytes, 24kHz/16-bit mono, 헤더 없음). 실패 시 예외."""
    if not available():
        raise RuntimeError("OPENROUTER_API_KEY 미설정 — TTS 불가")
    payload = {
        "model": config.OPENROUTER_TTS_MODEL,
        "input": text,
        "voice": voice or config.OPENROUTER_TTS_VOICE,
        "response_format": "pcm",
    }
    if speed != 1.0:
        payload["speed"] = speed
    req = urllib.request.Request(
        _TTS_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=config.OPENROUTER_TTS_TIMEOUT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body_s = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"OpenRouter TTS 오류 {e.code}: {body_s}") from e


def synthesize_mp3(text: str, *, voice: str | None = None, speed: float = 1.0,
                   timeout: int = 30) -> bytes:
    """synthesize()의 raw PCM을 ffmpeg로 mp3 bytes로 변환해서 반환 — 파일 업로드·미리듣기용."""
    pcm = synthesize(text, voice=voice, speed=speed)
    r = subprocess.run(
        [config.FFMPEG_BIN, "-y", "-f", "s16le", "-ar", str(PCM_RATE), "-ac", "1",
         "-i", "pipe:0", "-f", "mp3", "pipe:1"],
        input=pcm, capture_output=True, timeout=timeout, check=True)
    return r.stdout
