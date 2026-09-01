// ============================================================
// r-REPORT (1 ก.ย. 2569, Gift สั่ง) — 2 รีพอร์ตจัดลำดับการโทร
// ------------------------------------------------------------
// คอนเซ็ปต์ Gift: "โฟกัสเคสที่ใช่ ไม่คลุกฝุ่น"
//   เคสดี = เกรด A หรือ B เท่านั้น (Gift เคาะ 1 ก.ย.)
//   ยังไม่ได้โทร = คอลัมน์ M,N,O,P,S ว่างหมด (ไม่มีใครแตะเลย)
//     เหตุผลที่ใช้ 5 ช่อง: เซลแทบไม่กรอก N (วันที่ติดต่อ) เลย
//     จาก ~70 แถวใน SALES_Gift มีแค่ 2 แถวที่มีวันที่
//     ถ้าดูช่องเดียวจะได้กองใหญ่ที่ไม่มีใครเชื่อ = คลุกฝุ่น
// อ่านไฟล์เซลด้วย openById -> ไม่ต้องกดปุ่มอนุญาตแบบ IMPORTRANGE
// ============================================================
var RPT_GOOD_GRADES = ['A', 'B'];
var RPT_RECALL_DAYS = 2;
var RPT_PRACH_FILE  = '1xoxFIrngh7pJLlFJzJSGANo8ftV1jWSqbG6GWgDTC2Q';
var RPT_RECALL_FILE = '17Zsef28vK23zcer-1328rItkh-_SUABA74_xthJK7qs';
// ไฟล์บันทึกการทำงานจริงของปราช (เจ้าของ prach@) — ใช้ match เบอร์
var RPT_PRACH_LOG   = '130RAv0H3DaS75sA2gEWgI3bNrRwT0dTMBKBZ21V7iOg';

// ไฟล์เซลทั้งหมด — ชื่อที่โชว์ในรีพอร์ต : file id
var RPT_SALES = [
  ['Gift',      '1b7lzwHvSBaq-zPLzVpZ5fN3HeD1Cw8DsczwVEUzv5GQ'],
  ['ปราช',      '1lUwMfy1l7dDCTe_fURZ7PUDJEyMFtWrKnn2II2D5nXM'],
  ['Jeab',      '1FYwhCb8qKFPpo2ECwVNwbmTguCfllMhAe6GBlVva25A'],
  ['Pat',       '1jSw7WW-E0rgA3ea1SMu4L6yhXSU2YL2THcC7o7ViwHI'],
  ['เล็ก',      '1GLrCU2o-D-f3QLkF48d7QEIBYtPz6gtwn16eQAQgEc8'],
  ['หลี',       '1VLSrRGbljsbJWn0L1JtbTi59a5D6-ppHKxmNtT4Xjnk'],
  ['Mo',        '1P08vJYMVP0KVctq5IrwIPtT0coHKvhSiQF7EinUeJXg'],
  ['บุญ',       '1FsKSKd28thsKZC5hOPflNr1AebOpwQPArvZ_oKCqEM8'],
  ['พิ้ง',      '1PKJ4nQUnCBsKJKZ2SnLEIvTxetJkTBV9aHKawDCmoyU'],
  ['พลอย',      '1yAv5LNi3I6LPeGeB24QPx8oJ-QYKYNqyRZjd_Of7g5s'],
  ['Pop_Angel', '15h8BSjPkSKDIsrgk5kJoZSIlmRA5BgQup3ptJLvARZE'],
  ['Pop',       '1Rp_fHfrvW6PeDNk9TZPwYIX-L8LwlmJH5y2v--7SV18']
];

// คำในบันทึกจริงของปราช -> Stage (เรียงตามลำดับ เจอก่อนชนะ)
var RPT_STAGE_RULES = [
  ['ปิดการขาย',      ['ปิดขาย', 'ปิดการขาย', 'ส่งapp', 'ส่ง app', 'ส่งแอพ']],
  ['กำลังดำเนินการ', ['ส่งคำนวณ', 'ส่งประเมิน', 'รอเอกสาร', 'พรีวงเงิน', 'เก็บเอกสาร',
                      'รอใบคำนวณ', 'รอเลขงาน', 'ส่งเคสเข้าระบบ', 'นำเสนอแผน', 'เสนอแผน']],
  ['นัดคุย',         ['นัดคุย', 'นัดโทร', 'นัดหมาย', 'จะโทรกลับ', 'ขอเวลาคิด', 'ขอปรึกษา']],
  ['ติดต่อไม่ได้',   ['ไม่รับสาย', 'โทรไม่ติด', 'ติดต่อไม่ได้', 'ไม่ตอบ', 'ตัดสายทิ้ง',
                      'ยังไม่รับสาย', 'ไม่สะดวกคุย', 'หาเบอร์ไม่เจอ']],
  ['รีคอล',          ['รีคอล', 'recall']],
  ['ตกเคส',          ['ไม่สนใจ', 'ไม่เอาแล้ว', 'ไม่ผ่าน', 'ไปต่อไม่ได้', 'รายได้ไม่พอ',
                      'ภาระเกิน', 'เกินเกณฑ์', 'ไม่เข้าเกณฑ์', 'ติดบูโร', 'เข้าใจผิด',
                      'ไม่ได้สนใจ', 'ไม่ทำ']]
];

