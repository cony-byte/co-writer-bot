# -*- coding: utf-8 -*-
"""합본(에피소드 컴파일) 오케스트레이션 — video_index로 영상 커버리지를 모으고, edit_plan으로
LLM 편집 계획을 짠 뒤, ffmpeg로 컷들을 이어붙이고 나레이션 TTS 음성을 믹싱한 mp4 하나로 만든다.

★2026-07-14: 이전 버전엔 화면 번인 자막이 있었는데, 그 텍스트(narration_text)를 TTS 음성으로
읽어 믹싱하는 쪽으로 바꾸면서 자막 번인은 뺐다(사용자 요청 — "자막을 빼고 믹싱을 연결하자").
narration_text가 있는 구간마다 openrouter_tts로 음성을 합성하고, speaker가 등록된 캐릭터면
그 캐릭터에 고정 배정된 목소리(openrouter_image.voice_for)를, 지문 나레이션이면 고정 나레이터
목소리(openrouter_tts.NARRATION_VOICE)를 쓴다 — 각 구간의 타임라인 시작 위치에 오디오를
배치(adelay)한 뒤 전부 섞는다(amix).

★2026-07-15: "개별 컷은 소리가 나오는데 합본에서 소리가 안 나옴" 픽스 — seedance가
OPENROUTER_VIDEO_GENERATE_AUDIO=True로 구운 컷 자체 오디오(대사/앰비언트)를 각 컷의 타임라인
위치에 맞춰 추출·배치(_build_cut_audio_track)해 최종 믹스에 포함시킨다. 예전엔 컷 자체엔
오디오가 없다는 가정으로 -an으로 버렸는데(그 가정이 깨졌음), 이제 concat용 비디오 트랙에서만
-an으로 빼고 오디오는 따로 보존한다."""
from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from . import config
from . import openrouter_image as oi
from . import openrouter_music as music
from . import openrouter_tts as tts

log = logging.getLogger("storyboard-bot")


class CompileError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise CompileError(f"ffmpeg 실패: {' '.join(cmd)}\n{r.stderr[-2000:]}")


def _pcm_to_wav(pcm: bytes, out_path: Path, timeout: int = 30) -> None:
    """openrouter_tts.synthesize()가 주는 헤더 없는 raw PCM(24kHz/16-bit mono)을 wav로 변환."""
    subprocess.run(
        [config.FFMPEG_BIN, "-y", "-f", "s16le", "-ar", str(tts.PCM_RATE), "-ac", "1",
         "-i", "pipe:0", str(out_path)],
        input=pcm, capture_output=True, timeout=timeout, check=True)


_NARR_SPEEDUP_TOLERANCE = 0.15   # 이 정도(초) 넘치는 건 무시(트림/속도조절 안 함 — 자연스러운 여유)
_NARR_MAX_SPEEDUP = 1.5   # 이 이상 빠르게 읽혀야 하면 부자연스러워서 포기하고 트림으로 폴백


