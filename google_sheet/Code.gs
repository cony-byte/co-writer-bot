/**
 * co-writer-bot ↔ 구글 시트 바이블 웹앱
 *
 * 역할: 슬랙 봇이 넣은 작품 바이블을 시트에 저장하고, 봇이 생성 시 다시 읽는다.
 *       사람은 시트를 "열람"만 하면 된다 (입력은 슬랙 봇으로).
 *
 * 시트 구조 (탭 이름 'bible', 1행은 헤더):
 *   A: work(작품명)  B: kind(구분)  C: content(내용)  D: updated_at
 *   kind 예: 현재화 | 로그라인 | 타겟정서 | 인물 | 줄거리 | 회차표 | 24화_개요 | 24화_대본 | 기획안
 *   업서트 키 = (work, kind). 같은 작품+구분이면 그 행을 덮어씀.
 *
 * 배포:
 *   1) 구글 시트 새로 만들기 → 탭 이름을 'bible'로. (A1:D1에 work/kind/content/updated_at 헤더 권장)
 *   2) 확장 프로그램 → Apps Script → 이 코드 전체 붙여넣기
 *   3) SECRET 값을 아무 긴 무작위 문자열로 바꾸기 (봇 .env의 SHEET_SECRET과 동일하게)
 *   4) 배포 → 새 배포 → 유형: 웹 앱 → 실행: 나 / 액세스: 모든 사용자 → 배포
 *   5) 나온 웹앱 URL(/exec)을 봇 .env의 SHEET_WEBAPP_URL에 넣기
 */

var SECRET = "여기를_긴_무작위_문자열로_바꾸세요";  // .env SHEET_SECRET과 동일하게
var TAB = "bible";

function _sheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(TAB) || ss.insertSheet(TAB);
  if (sh.getLastRow() === 0) {
    sh.appendRow(["work", "kind", "content", "updated_at"]);
  }
  return sh;
}

function _rows(sh) {
  var last = sh.getLastRow();
  if (last < 2) return [];
  return sh.getRange(2, 1, last - 1, 4).getValues();  // 헤더 제외
}

/** 읽기: ?secret=..&work=작품명  (work 없으면 작품 목록) */
function doGet(e) {
  var p = e.parameter || {};
  if (p.secret !== SECRET) return _json({ error: "unauthorized" });
  var sh = _sheet();
  var rows = _rows(sh);
  if (!p.work) {
    var works = {};
    rows.forEach(function (r) { if (r[0]) works[r[0]] = true; });
    return _json({ works: Object.keys(works) });
  }
  var out = [];
  rows.forEach(function (r) {
    if (r[0] === p.work) out.push({ kind: r[1], content: r[2], updated_at: r[3] });
  });
  return _json({ work: p.work, rows: out });
}

/** 쓰기(업서트): POST JSON { secret, work, kind, content } */
function doPost(e) {
  var body = {};
  try { body = JSON.parse(e.postData.contents); } catch (err) { return _json({ error: "bad json" }); }
  if (body.secret !== SECRET) return _json({ error: "unauthorized" });
  if (!body.work || !body.kind) return _json({ error: "work, kind required" });

  var sh = _sheet();
  var rows = _rows(sh);
  var now = new Date().toISOString();
  for (var i = 0; i < rows.length; i++) {
    if (rows[i][0] === body.work && rows[i][1] === body.kind) {
      sh.getRange(i + 2, 3, 1, 2).setValues([[body.content || "", now]]);  // content, updated_at
      return _json({ ok: true, updated: true });
    }
  }
  sh.appendRow([body.work, body.kind, body.content || "", now]);
  return _json({ ok: true, created: true });
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
