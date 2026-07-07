/**
 * co-writer-bot ↔ 구글 시트 바이블 웹앱 (탭 = 작품, 실무자 친화 표 레이아웃)
 *
 * 구조: 스프레드시트 1개 = 바이블 전체. 탭 1개 = 작품 1개(탭 이름 = 작품명).
 * 각 작품 탭은 사람이 보기 좋은 레이아웃으로 관리된다:
 *
 *   ┌ 단일 항목 (A=라벨 | B=내용) ─ 실무자가 뭘 채울지 한눈에
 *   │ 진행상태   | 24화 작업 중
 *   │ 로그라인   | 정략결혼한 여주가…
 *   │ 키워드     | #후회남 #계약연애
 *   │ 타겟층     | 20~40 여성
 *   │ 핵심정서   | 사이다·통쾌
 *   │ 금지사항   | 남주가 여주에게 신체 폭력 금지
 *   │ 줄거리     | …
 *   │ (빈 줄)
 *   ├ 표: 제목행 + 헤더행(=소분류) + 데이터행 ─ 헤더가 곧 분류값
 *   │ 등장인물
 *   │ 이름   | 성별 | 나이 | 포지션 | 설정 | 핵심대사 | 설명
 *   │ 강태혁 | 남   | 32   | 남주   | …
 *   │ (빈 줄)
 *   │ 회차분배
 *   │ 막  | 구간 | 화수 | 핵심사건
 *   │ 1막 | …    | 1~12화 | …
 *   │ (빈 줄)  개요 / 대본도 동일 (화 | 내용)
 *   └
 *
 * 봇은 여전히 (top, mid, sub, content)로 보내고, 여기서 위 레이아웃의 알맞은 칸에 매핑한다.
 * doGet은 레이아웃을 파싱해 구조화 JSON을 돌려준다(봇은 그걸 조립만).
 *
 * 배포:
 *   1) 확장 프로그램 → Apps Script → 이 코드 전체 붙여넣기 (기존 코드 교체)
 *   2) SECRET을 .env의 SHEET_SECRET과 동일하게
 *   3) 배포 → 배포 관리 → 편집(연필) → 새 버전 → 배포  (URL 유지)
 *   4) 기존 작품 탭은 지우세요 — 새 레이아웃 골격을 다시 깔도록
 */

var SECRET = "나도몰라";                 // .env SHEET_SECRET과 동일하게

// 단일 항목:  [라벨(A열), 봇의 top, 봇의 mid]
var SINGLE = [
  ["진행상태", "진행상태", ""],
  ["로그라인", "로그라인/키워드", "로그라인"],
  ["키워드",   "로그라인/키워드", "키워드"],
  ["타겟층",   "타겟층/핵심정서", "타겟층"],
  ["핵심정서", "타겟층/핵심정서", "핵심정서"],
  ["금지사항", "금지사항", ""],
  ["줄거리",   "줄거리", ""]
];
var SINGLE_LABELS = SINGLE.map(function (r) { return r[0]; });

// 표:  {title(제목행·봇 top), key(행키 헤더), cols(소분류 헤더들)}
var TABLES = [
  { title: "등장인물", key: "이름", cols: ["성별", "나이", "포지션", "설정", "핵심대사", "설명"] },
  { title: "회차분배", key: "막",   cols: ["구간", "화수", "핵심사건"] },
  { title: "개요",     key: "화",   cols: ["내용"] },
  { title: "대본",     key: "화",   cols: ["내용"] }
];
var TABLE_TITLES = TABLES.map(function (t) { return t.title; });
function _tableByTitle(t) { for (var i = 0; i < TABLES.length; i++) if (TABLES[i].title === t) return TABLES[i]; return null; }

function _labelFor(top, mid) {
  for (var i = 0; i < SINGLE.length; i++)
    if (SINGLE[i][1] === top && SINGLE[i][2] === (mid || "")) return SINGLE[i][0];
  return null;
}

function _isWorkTab(sh) { return sh.getName().charAt(0) !== "_"; }

// ── 레이아웃 골격 생성 (새 탭) ───────────────────────────────────────────
function _sheetFor(work, create) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(work);
  if (!sh && create) { sh = ss.insertSheet(work); _initLayout(sh); }
  return sh;
}

