#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""whisperX 세그먼트 소비 — 크롤러 CSV의 script_segments_json을 라이브러리 script로 반영.
각 세그먼트 = 버블 하나(화자+타임스탬프+text_ko 전문). 화자는 등장순 화자1/2…로 라벨,
role_hint=SUP → set_gender가 BL이면 '남' 부여. text_ko를 그대로 넣어 축약 없음(커버리지 검증).
멱등: 재실행해도 같은 결과. 세그먼트 없는 편은 건드리지 않음.
사용: python3 reference/apply_segments.py --csv <260708 CSV>"""
import argparse, csv, json, os
csv.field_size_limit(10**7)
BASE=os.path.dirname(os.path.abspath(__file__)); DB=os.path.join(BASE,"reference_db.json")

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--csv",required=True); a=ap.parse_args()
    rows=list(csv.DictReader(open(a.csv,encoding="utf-8-sig")))
    segmap={}
    for r in rows:
        v=r.get("source_video_id","").strip()
        s=(r.get("script_segments_json") or "").strip()
        if v and v not in segmap and s and s not in ("","[]","null"):
            try: segmap[v]=json.loads(s)
            except: pass
    db=json.load(open(DB,encoding="utf-8")); byid={r["id"]:r for r in db}
    applied=[]; missing=[]; lowcov=[]
    for v,segs in segmap.items():
        if v not in byid: missing.append(v); continue
        segs=sorted(segs,key=lambda s:s.get("start",0))
        order={}; script=[]
        for s in segs:
            sp=s.get("speaker","")
            if sp not in order: order[sp]=len(order)+1
            txt=(s.get("text_ko") or "").strip()
            if not txt: continue
            script.append({"speaker":f"화자{order[sp]}","line":txt,
                           "start":round(float(s.get("start",0)),2),"end":round(float(s.get("end",0)),2),
                           "role_hint":"SUP"})
        rec=byid[v]
        raw=rec.get("transcript_raw") or ""
        raw_c=raw.replace(" ","").replace("\n","")
        seg_c="".join(l["line"] for l in script).replace(" ","").replace("\n","")
        cov=len(seg_c)/max(len(raw_c),1)
        if cov<0.9:
            # whisperX가 원본보다 덜 전사 → 전문 손실. 세그먼트 폐기, 원본 전문(줄단위) 유지.
            rec["script"]=[{"speaker":"UNK","line":ln.strip(),"role_hint":"UNK"}
                           for ln in raw.split("\n") if ln.strip()]
            rec["diarization"]="none"
            rec["tag_notes"]="whisperX 커버리지 미달(원본 전문 유지·화자 미구분)."
            lowcov.append((v,cov)); continue
        rec["script"]=script
        rec["diarization"]="whisperx"
        rec["tag_notes"]="whisperX 세그먼트(화자+타임스탬프) 반영. 세그먼트 단위 화자(거친 granularity)."
        applied.append((v,len(script),len(order),cov))
    json.dump(db,open(DB,"w",encoding="utf-8"),ensure_ascii=False,indent=1)
    print(f"세그먼트 반영: {len(applied)}편")
    for v,ns,nspk,cov in applied: print(f"  {v:13} 버블={ns:2} 화자={nspk} 커버={cov:.0%}")
    if missing: print("DB에 없어 건너뜀:",missing)
    if lowcov: print("커버리지 낮음(주의):",lowcov)

if __name__=="__main__": main()
