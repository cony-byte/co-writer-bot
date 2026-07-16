# -*- coding: utf-8 -*-
"""합본(에피소드 컴파일) 1단계 — 콘티+생성된 컷 영상을 보고 LLM이 편집 전략(컷 순서·길이·
나레이션 대본)을 설계한다.

★2026-07-14: 화면 번인 자막은 빼고(episode_compile.py가 더는 자막을 굽지 않음) 그 텍스트를
TTS 나레이션 음성으로 믹싱하는 쪽으로 방향이 바뀌었다 — 그래서 이 필드 이름을
subtitle_text→narration_text로 바꾸고, 누가 말하는 대사인지(speaker) 필드를 추가했다.
speaker가 있어야 TTS 합성 시 그 캐릭터에 고정 배정된 목소리(openrouter_image.voice_for)를
쓸 수 있다 — 없으면(지문/설명 나레이션) 고정 나레이터 목소리(openrouter_tts.NARRATION_VOICE)로
읽는다. 텍스트 자체는 여전히 콘티 원문 그대로(요약/재구성/의역 금지)."""
from __future__ import annotations

import json
import logging
import re
import subprocess
import unicodedata

from . import config
from . import generator

log = logging.getLogger("storyboard-bot")

_SYSTEM = (
    "너는 숏폼 드라마 합본 영상 편집자다. 주어진 씬별 콘티 본문과, 그 씬에 실제로 존재하는 컷 "
    "번호·영상 길이(초)를 보고 이 화 전체를 이어붙일 편집 컷 리스트를 설계해라.\n\n"
    "규칙:\n"
    "- 반드시 콘티에 실제로 나온 씬 번호·컷 번호만 사용해라(목록에 없는 씬/컷을 지어내지 마라).\n"
    "- 각 컷의 duration은 그 컷의 실측 길이(초)를 넘을 수 없다. start는 0 이상, start+duration은 "
    "실측 길이 이하여야 한다.\n"
    "- narration_text는 그 컷 구간에 해당하는 대사/나레이션을 콘티 원문에서 한 글자도 바꾸지 말고 "
    "그대로 옮겨라(요약·재구성·의역 금지) — 화면에 자막으로 굽는 게 아니라 TTS 음성으로 읽을 "
    "대본이다. 해당 구간에 대사/나레이션이 없으면 null로 둬라.\n"
    "- speaker는 그 narration_text를 말하는 캐릭터의 이름을 콘티에 쓰인 그대로 적어라(예: "
    "\"민재\"). 캐릭터 대사가 아니라 지문·상황 설명 나레이션이면 \"나레이션\"이라고 적어라. "
    "narration_text가 null이면 speaker도 null.\n"
    "- delivery: narration_text가 있으면, 콘티에 이미 적힌 연기 지시(괄호 안 표정·톤 묘사, 예: "
    "'화남이 아니라 오래 눌러온 짜증을 담담히 내뱉는 표정')를 그대로 요약해 TTS가 어떤 톤으로 "
    "읽어야 하는지 짧은 영어 구절로 적어라(예: 'restrained, quietly bitter, not angry'). 콘티에 "
    "그런 지시가 없으면 대사 내용·문맥으로 톤을 합리적으로 추론해라(예: 위협 대사면 'low and "
    "menacing', 애원이면 'trembling, pleading'). 절대 'neutral'/'normal' 같은 밋밋한 값으로 "
    "때우지 마라 — 반드시 구체적인 감정·톤 형용사를 써라. narration_text가 null이면 delivery도 null.\n"
    "- ★★각 씬에 '존재하는 컷'으로 제시된 컷은 전부, 순서대로, 빠짐없이 써라(2026-07-14) — "
    "작가가 콘티 단계에서 [N초] 표기로 이미 컷 하나하나를 필요해서 만들어둔 것이니, "
    "'이야기 흐름에 안 맞다'는 이유로 임의로 컷을 빼면 안 된다. 오직 그 컷의 실측 길이가 "
    "0이거나(=영상 생성 실패) 정말 명백히 결함이 있는 경우에만 예외로 뺄 수 있다.\n"
    "- 씬 헤더에 목표 길이(예: '15초')가 적혀 있으면, 그 씬에 배치하는 컷들의 duration 합이 "
    "그 목표 길이에 최대한 가깝도록 조정해라(단, 각 컷의 duration은 실측 길이를 넘을 수 없다).\n"
    "- ★콘티 본문의 각 비트 맨 앞에 있는 [N초] 표기가 그 비트(=컷 하나)의 의도된 길이다. 보통 "
    "그 컷의 영상 실측 길이가 이 [N초]와 거의 같으니, 그럴 땐 특별히 일부만 쓸 이유가 없으면 "
    "start=0, duration=실측 길이 거의 그대로를 써서 콘티가 설계한 타이밍을 그대로 따라가라 — "
    "임의로 짧게 잘라내지 마라.\n"
    "- ★★단, 컷 목록에 \"(실측 X초, 콘티 의도 Y초 — 여분 Z초)\"라고 표시된 컷은 영상 생성 API의 "
    "최소 길이 제한(4초) 때문에 원래 콘티가 의도한 길이(Y초)보다 길게(X초) 만들어진 것이다 — "
    "그 초과분(Z초)은 대본에 없던 내용을 영상 생성기가 임의로 채운 구간이라 부자연스러울 수 있다 "
    "(2026-07-15). 이런 컷은 기본적으로 start=0, duration≈Y(콘티 의도 길이)로 잘라 여분을 버려라. "
    "단 그 구간에 담아야 할 나레이션이 있고 발화에 Y초보다 더 필요하면, 필요한 만큼만(실측 길이 "
    "한도 내에서) duration을 늘려도 된다 — 무조건 Y초로 자르라는 뜻은 아니다.\n"
    "- transition_in: 이 컷 '직전'과 이 컷 사이를 어떻게 이을지 — 기본은 항상 \"cut\"(하드컷)이다. "
    "같은 씬 안에서 사건이 계속 이어지면 무조건 \"cut\". 오직 명백히 필요할 때만(예: 시간이 "
    "확 건너뛰거나, 장소가 완전히 바뀌거나, 회상/꿈 장면으로 들어가거나 나올 때) \"fade\"(페이드"
    "투블랙)를 써라. 화 전체에서 fade는 몇 번 안 되게(과용 금지) — 애매하면 \"cut\"으로 둬라. "
    "리스트의 첫 번째 컷은 항상 \"cut\"(들어갈 전 화면이 없으므로).\n"
    "- 출력은 다른 설명 없이 JSON 배열 하나만 내놔라. 마크다운 코드펜스도 쓰지 마라. 형식:\n"
    '[{"scene_num": 1, "cut_num": 1, "start": 0, "duration": 4.0, '
    '"narration_text": "원문 또는 null", "speaker": "이름/나레이션/null", '
    '"delivery": "짧은 영어 톤 묘사 또는 null", "transition_in": "cut 또는 fade"}, ...]'
)


