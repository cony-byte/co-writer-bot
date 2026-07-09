#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reference_db.json 데이터 강화 (v3 정제 레이어 후처리).

추가/변환:
  1. publish_dt   — TikTok source_video_id 상위 32비트 = 업로드 unix초 (재크롤링 불필요, #7)
  2. script 화자 중립화 — ML/FL/SUP → 화자1/화자2/… (영상 내 등장순), NAR→나레이션, UNK→미상.
                          원 추정치는 role_hint로 보존 (#6 1순위)
  3. scenes       — 소스 CSV 컷 롱포맷에서 컷별 [start,end,shot,people] 추출 (#5 컷 타임라인, #6 2순위)

사용법:
  python3 reference/enrich.py --csv <로우데이터.csv> [--csv ...]
  (CSV 없이 실행하면 publish_dt·화자중립화만 수행)

소스 CSV는 story-v1-system 로우데이터 (컷 단위 롱 포맷). scenes는 컷 데이터가 있는 편만 채워진다.
"""
import argparse
import csv
import json
import os
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "reference_db.json")

# 중립 라벨 매핑 — 역할 추정치를 숨기지 않고 role_hint에 남긴다
NAR_LABELS = {"NAR": "나레이션", "UNK": "미상"}


def publish_dt(vid):
    """TikTok video_id → ISO 업로드 시각 (상위 32비트 = unix초)."""
    try:
        ts = int(vid) >> 32
        if ts < 1_400_000_000 or ts > 2_000_000_000:  # 2014~2033 범위 밖이면 무효
            return ""
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return ""


def neutralize(script):
    """script[].speaker(ML/FL/SUP/NAR/UNK) → 중립 라벨. 등장순으로 화자N 부여, 원값은 role_hint.
    멱등: 이미 role_hint가 있으면(=변환 완료) 그대로 둔다."""
    if script and all("role_hint" in l for l in script):
        return script
    order = {}
    out = []
    for line in script:
        role = line.get("speaker", "UNK")
        if role in NAR_LABELS:
            label = NAR_LABELS[role]
        else:
            if role not in order:
                order[role] = f"화자{len(order) + 1}"
            label = order[role]
        out.append({"speaker": label, "role_hint": role, "line": line["line"]})
    return out


def extract_market(paths):
    """CSV들에서 video_id → 크롤 market (KR/EN/…). 지역 구분(한국/서양)의 근거."""
    mk = {}
    for p in paths:
        with open(p, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                vid = (r.get("source_video_id") or "").strip()
                m = (r.get("market") or "").strip().upper()
                if vid and vid not in mk and m:
                    mk[vid] = m
    return mk


def region_of(market):
    """크롤 market → 지역 라벨. KR만 한국, 나머지(EN/SEA/…)는 서양(글로벌)."""
    return "한국" if market == "KR" else "서양"


def extract_scenes(paths):
    """CSV들에서 video_id → [{cut, start, end, shot, people}] (컷 데이터 있는 편만)."""
    scenes = {}
    SHOT = {  # yolo 라벨 → 한국어 축약
        "close_up_or_upper_body": "클로즈업", "medium_shot": "미디엄",
        "full_or_group_shot": "풀/그룹", "wide_shot": "와이드",
        "no_person": "인물없음",
    }
    for p in paths:
        with open(p, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                vid = (r.get("source_video_id") or "").strip()
                st = (r.get("cut_start_time") or "").strip()
                if not vid or not st:
                    continue
                cut = {
                    "cut": int(float(r.get("cut_cut_id") or 0)),
                    "start": round(float(st), 2),
                    "end": round(float(r.get("cut_end_time") or 0), 2),
                    "shot": SHOT.get((r.get("yolo_shot_size_yolo") or "").strip(),
                                     (r.get("yolo_shot_size_yolo") or "").strip()),
                    "people": int(float(r.get("yolo_people_count_filtered") or 0)),
                }
                scenes.setdefault(vid, []).append(cut)
    # 컷 순서 정렬 + 중복 제거(같은 cut_id 첫값)
    for vid, cuts in scenes.items():
        seen, uniq = set(), []
        for c in sorted(cuts, key=lambda c: c["cut"]):
            if c["cut"] in seen:
                continue
            seen.add(c["cut"])
            uniq.append(c)
        scenes[vid] = uniq
    return scenes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", action="append", default=[], help="소스 로우데이터 CSV (컷 롱포맷)")
    a = ap.parse_args()

    db = json.load(open(DB, encoding="utf-8"))
    scenes = extract_scenes(a.csv) if a.csv else {}
    markets = extract_market(a.csv) if a.csv else {}

    # CSV publish_dt (유튜브 등 id로 시각 추출 불가한 경우용)
    csv_pub = {}
    for p in a.csv:
        with open(p, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                vid = (r.get("source_video_id") or "").strip()
                pd = (r.get("publish_dt") or "").strip()
                if vid and vid not in csv_pub and pd:
                    csv_pub[vid] = pd if "T" in pd else pd + "T00:00:00Z"

    n_pub = n_scene = 0
    for r in db:
        vid = r["id"]
        pd = publish_dt(vid) or csv_pub.get(vid) or r.get("publish_dt", "")
        r["publish_dt"] = pd
        if r["publish_dt"]:
            n_pub += 1
        r["script"] = neutralize(r.get("script") or [])
        if vid in scenes:
            r["scenes"] = scenes[vid]
            n_scene += 1
        else:
            r.setdefault("scenes", [])
        # 지역: CSV market 있으면 갱신, 없으면 기존 유지, 그래도 없으면 서양 기본
        if vid in markets:
            r["region"] = region_of(markets[vid])
        else:
            r.setdefault("region", "서양")

    json.dump(db, open(DB, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    from collections import Counter
    reg = Counter(r.get("region") for r in db)
    print(f"강화 완료: 발행시각 {n_pub}/{len(db)}, 장면 {n_scene}/{len(db)}, 화자중립 전편, 지역 {dict(reg)}")


if __name__ == "__main__":
    main()