function _initLayout(sh) {
  var row = 1;
  // 단일 항목
  for (var i = 0; i < SINGLE.length; i++) {
    sh.getRange(row, 1).setValue(SINGLE[i][0]).setFontWeight("bold");
    row++;
  }
  row++; // 빈 줄
  // 표들: 제목행 + 헤더행
  for (var t = 0; t < TABLES.length; t++) {
    var T = TABLES[t];
    sh.getRange(row, 1).setValue(T.title).setFontWeight("bold").setBackground("#e8eaed");
    row++;
    var header = [T.key].concat(T.cols);
    sh.getRange(row, 1, 1, header.length).setValues([header]).setFontWeight("bold");
    row += 2; // 헤더 + 빈 줄
  }
  sh.setColumnWidth(1, 120);
}

// ── 시트 스캔 헬퍼 ───────────────────────────────────────────────────────
function _grid(sh) {
  var last = sh.getLastRow(), lastc = Math.max(sh.getLastColumn(), 1);
  if (last < 1) return [];
  return sh.getRange(1, 1, last, lastc).getValues();
}

/** A열에서 값이 정확히 일치하는 행(1-base) 찾기. 없으면 -1. */
function _findRowByA(grid, val) {
  for (var i = 0; i < grid.length; i++) if (String(grid[i][0]).trim() === val) return i + 1;
  return -1;
}

/** 표 데이터 영역 [firstDataRow, lastDataRow] (1-base). 데이터 없으면 last<first. */
function _tableDataRange(grid, titleRow) {
  var headerRow = titleRow + 1;
  var r = headerRow + 1, last = headerRow; // 데이터 없으면 last=headerRow
  while (r <= grid.length) {
    var a = String(grid[r - 1][0]).trim();
    if (a === "") break;                                   // 빈 줄 = 블록 끝
    if (SINGLE_LABELS.indexOf(a) >= 0) break;              // 다른 블록 시작
    if (TABLE_TITLES.indexOf(a) >= 0) break;
    last = r; r++;
  }
  return [headerRow + 1, last];
}

// ── 쓰기 ─────────────────────────────────────────────────────────────────
function _writeSingle(sh, label, content) {
  var grid = _grid(sh);
  var row = _findRowByA(grid, label);
  if (row < 0) { // 골격이 없던 경우(구버전 탭) — 맨 위에 보강
    sh.insertRowBefore(1);
    sh.getRange(1, 1).setValue(label).setFontWeight("bold");
    row = 1;
  }
  sh.getRange(row, 2).setValue(content || "");
}

function _ensureTable(sh, T) {   // 표 골격(제목+헤더)이 없으면 만들고 titleRow 반환
  var titleRow = _findRowByA(_grid(sh), T.title);
  if (titleRow < 0) {
    var end = sh.getLastRow() + 2;
    sh.getRange(end, 1).setValue(T.title).setFontWeight("bold").setBackground("#e8eaed");
    var header = [T.key].concat(T.cols);
    sh.getRange(end + 1, 1, 1, header.length).setValues([header]).setFontWeight("bold");
    titleRow = end;
  }
  return titleRow;
}

/** 행(이름/막)만 등록 — 이미 있으면 그대로 둠(기존 값 보존). */
function _ensureRow(sh, T, rowKey) {
  var titleRow = _ensureTable(sh, T);
  var grid = _grid(sh);
  var range = _tableDataRange(grid, titleRow);
  for (var r = range[0]; r <= range[1]; r++)
    if (String(grid[r - 1][0]).trim() === String(rowKey).trim()) return { exists: true };
  var insertAfter = (range[1] >= range[0]) ? range[1] : titleRow + 1;
  sh.insertRowsAfter(insertAfter, 1);
  sh.getRange(insertAfter + 1, 1).setValue(rowKey);
  return { created: true };
}

