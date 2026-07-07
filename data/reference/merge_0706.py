#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0706 배치의 정제된 drama_clip(대본 100자+)을 reference_db.json에 병합.
CSV에서 메타/지표/transcript_raw를 뽑고, 아래 REFINED(수작업 정제)를 합쳐 레코드 생성.
이후 enrich.py(화자중립·발행시각·scenes)와 retag_v4.py(cats)를 돌린다.
사용: python3 reference/merge_0706.py --csv /tmp/260706.csv
"""
import argparse, csv, json, os
BASE=os.path.dirname(os.path.abspath(__file__))
DB=os.path.join(BASE,"reference_db.json")

def _f(x):
    try: return float(str(x).replace("%","").strip())
    except: return None
def _tags(x): return [t.strip() for t in (x or "").split("|") if t.strip()]

# 수작업 정제 (전체 대본 근거). speaker는 ML/FL/SUP/NAR/UNK (enrich가 화자N로 중립화)
REFINED={
"7652480175229160717":{"transcript_form":"dialogue","hook_desc":"두 사람이 취기 어린 농담을 주고받다 '우리 한 달 방과후 남게 됐다'며 함께 갇힌 상황이 드러난다.","hook_desc_confidence":0.55,
 "script":[{"speaker":"ML","line":"나 진짜 얘기하기 편한 사람이거든?"},{"speaker":"FL","line":"그냥 네가 취해서 그렇게 보이는 거 아니야?"},{"speaker":"ML","line":"취해서 보이는 거야, 네가 얼마나 인간적인지."},{"speaker":"FL","line":"저리 좀 떨어져, 링컨."},{"speaker":"ML","line":"그건 안 되지. 왜인지 알아? 우리 한 달 동안 방과후 남게 됐거든! 그럼 이만."}],
 "tags":{"hook_type":"status_quo_break_hook","story_type":"dialogue_conflict_driven","dialogue_tags":["emotional_question_or_confrontation","love_confession_or_desire"],"trope_tags":["enemies_to_lovers"],"male_lead":["cold_to_warm"],"setting":"school_campus","hook_modality":"dialogue","visual_hook":"","narration_form":"dialogue_only"},
 "tag_confidence":0.6,"tag_notes":"화자 배정 추정(링컨=ML). 방과후 남기 설정."},
"7655811837375597831":{"transcript_form":"dialogue","hook_desc":"아버지가 (위장 연인인) 두 사람의 키스를 목격하고 '그렇게 오래 입맞추라는 건 아니었다'며 흡족해한다.","hook_desc_confidence":0.6,
 "script":[{"speaker":"SUP","line":"야, 야, 그만해. 이제 그만하면 됐다."},{"speaker":"SUP","line":"아빠가 말한 건… 아니, 아니, 그렇게 오래 입맞추라는 뜻이 아니었어."},{"speaker":"SUP","line":"이제 믿는다, 믿어. 좋아, 이거 정말 잘됐구나. 이건 정말 기쁜 일이라고 생각한다."},{"speaker":"SUP","line":"내가 선물을 주마, 새아가 맞이하는 뜻으로. 아빠의 정성이라고 생각해다오."}],
 "tags":{"hook_type":"marriage_family_hook","story_type":"marriage_family_drama","dialogue_tags":["marriage_family_or_pregnancy"],"trope_tags":["contract_or_fake_relationship","marriage_contract_or_family_pressure"],"male_lead":["unknown"],"setting":"","hook_modality":"dialogue","visual_hook":"","narration_form":"dialogue_only"},
 "tag_confidence":0.65,"tag_notes":"대사 대부분 아버지(SUP). 위장 연인을 가족이 승인하는 장면. 남주 미등장."},
"7654974184598097172":{"transcript_form":"monologue","hook_desc":"남자가 '이렇게 미쳐버린 내 사랑'이라며 방에 틀어박혀 '언니'를 향한 집착적 사랑을 토로한다.","hook_desc_confidence":0.5,
 "script":[{"speaker":"ML","line":"그래, 다시는 돌아보지 않을 거야. 이렇게 미쳐버린 내 사랑."},{"speaker":"ML","line":"요즘 난 방 안에만 틀어박혀 지내. 내가 방에만 있었는지도 모르겠어."},{"speaker":"ML","line":"이러다간 정말 큰일 나겠어."},{"speaker":"ML","line":"언니, 사랑해요. 이렇게 오랫동안 언니를 기다려왔는데, 나는 계속 언니를 그리워할 거야, 알았지?"}],
 "tags":{"hook_type":"confession_or_desire_hook","story_type":"emotion_reaction_driven","dialogue_tags":["love_confession_or_desire"],"trope_tags":["obsessive_devotion"],"male_lead":["dominant_possessive"],"setting":"","hook_modality":"dialogue","visual_hook":"","narration_form":"narration_only"},
 "tag_confidence":0.5,"tag_notes":"STT 반복 정리. 집착 독백, '언니' 호칭(연상 여주 추정)."},
"7650585736294485262":{"transcript_form":"mixed","hook_desc":"하키 경기 직전 유니폼이 망가져 팀이 몰수패 위기에 몰리고, 그 사보타주가 여주 짓임이 드러난다.","hook_desc_confidence":0.6,
 "script":[{"speaker":"SUP","line":"이게 우리 경기다, 얘들아. 라이벌들한테 진짜 강자가 누군지 보여주자. 유니폼 입자!"},{"speaker":"SUP","line":"5분 안에 링크에 못 나가면 우리 몰수패야. 갈아입을 시간도 없어. 이제 어떡하지?"},{"speaker":"NAR","line":"날 괴롭히던 애는 모르겠지 — 저 유니폼 망친 게 나라는 걸."},{"speaker":"SUP","line":"킹스팀을 향해 큰 소리로 환호해 주세요! 주장, 메이슨 더 킹!"},{"speaker":"ML","line":"조심해, 공주님. 내가 이기고 나면 너부터 손에 넣고 말 테니까."}],
 "tags":{"hook_type":"status_quo_break_hook","story_type":"jealousy_rival_drama","dialogue_tags":["emotional_question_or_confrontation","humiliation_status_drop_or_bullying","jealousy_possession_or_rival"],"trope_tags":["enemies_to_lovers","revenge_betrayal_or_payback"],"male_lead":["dominant_possessive"],"setting":"school_campus","hook_modality":"dialogue","visual_hook":"","narration_form":"mixed"},
 "tag_confidence":0.6,"tag_notes":"화자 다수 추정, 여주 VO는 NAR. 하키부 라이벌물."},
"7644003826877451551":{"transcript_form":"monologue","hook_desc":"여자가 '내 이름은 콜레트야, 이미 알고 있잖아'라며 도발적으로 말을 건다.","hook_desc_confidence":0.4,
 "script":[{"speaker":"FL","line":"내 이름은 콜레트야, 근데 이미 알고 있잖아."},{"speaker":"FL","line":"난 벽 같은 거 필요 없어, 자기야. 난 그냥 알짜배기가 필요해."},{"speaker":"FL","line":"별 같은 거 쳐다보지 마, 친구야. 이건 그런 거 아니야."},{"speaker":"FL","line":"날것 그대로 줘봐, 난…"}],
 "tags":{"hook_type":"confession_or_desire_hook","story_type":"emotion_reaction_driven","dialogue_tags":["love_confession_or_desire"],"trope_tags":[],"male_lead":["unknown"],"setting":"school_campus","hook_modality":"dialogue","visual_hook":"","narration_form":"dialogue_only"},
 "tag_confidence":0.4,"tag_notes":"가사체/번역 모호, 화자 여주(콜레트) 추정. Taming My Bullies 시리즈(캠퍼스)."},
"7644648326046043406":{"transcript_form":"mixed","hook_desc":"실랑이 끝에, 자신을 싫어하던 카우보이가 남주의 몸을 씻겨주기 시작한다.","hook_desc_confidence":0.6,
 "script":[{"speaker":"SUP","line":"내가 알아서 할 수 있어."},{"speaker":"ML","line":"돌아서 봐. 너 나보다 더 더러워졌잖아."},{"speaker":"SUP","line":"보지 말라고 했잖아."},{"speaker":"ML","line":"겁쟁이처럼 굴지 마. 이래도 안 아파. 팔 내려봐."},{"speaker":"NAR","line":"날 싫어하는 이 섹시한 이성애자 카우보이가 왜 지금 내 몸을 씻겨주고 있는 거지?"},{"speaker":"SUP","line":"어디까지 내려갈 생각이야?"},{"speaker":"ML","line":"그냥 더 신경 써야 할 곳이 있나 보는 것뿐이야."},{"speaker":"SUP","line":"뭐 찾았어?"},{"speaker":"ML","line":"좀 더 자세히 봐야겠는데."}],
 "tags":{"hook_type":"emotional_question_hook","story_type":"dialogue_conflict_driven","dialogue_tags":["emotional_question_or_confrontation","protective_claim_or_rescue"],"trope_tags":["enemies_to_lovers","forbidden_love","healing_or_comfort"],"male_lead":["cold_to_warm"],"setting":"","hook_modality":"dialogue","visual_hook":"","narration_form":"mixed"},
 "tag_confidence":0.55,"tag_notes":"BL. 화자 2인(카우보이/주인공)+VO(NAR)."},
"7654574724214721805":{"transcript_form":"dialogue","hook_desc":"여주가 '나 좀 놔줘'라고 하자 남주가 '네가 도망 못 가게 하는 것뿐'이라며 가두려 든다.","hook_desc_confidence":0.7,
 "script":[{"speaker":"FL","line":"좋아, 좋아, 지금 뭐 하는 거야? 뭐 하는 거냐고? 나 좀 놔줘야지."},{"speaker":"ML","line":"네가 도망 못 가게 확인하는 것뿐이야. 이제 내 감정에서 도망 못 가게."},{"speaker":"FL","line":"너 진짜 미쳤구나, 그렇지?"},{"speaker":"ML","line":"너한테만 그래."}],
 "tags":{"hook_type":"emotional_question_hook","story_type":"dialogue_conflict_driven","dialogue_tags":["emotional_question_or_confrontation","jealousy_possession_or_rival"],"trope_tags":["obsessive_devotion"],"male_lead":["dominant_possessive","dangerous_forbidden"],"setting":"","hook_modality":"dialogue","visual_hook":"","narration_form":"dialogue_only"},
 "tag_confidence":0.7,"tag_notes":"집착·감금 코드(Caged by His Twisted Love)."},
"7649377666977533204":{"transcript_form":"mixed","hook_desc":"'그녀가 탈옥했다'는 경고와 함께, 냉철하던 부사장 남주가 여주 앞에 무릎 꿇고 아침을 챙긴다.","hook_desc_confidence":0.6,
 "script":[{"speaker":"SUP","line":"그녀가 탈옥했어요. 요즘 조심하세요."},{"speaker":"NAR","line":"사업에서는 그렇게 냉철하고 결단력 있던 부사장님이, 지금은 바닥에 무릎을 꿇고 있다."},{"speaker":"ML","line":"일어났어요? 먼저 아침 드세요. 아침 드시고 나면 기사한테 데려다 드리라고 할게요."}],
 "tags":{"hook_type":"threat_or_protection_hook","story_type":"power_status_romance","dialogue_tags":["threat_danger_or_revenge","protective_claim_or_rescue"],"trope_tags":["danger_rescue_romance","secret_identity_or_hidden_truth"],"male_lead":["powerful_status","devoted_straightforward"],"setting":"chaebol_highsociety","hook_modality":"dialogue","visual_hook":"","narration_form":"mixed"},
 "tag_confidence":0.6,"tag_notes":"탈옥·경호 서스펜스 + 부사장 헌신(Start From Scratch EP33)."},
}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--csv",required=True); a=ap.parse_args()
    rows=list(csv.DictReader(open(a.csv,encoding="utf-8-sig")))
    first={}
    for r in rows:
        vid=r.get("source_video_id","").strip()
        if vid and vid not in first: first[vid]=r
    db=json.load(open(DB,encoding="utf-8"))
    have={r["id"] for r in db}
    added=0
    for vid,ref in REFINED.items():
        if vid in have: print("이미 있음, 건너뜀:",vid); continue
        r=first.get(vid)
        if not r: print("CSV에 없음:",vid); continue
        tr=(r.get("script_transcript_ko") or "").strip()
        rec={
          "id":vid,"url":r.get("ranking_video_url",""),"author":r.get("ranking_author",""),
          "desc":(r.get("ranking_description") or "")[:200],"rank":int(_f(r.get("ranking_rank")) or 0) or None,
          "crawl_date":(r.get("crawl_date") or "2026-07-06").strip(),
          "metrics":{"views":_f(r.get("ranking_views")),"likes":_f(r.get("ranking_likes")),
            "saves":_f(r.get("ranking_saves")),"shares":_f(r.get("ranking_shares")),
            "comments":_f(r.get("ranking_comments")),
            "er":_f(r.get("ranking_ER%_(save+share+cmt)/views")),"save_rate":_f(r.get("ranking_save_rate%")),
            "dur":_f(r.get("ranking_duration_s")),"cut_count":_f(r.get("summary_cut_count")),
            "avg_cut":_f(r.get("summary_avg_cut_duration"))},
          "content_type":"drama_clip","transcript_raw":tr,
          "transcript_form":ref["transcript_form"],"script":ref["script"],
          "hook_desc":ref["hook_desc"],"hook_desc_confidence":ref["hook_desc_confidence"],
          "tags":{"hook_type":ref["tags"]["hook_type"],"story_type":ref["tags"]["story_type"],
            "dialogue_tags":ref["tags"]["dialogue_tags"],"trope_tags":ref["tags"]["trope_tags"],
            "male_lead":ref["tags"]["male_lead"],"setting":ref["tags"]["setting"],
            "visual_hook":ref["tags"].get("visual_hook",""),"hook_modality":ref["tags"].get("hook_modality",""),
            "narration_form":ref["tags"].get("narration_form","")},
          "tag_confidence":ref["tag_confidence"],"tag_notes":ref["tag_notes"],"tag_version":"v3.0",
          "needs_review": ref["tag_confidence"]<0.7 or ref["hook_desc_confidence"]<0.6,
          "legacy_tags":{"hook_type":r.get("script_hook_type",""),"story_type":r.get("script_story_type",""),
            "dialogue_tags":_tags(r.get("script_dialogue_grammar_tags")),"trope_tags":_tags(r.get("script_romance_trope_tags")),
            "hook":(r.get("script_hook_text_ko") or "")[:200],"tag_confidence":None},
        }
        db.append(rec); added+=1
    db.sort(key=lambda r:-(r["metrics"].get("er") or 0))
    json.dump(db,open(DB,"w",encoding="utf-8"),ensure_ascii=False,indent=1)
    print(f"병합 완료: 신규 {added}편, 총 {len(db)}편")

if __name__=="__main__": main()
