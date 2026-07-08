# -*- coding: utf-8 -*-
"""생성 검증 관문(Gate) — '생성과 감사를 분리하는 3단계' 설계의 3단계.

1단계 규칙 주입(FAILSAFE 프롬프트) → 2단계 생성(generator) → **3단계 감사(여기)**.

생성자 스스로 점검하는 방식은 창작 몰입 중 규칙을 흘리는 맹점이 있다(이 프로젝트의
출발점). 그래서 생성이 끝난 초안을, 생성자와 **다른 감사자 페르소나**로 1콜 재검한다:
 - 체크리스트 6항목(prompts.FAILSAFE 1~6과 1:1)을 PASS / FAIL / NA로 판정
 - 판정 결과 활용은 호출측(app._do_generate) 정책: 금지사항(이진)만 자동 최소 교정,
   나머지 FAIL은 고치지 않고 ⚠️ 플래그로 작가에게 알림만 한다.
   (correct_draft는 넘겨받은 위반만 교정하므로 어느 항목을 고칠지는 호출측이 결정)

바이블이 없으면(패턴·사례 기반 생성) 검증 기준이 없으므로 그냥 통과시킨다.
"""
import json
import logging
import re

from . import prompts

log = logging.getLogger("cowriter.verify")

# 체크리스트 — prompts.FAILSAFE 1~6과 1:1 매핑 (7 톤·문법은 게이트 대상 아님)
CHECKLIST = [
    ("금지사항",   "[금지사항]에 명시된 것을 위반했는가"),
    ("시점-캐릭터", "지금 시점에 없거나 회상으로만 나올 인물을 현재 장면에 등장시켰는가"),
    ("캐릭터붕괴", "인물의 포지션·설정·말투(핵심대사)가 바이블과 어긋나는가"),
    ("타임라인",   "지금 시점 이후의 사건을 앞당겼거나, 이미 끝난 과거 사건을 되풀이했는가"),
    ("개요준수",   "(대본일 때) 이 화 [개요]의 사건·순서·엔딩을 따랐는가 — 개요가 없으면 NA"),
    ("설정혼입",   "다른 작품 정보가 섞였거나, 바이블에 없는 큰 설정·사건을 지어냈는가"),
]

_VERIFY_SYSTEM = """너는 숏폼 드라마 대본·개요의 **감사자(auditor)**다. 네 임무는 창작이 아니라, 아래 [작품 바이블] 기준으로 [초안]이 규칙을 어겼는지 **판정**하는 것이다.

원칙:
- 각 점검 항목을 PASS / FAIL / NA 로 판정한다. 확인 대상 자체가 없으면(예: 이 화 개요가 없어 개요준수를 못 봄) NA.
- **FAIL은 초안에 명백한 근거가 있을 때만 매긴다. 애매하거나 확신이 없으면 PASS.** 억측·과잉지적 금지.
- FAIL이면 reason에 **초안의 어느 대목이 무엇을 어겼는지** 한 줄로 구체적으로 (짧은 인용 포함).
- 문체·재미·완성도·길이는 판정 대상이 아니다. 오직 바이블 준수만 본다.

출력은 아래 JSON 객체 하나만. 설명·머리말·코드펜스 금지.
{"results":[{"n":1,"name":"금지사항","verdict":"PASS","reason":""}, ... 6개 항목 모두]}"""

_CORRECT_SYSTEM = """너는 숏폼 드라마 작가의 교정 담당이다. 아래 [초안]에서 **지정된 위반 사항만** 최소 수정으로 바로잡는다.

철칙:
- 지적된 위반만 고친다. 나머지 문장·구성·전개·톤·길이·대사 문법(화자 표기·나레이션)은 **그대로 유지**하라.
- 새 사건·설정·인물·미래 전개를 추가하지 마라. 바이블 범위 안에서만 고친다.
- '⚠️ 바이블 확인 필요' 표시는 지우지 마라.
- 결과는 **교정된 초안 본문만** 출력. 수정 설명·머리말·코드펜스 금지."""


def _checklist_text():
    return "\n".join(f"{i}. [{name}] {desc}"
                     for i, (name, desc) in enumerate(CHECKLIST, 1))


def _parse(raw):
    """LLM 응답에서 results 배열 추출. 실패 시 None (→ 게이트는 통과 처리)."""
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.M).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(s[i:j + 1])
    except (ValueError, TypeError):
        return None
    res = obj.get("results") if isinstance(obj, dict) else None
    return res if isinstance(res, list) else None


def verify_draft(draft, bible, target_episode=None, kind=None, llm=None):
    """초안을 바이블 기준으로 감사. 반환:
       {checked: 실제 판정했나, passed: 위반 없나, fails: [{name,reason,...}], raw}.
    bible/draft/llm 없거나 파싱 실패면 checked=False·passed=True (생성 흐름 막지 않음)."""
    skip = {"checked": False, "passed": True, "fails": [], "raw": ""}
    if not bible or not (draft or "").strip() or llm is None:
        return skip
    ctx = prompts.build_bible_block(bible, target_episode, kind=kind)
    user = (f"# 점검 항목\n{_checklist_text()}\n\n"
            f"{ctx}\n\n"
            f"# [초안] (종류={kind or '?'}) — 위 [작품 바이블] 기준으로 점검하라\n{draft}")
    try:
        raw = llm(_VERIFY_SYSTEM, user) or ""
    except Exception:
        log.exception("verify llm 실패")
        return skip
    results = _parse(raw)
    if results is None:
        log.warning("verify: JSON 파싱 실패 — 게이트 통과 처리 (raw 앞부분: %s)", raw[:120])
        return {**skip, "raw": raw}
    fails = [r for r in results if str(r.get("verdict", "")).upper() == "FAIL"]
    return {"checked": True, "passed": not fails, "fails": fails, "raw": raw}


def correct_draft(draft, fails, bible, target_episode=None, kind=None, llm=None):
    """FAIL 위반만 최소 수정한 교정본 반환. 실패·빈 응답이면 원본 그대로."""
    if not fails or llm is None:
        return draft
    ctx = prompts.build_bible_block(bible, target_episode, kind=kind)
    viol = "\n".join(f"- [{f.get('name', '?')}] {f.get('reason', '')}".rstrip() for f in fails)
    user = (f"{ctx}\n\n"
            f"# 고쳐야 할 위반 (이것만 최소 수정, 나머지는 그대로)\n{viol}\n\n"
            f"# [초안]\n{draft}")
    try:
        out = (llm(_CORRECT_SYSTEM, user) or "").strip()
    except Exception:
        log.exception("correct llm 실패")
        return draft
    return out or draft