function _writeTableCell(sh, T, rowKey, colName, content) {
  _ensureTable(sh, T);
  var grid = _grid(sh);
  var titleRow = _findRowByA(grid, T.title);
  var headerRow = titleRow + 1;
  var header = grid[headerRow - 1];
  var colIdx = -1;
  for (var c = 0; c < header.length; c++) if (String(header[c]).trim() === colName) { colIdx = c; break; }
  if (colIdx < 0) return { error: "no column: " + colName };

  var range = _tableDataRange(grid, titleRow);
  var first = range[0], last = range[1];
  // 기존 행키 찾기
  for (var r = first; r <= last; r++) {
    if (String(grid[r - 1][0]).trim() === String(rowKey).trim()) {
      sh.getRange(r, colIdx + 1).setValue(content || "");
      return { updated: true };
    }
  }
  // 없으면 새 데이터 행 삽입 (헤더 바로 아래 또는 데이터 끝)
  var insertAfter = (last >= first) ? last : headerRow;
  sh.insertRowsAfter(insertAfter, 1);
  var newRow = insertAfter + 1;
  sh.getRange(newRow, 1).setValue(rowKey);
  sh.getRange(newRow, colIdx + 1).setValue(content || "");
  return { created: true };
}

// ── 읽기 (레이아웃 → 구조화 JSON) ────────────────────────────────────────
function _parse(sh) {
  var grid = _grid(sh);
  var out = { single: {}, "등장인물": [], "회차분배": [], "개요": [], "대본": [] };
  var i = 0;
  while (i < grid.length) {
    var a = String(grid[i][0]).trim();
    if (a === "") { i++; continue; }
    if (SINGLE_LABELS.indexOf(a) >= 0) {
      out.single[a] = grid[i][1] === undefined ? "" : String(grid[i][1]);
      i++; continue;
    }
    var T = _tableByTitle(a);
    if (T) {
      var titleRow = i + 1;                       // 1-base
      var header = grid[titleRow];                // 헤더행 (0-base index = titleRow)
      var range = _tableDataRange(grid, titleRow);
      for (var r = range[0]; r <= range[1]; r++) {
        var rowVals = grid[r - 1];
        var obj = {};
        var any = false;
        for (var c = 0; c < header.length; c++) {
          var h = String(header[c]).trim();
          if (!h) continue;
          var v = rowVals[c] === undefined ? "" : String(rowVals[c]);
          obj[h] = v;
          if (v.trim()) any = true;
        }
        if (any) out[T.title].push(obj);
      }
      i = range[1] >= range[0] ? range[1] : titleRow + 1;
      continue;
    }
    i++;
  }
  return out;
}

// ── HTTP ─────────────────────────────────────────────────────────────────
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
  if (!sh) return _json({ work: p.work, single: {}, "등장인물": [], "회차분배": [], "개요": [], "대본": [] });
  var parsed = _parse(sh);
  parsed.work = p.work;
  return _json(parsed);
}

/** 쓰기(업서트): POST JSON { secret, work, top, mid, sub, content } */
function doPost(e) {
  var body = {};
  try { body = JSON.parse(e.postData.contents); } catch (err) { return _json({ error: "bad json" }); }
  if (body.secret !== SECRET) return _json({ error: "unauthorized" });
  if (!body.work || !body.top) return _json({ error: "work, top required" });

  var sh = _sheetFor(body.work, true);
  var top = body.top, mid = body.mid || "", sub = body.sub || "", content = body.content || "";

  var label = _labelFor(top, mid);
  if (label) { _writeSingle(sh, label, content); return _json({ ok: true, kind: "single" }); }

  var T = _tableByTitle(top);
  if (T) {
    if (!mid) return _json({ error: "행 키가 필요합니다: " + top });
    var colName = (T.cols.length === 1) ? T.cols[0] : sub;  // 개요·대본은 항상 '내용'
    if (!colName) {                                          // 소분류 없음
      if (content) return _json({ error: "소분류가 필요합니다: " + top });
      var er = _ensureRow(sh, T, mid);                       // 내용 없으면 행만 등록
      er.ok = true; er.kind = "row";
      return _json(er);
    }
    var res = _writeTableCell(sh, T, mid, colName, content);
    res.ok = !res.error; res.kind = "table";
    return _json(res);
  }
  return _json({ error: "알 수 없는 대분류: " + top });
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