function rptNorm_(v) { return String(v == null ? '' : v).trim(); }

// เบอร์โทรให้เหลือแต่ตัวเลข 9-10 หลัก เติม 0 หน้าถ้าหาย (ชีตชอบกินเลข 0)
function rptPhoneKey_(v) {
  var d = rptNorm_(v).replace(/\D/g, '');
  if (!d) return '';
  if (d.length === 9) d = '0' + d;
  if (d.length > 10) d = d.slice(-10);
  return d;
}

function rptIsGood_(grade) {
  var g = rptNorm_(grade).toUpperCase();
  return RPT_GOOD_GRADES.indexOf(g) >= 0;
}

// "ยังไม่มีใครแตะ" — M,N,O,P,S ต้องว่างหมด (index 12,13,14,15,18)
function rptUntouched_(row) {
  var cols = [12, 13, 14, 15, 18];
  for (var i = 0; i < cols.length; i++) {
    if (rptNorm_(row[cols[i]])) return false;
  }
  return true;
}

function rptAgeDays_(v) {
  var d = (v instanceof Date) ? v : new Date(rptNorm_(v));
  if (isNaN(d.getTime())) return null;
  return Math.floor((new Date().getTime() - d.getTime()) / 86400000);
}

function rptStageFromNote_(note) {
  var t = rptNorm_(note).toLowerCase();
  if (!t) return '';
  for (var i = 0; i < RPT_STAGE_RULES.length; i++) {
    var words = RPT_STAGE_RULES[i][1];
    for (var j = 0; j < words.length; j++) {
      if (t.indexOf(String(words[j]).toLowerCase()) >= 0) return RPT_STAGE_RULES[i][0];
    }
  }
  return 'มีบันทึกแล้ว';
}

// อ่านไฟล์เซล 1 ไฟล์ -> แถวดิบของแท็บแรก
function rptReadSales_(id) {
  try {
    var sh = SpreadsheetApp.openById(id).getSheets()[0];
    if (sh.getLastRow() < 2) return [];
    return sh.getRange(2, 1, sh.getLastRow() - 1, 20).getValues();
  } catch (e) {
    Logger.log('[RPT] อ่านไฟล์เซลไม่ได้ ' + id + ' : ' + e);
    return [];
  }
}

// อ่านบันทึกจริงของปราช -> map เบอร์ -> {note, sale, date}
function rptReadPrachLog_() {
  var map = {};
  try {
    var ss = SpreadsheetApp.openById(RPT_PRACH_LOG);
    var sheets = ss.getSheets();
    for (var s = 0; s < sheets.length; s++) {
      var sh = sheets[s];
      if (sh.getLastRow() < 2 || sh.getLastColumn() < 14) continue;
      var vals = sh.getRange(1, 1, sh.getLastRow(), Math.min(sh.getLastColumn(), 21)).getValues();
      for (var r = 0; r < vals.length; r++) {
        var key = rptPhoneKey_(vals[r][13]);          // N = เบอร์ติดต่อ
        if (!key) continue;
        var note = rptNorm_(vals[r][18]);             // S = หมายเหตุ-เพิ่มเติม
        if (!note) note = rptNorm_(vals[r][16]);      // Q = สถานะ-เคส
        if (!note) continue;
        map[key] = { note: note, sale: rptNorm_(vals[r][5]), date: rptNorm_(vals[r][3]),
                     tab: sh.getName() };
      }
    }
  } catch (e) {
    Logger.log('[RPT] อ่านไฟล์บันทึกปราชไม่ได้: ' + e);
  }
  return map;
}

function rptWrite_(fileId, tabName, head, rows, note) {
  var ss = SpreadsheetApp.openById(fileId);
  var sh = ss.getSheetByName(tabName) || ss.insertSheet(tabName);
  sh.clear();
  sh.getRange(1, 1, 1, 1).setValue(note);
  sh.getRange(2, 1, 1, head.length).setValues([head]).setFontWeight('bold');
  if (rows.length) {
    sh.getRange(3, 1, rows.length, head.length).setValues(rows);
    sh.getRange(3, 4, rows.length, 1).setNumberFormat('@');   // เบอร์เป็นข้อความ
  }
  sh.setFrozenRows(2);
  for (var c = 1; c <= head.length; c++) sh.autoResizeColumn(c);
  // เก็บเฉพาะแท็บที่ใช้ ลบแท็บเปล่าที่ Google สร้างมาให้ตอนสร้างไฟล์
  var all = ss.getSheets();
  for (var i = 0; i < all.length; i++) {
    if (all[i].getName() !== tabName && all[i].getLastRow() === 0 && all.length > 1) {
      try { ss.deleteSheet(all[i]); } catch (e2) {}
    }
  }
}