def _probe_duration(path: str) -> float:
    """ffprobe로 영상 실측 길이(초) 조회. 실패하면 0.0(그 컷은 나중에 validate에서 걸러짐)."""
    try:
        out = subprocess.run(
            [config.FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=20, check=True,
        )
        return float(out.stdout.strip())
    except Exception:
        log.exception(f"ffprobe 실패: {path}")
        return 0.0


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", re.sub(r"\s+", "", s or ""))


_TARGET_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*초")


def _parse_target_duration(hdr: str) -> float | None:
    """씬 헤더(예: '씬1 · 15초 · 복도 화장실 앞')에서 목표 길이(초)를 뽑아낸다. 없으면 None."""
    m = _TARGET_DURATION_RE.search(hdr or "")
    return float(m.group(1)) if m else None


_BEAT_SEC_RE = re.compile(r"\[(\d+(?:\.\d+)?)\s*초\]")


def _cut_intended_durations(body: str, vids: list[dict]) -> dict[int, float]:
    """콘티 본문의 [N초] 비트를 순서대로 뽑아 그 씬의 컷들(cut_num 오름차순)과 1:1 대응시켜
    {cut_num: 콘티가 의도한 길이(초)}를 반환. ★2026-07-15 — 영상화 API 최소 길이(4초) 때문에
    실측 길이가 이 의도 길이보다 길어진 컷을 편집 계획 LLM에게 구분해서 알려주기 위함(아래
    _build_user_prompt 참고). 비트 개수와 컷 개수가 안 맞으면(컷 누락·구버전 콘티 등 예외) 그냥
    빈 dict를 반환 — 그런 경우는 억지로 추정하지 않고 기존처럼 실측 길이를 그대로 신뢰한다."""
    beats = [float(m.group(1)) for m in _BEAT_SEC_RE.finditer(body or "")]
    cuts = sorted(vids, key=lambda v: v["cut_num"])
    if len(beats) != len(cuts):
        return {}
    return {c["cut_num"]: b for c, b in zip(cuts, beats)}


_BEAT_QUOTE_RE = re.compile(r"'[^']{2,}'")


def _cut_beat_texts(body: str) -> list[str]:
    """[N초] 태그 순서대로 그 비트의 본문 텍스트(다음 태그 전까지, 없으면 끝까지)를 뽑는다 —
    ★2026-07-15 러프 플랜에서 이 비트에 대사가 있는지(따옴표 존재) 판단하는 용도."""
    matches = list(_BEAT_SEC_RE.finditer(body or ""))
    out = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out.append(body[start:end])
    return out


def _build_rough_plan(scenes: list[tuple], videos_with_dur: dict[int, list[dict]]) -> list[dict]:
    """★2026-07-15 "플랜 짜지말고 그냥 앵간하면 쭉 붙이고 짧게 자를 때만 잘라야하는 식으로
    러프하게" — LLM 편집계획(_SYSTEM/build_edit_plan의 기존 방식)이 자꾸 이상하게 잘라서
    사용자 요청으로 잠깐 우회한다. 이제 편집전략을 LLM에 안 맡기고, 존재하는 컷을 씬 순서·
    컷 번호 순서 그대로, 기본은 항상 실측 길이 전체 + 하드컷으로 이어붙인다.
    유일한 예외(짧게 자르는 경우): 영상화 API 4초 최소 길이 때문에 콘티 의도([N초])보다
    길게 만들어진 "여분" 구간 — 이것도 그 컷에 대사(따옴표)가 있으면 자르지 않는다(대사가
    중간에 끊기는 사고를 막기 위해 최근에 넣은 것과 동일 원칙). narration_text/speaker/
    delivery는 LLM이 만드는 필드라 항상 None(현재 TTS 나레이션 트랙 자체도 비활성 상태라
    실질적 영향 없음)."""
    scenes_by_num = {num: (hdr, body) for num, hdr, body in scenes}
    out = []
    for num, hdr, body in scenes:
        vids = sorted(videos_with_dur.get(num) or [], key=lambda v: v["cut_num"])
        if not vids:
            continue
        intended = _cut_intended_durations(body, vids)
        beat_texts = _cut_beat_texts(body)
        for i, v in enumerate(vids):
            if v["duration"] <= 0:
                log.warning(f"러프 플랜 — 씬{num} 컷{v['cut_num']} 실측 길이 0(건너뜀, ffprobe 실패 가능성)")
                continue
            duration = v["duration"]
            iv = intended.get(v["cut_num"])
            has_dialogue = i < len(beat_texts) and bool(_BEAT_QUOTE_RE.search(beat_texts[i]))
            if iv is not None and duration - iv > 0.3 and not has_dialogue:
                duration = iv
            out.append({"scene_num": num, "cut_num": v["cut_num"], "video_path": v["path"],
                       "start": 0.0, "duration": duration, "narration_text": None,
                       "speaker": None, "delivery": None, "transition_in": "cut"})
    return out


def _build_user_prompt(episode_title: str, scenes: list[tuple], videos_by_scene: dict) -> str:
    lines = [f"화 제목: {episode_title}", ""]
    for num, hdr, body in scenes:
        vids = videos_by_scene.get(num) or []
        if not vids:
            continue
        intended = _cut_intended_durations(body, vids)
        parts = []
        for v in sorted(vids, key=lambda v: v["cut_num"]):
            iv = intended.get(v["cut_num"])
            # 0.3초 미만 차이는 math.ceil() 반올림 정도라 "여분"으로 보지 않는다 — 4초 하한 때문에
            # 실제로 늘어난 경우만(보통 1초 이상 차이) 표시.
            if iv is not None and v["duration"] - iv > 0.3:
                parts.append(f"컷{v['cut_num']}(실측 {v['duration']:.1f}초, 콘티 의도 {iv:.1f}초 — "
                            f"여분 {v['duration'] - iv:.1f}초)")
            else:
                parts.append(f"컷{v['cut_num']}({v['duration']:.1f}초)")
        cut_info = ", ".join(parts)
        target = _parse_target_duration(hdr)
        target_info = f" — 목표 길이: {target:.0f}초" if target else ""
        lines.append(f"### 씬{num}{target_info} — 존재하는 컷: {cut_info}")
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


def _parse_llm_json(text: str) -> list:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip(), flags=re.S)
    return json.loads(t)


