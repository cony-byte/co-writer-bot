# -*- coding: utf-8 -*-
"""
트렌드서치 모듈 v2 — 슬랙 에이전트용 (통합 DB v5 대응)
- 통합 정제 DB(reference_db.json, tag_version=v5.0)를 읽어 성과 가중 트렌드를 집계
- 검색 풀은 v4 생성축이 태깅된 편만: v4_tagged=true and v4_tag_confidence>=MIN_CONF
  (병합 후 전 레코드가 v5.0이므로 tag_version이 아니라 v4_tagged로 게이트 — 미태깅편 오염 방지)
- 카테고리 필터: trope는 trope_tags_ko(한글) 참조
- 시간축: crawl_date가 2개 구간 이상 쌓이면 자동으로 rising/falling 비교, 아니면 스냅샷 모드

사용:
    from bot.trend_search import TrendSearch
    ts = TrendSearch("data/reference/reference_db.json")
    print(ts.answer("요즘 뭐가 트렌드야?"))
    print(ts.answer("후회남 쪽 훅은 어때?"))
"""
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta

from .tag_vocab import FILTER_ALIASES  # 한글 키워드 → (catharsis|trope) 공유 테이블

# ---------------- 상수 ----------------
CAT_KO = {
    "regret_grovel": "후회남(처절한 후회·매달림)",
    "revenge_payback": "복수·응징 통쾌",
    "status_reversal": "신분 반전(신데렐라)",
    "devotion_thrill": "집착·독점욕 설렘",
    "salvation": "구원·치유",
    "forbidden_tension": "금단 긴장",
    "humor_flutter": "코믹 티키타카 설렘",
}
MIN_CONF = 0.6          # 검색 풀 신뢰도 게이트 (v4_tag_confidence)
MIN_SIDE = 5            # 시간축 비교: 최근/과거 각 최소 표본
MIN_SPAN_DAYS = 30      # 게시일 범위가 이 이상이어야 추세 비교 발동