// ---------- รีพอร์ต 1: Priority ของปราช ----------
function rptBuildPrach() {
  var log = rptReadPrachLog_();
  var rows = rptReadSales_('1lUwMfy1l7dDCTe_fURZ7PUDJEyMFtWrKnn2II2D5nXM');
  var out = [], matched = 0;
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    if (!rptIsGood_(r[7])) continue;                       // H = เกรด
    var key = rptPhoneKey_(r[4]);                          // E = เบอร์โทร
    var hit = key ? log[key] : null;
    if (hit) matched++;
    var stage = hit ? rptStageFromNote_(hit.note) : rptNorm_(r[18]);
    var touched = !!hit || !rptUntouched_(r);
    if (touched) continue;                                 // โฟกัสเฉพาะที่ยังไม่ได้โทร
    var age = rptAgeDays_(r[0]);
    out.push([rptNorm_(r[7]), age == null ? '' : age, rptNorm_(r[3]) || '(ยังไม่ได้ชื่อ)',
              rptNorm_(r[4]), rptNorm_(r[5]), rptNorm_(r[2]), rptNorm_(r[8]),
              rptNorm_(r[9]), rptNorm_(r[1]), stage]);
  }
  out.sort(function (a, b) {
    if (a[0] !== b[0]) return a[0] < b[0] ? -1 : 1;        // A ก่อน B
    return (b[1] || 0) - (a[1] || 0);                       // เก่าสุดก่อน
  });
  rptWrite_(RPT_PRACH_FILE, 'Priority',
    ['เกรด', 'อายุเคส(วัน)', 'ชื่อลูกค้า', 'เบอร์โทร', 'LINE', 'เพจ',
     'รายได้/เดือน', 'ภาระผ่อน/เดือน', 'Lead ID', 'Stage'],
    out,
    'เคสเกรด A/B ของปราชที่ยังไม่มีใครแตะ — เรียงตามเกรดแล้วเก่าสุดก่อน · ' +
    'จับคู่เบอร์กับไฟล์บันทึกจริงแล้ว ' + matched + ' เคส (เคสที่จับคู่ได้ = โทรแล้ว ตัดออก) · ' +
    'อัปเดตล่าสุด ' + Utilities.formatDate(new Date(), 'Asia/Bangkok', 'd/M/yyyy HH:mm'));
  Logger.log('[RPT] ปราช: ' + out.length + ' เคสรอโทร · จับคู่บันทึกจริงได้ ' + matched);
  return out.length;
}

// ---------- รีพอร์ต 2: Recall ด่วน ทุกเซล ----------
function rptBuildRecall() {
  var out = [];
  for (var s = 0; s < RPT_SALES.length; s++) {
    var name = RPT_SALES[s][0];
    var rows = rptReadSales_(RPT_SALES[s][1]);
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (!rptIsGood_(r[7])) continue;
      if (!rptUntouched_(r)) continue;
      var age = rptAgeDays_(r[0]);
      if (age == null || age < RPT_RECALL_DAYS) continue;
      if (!rptNorm_(r[4]) && !rptNorm_(r[5])) continue;     // ไม่มีทั้งเบอร์และไลน์ = โทรไม่ได้
      out.push([rptNorm_(r[7]), age, name, rptNorm_(r[3]) || '(ยังไม่ได้ชื่อ)',
                rptNorm_(r[4]), rptNorm_(r[5]), rptNorm_(r[2]), rptNorm_(r[8]),
                rptNorm_(r[9]), rptNorm_(r[1])]);
    }
  }
  out.sort(function (a, b) {
    if (a[0] !== b[0]) return a[0] < b[0] ? -1 : 1;
    return b[1] - a[1];
  });
  rptWrite_(RPT_RECALL_FILE, 'Recall',
    ['เกรด', 'อายุเคส(วัน)', 'เซล', 'ชื่อลูกค้า', 'เบอร์โทร', 'LINE', 'เพจ',
     'รายได้/เดือน', 'ภาระผ่อน/เดือน', 'Lead ID'],
    out,
    'เคสเกรด A/B ที่ได้ไปเกิน ' + RPT_RECALL_DAYS + ' วันแล้วยังไม่มีใครแตะเลย — ' +
    'รวมทุกเซล เรียงเกรดแล้วเก่าสุดก่อน · ' +
    '"ยังไม่แตะ" = ช่องสถานะติดต่อ/วันที่ติดต่อ/ครั้งที่ติดต่อ/ผลการคุย/Stage ว่างหมด · ' +
    'อัปเดตล่าสุด ' + Utilities.formatDate(new Date(), 'Asia/Bangkok', 'd/M/yyyy HH:mm'));
  Logger.log('[RPT] recall ด่วน: ' + out.length + ' เคส');
  return out.length;
}

function rptBuildAll() {
  var a = 0, b = 0;
  try { a = rptBuildPrach(); } catch (e) { Logger.log('[RPT] ปราช ล้ม: ' + e); }
  try { b = rptBuildRecall(); } catch (e) { Logger.log('[RPT] recall ล้ม: ' + e); }
  return 'ปราช ' + a + ' เคส · recall ' + b + ' เคส';
}
