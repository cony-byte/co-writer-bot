/**
 * co-writer-bot ↔ 구글 시트 바이블 웹앱 (탭 = 작품)
 *
 * 구조: 스프레드시트 1개 = 바이블 전체. 탭 1개 = 작품 1개(탭 이름 = 작품명).
 *   각 작품 탭의 열: A 대분류 | B 중분류 | C 소분류 | D 내용 | E 갱신시각
 *   업서트 키 = (대분류, 중분류, 소분류). 같은 자리면 그 행을 덮어씀.
 *
 * 배포:
 *   1) 구글 시트 새로 만들기 (탭은 봇이 작품별로 자동 생성)
 *   2) 확장 프로그램 → Apps Script → 이 코드 전체 붙여넣기
 *   3) SECRET 값을 .env의 SHEET_SECRET과 동일하게
 *   4) 배포 → 새 배포 → 웹 앱 → 액세스: 모든 사용자 → 배포
 *   5) 웹앱 URL(/exec)을 .env의 SHEET_WEBAPP_URL에
 */

var SECRET = "나도몰라";              // .env SHEET_SECRET과 동일하게
var HEADER = ["대분류", "중분류", "소분류", "내용", "갱신시각"];
var META_TABS = {};                   // 작품 아님(제외할 시스템 탭 이름) — 필요시 추가

function _isWorkTab(sh) {
  var name = sh.getName();
  return !META_TABS[name] && name.charAt(0) !== "_";
}

function _sheetFor(work, create) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(work);
  if (!sh && create) {
    sh = ss.insertSheet(work);
    sh.appendRow(HEADER);
    sh.setFrozenRows(1);
  }
  return sh;
}

function _rows(sh) {
  var last = sh.getLastRow();
  if (last < 2) return [];
  return sh.getRange(2, 1, last - 1, 5).getValues();  // 헤더 제외 (5열)
}

/** 읽기: ?secret=..&work=작품명  (work 없으면 작품(탭) 목록) */
function doGet(e) {
  var p = e.parameter || {};
  if (p.secret !== SECRET) return _json({ error: "unauthorized" });
  if (!p.work) {
    var works = SpreadsheetApp.getActiveSpreadsheet().getSheets()
      .filter(_isWorkTab).map(function (s) { return s.getName(); });
    return _json({ works: works });
  }
  var sh = _sheetFor(p.work, false);
  if (!sh) return _json({ work: p.work, rows: [] });
  var out = _rows(sh).map(function (r) {
    return { top: r[0], mid: r[1], sub: r[2], content: r[3], updated_at: r[4] };
  });
  return _json({ work: p.work, rows: out });
}

/** 쓰기(업서트): POST JSON { secret, work, top, mid, sub, content } */
function doPost(e) {
  var body = {};
  try { body = JSON.parse(e.postData.contents); } catch (err) { return _json({ error: "bad json" }); }
  if (body.secret !== SECRET) return _json({ error: "unauthorized" });
  if (!body.work || !body.top) return _json({ error: "work, top required" });

  var sh = _sheetFor(body.work, true);
  var mid = body.mid || "", sub = body.sub || "", now = new Date().toISOString();
  var rows = _rows(sh);
  for (var i = 0; i < rows.length; i++) {
    if (rows[i][0] === body.top && rows[i][1] === mid && rows[i][2] === sub) {
      sh.getRange(i + 2, 4, 1, 2).setValues([[body.content || "", now]]);  // 내용, 갱신시각
      return _json({ ok: true, updated: true });
    }
  }
  sh.appendRow([body.top, mid, sub, body.content || "", now]);
  return _json({ ok: true, created: true });
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