def _build_narration_track(work: str, plan: list[dict], total_duration: float,
                           tmpdir: Path) -> Path | None:
    """plan의 누적 타임라인 기준으로 구간별 TTS 음성을 합성해 전체 길이(total_duration)에 맞춘
    믹스 오디오 트랙(wav) 하나로 반환. 나레이션 있는 구간이 하나도 없으면 None(호출자는 무음
    mp4로 진행). 개별 TTS 합성 실패는 그 구간만 무음으로 건너뛰고 나머지는 계속 진행한다.

    ★2026-07-15 "자꾸 나레이션을 끊어버림"(실사용자 리포트) — 예전엔 다음 나레이션 시작 전까지
    (max_len)로 무조건 atrim해서, 실제 TTS 발화 길이가 그 창보다 길면 문장이 중간에 뚝 잘렸다.
    이제 먼저 각 구간의 다음 구간 시작 시각(=쓸 수 있는 시간 창)을 계산해두고, 그 창보다 실제
    발화 길이가 넘치면(사소한 초과는 무시) 그 구간만 TTS를 더 빠른 속도(tts.synthesize의 speed
    파라미터, Gemini TTS 자체 속도조절 — ffmpeg atempo보다 자연스러움)로 다시 합성해 창 안에
    맞춘다. 그래도(과도하게 길어) 안 맞으면 그때만 마지막 수단으로 atrim(잘림)에 맡긴다 —
    예전엔 이 트림이 유일한 방어선이라 항상 발동했는데, 이제는 정말 극단적인 경우에만 발동."""
    # 1차: 실제 합성 없이 이 화의 나레이션 구간들과 각 구간이 시작하는 누적 타임라인 위치만 먼저
    # 모은다 — 다음 구간 시작 시각(=이번 구간이 쓸 수 있는 최대 길이)을 알아야 speed 조절
    # 여부를 판단할 수 있는데, 그건 "다음" 구간을 먼저 봐야 알 수 있어서 2단계로 나눈다.
    specs = []   # [(start_offset_sec, text, voice, delivery), ...]
    cursor = 0.0
    for seg in plan:
        text = seg.get("narration_text")
        if text:
            speaker = seg.get("speaker")
            voice = (tts.NARRATION_VOICE if not speaker or speaker == "나레이션"
                    else oi.voice_for(work, speaker))
            # ★2026-07-14, "톤/딜리버리가 안 맞음" 피드백: 대사 텍스트만 던지면 항상 flat하게
            # 읽혀서 심각한 대사도 밋밋하게 나왔다 — edit_plan이 콘티의 연기 지시에서 뽑아낸
            # delivery(짧은 영어 톤 묘사)를 인라인 스타일 태그로 앞에 붙여 Gemini TTS가 그
            # 톤으로 읽게 유도한다.
            delivery = seg.get("delivery")
            specs.append((cursor, text, voice, delivery))
        cursor += seg["duration"]

    clips = []   # [(start_offset_sec, wav_path), ...]
    for i, (start, text, voice, delivery) in enumerate(specs):
        next_start = specs[i + 1][0] if i + 1 < len(specs) else total_duration
        max_len = max(0.1, next_start - start)
        tts_text = f"[{delivery}] {text}" if delivery else text
        try:
            pcm = tts.synthesize(tts_text, voice=voice)
            dur = len(pcm) / (2 * tts.PCM_RATE)   # 16-bit(2바이트)/모노/PCM_RATE 고정 사양
            if dur > max_len + _NARR_SPEEDUP_TOLERANCE:
                speed = min(_NARR_MAX_SPEEDUP, dur / max_len)
                try:
                    pcm = tts.synthesize(tts_text, voice=voice, speed=speed)
                except Exception:
                    log.exception(f"나레이션 속도조절 재합성 실패, 원래 속도로 진행(트림될 수 있음): {text[:50]!r}")
            wav_path = tmpdir / f"narr_{i:04d}.wav"
            _pcm_to_wav(pcm, wav_path)
            clips.append((start, wav_path))
        except Exception:
            log.exception(f"나레이션 TTS 합성 실패, 이 구간은 무음 처리: {text[:50]!r}")

    if not clips:
        return None

    inputs = [config.FFMPEG_BIN, "-y"]
    filter_parts = []
    for i, (start, path) in enumerate(clips):
        inputs += ["-i", str(path)]
        delay_ms = int(start * 1000)
        # ★2026-07-15: 위에서 속도조절로 이미 창 안에 맞춰뒀지만(정상 케이스), 그래도 못 맞춘
        # 극단적 경우(재합성 실패·_NARR_MAX_SPEEDUP으로도 부족)에 대한 마지막 안전장치로 여전히
        # 창 길이로 atrim은 해둔다 — 이제는 사실상 발동 안 하는 게 정상.
        next_start = clips[i + 1][0] if i + 1 < len(clips) else total_duration
        max_len = max(0.1, next_start - start)
        filter_parts.append(
            f"[{i}:a]atrim=duration={max_len:.3f},adelay={delay_ms}|{delay_ms}[a{i}]")
    mix_in = "".join(f"[a{i}]" for i in range(len(clips)))
    # amix 뒤 apad로 전체 길이를 영상 길이(total_duration)에 정확히 맞춘다 — 마지막 구간에
    # 나레이션이 없으면 amix 자체 길이가 영상보다 짧아져서 뒷부분이 잘려 들리는 걸 방지.
    filter_parts.append(
        f"{mix_in}amix=inputs={len(clips)}:duration=longest:normalize=0,"
        f"apad=whole_dur={total_duration:.3f}[aout]")
    out_path = tmpdir / "narration.wav"
    _run(inputs + ["-filter_complex", ";".join(filter_parts), "-map", "[aout]",
                   "-t", f"{total_duration:.3f}", str(out_path)], timeout=config.COMPILE_TIMEOUT)
    return out_path


