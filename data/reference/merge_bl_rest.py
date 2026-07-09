#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""최속 루트 — 260708 유튜브 BL AI 나머지 전량 자동 병합 (침향 제외, 파일럿 제외).
transcript_raw를 줄 단위로만 분할(요약·가공 없음, 100% 전문 보존). 화자는 임시 미상(UNK)
→ whisperX 도입 후 교체. 태그는 s5 자동값. content_type=ai_generated·platform=youtube·genre=bl.
사용: python3 reference/merge_bl_rest.py --csv <260708 CSV>"""
import argparse, csv, json, os, collections
BASE=os.path.dirname(os.path.abspath(__file__)); DB=os.path.join(BASE,"reference_db.json")
def _f(x):
    try: return float(str(x).replace("%","").strip())
    except: return None
def _tags(x): return [t.strip() for t in (x or "").split("|") if t.strip() and "general" not in t]

ML_OK={"dominant_possessive","protective_rescuer","cold_to_warm","devoted_straightforward","powerful_status","dangerous_forbidden","unknown"}

def is_chim(r):
    return "침향" in (r.get("ranking_description") or "") or r.get("ranking_author","")=="@latebluenight"

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--csv",required=True); a=ap.parse_args()
    rows=list(csv.DictReader(open(a.csv,encoding="utf-8-sig")))
    byv=collections.OrderedDict()
    for r in rows:
        v=r.get("source_video_id","").strip()
        if v and v not in byv: byv[v]=r
    def tr(v):
        for x in [r for r in rows if r.get("source_video_id")==v]:
            t=(x.get("script_transcript_ko") or "").strip()
            if t and not t.startswith("[NO_"): return t
        return ""
    db=json.load(open(DB,encoding="utf-8")); have={r["id"] for r in db}
    added=0; ratios=[]
    for v,r in byv.items():
        if r.get("content_type")!="ai_generated": continue
        if v in have: continue
        if is_chim(r): continue
        raw=tr(v)
        if len(raw)<100: continue
        # 줄 단위 분할 (전문 보존, 가공 없음)
        lines=[ln.strip() for ln in raw.split("\n") if ln.strip()]
        script=[{"speaker":"UNK","line":ln} for ln in lines]
        # 보존율 검증(공백 제외 문자 기준)
        raw_c=raw.replace(" ","").replace("\n","")
        scr_c="".join(ln for l in script for ln in [l["line"]]).replace(" ","")
        ratios.append(len(scr_c)/len(raw_c) if raw_c else 1)
        hook=(r.get("script_hook_text_ko") or "").strip()
        if not hook or hook.startswith("[NO_"): hook=lines[0][:70] if lines else ""
        ml=[m for m in _tags(r.get("male_lead_type")) if m in ML_OK]
        rec={"id":v,"url":r.get("ranking_video_url",""),"author":r.get("ranking_author",""),
          "desc":(r.get("ranking_description") or "")[:200],"rank":int(_f(r.get("ranking_rank")) or 0) or None,
          "crawl_date":(r.get("crawl_date") or "2026-07-08").strip(),"publish_dt":(r.get("publish_dt") or "").strip(),
          "metrics":{"views":_f(r.get("ranking_views")),"likes":_f(r.get("ranking_likes")),
            "saves":_f(r.get("ranking_saves")),"shares":_f(r.get("ranking_shares")),"comments":_f(r.get("ranking_comments")),
            "er":_f(r.get("ranking_ER%_(save+share+cmt)/views")),"save_rate":_f(r.get("ranking_save_rate%")),
            "dur":_f(r.get("ranking_duration_s")),"cut_count":_f(r.get("summary_cut_count")),"avg_cut":_f(r.get("summary_avg_cut_duration"))},
          "content_type":"ai_generated","platform":(r.get("platform") or "youtube").strip(),"genre":(r.get("genre") or "bl").strip(),
          "transcript_raw":raw,"transcript_form":"dialogue","script":script,
          "hook_desc":hook,"hook_desc_confidence":0.4,
          "tags":{"hook_type":(r.get("script_hook_type") or "").strip() if "general" not in (r.get("script_hook_type") or "") else "",
            "story_type":(r.get("script_story_type") or "").strip() if "general" not in (r.get("script_story_type") or "") else "",
            "dialogue_tags":_tags(r.get("script_dialogue_grammar_tags")),"trope_tags":_tags(r.get("script_romance_trope_tags")),
            "male_lead":ml,"setting":"","visual_hook":"","hook_modality":"dialogue","narration_form":""},
          "tag_confidence":0.4,"tag_notes":"자동 병합(줄단위 전문·화자 미구분·s5 자동태그). whisperX 화자/타임스탬프 대기.","tag_version":"v3.0",
          "needs_review":False,
          "legacy_tags":{"hook_type":r.get("script_hook_type",""),"story_type":r.get("script_story_type",""),
            "dialogue_tags":_tags(r.get("script_dialogue_grammar_tags")),"trope_tags":_tags(r.get("script_romance_trope_tags")),
            "hook":(r.get("script_hook_text_ko") or "")[:200],"tag_confidence":None}}
        db.append(rec); added+=1
    db.sort(key=lambda r:-(r["metrics"].get("er") or 0))
    json.dump(db,open(DB,"w",encoding="utf-8"),ensure_ascii=False,indent=1)
    mn=min(ratios) if ratios else 1
    print(f"자동 병합: 신규 {added}편, 총 {len(db)}편 | 대본 보존율 최소 {mn:.0%} (전량 100% 목표)")

if __name__=="__main__": main()
