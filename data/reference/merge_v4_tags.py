#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3·v4 정제 DB 병합 — 일회성·idempotent (핸드오프_v3v4_정제DB병합.md §3).

reference_db.json(v3 base, 76편 자산 SSOT)에 v4 파일의 v4.0 레코드 태그를 얹어
단일 superset DB(tag_version=v5.0)로 제자리 업그레이드한다.

- v3 정제자산(script/scenes/publish_dt/hook_desc/transcript_raw/metrics) 전부 보존.
- v4.0 편: v4축(catharsis_type·hook_beat·cliffhanger_type·character_setup·trope_candidates)
  + trope_tags_ko 복사, v4_tagged=true, v4_tag_confidence 세팅.
- v4 미태깅 편: v4_tagged=false, v4_tag_confidence=null, v4축 빈값, needs_review=true,
  tag_notes에 "v4축 미태깅" 추가. trope_tags_ko는 영어 trope_tags에서 trope_map으로 backfill.
- 재실행해도 태깅편을 미태깅으로 되돌리지 않음(idempotent).

사용:
    python3 reference/merge_v4_tags.py                 # 기본 경로
    python3 reference/merge_v4_tags.py --v4 <경로> --out <경로>
출력 형식은 enrich.py와 동일: ensure_ascii=False, indent=1.
"""
import argparse
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
V3_DEFAULT = os.path.join(BASE, "reference_db.json")
V4_DEFAULT = os.path.join(BASE, "..", "..", "co-writer-bot", "data", "reference_db_v4.json")

# 영어 머신키 → 한글 (태깅프롬프트_v5.md trope_map과 동일 SSOT).
# 미태깅 편의 trope_tags_ko backfill용. 확실한 대응만 — 없으면 스킵(트렌드는 v4_tagged 게이트로 제외).
TROPE_EN_KO = {
    "boss_employee_or_power_romance": "오피스로맨스",
    "class_gap_cinderella": "신데렐라(신분격차)",
    "contract_or_fake_relationship": "계약연애",
    "marriage_contract_or_family_pressure": "선결혼후연애",
    "enemies_to_lovers": "혐관",
    "forbidden_love": "금단물",
    "love_triangle_or_rival": "삼각관계",
    "obsessive_devotion": "집착남",
    "revenge_betrayal_or_payback": "복수극",
    "second_chance_or_regret": "재회물",
    "secret_identity_or_hidden_truth": "신분숨김(히든재벌·정체은닉)",
    "breakup_sacrifice_or_noble_idiot": "희생·놓아주기",
    "danger_rescue_romance": "능력·판타지",
    "healing_or_comfort": "시한부·질병",
}

V4_AXES = ["catharsis_type", "hook_beat", "cliffhanger_type", "trope_candidates", "character_setup"]
V4_EMPTY = {"catharsis_type": "", "hook_beat": [], "cliffhanger_type": "",
            "trope_candidates": [], "character_setup": {}}


def backfill_ko(en_tropes):
    return [TROPE_EN_KO[e] for e in (en_tropes or []) if e in TROPE_EN_KO]


def merge(v3_path, v4_path, out_path):
    v3 = json.load(open(v3_path, encoding="utf-8"))
    v4_list = json.load(open(v4_path, encoding="utf-8"))
    v4_by_id = {x["id"]: x for x in v4_list if x.get("tag_version") == "v4.0"}

    tagged = untagged = 0
    for rec in v3:
        t = rec.setdefault("tags", {})
        src = v4_by_id.get(rec["id"])
        if src:
            st = src.get("tags", {})
            for ax in V4_AXES:
                t[ax] = st.get(ax, V4_EMPTY[ax])
            # trope_tags_ko: v4 한글 우선, 없으면 영어에서 backfill
            ko = st.get("trope_tags") or []
            t["trope_tags_ko"] = ko if ko else backfill_ko(t.get("trope_tags"))
            rec["v4_tagged"] = True
            rec["v4_tag_confidence"] = src.get("tag_confidence")
            tagged += 1
        else:
            for ax in V4_AXES:
                t.setdefault(ax, V4_EMPTY[ax])
            t.setdefault("trope_tags_ko", backfill_ko(t.get("trope_tags")))
            rec["v4_tagged"] = False
            rec["v4_tag_confidence"] = None
            rec["needs_review"] = True
            note = rec.get("tag_notes") or ""
            if "v4축 미태깅" not in note:
                rec["tag_notes"] = (note + " | v4축 미태깅 — 재태깅 대상").strip(" |")
            untagged += 1
        rec["tag_version"] = "v5.0"

    json.dump(v3, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"병합 완료: {len(v3)}편 → {out_path}")
    print(f"  v4_tagged=true {tagged} / 미태깅 {untagged}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--v3", default=V3_DEFAULT)
    ap.add_argument("--v4", default=V4_DEFAULT)
    ap.add_argument("--out", default=V3_DEFAULT)  # 제자리 업그레이드
    a = ap.parse_args()
    merge(a.v3, a.v4, a.out)


if __name__ == "__main__":
    main()