def _validate_plan(plan: list, scenes_by_num: dict, videos_by_scene: dict) -> list[dict]:
    """씬/컷이 실제로 존재하는지, start+duration이 실측 길이를 안 넘는지, narration_text가
    그 씬의 콘티 본문에 실제로 있는 문장인지(공백만 무시하고 비교) 검사 — 위반 항목은
    드롭(또는 나레이션만 지우고 클립은 살림)해서 후속 ffmpeg 렌더가 항상 안전한 입력만 받게 한다.

    ★2026-07-14: 드롭 사유를 조용히 삼키면(continue만) 최종 plan이 비어도 왜 비었는지
    로그에서 알 방법이 없었다(ffprobe 실패로 모든 컷 duration이 0이 되는 등) — 드롭할 때마다
    사유를 로그에 남긴다."""
    out = []
    for i, seg in enumerate(plan):
        try:
            scene_num = int(seg["scene_num"])
            cut_num = int(seg["cut_num"])
            start = float(seg.get("start") or 0)
            duration = float(seg["duration"])
        except (KeyError, TypeError, ValueError) as e:
            log.warning(f"편집계획 항목 #{i} 드롭 — 필드 파싱 실패({e}): {seg!r}")
            continue
        vids = {v["cut_num"]: v for v in (videos_by_scene.get(scene_num) or [])}
        v = vids.get(cut_num)
        if not v:
            log.warning(f"편집계획 항목 #{i} 드롭 — 씬{scene_num} 컷{cut_num}은 존재하는 컷 목록에 없음")
            continue
        if v["duration"] <= 0:
            log.warning(f"편집계획 항목 #{i} 드롭 — 씬{scene_num} 컷{cut_num} 실측 길이 0 "
                       f"(ffprobe 실패 가능성, 경로: {v['path']})")
            continue
        if start < 0 or duration <= 0 or start + duration > v["duration"] + 0.05:
            duration = max(0.1, v["duration"] - start)
            if duration <= 0:
                log.warning(f"편집계획 항목 #{i} 드롭 — 씬{scene_num} 컷{cut_num} start({start})가 "
                           f"실측 길이({v['duration']})를 넘음")
                continue
        sub = seg.get("narration_text")
        speaker = seg.get("speaker") if sub else None
        delivery = seg.get("delivery") if sub else None
        if sub:
            body = scenes_by_num.get(scene_num, "")
            if _norm(sub) not in _norm(body):
                sub = None
                speaker = None
                delivery = None
        if sub:
            # ★2026-07-14, "영상은 말하고 있는데 대사가 안 나온다" 피드백 — LLM이 "[N초]/실측
            # 길이 그대로 써라" 지시를 안 따르고 씬 전체 목표시간에 맞추려 duration을 줄여버리는
            # 경우가 있었다. 그러면 나레이션 TTS 오디오가 이 구간 길이를 넘어 다음 구간까지
            # 흘러들어가 화면과 안 맞게 들린다 — 실측 발화시간(한국어 4자/초 어림 + 1.5초 여유)
            # 보다 duration이 부족하면, 이 컷의 실제 영상 길이(v["duration"]) 안에서 늘릴 수
            # 있는 만큼 늘리고, 그래도 부족하면 나레이션만 포기(클립은 그대로 살림)해서 겹침을
            # 원천 차단한다.
            needed = len(sub) / 4.5 + 1.5
            if needed > duration + 0.05:
                room = v["duration"] - start
                if room >= needed - 0.05:
                    duration = needed
                else:
                    log.warning(f"편집계획 항목 #{i} 나레이션 드롭 — 씬{scene_num} 컷{cut_num} "
                               f"발화 예상 {needed:.1f}초 > 가용 {room:.1f}초(실측 길이 부족): {sub[:30]!r}")
                    sub = None
                    speaker = None
                    delivery = None
        if sub:
            # ★2026-07-15 "합본에서 자꾸 나레이션(대사)을 끊어버림" 실사용자 리포트 — 지금 합본의
            # 실제 대사 오디오는 TTS 나레이션 트랙(_build_narration_track, 현재 미사용)이 아니라
            # seedance가 이 컷 영상 자체에 구운 오디오다. 그런데 위 _SYSTEM 프롬프트는 "실측 길이가
            # 콘티 의도보다 긴(=영상화 API 4초 최소 길이 때문에 생긴 여분) 컷은 의도 길이로 잘라
            # 여분을 버려라"라고 LLM에게 지시하는데, seedance가 그 여분 구간까지 대사를 채워
            # 넣었을 수 있다는 걸 LLM은 텍스트 길이 추정(needed, 위)만으로 판단해야 해서 부정확할
            # 때가 있다 — LLM이 그 추정을 잘못하면 실제로 아직 말하고 있는 도중에 클립이 잘린다.
            # 프롬프트 지시를 더 정교하게 다듬어 100% 신뢰하기보다, 이 컷에 대사(narration_text)가
            # 있다고 확정된 이상 코드에서 강제로 "그 컷의 실측 길이 전체"를 쓰게 한다 — LLM이
            # 뭐라고 제안했든 무시하고 안전하게 덮어쓴다. 대신 여분(패딩) 구간에 AI가 즉흥으로
            # 채운 부자연스러운 화면/소리가 살짝 딸려올 수 있는데, 대사가 중간에 끊기는 것보다는
            # 훨씬 낫다는 판단.
            full_len = v["duration"] - start
            if full_len > duration + 0.05:
                log.info(f"편집계획 항목 #{i} 대사 있는 컷이라 duration을 실측 전체로 강제 확장 — "
                        f"씬{scene_num} 컷{cut_num}: {duration:.1f}초 → {full_len:.1f}초")
                duration = full_len
        transition_in = "fade" if seg.get("transition_in") == "fade" else "cut"
        if not out:
            transition_in = "cut"   # 화 맨 첫 컷은 이을 대상이 없으니 항상 cut
        out.append({"scene_num": scene_num, "cut_num": cut_num, "video_path": v["path"],
                    "start": start, "duration": duration, "narration_text": sub, "speaker": speaker,
                    "delivery": delivery, "transition_in": transition_in})
    # ★2026-07-14, "4컷인데 3컷으로 만들었다" 실측 — LLM이 위 지시를 어기고 컷을 조용히
    # 빼버려도 로그에 안 남으면 다음에도 똑같이 재발한다. 존재하는 컷(실측 길이>0)인데 최종
    # plan에 안 들어간 게 있으면 경고로 남긴다.
    used = {(o["scene_num"], o["cut_num"]) for o in out}
    for scene_num, vids in videos_by_scene.items():
        for v in vids:
            if v["duration"] > 0 and (scene_num, v["cut_num"]) not in used:
                log.warning(f"편집계획이 씬{scene_num} 컷{v['cut_num']}을 빠뜨림 "
                           f"(실측 길이 {v['duration']:.1f}초로 정상 존재하는데 최종 plan에 없음)")
    return out