_FADE_SEC = 0.5   # 페이드투블랙 길이(초) — 필요할 때만(transition_in="fade") 적용


def _build_music_track(mood_prompt: str | None, total_duration: float,
                       tmpdir: Path) -> Path | None:
    """config.OPENROUTER_MUSIC_ENABLED가 켜져 있을 때만 호출됨(호출자 쪽 게이트).
    mood_prompt(app.py._work_mood_hint() 기반으로 미리 만들어진 영어 곡 설명 프롬프트)로
    Lyria 3 배경음악을 생성, 영상 전체 길이에 맞춰 반복(loop)+트림하고 볼륨을 낮춘
    (config.OPENROUTER_MUSIC_VOLUME_DB) wav로 반환. 실패해도 None만 반환하고 예외를 밖으로
    던지지 않는다 — 배경음악은 비필수 기능이라 실패해도 합본 자체는 계속 진행돼야 한다."""
    if not mood_prompt or not music.available():
        return None
    try:
        mp3 = music.generate(mood_prompt, timeout=config.OPENROUTER_MUSIC_TIMEOUT)
        mp3_path = tmpdir / "music_raw.mp3"
        mp3_path.write_bytes(mp3)
        out_path = tmpdir / "music.wav"
        # Lyria 클립이 영상보다 짧을 수 있어 -stream_loop -1로 반복시킨 뒤 total_duration으로
        # 잘라내고, 대사보다 한참 낮은 볼륨으로 깐다.
        _run([config.FFMPEG_BIN, "-y", "-stream_loop", "-1", "-i", str(mp3_path),
             "-af", f"volume={config.OPENROUTER_MUSIC_VOLUME_DB}dB",
             "-t", f"{total_duration:.3f}", str(out_path)], timeout=config.COMPILE_TIMEOUT)
        return out_path
    except Exception:
        log.exception(f"배경음악 생성/처리 실패, 배경음악 없이 진행: {mood_prompt[:80]!r}")
        return None


