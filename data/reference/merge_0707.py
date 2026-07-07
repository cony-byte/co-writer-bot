#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0707 배치 drama_clip(대본 100자+) 정제분 병합. merge_0706.py와 동일 구조.
사용: python3 reference/merge_0707.py --csv /tmp/260707.csv
이후 enrich.py + retag_v4.py 실행."""
import argparse, csv, json, os
BASE=os.path.dirname(os.path.abspath(__file__)); DB=os.path.join(BASE,"reference_db.json")
def _f(x):
    try: return float(str(x).replace("%","").strip())
    except: return None
def _tags(x): return [t.strip() for t in (x or "").split("|") if t.strip()]

REFINED={
"7659294600489864468":{"transcript_form":"dialogue","hook_desc":"'너 나랑 진짜 닮았어'라며, 과거로 돌아가 부모를 구하려는 인물의 얽힌 정체가 드러난다.","hook_desc_confidence":0.4,
 "script":[{"speaker":"UNK","line":"너 나랑 진짜 닮았어, 완전 똑같아."},{"speaker":"UNK","line":"걔가 제 발로 떠난 거야. 어떻게 걔를 버릴 수가 있겠어."},{"speaker":"UNK","line":"너 계속 네 아빠한테 투자받으려고 한 거야, 아니면 네 아빠한테 투자하려던 거야?"}],
 "tags":{"hook_type":"identity_reveal_hook","story_type":"secret_reveal_betrayal_drama","dialogue_tags":["secret_lie_or_reveal","emotional_question_or_confrontation"],"trope_tags":["secret_identity_or_hidden_truth","second_chance_or_regret"],"male_lead":["unknown"],"setting":"","hook_modality":"dialogue","visual_hook":"","narration_form":"dialogue_only"},
 "tag_confidence":0.4,"tag_notes":"대사 파편적, 화자 불명. 과거로 돌아가 부모 구하기(회귀) 모티프(desc)."},
"7639357726589504781":{"transcript_form":"mixed","hook_desc":"'얼마 받아?' '시간당 100달러' — 계약으로 시작한 관계가 5년 뒤 '계약 끝'이라는 배신으로 뒤집힌다.","hook_desc_confidence":0.7,
 "script":[{"speaker":"FL","line":"어머, 안녕. 얼마 받아?"},{"speaker":"ML","line":"시간당 100달러야."},{"speaker":"FL","line":"그럼 5년 계약으로 할게. 안아줘, 나 너무 힘들어."},{"speaker":"ML","line":"그래, 자기야. 왠지 너를 평생 알아온 것 같아."},{"speaker":"FL","line":"나 테스트해봤어. 축하드려요."},{"speaker":"ML","line":"야호! 내가 아빠가 되는 거야! 이거 진짜 내 아이 맞아?"},{"speaker":"FL","line":"응. 당신이 우리 아기 아빠라서 정말 행복해."},{"speaker":"FL","line":"자기야, 무슨 일 있어? 구독이 끝났어!"},{"speaker":"ML","line":"5년이 지났어, 계약이 끝났어. 원하면 연장해."},{"speaker":"FL","line":"우리 애는? 그냥 떠날 거야?"},{"speaker":"ML","line":"내 인생에서 가장 비싼 계약이었지."},{"speaker":"FL","line":"너 우리를 갖고 논 거야?"},{"speaker":"ML","line":"난 항상 계약 조건대로 할 뿐이야."},{"speaker":"NAR","line":"그는 왜 나한테 이렇게 잔인하게 굴었을까? 난 그를 사랑했는데, 우리 아이도 사랑했는데."}],
 "tags":{"hook_type":"power_or_money_hook","story_type":"marriage_family_drama","dialogue_tags":["power_money_or_status","love_confession_or_desire","marriage_family_or_pregnancy","secret_lie_or_reveal"],"trope_tags":["contract_or_fake_relationship","marriage_contract_or_family_pressure","revenge_betrayal_or_payback"],"male_lead":["dominant_possessive","powerful_status"],"setting":"fantasy_supernatural","hook_modality":"dialogue","visual_hook":"","narration_form":"mixed"},
 "tag_confidence":0.65,"tag_notes":"Brawl Stars 팬드라마('저승의 사랑'). 계약→임신→계약만료 배신."},
"7656737848904699153":{"transcript_form":"mixed","hook_desc":"'이 남자는 그룹 후계자, 나는 피라미드 맨 아래' — 신분 격차를 나레이션으로 깔고 날아온 공으로 두 세계가 처음 부딪힌다.","hook_desc_confidence":0.65,
 "script":[{"speaker":"NAR","line":"지금 보고 있는 이 남자는 렐릭스, 에릭 그룹의 후계자이자 미래의 하프더시티 주인. 태어날 때부터 먹이사슬 최상위, 쿼터백, 전과목 A."},{"speaker":"NAR","line":"그리고 이건 나, 플립. 학자금 대출로 겨우 이 학교에 들어온, 피라미드 맨 아래. 우린 절대 마주칠 일이 없는 사람들이었어."},{"speaker":"FL","line":"어, 뭐야, 저 사람이 나를 보고 있어. 저런 눈빛은 처음 보는데. 죄송하다는 말도 안 하네."},{"speaker":"ML","line":"죄송해요, 그럴 생각은 아니었는데. 이거 네 거네. 세게 부딪혔던데, 괜찮은 거 맞아?"},{"speaker":"FL","line":"네, 괜찮아요. 그냥 공에 좀 맞은 거예요."},{"speaker":"ML","line":"다음엔 구석에 앉지 마."},{"speaker":"SUP","line":"렐릭 씨가 그러던데, 우리한테 돌려줘야 할 펜이 있다고."}],
 "tags":{"hook_type":"power_or_money_hook","story_type":"power_status_romance","dialogue_tags":["power_money_or_status","emotional_question_or_confrontation"],"trope_tags":["class_gap_cinderella","protective_male_or_partner"],"male_lead":["powerful_status","protective_rescuer"],"setting":"school_campus","hook_modality":"dialogue","visual_hook":"","narration_form":"mixed"},
 "tag_confidence":0.6,"tag_notes":"나레이션 도입부 다량. 신분격차 캠퍼스(Misplaced EP1)."},
"7658266002110631199":{"transcript_form":"mixed","hook_desc":"미래에서 온 아이가 서로 앙숙인 두 남녀 앞에 나타나 '엄마·아빠'라 부르며 친자 확인 소동이 벌어진다.","hook_desc_confidence":0.6,
 "script":[{"speaker":"SUP","line":"오, 우리 아들 드디어 여자친구 생겼구나?"},{"speaker":"ML","line":"여자친구요? 아, 아닙니다."},{"speaker":"NAR","line":"사실 그건 그가 세상에서 가장 싫어하는, 어릴 때부터 만나기만 하면 싸우던 여자가 보낸 것이었다."},{"speaker":"SUP","line":"이 아이가 본인 자녀라고 하는데요."},{"speaker":"FL","line":"네? 무슨 소리예요?"},{"speaker":"SUP","line":"엄마. 저는 2031년에 태어났어요. 엄마 집 찾는 건 어렵지 않았어요."},{"speaker":"FL","line":"얘… 미래에서 온 거야? (남자를 찾아가) 이거 봐. 이 아이, 네 애야."},{"speaker":"ML","line":"병원 가봐. 정신과 쪽으로. 저 애가 방금 나한테 아빠라고 했어? 나 스물넷이야, 연애도 한 번 안 해봤는데."},{"speaker":"FL","line":"자, 봐. 이게 증거야."}],
 "tags":{"hook_type":"identity_reveal_hook","story_type":"marriage_family_drama","dialogue_tags":["marriage_family_or_pregnancy","secret_lie_or_reveal","emotional_question_or_confrontation"],"trope_tags":["enemies_to_lovers","secret_identity_or_hidden_truth"],"male_lead":["cold_to_warm","powerful_status"],"setting":"","hook_modality":"dialogue","visual_hook":"","narration_form":"mixed"},
 "tag_confidence":0.6,"tag_notes":"미래에서 온 시크릿베이비 + 앙숙물(중국 드라마). STT에 LLM 지문 다수."},
"7657013795637775647":{"transcript_form":"mixed","hook_desc":"짝사랑 지크를 얻으려는 여주가 '나 좀 가르쳐줘'라 청하자 조정부 남주가 연애 코치를 자처한다.","hook_desc_confidence":0.6,
 "script":[{"speaker":"NAR","line":"올림피아 기숙학교에서는 제일 잘생긴 애들만 조정팀에 들어가. 내가 여기 있는 진짜 이유는 지크를 계속 좋아해왔기 때문이야."},{"speaker":"SUP","line":"타샤? 지크가 너 같은 매력 제로인 애한테 눈길이나 줄 것 같아?"},{"speaker":"FL","line":"지크는 뭘 좀 아는 여자를 원해. 그러니까 네가 나 좀 가르쳐줄래?"},{"speaker":"ML","line":"나 사실 가르치는 거 되게 잘해. 다음 수업 시작한다. 두 번째 단계."},{"speaker":"ML","line":"걔는 너한테 안 어울려."},{"speaker":"FL","line":"너 내 남자친구 아니잖아. 그런 말 한 건 그냥 너랑 자고 싶어서 그런 거였어."},{"speaker":"ML","line":"그럼 그날 밤은 뭔데? 나에 대해서 그렇게 말했던 건 다 뭐고?"},{"speaker":"FL","line":"나 너 좋아하는 것 같아, 브래드. 친구 이상으로."}],
 "tags":{"hook_type":"confession_or_desire_hook","story_type":"dialogue_conflict_driven","dialogue_tags":["love_confession_or_desire","emotional_question_or_confrontation"],"trope_tags":["love_triangle_or_rival","misunderstanding_to_reconciliation"],"male_lead":["cold_to_warm"],"setting":"school_campus","hook_modality":"dialogue","visual_hook":"","narration_form":"mixed"},
 "tag_confidence":0.6,"tag_notes":"연애 코치→진심 전환. 캠퍼스 조정부."},
"7630935978835791120":{"transcript_form":"dialogue","hook_desc":"'널 구해준 게 아니라 사들인 거야 — 2천만 달러' 며 남주가 몸값을 내세워 소유를 선언한다.","hook_desc_confidence":0.7,
 "script":[{"speaker":"FL","line":"이건 구조가 아니야."},{"speaker":"ML","line":"널 구해준 게 아니라고, 셰인. 널 사들인 거야. 방금 너한테 2천만 달러를 썼어. 네 몸 구석구석, 근육 하나하나까지 다 내 거야."},{"speaker":"FL","line":"갚을게."},{"speaker":"ML","line":"그래, 갚아야지. 어떻게 갚을지는 내가 정해."},{"speaker":"FL","line":"싫어, 이건 못 해. 돈으로 갚으면 그걸로 끝이야."},{"speaker":"ML","line":"힘이 좋네, 셰인. 난 내 애완동물이 강할수록 좋더라. 무너뜨릴 때 더 짜릿해지거든."},{"speaker":"SUP","line":"네, 보스. 셰인 브룩스를 별장으로 데려오겠습니다."}],
 "tags":{"hook_type":"power_or_money_hook","story_type":"power_status_romance","dialogue_tags":["power_money_or_status","threat_danger_or_revenge","jealousy_possession_or_rival"],"trope_tags":["obsessive_devotion","boss_employee_or_power_romance","enemies_to_lovers"],"male_lead":["dominant_possessive","powerful_status"],"setting":"chaebol_highsociety","hook_modality":"dialogue","visual_hook":"","narration_form":"dialogue_only"},
 "tag_confidence":0.7,"tag_notes":"매수·소유 코드, 라이벌물(Bound To My Hot Rival)."},
"7624514679674539285":{"transcript_form":"mixed","hook_desc":"재벌 남편이 계약 아내에게 '500만은 비상금'이라며 명품·교양을 요구하고, 여주는 '돈 벌러 온 것'이라 다짐한다.","hook_desc_confidence":0.65,
 "script":[{"speaker":"ML","line":"카드에 500만 비상금 있으니까 먼저 갖고 써. 이 돈으로 차 한 대 사도 되고, 운전하기 싫으면 기사 붙여줄게."},{"speaker":"FL","line":"좋아요, 그럼 받을게요."},{"speaker":"NAR","line":"이렇게 대범하게 나오는데, 나도 프로답게 굴어야지."},{"speaker":"ML","line":"명심해, 내 아내는 내 얼굴이야. 옷차림, 화장, 자세, 교양, 전부 신경 써야 해. 옷은 속옷부터 겉옷까지 전부 명품이어야 하고."},{"speaker":"NAR","line":"난 돈 벌러 온 거지, 연애하러 온 게 아니야. 돈 쓰는 걸 일로 삼을 수 있다니 이건 내 복이지."},{"speaker":"FL","line":"알겠어요, 사장님. 내일 바로 백화점 가서 쇼핑할게요."},{"speaker":"ML","line":"지금은 아직 시혼 기간이니까, 정식으로 결혼하면 그때 다시 불러. 앉아요, 송 양."}],
 "tags":{"hook_type":"power_or_money_hook","story_type":"power_status_romance","dialogue_tags":["power_money_or_status","marriage_family_or_pregnancy"],"trope_tags":["contract_or_fake_relationship","boss_employee_or_power_romance","class_gap_cinderella"],"male_lead":["dominant_possessive","powerful_status"],"setting":"chaebol_highsociety","hook_modality":"dialogue","visual_hook":"","narration_form":"mixed"},
 "tag_confidence":0.7,"tag_notes":"계약결혼(시혼) 재벌물, 혼잣말 NAR. 중국 드라마."},
"7634117233039494420":{"transcript_form":"mixed","hook_desc":"'너희를 위해서라면 뭐든 하겠다'는 엄마가 아이를 떠나보내며 희생을 감내한다.","hook_desc_confidence":0.5,
 "script":[{"speaker":"FL","line":"미안해, 가끔 내가 약해질 때도 있지만 넌 내 힘이야. 너희 둘을 위해서라면 뭐든 다 할 거야."},{"speaker":"FL","line":"공부 열심히 해. 우리 걱정은 하지 말고."},{"speaker":"SUP","line":"근데 엄마, 나 떠나고 싶지 않아."},{"speaker":"FL","line":"가야 해. 이게 다 너 미래를 위한 거야. 보고 싶을 거야. 정말 많이 사랑해."},{"speaker":"NAR","line":"어머니의 사랑엔 한계도 조건도 없다. 때로는 가장 큰 사랑이 가장 아픈 희생과 함께 온다."}],
 "tags":{"hook_type":"breakup_or_sacrifice_hook","story_type":"emotion_reaction_driven","dialogue_tags":["apology_regret_or_sacrifice","marriage_family_or_pregnancy"],"trope_tags":["breakup_sacrifice_or_noble_idiot"],"male_lead":["unknown"],"setting":"","hook_modality":"dialogue","visual_hook":"","narration_form":"mixed"},
 "tag_confidence":0.5,"tag_notes":"로맨스보다 모성 신파(Mother's Love). 남주 미등장."},
"7650076172520361229":{"transcript_form":"dialogue","hook_desc":"'그 아저씨는 우리 아빠가 아니야'라며 우는 아이에게, 언니가 '더 좋은 새 아빠를 찾아주겠다'고 약속한다.","hook_desc_confidence":0.6,
 "script":[{"speaker":"FL","line":"미안해, 아나. 울지 마. 이번엔 진짜 아빠를 못 찾았지만, 언니가 더 좋은 새 아빠를 꼭 만들어 줄게."},{"speaker":"SUP","line":"새 아빠? 정말? 너 지금 거짓말로 속이는 거지?"},{"speaker":"SUP","line":"이 애 진짜 딸이에요."},{"speaker":"FL","line":"넌 누구야?"},{"speaker":"SUP","line":"애 밥 굶기지 않으면 됐지, 뭘 더 바라."},{"speaker":"FL","line":"근데 왜 얘를 계속 고아로 놔두는 건데? 아나는 내가 지켜주는 애야. 한 번만 더 건드리면 절대 가만 안 둬."}],
 "tags":{"hook_type":"threat_or_protection_hook","story_type":"danger_protection_drama","dialogue_tags":["protective_claim_or_rescue","emotional_question_or_confrontation","threat_danger_or_revenge"],"trope_tags":["danger_rescue_romance","class_gap_cinderella"],"male_lead":["protective_rescuer","dangerous_forbidden"],"setting":"","hook_modality":"dialogue","visual_hook":"","narration_form":"dialogue_only"},
 "tag_confidence":0.5,"tag_notes":"스페인어 원작, 고아 보호 서사. 보호자가 여성(언니)."},
"7644620832018418960":{"transcript_form":"mixed","hook_desc":"인턴 첫날, 재벌 여자가 '앞으로 석 달간 넌 내 거'라며 상대 여자에게 계약 연애를 선언한다.","hook_desc_confidence":0.6,
 "script":[{"speaker":"NAR","line":"인턴 첫날."},{"speaker":"SUP","line":"어젯밤 그 여자? 어떻게 이렇게 예쁠 수가 있지? 돈까지 많으면, 내 여자친구 해줄래?"},{"speaker":"FL","line":"나 예쁘기만 한 거 아니야. 돈도 엄청 많아, 딱 네 타입이지. 이제야 내가 누군지 알아본 거야? 앞으로 석 달 동안, 넌 내 거야."}],
 "tags":{"hook_type":"first_encounter_hook","story_type":"power_status_romance","dialogue_tags":["love_confession_or_desire","power_money_or_status"],"trope_tags":["class_gap_cinderella","contract_or_fake_relationship","obsessive_devotion"],"male_lead":["unknown"],"setting":"office_workplace","hook_modality":"dialogue","visual_hook":"","narration_form":"mixed"},
 "tag_confidence":0.55,"tag_notes":"GL(백합) 시리즈, 남주 없음. 재벌 여주 주도."},
"7644016116787842334":{"transcript_form":"mixed","hook_desc":"여주가 '나 수잔은 벤과의 소울메이트 결속을 끊는다'며 7년의 인연을 스스로 끊어낸다.","hook_desc_confidence":0.7,
 "script":[{"speaker":"FL","line":"얼음과 불의 피로써, 나 수잔은 지금 이 순간 벤 롬바르디와의 소울메이트 결속을 끊는다. 7년의 감옥이 마침내 깨졌다."},{"speaker":"SUP","line":"일어났어!"},{"speaker":"ML","line":"수잔? 어디 있어? 대답해! 수잔! 말 좀 해봐!"},{"speaker":"NAR","line":"말도 안 돼. 그녀가 감히 결속을 끊을 리 없어. 날 7년이나 기다렸잖아."}],
 "tags":{"hook_type":"breakup_or_sacrifice_hook","story_type":"emotion_reaction_driven","dialogue_tags":["breakup_rejection_or_distance","emotional_question_or_confrontation"],"trope_tags":["breakup_sacrifice_or_noble_idiot","obsessive_devotion"],"male_lead":["devoted_straightforward","dangerous_forbidden"],"setting":"fantasy_supernatural","hook_modality":"dialogue","visual_hook":"","narration_form":"mixed"},
 "tag_confidence":0.65,"tag_notes":"늑대인간 소울메이트 결속 파기(Luna)."},
"7651219149707889951":{"transcript_form":"mixed","hook_desc":"아버지가 '한 푼도 없다, 나가라'며 딸을 내치고, 남동생이 신탁 포기 서명을 강요하자 여주가 거절한다.","hook_desc_confidence":0.6,
 "script":[{"speaker":"SUP","line":"변호사들이 다 처리했다. 한 푼도 없어. 타일러가 전부 가져가. 신탁도, 집도 이제 다 타일러 명의야. 이제 나가라, 에바."},{"speaker":"SUP","line":"아빠! 저는 아빠 딸이에요! 어떻게 저한테 이러실 수 있어요?"},{"speaker":"SUP","line":"서명 하나면 돼, 누나. 사인해. 그럼 다신 우리 볼 일 없을 테니까."},{"speaker":"FL","line":"사인 안 해. 엄마 신탁을 포기하라고? 종이 한 장으로 엄마가 나한테 남긴 걸 지울 수 있을 것 같아?"},{"speaker":"ML","line":"그 손 치워. 셋. 둘. 하나."},{"speaker":"FL","line":"고마워요. 우린 이만 가야 해서. 가자, 아가."},{"speaker":"NAR","line":"헤이즈 가문 신탁의 상속녀 스텔라, 그리고 그걸 미련 없이 등지고 떠난 여자."}],
 "tags":{"hook_type":"power_or_money_hook","story_type":"secret_reveal_betrayal_drama","dialogue_tags":["power_money_or_status","threat_danger_or_revenge","humiliation_status_drop_or_bullying","secret_lie_or_reveal"],"trope_tags":["revenge_betrayal_or_payback","secret_identity_or_hidden_truth","protective_male_or_partner"],"male_lead":["powerful_status"],"setting":"chaebol_highsociety","hook_modality":"dialogue","visual_hook":"","narration_form":"mixed"},
 "tag_confidence":0.6,"tag_notes":"상속 박탈+시크릿베이비(CEO가 아이 목소리 들음)+사이다. 화자 다수."},
"7645019504455503118":{"transcript_form":"dialogue","hook_desc":"마피아 남주가 '입 벌려, 피임약이야'라며 아이를 거부하고, 여주는 '당신 아이를 갖고 싶다'고 맞선다.","hook_desc_confidence":0.7,
 "script":[{"speaker":"ML","line":"입 벌려봐, 공주님. 피임약이야. 그냥 조심하자는 거지, 늘 하던 대로."},{"speaker":"FL","line":"알겠어, 그런데 만약 내가 이제 그만 먹고 싶다면? 당신 아이를 갖고 싶다면?"},{"speaker":"ML","line":"아리아, 마피아의 세계는 폭력과 피로 가득해. 그런 곳에 아이를 데려오는 거, 내가 어떻게 생각하는지 알잖아."},{"speaker":"FL","line":"그럼 내가 당신 아이를 갖고 싶어 하는 마음은 어쩌고? 내 꿈 같은 건 중요하지 않다는 거야?"},{"speaker":"ML","line":"이제 패밀리아가 네 가족이야, 아리아. 나랑 결혼한 순간 네 인생은 정해진 거야. 넌 카포의 여자니까."},{"speaker":"FL","line":"착한 애 될게."}],
 "tags":{"hook_type":"marriage_family_hook","story_type":"marriage_family_drama","dialogue_tags":["marriage_family_or_pregnancy","power_money_or_status","threat_danger_or_revenge"],"trope_tags":["marriage_contract_or_family_pressure","obsessive_devotion","danger_rescue_romance"],"male_lead":["dominant_possessive","dangerous_forbidden"],"setting":"","hook_modality":"dialogue","visual_hook":"","narration_form":"dialogue_only"},
 "tag_confidence":0.7,"tag_notes":"마피아 임신 갈등(Bound by Love, Luca)."},
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
        rec={"id":vid,"url":r.get("ranking_video_url",""),"author":r.get("ranking_author",""),
          "desc":(r.get("ranking_description") or "")[:200],"rank":int(_f(r.get("ranking_rank")) or 0) or None,
          "crawl_date":(r.get("crawl_date") or "2026-07-07").strip(),
          "metrics":{"views":_f(r.get("ranking_views")),"likes":_f(r.get("ranking_likes")),
            "saves":_f(r.get("ranking_saves")),"shares":_f(r.get("ranking_shares")),"comments":_f(r.get("ranking_comments")),
            "er":_f(r.get("ranking_ER%_(save+share+cmt)/views")),"save_rate":_f(r.get("ranking_save_rate%")),
            "dur":_f(r.get("ranking_duration_s")),"cut_count":_f(r.get("summary_cut_count")),"avg_cut":_f(r.get("summary_avg_cut_duration"))},
          "content_type":"drama_clip","transcript_raw":(r.get("script_transcript_ko") or "").strip(),
          "transcript_form":ref["transcript_form"],"script":ref["script"],
          "hook_desc":ref["hook_desc"],"hook_desc_confidence":ref["hook_desc_confidence"],
          "tags":{k:ref["tags"].get(k, "" if k not in("dialogue_tags","trope_tags","male_lead") else []) for k in
            ("hook_type","story_type","dialogue_tags","trope_tags","male_lead","setting","visual_hook","hook_modality","narration_form")},
          "tag_confidence":ref["tag_confidence"],"tag_notes":ref["tag_notes"],"tag_version":"v3.0",
          "needs_review": ref["tag_confidence"]<0.7 or ref["hook_desc_confidence"]<0.6,
          "legacy_tags":{"hook_type":r.get("script_hook_type",""),"story_type":r.get("script_story_type",""),
            "dialogue_tags":_tags(r.get("script_dialogue_grammar_tags")),"trope_tags":_tags(r.get("script_romance_trope_tags")),
            "hook":(r.get("script_hook_text_ko") or "")[:200],"tag_confidence":None}}
        db.append(rec); added+=1
    db.sort(key=lambda r:-(r["metrics"].get("er") or 0))
    json.dump(db,open(DB,"w",encoding="utf-8"),ensure_ascii=False,indent=1)
    print(f"병합 완료: 신규 {added}편, 총 {len(db)}편")

if __name__=="__main__": main()
