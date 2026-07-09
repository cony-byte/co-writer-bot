#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""260708 유튜브 BL AI 드라마 파일럿 10편 — 화자 구분만(+s5 자동태그, context 생략) 병합.
content_type=ai_generated, platform=youtube, genre=bl 설정. 이후 enrich→retag→set_gender.
사용: python3 reference/merge_bl_pilot.py --csv <260708 CSV>"""
import argparse, csv, json, os
BASE=os.path.dirname(os.path.abspath(__file__)); DB=os.path.join(BASE,"reference_db.json")
def _f(x):
    try: return float(str(x).replace("%","").strip())
    except: return None
def _tags(x): return [t.strip() for t in (x or "").split("|") if t.strip()]

# 화자 구분 중심(ML=남주,FL=여성캐,SUP=기타(BL이라 대개 남),NAR). hook_desc는 짧게. context 생략.
REFINED={
"wAxvhxmjADI":{"form":"mixed","hook":"회귀한 주인공이 '리셋 버튼'을 찾는 와중에 황태자가 '첫눈에 반했다'며 다가온다.",
 "script":[{"speaker":"UNK","line":"죽고 싶어서 환장했나 보네. 리셋 버튼이 어디 있지? 리셋!"},{"speaker":"SUP","line":"전하, 마음에 듭니다."},{"speaker":"ML","line":"우리 초면 아닌가요? 첫눈에 반했습니다."},{"speaker":"UNK","line":"핑계치고는 참 신선하네요. 이번 거짓말은 넘어가 드리죠. 다음엔 좀 더 창의적인 핑계를 준비하세요."},{"speaker":"NAR","line":"시스템 창이 안 뜨네. 리셋 버튼이 없어. 이러다 죽으면 진짜 끝인데."},{"speaker":"ML","line":"다치셨어요? 안색이 안 좋으신데."},{"speaker":"UNK","line":"처음 뵙는 분 앞에서 추한 모습을 보였네요. 방금 건 못 본 걸로 해주세요."},{"speaker":"ML","line":"잠깐만요, 아가씨. 피가 나고 있어요. 병원부터 가시죠."},{"speaker":"UNK","line":"괜찮습니다. 이 은혜는 꼭 갚겠습니다."}],
 "s5":{"hook":"first_encounter_hook","story":"danger_protection_drama","trope":"danger_rescue_romance|protective_male_or_partner|second_chance_or_regret","ml":"protective_rescuer|cold_to_warm","setting":"fantasy_supernatural"}},
"XtVsYeGUwms":{"form":"mixed","hook":"피범벅으로 동생 생일 연회에 난입한 황태자, '로맨스 루트' 시스템 창이 뜨며 회귀 게임이 시작된다.",
 "script":[{"speaker":"ML","line":"무슨 쥐새끼가 기어들어왔나 했더니, 에르카르트가의 미친개셨군."},{"speaker":"NAR","line":"두 시간 전으로 돌아가 보자. 칼리스토 황태자가 둘째 왕자의 생일 연회에 난입했다. 그것도 온몸이 피범벅인 채로."},{"speaker":"ML","line":"생일 축하한다, 아우야."},{"speaker":"NAR","line":"실물이 더 미쳤네. 절대 저 인간이랑은 엮이지 말아야겠다 싶었다. 그런데 바로 그때, 눈앞에 시스템 창이 떴다."},{"speaker":"UNK","line":"저 인간이랑 로맨스 루트라고? 로맨스는 무슨, 안 죽이면 다행이지. 뭐 어때, 죽으면 리셋하면 그만인데."},{"speaker":"ML","line":"연회에서 그 꼴을 보고도 날 따라오다니. 어지간히 뒤지고 싶은가 본데—"},{"speaker":"UNK","line":"리셋 버튼 어디 있어? 리셋!"}],
 "s5":{"hook":"threat_or_protection_hook","story":"danger_protection_drama","trope":"danger_rescue_romance|secret_identity_or_hidden_truth|enemies_to_lovers","ml":"dangerous_forbidden|powerful_status","setting":"fantasy_supernatural"}},
"bOnYhXPA3Fk":{"form":"dialogue","hook":"시한부 황태자가 혼약 논의를 피해 물러나고, 곁의 인물이 '저도 같은 저주에 걸려 있다'고 고백한다.",
 "script":[{"speaker":"SUP","line":"전하? 무슨 일이십니까? 온 사방을 다 찾아다녔습니다. 황제 폐하께서 혼약에 관해 이야기를 나누고 싶어하십니다."},{"speaker":"ML","line":"먼저 실례하겠습니다. 몸이 좋지 않아서요. 생각할 시간을 좀 주십시오."},{"speaker":"SUP","line":"여전히 건방지시군요. 괜찮으십니까, 전하!"},{"speaker":"ML","line":"부패가 벌써 심장까지 다다랐단 말입니까?"},{"speaker":"SUP","line":"완전히 퍼졌습니다. 저주를 건 초대 예언자를 찾아야 합니다. 부패를 억제해 드릴 수는 있지만, 완전히 치유해 드릴 수는 없습니다."},{"speaker":"ML","line":"그게 무슨 말이지?"},{"speaker":"SUP","line":"저도 저주에 걸려 있습니다, 전하. 전하와 마찬가지로요."}],
 "s5":{"hook":"marriage_family_hook","story":"secret_reveal_betrayal_drama","trope":"marriage_contract_or_family_pressure|secret_identity_or_hidden_truth","ml":"dominant_possessive|cold_to_warm","setting":"fantasy_supernatural"}},
"YTqLe9n6zI4":{"form":"dialogue","hook":"연회에서 황태자의 약혼(오라클 가문 영애)이 공표되고, 정작 전하는 사절단을 찾으며 딴 데 관심을 둔다.",
 "script":[{"speaker":"SUP","line":"폐하와 황태자 전하께서 입장하십니다."},{"speaker":"FL","line":"소식 들었어? 황태자 전하의 약혼녀가 이미 정해졌대. 오라클 가문의 영애, 엘레나 로엘이라던데."},{"speaker":"FL","line":"세상에! 저런 분을 낭군으로 맞다니. 다들 얼마나 부러워하는지."},{"speaker":"FL","line":"이렇게 전하를 직접 뵙게 되어 영광입니다. 오라클 가문의 엘레나 로엘입니다. 남부 전선 승리를 기념하는 연회, 참으로 아름답습니다."},{"speaker":"ML","line":"과분한 말씀이십니다. 오늘 연회에 사절단은 참석하지 않았나?"},{"speaker":"SUP","line":"사절단 모두 초대받아 참석했습니다. 무슨 문제라도?"},{"speaker":"ML","line":"아닙니다, 신경 쓰지 마세요."}],
 "s5":{"hook":"marriage_family_hook","story":"marriage_family_drama","trope":"marriage_contract_or_family_pressure|love_triangle_or_rival|secret_identity_or_hidden_truth","ml":"dominant_possessive|powerful_status","setting":"fantasy_supernatural"}},
"aWO8ZjInsuY":{"form":"mixed","hook":"90일간 근무할 외딴 등대에서 두 남자가 처음 만나고, 귀신 소문 속에 어색한 동거가 시작된다.",
 "script":[{"speaker":"ML","line":"안녕하세요, 서혜성입니다. 해수구에서 왔습니다. 잘 부탁드려요."},{"speaker":"SUP","line":"최무진입니다."},{"speaker":"ML","line":"무진 씨, 여기 가본 적 있으세요? 저 잠깐 바다 좀 보고 올게요."},{"speaker":"NAR","line":"저 사람이랑 어떻게 지내지? 근데 바다는 진짜 예쁘다."},{"speaker":"SUP","line":"바다 좋아해요?"},{"speaker":"ML","line":"오, 말할 줄 아네. 네, 좋아해요."},{"speaker":"SUP","line":"멀리서 보면 섬처럼 보인다고 옛날 사람들이 묵도라 불렀대요."},{"speaker":"SUP","line":"밤에 가끔 누가 계단 걷는 소리가 들리기도… 콘크리트가 소리를 잘 받아서 그래요."},{"speaker":"ML","line":"조심할게요, 선생님."}],
 "s5":{"hook":"first_encounter_hook","story":"secret_reveal_betrayal_drama","trope":"secret_identity_or_hidden_truth","ml":"cold_to_warm","setting":"everyday_neighborhood"}},
"RuArfVS3VYI":{"form":"mixed","hook":"등대 동거 중 무뚝뚝한 형이 '정 없는 척하면서도' 동생뻘 상대를 자꾸 챙긴다.",
 "script":[{"speaker":"ML","line":"무진 씨? 왜 이 시간에 여기 있어요? 전 스물 넷이에요. 형이라고 불러도 돼요?"},{"speaker":"SUP","line":"편한 대로 해요."},{"speaker":"NAR","line":"형이 먼저 말 꺼내는 일은 별로 없었지만 내 말에 늘 착실히 대답해줬다."},{"speaker":"ML","line":"형 여기 기름 묻었어요. 제가 닦을게요."},{"speaker":"SUP","line":"누가 잡아먹나?"},{"speaker":"ML","line":"형은 정 없는 척은 다 하면서 챙길 건 또 다 챙긴다니까. 왜 자꾸 챙겨줘요?"},{"speaker":"SUP","line":"제 동생 같아서요. 밤엔 추워요."},{"speaker":"ML","line":"우와 별 진짜 많다. 여기 오길 잘한 것 같아요."}],
 "s5":{"hook":"first_encounter_hook","story":"emotion_reaction_driven","trope":"healing_or_comfort|protective_male_or_partner","ml":"cold_to_warm|protective_rescuer","setting":"everyday_neighborhood"}},
"perJ5JZ8V8g":{"form":"dialogue","hook":"악몽에 시달리는 정우를 유연이 소금빵을 들고 찾아와 능청스럽게 곁을 파고든다.",
 "script":[{"speaker":"SUP","line":"정우야, 괜찮아? 안 좋은 꿈 꿨어? 안에서 끙끙대는 소리가 들리잖아."},{"speaker":"ML","line":"넌 여기 왜 있어? 이제 무단침입도 하네."},{"speaker":"SUP","line":"도어락 달아줄까? 배고프지? 나 소금빵 사왔는데, 같이 먹고 가면 안 돼?"},{"speaker":"ML","line":"…알았어. 먹고 가."},{"speaker":"SUP","line":"착하다니까. 근데 아까 무슨 꿈을 꿨길래 그래? 아파 보이길래 걱정했지."},{"speaker":"ML","line":"별 거 아니야. 가끔 악몽도 꾸고 그런 거지."},{"speaker":"SUP","line":"안 되겠다. 내가 윤정우 구하러 가야겠네."},{"speaker":"NAR","line":"김유연 눈이 이렇게 예뻤었나? 입술도 예쁘네."}],
 "s5":{"hook":"emotional_question_hook","story":"emotion_reaction_driven","trope":"healing_or_comfort|obsessive_devotion|secret_identity_or_hidden_truth","ml":"cold_to_warm|devoted_straightforward","setting":""}},
"u2jcvoaUk8M":{"form":"monologue","hook":"매 생마다 1년 뒤 죽음을 반복하던 회귀자가, 수많은 생 중 처음으로 '너뿐'인 사람 윤정우를 만난다.",
 "script":[{"speaker":"NAR","line":"나의 첫 번째 인생은 지옥이었다. 생의 끝에서 신에게 빌었다. 보통의 사람으로 다시 태어나게 해달라고."},{"speaker":"NAR","line":"나는 매번 새로운 세계에서 1년을 살았다. 끝은 늘 고통스러운 죽음이었다. 그렇게 죽고 다시 태어나기를 반복했다. 나는 영원히 21살에 갇혀 있었다."},{"speaker":"NAR","line":"몇 번째인지도 모를 그 인생에서, 기어이 너를 만났다. 윤정우. 내 모든 인생을 통틀어 나에겐 너뿐이었다."},{"speaker":"ML","line":"우리 정우 많이 무서웠겠다. 이런 걸로는 죽지 않아."},{"speaker":"SUP","line":"나 좀 행복했나 봐. 모든 걸 잠시 잊을 정도로."},{"speaker":"ML","line":"내가 영원히 여기에서 너를 기다리고 있을 거니까. 우리의 마지막은 해피엔딩일 거야. 사랑해, 정우야."}],
 "s5":{"hook":"","story":"emotion_reaction_driven","trope":"second_chance_or_regret|breakup_sacrifice_or_noble_idiot|obsessive_devotion","ml":"devoted_straightforward","setting":""}},
"RIisGq8Wes4":{"form":"dialogue","hook":"'네가 와줘서 다행'이라는 승희에게 도하가 저녁을 청하고, 취한 그를 집까지 데려다주겠다 나선다.",
 "script":[{"speaker":"SUP","line":"나 어떡해야 해? 네가 와줘서 정말 다행이야. 네가 도와주면 난 편히 눈감을 수 있어."},{"speaker":"ML","line":"승희야, 너한테 할 얘기가 너무 많아. 훈련에 대해서도, 도하에 대해서도."},{"speaker":"SUP","line":"내일 내가 놀러 가도 될까? 우리 뭐 좀 해 먹을까."},{"speaker":"ML","line":"혹시 나랑 저녁 같이 먹을래? 7시쯤 데리러 갈게. 괜찮아?"},{"speaker":"SUP","line":"응. 딱 좋아."},{"speaker":"ML","line":"오늘 밤은 이 정도면 충분히 마신 것 같은데. 어차피 교대 끝났어. 내가 집까지 데려다줄게."}],
 "s5":{"hook":"emotional_question_hook","story":"secret_reveal_betrayal_drama","trope":"misunderstanding_to_reconciliation|second_chance_or_regret|healing_or_comfort","ml":"protective_rescuer|devoted_straightforward","setting":""}},
"eHcxviZLiJM":{"form":"dialogue","hook":"'쏘지 마요' — 무장 안 한 케이를 두고 대치하며, 그를 잡으려는 과거사와 위험이 드러난다.",
 "script":[{"speaker":"ML","line":"저 사람은 무장도 안 했어요. 쏘지 말아줘."},{"speaker":"SUP","line":"저런 건 믿기야. 나를 잡기 위한 덫이지. 이제 너도 포함됐을지도."},{"speaker":"ML","line":"왜 케이를 잡으려고 해요?"},{"speaker":"SUP","line":"과거사야. 신경 꺼. 너무 조용한데?"},{"speaker":"ML","line":"다른 건물은 숨을 곳이 없어요."},{"speaker":"SUP","line":"나는 당신이 원하는 사람이다."}],
 "s5":{"hook":"threat_or_protection_hook","story":"danger_protection_drama","trope":"danger_rescue_romance|secret_identity_or_hidden_truth|protective_male_or_partner","ml":"dangerous_forbidden|protective_rescuer","setting":"fantasy_supernatural"}},
}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--csv",required=True); a=ap.parse_args()
    rows=list(csv.DictReader(open(a.csv,encoding="utf-8-sig")))
    first={}
    for r in rows:
        vid=r.get("source_video_id","").strip()
        if vid and vid not in first: first[vid]=r
    db=json.load(open(DB,encoding="utf-8")); have={r["id"] for r in db}; added=0
    for vid,ref in REFINED.items():
        if vid in have: print("이미 있음:",vid); continue
        r=first.get(vid)
        if not r: print("CSV에 없음:",vid); continue
        s5=ref["s5"]
        rec={"id":vid,"url":r.get("ranking_video_url",""),"author":r.get("ranking_author",""),
          "desc":(r.get("ranking_description") or "")[:200],"rank":int(_f(r.get("ranking_rank")) or 0) or None,
          "crawl_date":(r.get("crawl_date") or "2026-07-08").strip(),
          "publish_dt":(r.get("publish_dt") or "").strip(),
          "metrics":{"views":_f(r.get("ranking_views")),"likes":_f(r.get("ranking_likes")),
            "saves":_f(r.get("ranking_saves")),"shares":_f(r.get("ranking_shares")),"comments":_f(r.get("ranking_comments")),
            "er":_f(r.get("ranking_ER%_(save+share+cmt)/views")),"save_rate":_f(r.get("ranking_save_rate%")),
            "dur":_f(r.get("ranking_duration_s")),"cut_count":_f(r.get("summary_cut_count")),"avg_cut":_f(r.get("summary_avg_cut_duration"))},
          "content_type":"ai_generated","platform":(r.get("platform") or "youtube").strip(),"genre":(r.get("genre") or "bl").strip(),
          "transcript_raw":(r.get("script_transcript_ko") or "").strip(),
          "transcript_form":ref["form"],"script":ref["script"],
          "hook_desc":ref["hook"],"hook_desc_confidence":0.5,
          "tags":{"hook_type":s5["hook"],"story_type":s5["story"],"dialogue_tags":[],
            "trope_tags":_tags(s5["trope"]),"male_lead":_tags(s5["ml"]),"setting":s5["setting"],
            "visual_hook":"","hook_modality":"dialogue","narration_form":""},
          "tag_confidence":0.5,"tag_notes":"파일럿: 화자 구분+s5 자동태그, context 미저작. AI(유튜브 BL).","tag_version":"v3.0",
          "needs_review":False,
          "legacy_tags":{"hook_type":r.get("script_hook_type",""),"story_type":r.get("script_story_type",""),
            "dialogue_tags":_tags(r.get("script_dialogue_grammar_tags")),"trope_tags":_tags(r.get("script_romance_trope_tags")),
            "hook":(r.get("script_hook_text_ko") or "")[:200],"tag_confidence":None}}
        db.append(rec); added+=1
    db.sort(key=lambda r:-(r["metrics"].get("er") or 0))
    json.dump(db,open(DB,"w",encoding="utf-8"),ensure_ascii=False,indent=1)
    print(f"파일럿 병합: 신규 {added}편, 총 {len(db)}편")

if __name__=="__main__": main()