def _has_audio_stream(video_path: str, timeout: int = 15) -> bool:
    """★2026-07-15, "개별 컷은 소리가 나오는데 합본에서 소리가 안 나옴" 조사 중 추가.
    seedance가 오디오 없이 만든 컷(예: OPENROUTER_VIDEO_GENERATE_AUDIO가 꺼져 있던 시점에
    생성된 과거 컷)도 섞여 있을 수 있어, 오디오 스트림 유무를 먼저 확인하고 있을 때만 추출을
    시도한다 — 없는데 -vn만 걸고 추출하면 ffmpeg가 실패하므로 여기서 걸러 무음 처리한다."""
    try:
        r = subprocess.run(
            [config.FFPROBE_BIN, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=timeout)
        return bool(r.stdout.strip())
    except Exception:
        return False


def _build_cut_audio_track(clips: list[tuple[float, Path]], total_duration: float,
                           tmpdir: Path) -> Path | None:
    """★2026-07-15 "개별 컷은 소리가 나오는데 합본에서 소리가 안 나옴" 픽스 — 각 컷의 자체
    오디오(대사/앰비언트, seedance가 구운 것)를 그 컷의 타임라인 시작 위치(adelay)에 배치해
    하나의 트랙으로 합친다. _build_narration_track과 동일한 패턴(구간별 오디오를 누적 커서
    위치에 배치 후 amix)을 그대로 재사용 — 오디오가 있는 컷이 하나도 없으면 None을 반환해
    호출자가 예전과 동일하게(무음) 진행하도록 한다."""
    if not clips:
        return None
    inputs = [config.FFMPEG_BIN, "-y"]
    filter_parts = []
    for i, (start, path) in enumerate(clips):
        inputs += ["-i", str(path)]
        delay_ms = int(start * 1000)
        next_start = clips[i + 1][0] if i + 1 < len(clips) else total_duration
        max_len = max(0.1, next_start - start)
        filter_parts.append(
            f"[{i}:a]atrim=duration={max_len:.3f},adelay={delay_ms}|{delay_ms}[a{i}]")
    mix_in = "".join(f"[a{i}]" for i in range(len(clips)))
    filter_parts.append(
        f"{mix_in}amix=inputs={len(clips)}:duration=longest:normalize=0,"
        f"apad=whole_dur={total_duration:.3f}[aout]")
    out_path = tmpdir / "cut_audio.wav"
    _run(inputs + ["-filter_complex", ";".join(filter_parts), "-map", "[aout]",
                   "-t", f"{total_duration:.3f}", str(out_path)], timeout=config.COMPILE_TIMEOUT)
    return out_path


def _combine_audio_tracks(tracks: list[Path | None], total_duration: float,
                          tmpdir: Path) -> Path | None:
    """나레이션·배경음악 등 여러 오디오 트랙을 하나로 믹스. 트랙이 0개면 None, 1개면 그대로,
    2개 이상이면 amix로 합친다."""
    tracks = [t for t in tracks if t]
    if not tracks:
        return None
    if len(tracks) == 1:
        return tracks[0]
    inputs = [config.FFMPEG_BIN, "-y"]
    for t in tracks:
        inputs += ["-i", str(t)]
    mix_in = "".join(f"[{i}:a]" for i in range(len(tracks)))
    filter_complex = f"{mix_in}amix=inputs={len(tracks)}:duration=longest:normalize=0[aout]"
    out_path = tmpdir / "mixed.wav"
    _run(inputs + ["-filter_complex", filter_complex, "-map", "[aout]",
                   "-t", f"{total_duration:.3f}", str(out_path)], timeout=config.COMPILE_TIMEOUT)
    return out_path


def _render(work: str, plan: list[dict], out_path: Path, tmpdir: Path, *,
           width: int, height: int, fps: int, mood_prompt: str | None = None,
           progress_cb=None) -> None:
    """progress_cb(i, total): ★2026-07-16, 합본 생성 중 진행상황이 안 보인다는 지적 —
    컷별 세그먼트 인코딩이 끝날 때마다(전체 중 몇 번째인지) 호출해 app.py가 슬랙 메시지를
    갱신할 수 있게 한다. None이면 기존과 동일(콜백 없이 조용히 진행)."""
    seg_paths = []
    cut_audio_clips: list[tuple[float, Path]] = []  # [(timeline_start_sec, wav_path), ...]
    cursor = 0.0
    for i, seg in enumerate(plan):
        seg_path = tmpdir / f"seg_{i:04d}.mp4"
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
             f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1")
        dur = seg["duration"]
        fd = min(_FADE_SEC, dur / 2)
        # ★2026-07-14, "트랜지션이 필요할 때만" 요청 — edit_plan이 씬 전환·시간 점프처럼
        # 명백히 필요한 경우에만 각 컷의 transition_in을 "fade"로 표시한다. 이 컷 자신이
        # fade로 들어오면(직전과 이어지는 게 아니라 새로 시작) 앞쪽에 fade-in, 다음 컷이
        # fade로 들어오면(=이 컷에서 다음 컷으로 넘어갈 때 끊어져야 함) 이 컷 뒤쪽에 fade-out을
        # 미리 구워 넣는다 — 나머지 대부분(하드컷)은 그냥 이어붙이기만 하면 되니 손 안 댐.
        if seg.get("transition_in") == "fade" and i > 0:
            vf += f",fade=t=in:st=0:d={fd:.2f}"
        next_seg = plan[i + 1] if i + 1 < len(plan) else None
        if next_seg and next_seg.get("transition_in") == "fade":
            vf += f",fade=t=out:st={max(0, dur - fd):.2f}:d={fd:.2f}"
        # ★2026-07-15 "개별 컷은 소리가 나오는데 합본에서 소리가 안 나옴" 픽스: 이 -an은
        # concat용 비디오 트랙에서만 오디오를 뺀다(코덱 통일을 위해 그대로 둠) — 예전엔
        # OPENROUTER_VIDEO_GENERATE_AUDIO가 기본 False라 컷 자체에 오디오가 없어서 -an으로
        # 버려도 문제가 없었지만, 지금은 기본 True라 여기서 버려지는 오디오가 진짜 대사/
        # 앰비언트다. 그래서 아래에서 같은 구간의 오디오를 따로 추출해 별도 트랙으로 보존하고,
        # 최종 믹스(_combine_audio_tracks)에 나레이션/배경음악과 함께 태운다.
        _run([
            config.FFMPEG_BIN, "-y", "-ss", str(seg["start"]), "-t", str(dur),
            "-i", seg["video_path"], "-c:v", "libx264", "-preset", "veryfast", "-an",
            "-r", str(fps),
            "-vf", vf,
            str(seg_path),
        ], timeout=config.COMPILE_TIMEOUT)
        seg_paths.append(seg_path)

        if _has_audio_stream(seg["video_path"]):
            seg_audio_path = tmpdir / f"seg_audio_{i:04d}.wav"
            try:
                _run([
                    config.FFMPEG_BIN, "-y", "-ss", str(seg["start"]), "-t", str(dur),
                    "-i", seg["video_path"], "-vn", "-ar", "44100", "-ac", "2",
                    "-c:a", "pcm_s16le", str(seg_audio_path),
                ], timeout=config.COMPILE_TIMEOUT)
                cut_audio_clips.append((cursor, seg_audio_path))
            except CompileError:
                # 컷 자체 오디오는 부가 요소라 추출 실패해도 합본 전체를 막지 않는다 —
                # 이 구간만 무음으로 두고(나레이션/배경음악은 정상 진행) 계속한다.
                log.exception(f"컷 오디오 추출 실패, 이 구간은 무음 처리: seg {i}")
        cursor += dur
        if progress_cb:
            try:
                progress_cb(i + 1, len(plan))
            except Exception:
                log.exception("합본 진행률 콜백 실패 — 렌더링은 계속 진행")

    filelist = tmpdir / "filelist.txt"
    filelist.write_text("\n".join(f"file '{p.name}'" for p in seg_paths), encoding="utf-8")
    concat_path = tmpdir / "concat.mp4"
    _run([config.FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", str(filelist),
         "-c", "copy", str(concat_path)], timeout=config.COMPILE_TIMEOUT)

    # ★2026-07-14: "목소리 타이밍이 하나도 안 맞음" 피드백으로 나레이션 믹싱을 잠깐 뺐다 —
    # _build_narration_track 함수 자체는 남겨뒀으니, 타이밍 어긋나는 원인을 찾아 고친 뒤
    # 이 rename 대신 다시 그 함수를 불러 믹싱하게 되돌리면 된다. 지금은 나레이션 없이 진행.
    #
    # 배경음악(2026-07-15 재작업)은 config.OPENROUTER_MUSIC_ENABLED가 켜져 있을 때만 시도한다
    # — 기본 OFF라 이 토글을 켜지 않는 한 아래는 항상 no-op이고 결과는 오늘과 동일하다.
    total_duration = sum(seg["duration"] for seg in plan)
    music_path = (_build_music_track(mood_prompt, total_duration, tmpdir)
                 if config.OPENROUTER_MUSIC_ENABLED else None)
    # ★2026-07-15: 컷 자체 오디오(cut_audio_path)가 하나도 없으면(과거 무음 컷들만 있는 경우)
    # _build_cut_audio_track가 None을 반환하므로, 오디오가 진짜 하나도 없을 때(나레이션도
    # 꺼져있고 배경음악도 꺼져있을 때)는 기존과 완전히 동일하게 무음 mp4로 진행된다.
    cut_audio_path = _build_cut_audio_track(cut_audio_clips, total_duration, tmpdir)
    audio_path = _combine_audio_tracks([cut_audio_path, music_path], total_duration, tmpdir)
    if audio_path is None:
        concat_path.rename(out_path)
        return
    _run([config.FFMPEG_BIN, "-y", "-i", str(concat_path), "-i", str(audio_path),
         "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
         "-shortest", str(out_path)], timeout=config.COMPILE_TIMEOUT)


def confirm_final(draft_path: str) -> str:
    """확정 버튼 클릭 시 호출 — draft_*.mp4를 최종본(_최종.mp4)으로 바꾸고, 같은 화 제목의
    다른 draft/최종 파일은 지운다(2026-07-14, "합본도 최종본만 저장하게" 요청 — 그동안 재생성
    할 때마다 파일이 계속 쌓이고 있었음). 반환: 최종본 절대경로."""
    p = Path(draft_path)
    m = re.match(r"^(.*)_draft_[0-9a-f]{8}\.mp4$", p.name)
    stem = m.group(1) if m else p.stem
    final_path = p.parent / f"{stem}_최종.mp4"
    for old in p.parent.glob(f"{stem}_*.mp4"):
        if old != p:
            old.unlink(missing_ok=True)
    p.rename(final_path)
    return str(final_path)


def discard_draft(draft_path: str) -> None:
    """재생성(다시 만들기) 시 지금 draft는 필요 없어졌으니 지운다."""
    Path(draft_path).unlink(missing_ok=True)


def compile_episode(work: str, episode_title: str, plan: list[dict],
                    mood_prompt: str | None = None, progress_cb=None) -> str:
    """이미 만들어진 edit_plan(list[dict])을 받아 실제로 렌더링. draft mp4 절대경로 반환 —
    사용자가 확인 버튼으로 confirm_final()을 불러야 최종본이 된다(2026-07-14).
    plan 설계(LLM 호출)는 bot.edit_plan.build_edit_plan에서 미리 하고 넘겨받는다 — 이 함수는
    순수 렌더링(+ 나레이션·배경음악 믹싱)만 담당.
    mood_prompt: 배경음악용 영어 곡 설명(app.py가 _work_mood_hint()로 미리 만들어 넘김) —
    이 모듈은 app.py를 import하지 않으므로(기존 import 방향 유지) 호출자가 문자열로 전달한다.
    config.OPENROUTER_MUSIC_ENABLED가 꺼져 있으면(기본값) 이 값은 아예 쓰이지 않는다."""
    if not plan:
        raise CompileError("편집 계획이 비어 있어요 — 영상화된 컷이 하나도 없나 봐요.")
    proj = oi.vp_project_dir(work)
    if not proj:
        raise CompileError(f"'{work}' 프로젝트 폴더를 못 찾았어요.")
    out_dir = proj / "outputs" / "compiled"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 이전에 확정 안 하고 남겨둔 draft가 있으면 새로 만들기 전에 정리(최종본은 안 건드림).
    for old in out_dir.glob(f"{episode_title}_draft_*.mp4"):
        old.unlink(missing_ok=True)
    out_path = out_dir / f"{episode_title}_draft_{uuid.uuid4().hex[:8]}.mp4"

    with tempfile.TemporaryDirectory(prefix="sb_compile_") as td:
        tmpdir = Path(td)
        _render(work, plan, out_path, tmpdir, mood_prompt=mood_prompt, progress_cb=progress_cb,
               width=config.COMPILE_WIDTH, height=config.COMPILE_HEIGHT, fps=config.COMPILE_FPS)
    return str(out_path)