class TrendSearch:
    def __init__(self, db_path):
        with open(db_path, encoding="utf-8") as f:
            data = json.load(f)
        # v4 생성축 태깅 + 신뢰도 게이트 통과분만 검색 풀에 편입
        self.pool = [
            x for x in data
            if x.get("v4_tagged") and (x.get("v4_tag_confidence") or 0) >= MIN_CONF
        ]
        self.all_tagged = [x for x in data if x.get("v4_tagged")]
        # 시간축 = 실제 게시일(publish_dt). 우리가 긁은 날(crawl_date)이 아니라 콘텐츠가 뜬 시점.
        self.pub_dates = sorted(d for d in (self._pub(x) for x in self.pool) if d)

    @staticmethod
    def _pub(x):
        s = x.get("publish_dt") or ""
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    # ---------------- 성과 점수 ----------------
    @staticmethod
    def _score(x):
        m = x.get("metrics", {})
        views = math.log10((m.get("views") or 1) + 1)   # 규모(로그 보정)
        er = m.get("er") or 0                            # 반응률
        sr = m.get("save_rate") or 0                     # 저장률(대본 참고가치 신호)
        return views * (1 + er / 2 + sr)

    # ---------------- 시간축 ----------------
    def _split_time(self):
        """게시일(publish_dt) 중앙값으로 (최근, 과거) 두 풀로 분리. 표본·범위 부족 시 None."""
        dated = sorted(((d, x) for x in self.pool if (d := self._pub(x))),
                       key=lambda t: t[0])
        if len(dated) < MIN_SIDE * 2:
            return None
        if (dated[-1][0] - dated[0][0]).days < MIN_SPAN_DAYS:
            return None
        mid = len(dated) // 2
        past = [x for _, x in dated[:mid]]
        recent = [x for _, x in dated[mid:]]
        if len(recent) < MIN_SIDE or len(past) < MIN_SIDE:
            return None
        return recent, past

    # ---------------- 집계 ----------------
    def _agg(self, items, field, is_list=False):
        s = defaultdict(lambda: [0.0, 0])
        for x in items:
            vals = x["tags"].get(field) or []
            if not is_list:
                vals = [vals] if vals else []
            for v in vals:
                s[v][0] += self._score(x)
                s[v][1] += 1
        return sorted(s.items(), key=lambda kv: -kv[1][0])

    def _apply_filter(self, items, flt):
        if not flt:
            return items
        kind, val = flt
        if kind == "catharsis":
            return [x for x in items if x["tags"].get("catharsis_type") == val]
        return [x for x in items if val in (x["tags"].get("trope_tags_ko") or [])]

    def _rising(self, field, is_list=False):
        """시간축 있으면 최근 vs 과거 점유율 변화 상위/하위 반환."""
        split = self._split_time()
        if not split:
            return None
        recent, past = split
        def share(items):
            agg = self._agg(items, field, is_list)
            total = sum(sc for _, (sc, _) in agg) or 1
            return {k: sc / total for k, (sc, _) in agg}
        r, p = share(recent), share(past)
        keys = set(r) | set(p)
        delta = sorted(((k, r.get(k, 0) - p.get(k, 0)) for k in keys), key=lambda kv: -kv[1])
        return delta

    # ---------------- 응답 빌더 (각각 따로 호출 가능) ----------------
    def _pool_note(self, items):
        if self.pub_dates:
            d = f"게시 {self.pub_dates[0].date()}~{self.pub_dates[-1].date()}"
        else:
            d = "게시일 미상"
        mode = "추세 비교(최근 vs 과거)" if self._split_time() else "스냅샷(성과 상위)"
        return f"_(검색풀 {len(items)}건 · {d} · {mode})_"

    def catharsis(self, flt=None):
        items = self._apply_filter(self.pool, flt)
        if not items:
            return "해당 카테고리의 신뢰도 높은 레퍼런스가 아직 없어요. (크롤링 보강 대상)"
        agg = self._agg(items, "catharsis_type")
        lines = [f"*🎭 정서 축 순위* {self._pool_note(items)}"]
        for i, (k, (sc, n)) in enumerate(agg[:4], 1):
            lines.append(f"{i}. {CAT_KO.get(k, k)} — 성과지수 {sc:.0f} (n={n})")
        rising = self._rising("catharsis_type")
        if rising:
            up = [CAT_KO.get(k, k) for k, dv in rising[:2] if dv > 0.03]
            if up:
                lines.append(f"📈 최근 상승: {', '.join(up)}")
        # 표본 경고
        thin = [CAT_KO[k] for k in CAT_KO if k not in dict(agg) or dict(agg)[k][1] < 3]
        if thin and not flt:
            lines.append(f"⚠️ 표본 부족(3건 미만): {', '.join(thin[:3])} — 이 축 트렌드는 아직 신뢰 낮음")
        return "\n".join(lines)

    def combos(self, flt=None):
        items = self._apply_filter(self.pool, flt)
        if len(items) < 3:
            return "조합을 뽑기엔 레퍼런스가 부족해요."
        s = defaultdict(lambda: [0.0, 0])
        for x in items:
            tr = sorted(set(x["tags"].get("trope_tags_ko") or []))
            for i in range(len(tr)):
                for j in range(i + 1, len(tr)):
                    s[(tr[i], tr[j])][0] += self._score(x)
                    s[(tr[i], tr[j])][1] += 1
        top = sorted(s.items(), key=lambda kv: -kv[1][0])[:4]
        lines = [f"*🧩 잘 나가는 트로프 조합* {self._pool_note(items)}"]
        for (a, b), (sc, n) in top:
            if n < 2:
                continue
            lines.append(f"• {a} × {b} — 성과지수 {sc:.0f} (n={n})")
        if len(lines) == 1:
            lines.append("• 반복 출현하는 조합이 아직 없음 (표본 확대 필요)")
        return "\n".join(lines)

    def hooks(self, flt=None):
        items = self._apply_filter(self.pool, flt)
        if not items:
            return "해당 카테고리의 신뢰도 높은 레퍼런스가 아직 없어요."
        agg = self._agg(items, "hook_beat", is_list=True)
        lines = [f"*🪝 도입 훅 비트 순위* {self._pool_note(items)}"]
        for i, (k, (sc, n)) in enumerate(agg[:4], 1):
            lines.append(f"{i}. {k} — 성과지수 {sc:.0f} (n={n})")
        # 대표 훅 사례: 상위 클립의 hook_desc
        top = sorted(items, key=lambda x: -self._score(x))[:2]
        for x in top:
            lines.append(f"  ↳ 예) {x['hook_desc'][:70]} (@{x['author']})")
        return "\n".join(lines)

    def cliffhangers(self, flt=None):
        items = self._apply_filter(self.pool, flt)
        if not items:
            return "해당 카테고리의 신뢰도 높은 레퍼런스가 아직 없어요."
        agg = self._agg(items, "cliffhanger_type")
        lines = [f"*✂️ 엔딩/절단점 유형 순위* {self._pool_note(items)}",
                 "_(텍스트 기반 추정치 — 실제 컷 지점은 영상 확인 필요)_"]
        for i, (k, (sc, n)) in enumerate(agg[:4], 1):
            lines.append(f"{i}. {k} — 성과지수 {sc:.0f} (n={n})")
        return "\n".join(lines)

    def top_clips(self, flt=None, n=3):
        items = self._apply_filter(self.pool, flt)
        top = sorted(items, key=lambda x: -self._score(x))[:n]
        if not top:
            return "해당 카테고리의 신뢰도 높은 레퍼런스가 아직 없어요."
        lines = [f"*🏆 성과 톱 클립* {self._pool_note(items)}"]
        for x in top:
            m = x["metrics"]
            lines.append(
                f"• @{x['author']} — {CAT_KO.get(x['tags'].get('catharsis_type'),'')} / "
                f"{'·'.join((x['tags'].get('trope_tags_ko') or [])[:2])}\n"
                f"  {x['hook_desc'][:70]}\n"
                f"  조회 {m['views']:,.0f} · ER {m['er']}% · 저장률 {m['save_rate']}% · {x['url']}"
            )
        return "\n".join(lines)

    def overall(self, flt=None):
        parts = [self.catharsis(flt), self.combos(flt), self.hooks(flt), self.top_clips(flt, n=2)]
        return "\n\n".join(parts)

    # ---------------- 카테고리 필터 감지 ----------------
    @staticmethod
    def _alias_filter(q):
        """alias 테이블 부분일치 (즉시·무료). 첫 매칭 반환, 없으면 None."""
        for kw, f in FILTER_ALIASES.items():
            if kw in q:
                return f
        return None

    def _llm_filter(self, q, llm):
        """alias 미스 시 폴백 — LLM이 질문을 필터 코드 1개(또는 none)로 분류.
        '회한·미련·뒤늦게 깨달음'처럼 사전에 없는 자연어도 잡기 위함.
        llm: Callable[[system, user], str]. 실패·무관 판정 시 None(전체 트렌드로 진행)."""
        cats, trps = [], []
        for _, (kind, val) in FILTER_ALIASES.items():
            (cats if kind == "catharsis" else trps).append(val)
        cats = list(dict.fromkeys(cats))          # 순서 유지 dedup
        trps = list(dict.fromkeys(trps))
        system = (
            "너는 숏드라마 트렌드 질문을 카테고리 코드 하나로 분류하는 라우터다.\n"
            "작가의 질문이 아래 목록 중 무엇을 겨냥하는지 판단해 코드를 정확히 하나만 출력한다.\n"
            "특정 카테고리를 겨냥하지 않는 포괄적 질문(예: '요즘 뭐가 유행이야')이면 none 을 출력한다.\n"
            "코드 문자열 하나 또는 none 외의 다른 말·설명·기호·따옴표는 절대 출력하지 마라.\n\n"
            "[정서 코드]\n" + "\n".join(f"- {v} ({CAT_KO.get(v, v)})" for v in cats) +
            "\n\n[트로프 코드]\n" + "\n".join(f"- {v}" for v in trps) +
            "\n\n[해당 없음]\n- none"
        )
        try:
            raw = (llm(system, f"질문: {q}") or "").strip()
        except Exception:
            return None
        if not raw:
            return None
        token = raw.splitlines()[0].strip().strip("`\"' ")
        if token.lower() == "none":
            return None
        if token in cats:
            return ("catharsis", token)
        if token in trps:
            return ("trope", token)
        # 느슨한 폴백: 응답 안에 코드가 통째로 포함돼 있으면 인정 (영/한 겹침 없음)
        for v in cats:
            if v in raw:
                return ("catharsis", v)
        for v in trps:
            if v in raw:
                return ("trope", v)
        return None

    # ---------------- 질문 라우팅 ----------------
    def answer(self, question: str, llm=None) -> str:
        q = question.strip()
        # 1) 카테고리 필터: alias 부분일치 먼저 → 미스면 LLM 폴백(있을 때만)
        flt = self._alias_filter(q)
        if flt is None and llm is not None:
            flt = self._llm_filter(q, llm)
        # 2) 의도 감지 (구체적 의도 먼저, 없으면 전체)
        if re.search(r"엔딩|절단|끊|클리프|마무리", q):
            return self.cliffhangers(flt)
        if re.search(r"훅|도입|첫\s*(3초|장면)|오프닝", q):
            return self.hooks(flt)
        if re.search(r"조합|공식|같이|섞", q):
            return self.combos(flt)
        if re.search(r"정서|카타르시스|감정\s*축", q):
            return self.catharsis(flt)
        if re.search(r"클립|레퍼런스|사례|예시|영상", q):
            return self.top_clips(flt)
        # 전체 스냅샷
        return self.overall(flt)


if __name__ == "__main__":
    # 데모: python3 -m bot.trend_search (상대 import 때문에 -m으로 실행)
    from . import config
    ts = TrendSearch(str(config.REFERENCE_DIR / "reference_db.json"))
    for q in [
        "요즘 뭐가 트렌드야?",
        "요즘 잘 나가는 조합 뭐야",
        "엔딩은 뭘로 끊는 게 좋아?",
        "후회남 쪽 훅은 어때?",
    ]:
        print("=" * 60)
        print("Q:", q)
        print(ts.answer(q))
        print()