def build_edit_plan(work: str, episode_title: str, scenes: list[tuple],
                    videos_by_scene: dict[int, list[dict]], job_key: str | None = None) -> list[dict]:
    """편집 컷 리스트를 반환. scenes: app.py의 _split_scenes(conti) 결과
    [(num, header, body), ...] — 순환 임포트를 피하기 위해 호출자가 이미 쪼갠 걸 그대로 받는다.
    반환: [{"scene_num", "cut_num", "video_path", "start", "duration", "narration_text", "speaker", "delivery"}, ...]

    ★2026-07-15 "야 합본 개이상해 플랜 짜지말고 그냥 앵간하면 쭉 붙이고 짧게 자를 때만 잘라야
    하는 식으로 일단 러프하게 바꾸자" — LLM에게 편집전략(어느 컷을 얼마나 쓸지·트랜지션 등)을
    맡기던 방식(_SYSTEM/_build_user_prompt/_validate_plan의 LLM-plan 검증 로직)이 계속 이상하게
    잘라서 사용자 요청으로 우회. 이제 존재하는 컷을 씬·컷 번호 순서 그대로, 항상 실측 길이
    전체 + 하드컷으로 이어붙이는 _build_rough_plan을 쓴다(자세한 설명은 그 함수 docstring).
    LLM 기반 코드(_SYSTEM/_build_user_prompt/_validate_plan/_parse_llm_json)는 나중에 되돌릴 수
    있게 그대로 남겨뒀다 — 지금은 호출부에서 안 쓸 뿐."""
    videos_with_dur: dict[int, list[dict]] = {}
    for scene_num, vids in videos_by_scene.items():
        videos_with_dur[scene_num] = [
            {**v, "duration": _probe_duration(v["path"])} for v in vids
        ]
    plan = _build_rough_plan(scenes, videos_with_dur)
    log.info(f"편집계획(러프): {len(plan)}개 항목, 실측 길이 0인 컷 "
            f"{sum(1 for vs in videos_with_dur.values() for v in vs if v['duration'] <= 0)}개")
    return plan
