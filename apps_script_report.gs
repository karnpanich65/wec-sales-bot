// ============================================================
// r-REPORT v3 (1 ก.ย. 2569) — 2 รีพอร์ตจัดลำดับการโทร
// ------------------------------------------------------------
// คอนเซ็ปต์ Gift: "โฟกัสเคสที่ใช่ ไม่คลุกฝุ่น"
//   v3 (Gift สั่ง 1 ก.ย. บ่าย):
//     - "C กับ N จะมาทำไม"  -> หน้างานเหลือเฉพาะ A/B เท่านั้น
//     - "อย่าลืม match เบอร์กับไฟล์เพจกลาง แล้วดึงสถานะมา
//        จะได้รู้ว่าเคสไหนทำไปแล้วบ้าง"
//     - "ในไฟล์หลัก ไม่มี 0 ข้างหน้าเบอร์โทร"
//        -> คีย์เบอร์ใช้ 9 หลักท้าย ใช้ได้ทั้งมี 0 / ไม่มี 0 / +66 / มีขีด
// อ่านไฟล์เซลด้วย openById -> ไม่ต้องกดปุ่มอนุญาตแบบ IMPORTRANGE
// ============================================================
var RPT_GOOD_GRADES = ['A', 'B'];          // เท่านั้น — C/N/D/X/W ไม่ขึ้นหน้างาน
var RPT_RECALL_DAYS = 2;
var RPT_PRACH_FILE  = '1xoxFIrngh7pJLlFJzJSGANo8ftV1jWSqbG6GWgDTC2Q';
var RPT_RECALL_FILE = '17Zsef28vK23zcer-1328rItkh-_SUABA74_xthJK7qs';
// ไฟล์บันทึกการทำงานจริง "เคสเพจกลาง MA" (เจ้าของ prach@) — ใช้ match เบอร์
var RPT_PRACH_LOG   = '130RAv0H3DaS75sA2gEWgI3bNrRwT0dTMBKBZ21V7iOg';
var RPT_PRACH_SALES = '1lUwMfy1l7dDCTe_fURZ7PUDJEyMFtWrKnn2II2D5nXM';

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

// คำในบันทึกจริง -> Stage (เรียงตามลำดับ เจอก่อนชนะ)
var RPT_STAGE_RULES = [
  ['ปิดการขาย',      ['ปิดขาย', 'ปิดการขาย', 'ส่งapp', 'ส่ง app', 'ส่งแอพ']],
  ['กำลังดำเนินการ', ['ส่งคำนวณ', 'ส่งประเมิน', 'รอเอกสาร', 'พรีวงเงิน', 'เก็บเอกสาร',
                      'รอใบคำนวณ', 'รอเลขงาน', 'ส่งเคสเข้าระบบ', 'นำเสนอแผน', 'เสนอแผน',
                      'รอปิดหนี้', 'รอดูทรัพย์', 'รอสรุป', 'รอบูโร', 'รอปรับบูโร']],
  ['นัดคุย',         ['นัดคุย', 'นัดโทร', 'นัดหมาย', 'จะโทรกลับ', 'ขอเวลาคิด', 'ขอปรึกษา',
                      'ลูกค้าสนใจ', 'ขอดูรายละเอียด']],
  ['ติดต่อไม่ได้',   ['ไม่รับสาย', 'โทรไม่ติด', 'ติดต่อไม่ได้', 'ไม่ตอบ', 'ตัดสายทิ้ง',
                      'ยังไม่รับสาย', 'ไม่สะดวกคุย', 'หาเบอร์ไม่เจอ', 'ไม่ได้เบอร์']],
  ['รีคอล',          ['รีคอล', 'recall']],
  ['ตกเคส',          ['ไม่สนใจ', 'ไม่เอาแล้ว', 'ไม่ผ่าน', 'ไปต่อไม่ได้', 'รายได้ไม่พอ',
                      'ภาระเกิน', 'เกินเกณฑ์', 'ไม่เข้าเกณฑ์', 'ติดบูโร', 'เข้าใจผิด',
                      'ไม่ได้สนใจ', 'ไม่ทำ', 'อายุเกิน', 'ซ้ำกับเพจ', 'ไม่ได้ลงทุน',
                      'อายุงานยังไม่ถึง', 'ไม่ใช่สิ่งที่เค้าคิด']]
];

// คำที่แปลว่า "เคสนี้จบไปแล้ว อย่าไปคลุกฝุ่นซ้ำ"
var RPT_DEAD_STAGES = ['ตกเคส', 'ปิดการขาย'];

function rptNorm_(v) { return String(v == null ? '' : v).trim(); }

// ------------------------------------------------------------
// คีย์เบอร์โทร = 9 หลักท้าย
//   ไฟล์หลักเก็บเบอร์เป็น "ตัวเลข" 0 ข้างหน้าหายไป (Gift แจ้ง 1 ก.ย.)
//   ไฟล์เพจกลางเก็บเป็นข้อความ มีขีด มี 0 บ้างไม่มีบ้าง
//   9 หลักท้ายจึงเป็นคีย์เดียวที่ตรงกันทั้งสองฝั่ง
//   คืนเป็น "หลายคีย์" เพราะบางช่องใส่ 2 เบอร์ / มีข้อความปน เช่น "โทร 081-xxx"
// ------------------------------------------------------------
function rptPhoneKeys_(v) {
  var s = rptNorm_(v);
  if (!s) return [];
  var runs = [], cur = '';
  for (var i = 0; i < s.length; i++) {
    var c = s.charAt(i);
    if (c >= '0' && c <= '9') { cur += c; continue; }
    if (c === '-' || c === ' ' || c === '.' || c === '(' || c === ')' || c === '+') continue;
    if (cur) { runs.push(cur); cur = ''; }          // เจอตัวอักษร = ตัดเป็นคนละเบอร์
  }
  if (cur) runs.push(cur);

  var out = [], seen = {};
  for (var j = 0; j < runs.length; j++) {
    var d = runs[j];
    if (d.length >= 11 && d.slice(0, 2) === '66') d = d.slice(2);   // +66
    var cands = [];
    if (d.length > 11) { cands.push(d.slice(0, 10)); cands.push(d.slice(-10)); }
    else cands.push(d);
    for (var k = 0; k < cands.length; k++) {
      var x = cands[k];
      if (x.length === 8) x = '0' + x;
      if (x.length < 9) continue;
      var key = x.slice(-9);
      if (!seen[key]) { seen[key] = 1; out.push(key); }
    }
  }
  return out;
}
function rptPhoneKey_(v) { var a = rptPhoneKeys_(v); return a.length ? a[0] : ''; }

// เบอร์ที่ "กดโทรได้จริง" — ไฟล์หลักเก็บเป็นตัวเลข 0 หน้าหาย ต้องเติมคืน
function rptPhoneShow_(v) {
  var s = rptNorm_(v);
  if (!s) return '';
  var k = rptPhoneKeys_(v);
  if (!k.length) return s;
  var d = k[0];
  return (d.charAt(0) === '0') ? d : '0' + d;   // มือถือไทย 9 หลักท้ายไม่ขึ้นต้นด้วย 0
}

function rptIsGood_(grade) {
  return RPT_GOOD_GRADES.indexOf(rptNorm_(grade).toUpperCase()) >= 0;
}

// "ยังไม่มีใครแตะ" — M,N,O,P,S ต้องว่างหมด (index 12,13,14,15,18)
function rptUntouched_(row) {
  var cols = [12, 13, 14, 15, 18];
  for (var i = 0; i < cols.length; i++) if (rptNorm_(row[cols[i]])) return false;
  return true;
}

function rptAgeDays_(v) {
  var d = (v instanceof Date) ? v : new Date(rptNorm_(v));
  if (isNaN(d.getTime())) return null;
  return Math.floor((new Date().getTime() - d.getTime()) / 86400000);
}

// วันที่ในไฟล์เพจกลางเป็น d/m/พ.ศ.สองหลัก เช่น 31/3/69
function rptLogDate_(v) {
  if (v instanceof Date) return v.getTime();
  var m = rptNorm_(v).match(/^(\d{1,2})\/(\d{1,2})\/(\d{2,4})$/);
  if (!m) return 0;
  var y = Number(m[3]);
  if (y < 100) {
    // ปีสองหลักในไฟล์ปนกัน: 69 = พ.ศ.2569 (=2026) แต่ 26 = ค.ศ.2026
    // เดาโดยเลือกตัวที่ใกล้ปีนี้ที่สุดและไม่ล้ำอนาคต
    var be = 1957 + y, ce = 2000 + y, nowY = new Date().getFullYear();
    var okBe = be <= nowY + 1, okCe = ce <= nowY + 1;
    if (okBe && okCe) y = (Math.abs(nowY - be) <= Math.abs(nowY - ce)) ? be : ce;
    else if (okBe) y = be;
    else if (okCe) y = ce;
    else y = be;
  } else if (y > 2400) y -= 543;
  return new Date(y, Number(m[2]) - 1, Number(m[1])).getTime();
}
function rptLogDateText_(v) {
  var t = rptLogDate_(v);
  if (!t) return rptNorm_(v);
  return Utilities.formatDate(new Date(t), 'Asia/Bangkok', 'd/M/yy');
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

// ------------------------------------------------------------
// อ่าน "เคสเพจกลาง MA" ทุกแท็บ -> map เบอร์(9 หลักท้าย) -> ผลงานจริง
//   คอลัมน์ (0-based): 3=วันที่ส่งเคส 4=ชื่อ 5=เซลล์ 12=Line 13=เบอร์
//                      14=รายละเอียด 15=ออกผล 16=สถานะ-เคส 18=หมายเหตุ 19=ส่งคำนวณ
//   แถวเดียวกันเบอร์ซ้ำ -> เอาแถวที่ "วันที่ใหม่กว่า" ชนะ
//   แถวที่ยังไม่มีบันทึกก็เก็บ (จะได้รู้ว่า "อยู่ในเพจกลางแล้ว แต่ยังไม่มีผล")
// ------------------------------------------------------------
function rptReadPrachLog_() {
  var map = {}, stat = { sheets: 0, rows: 0, keys: 0, withNote: 0, oldest: 0, newest: 0 };
  try {
    var sheets = SpreadsheetApp.openById(RPT_PRACH_LOG).getSheets();
    for (var s = 0; s < sheets.length; s++) {
      var sh = sheets[s];
      if (sh.getLastRow() < 2 || sh.getLastColumn() < 14) continue;
      stat.sheets++;
      var wide = Math.min(sh.getLastColumn(), 21);
      var vals = sh.getRange(1, 1, sh.getLastRow(), wide).getValues();
      for (var r = 0; r < vals.length; r++) {
        var row = vals[r];
        var ks = rptPhoneKeys_(row[13]);
        if (!ks.length && wide > 12) ks = rptPhoneKeys_(row[12]);   // บางแถวใส่เบอร์ในช่อง Line
        if (!ks.length) continue;
        stat.rows++;

        var note = rptNorm_(row[18]);
        if (!note) note = rptNorm_(row[16]);
        if (!note) note = rptNorm_(row[15]);
        var when = rptLogDate_(row[3]);
        if (when) {
          if (!stat.oldest || when < stat.oldest) stat.oldest = when;
          if (when > stat.newest) stat.newest = when;
        }
        var rec = {
          note: note,
          sale: rptNorm_(row[5]),
          date: rptLogDateText_(row[3]),
          when: when,
          detail: rptNorm_(row[14]),
          job: rptNorm_(row[19]),
          tab: sh.getName()
        };
        for (var i = 0; i < ks.length; i++) {
          var old = map[ks[i]];
          // ใหม่กว่าชนะ / ถ้าวันเท่ากันให้แถวที่มีบันทึกชนะ
          if (!old || when > old.when || (when === old.when && !old.note && note)) map[ks[i]] = rec;
        }
      }
    }
    for (var kk in map) { stat.keys++; if (map[kk].note) stat.withNote++; }
  } catch (e) {
    Logger.log('[RPT] อ่านไฟล์เพจกลางไม่ได้: ' + e);
  }
  return { map: map, stat: stat };
}

function rptLogRange_(stat) {
  if (!stat.oldest) return 'ไม่พบวันที่';
  var f = function (t) { return Utilities.formatDate(new Date(t), 'Asia/Bangkok', 'd/M/yy'); };
  return f(stat.oldest) + ' – ' + f(stat.newest);
}

function rptWrite_(fileId, tabName, head, rows, note, textCols) {
  var ss = SpreadsheetApp.openById(fileId);
  var sh = ss.getSheetByName(tabName) || ss.insertSheet(tabName);
  sh.clear();
  sh.getRange(1, 1, 1, 1).setValue(note);
  sh.getRange(2, 1, 1, head.length).setValues([head]).setFontWeight('bold');
  if (rows.length) {
    sh.getRange(3, 1, rows.length, head.length).setValues(rows);
    var tc = textCols || [];
    for (var t = 0; t < tc.length; t++) {
      sh.getRange(3, tc[t], rows.length, 1).setNumberFormat('@');   // เบอร์เป็นข้อความ
    }
  }
  sh.setFrozenRows(2);
  for (var c = 1; c <= head.length; c++) sh.autoResizeColumn(c);
  var all = ss.getSheets();
  for (var i = 0; i < all.length; i++) {
    if (all[i].getName() !== tabName && all[i].getLastRow() === 0 && all.length > 1) {
      try { ss.deleteSheet(all[i]); } catch (e2) {}
    }
  }
}

// ---------- รีพอร์ต 1: หน้างานของปราช (หน้าเดียว เฉพาะ A/B) ----------
function rptBuildPrach() {
  var lg  = rptReadPrachLog_();
  var log = lg.map, st = lg.stat;
  var rows = rptReadSales_(RPT_PRACH_SALES);
  var out = [], matched = 0, todo = 0, recall = 0, dropped = 0, deadN = 0;

  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    if (!rptNorm_(r[1]) && !rptNorm_(r[3]) && !rptNorm_(r[4])) continue;   // แถวว่าง
    var grade = rptNorm_(r[7]).toUpperCase();
    if (!rptIsGood_(grade)) { dropped++; continue; }                       // C/N/D/X/W ตัดทิ้ง

    // --- match เบอร์กับไฟล์เพจกลาง (ลองทุกเบอร์ที่อ่านได้จากช่องเบอร์ + ช่อง LINE) ---
    var hit = null, ks = rptPhoneKeys_(r[4]);
    for (var a = 0; a < ks.length && !hit; a++) hit = log[ks[a]] || null;
    if (!hit) { var ks2 = rptPhoneKeys_(r[5]); for (var b = 0; b < ks2.length && !hit; b++) hit = log[ks2[b]] || null; }
    if (hit) matched++;

    var age      = rptAgeDays_(r[0]);
    var noteReal = hit ? hit.note : '';
    var saleNote = rptNorm_(r[15]);                      // P ผลการคุย (เซลกรอกเอง)
    var stage    = noteReal ? rptStageFromNote_(noteReal)
                            : (rptNorm_(r[18]) || (saleNote ? rptStageFromNote_(saleNote) : ''));
    var touched  = !!noteReal || !rptUntouched_(r);
    var dead     = RPT_DEAD_STAGES.indexOf(stage) >= 0;
    if (dead) deadN++;

    // เคยผ่านเพจกลางไหม (โชว์ให้เห็นว่าเคสนี้เคยมีคนทำแล้ว)
    var past = hit ? (hit.date + (hit.sale ? ' · ' + hit.sale : '') + (noteReal ? '' : ' (ยังไม่มีบันทึกผล)'))
                   : '';

    // ---- ลำดับงาน: เลขน้อย = ทำก่อน ----
    var pri, label;
    if (dead)                                        { pri = 7; label = '7 · เคยจบไปแล้ว อย่าคลุกฝุ่น'; }
    else if (!touched && grade === 'A')              { pri = 1; label = '1 · โทรก่อน (A ยังไม่โทร)'; }
    else if (!touched && grade === 'B')              { pri = 2; label = '2 · โทรต่อ (B ยังไม่โทร)'; }
    else if (stage === 'นัดคุย' || stage === 'รีคอล'){ pri = 3; label = '3 · มีนัด / รีคอล'; }
    else if (stage === 'กำลังดำเนินการ')             { pri = 4; label = '4 · กำลังดำเนินการ'; }
    else if (stage === 'ติดต่อไม่ได้')               { pri = 5; label = '5 · โทรไม่ติด ลองใหม่'; }
    else                                             { pri = 6; label = '6 · แตะแล้ว รอผล'; }
    if (pri <= 2) todo++;

    var urgent = '';
    if (!touched && !dead && age != null && age >= RPT_RECALL_DAYS) {
      urgent = 'เร่ง ' + age + ' วัน'; recall++;
    }

    out.push([pri, label, urgent, grade, stage, rptNorm_(r[3]) || '(ยังไม่ได้ชื่อ)',
              rptPhoneShow_(r[4]), rptNorm_(r[5]), age == null ? '' : age, rptNorm_(r[2]),
              rptNorm_(r[8]), rptNorm_(r[9]), rptNorm_(r[10]),
              past, noteReal, saleNote, rptNorm_(r[11]), rptNorm_(r[1])]);
  }

  out.sort(function (x, y) {
    if (x[0] !== y[0]) return x[0] - y[0];
    if (!!y[2] !== !!x[2]) return y[2] ? 1 : -1;
    return (y[8] || 0) - (x[8] || 0);
  });
  for (var k = 0; k < out.length; k++) out[k] = out[k].slice(1);

  rptWrite_(RPT_PRACH_FILE, 'หน้างาน',
    ['ลำดับงาน', 'เร่ง', 'เกรด', 'Stage', 'ชื่อลูกค้า', 'เบอร์โทร', 'LINE',
     'อายุเคส(วัน)', 'เพจ', 'รายได้/เดือน', 'ภาระผ่อน/เดือน', 'วงเงินประเมิน',
     'เคยผ่านเพจกลาง', 'ผลจริงจากเพจกลาง', 'บันทึกของเซลในไฟล์', 'สัญญาณจากบอท', 'Lead ID'],
    out,
    'หน้างานปราช — เฉพาะเกรด A/B เรียงจากงานที่ต้องทำก่อน · ' +
    'รอโทร ' + todo + ' · เร่ง ' + recall + ' · เคยจบไปแล้ว ' + deadN + ' · รวม ' + out.length + ' เคส · ' +
    'ตัดเกรดอื่นออก ' + dropped + ' เคส   ||   ' +
    'จับคู่เบอร์กับ "เคสเพจกลาง MA" ได้ ' + matched + ' เคส ' +
    '(ไฟล์เพจกลางมี ' + st.sheets + ' แท็บ / ' + st.rows + ' แถวมีเบอร์ / ' + st.keys + ' เบอร์ไม่ซ้ำ / ' +
    'มีบันทึกผล ' + st.withNote + ' · ช่วงวันที่ ' + rptLogRange_(st) + ')   ||   ' +
    'อัปเดต ' + Utilities.formatDate(new Date(), 'Asia/Bangkok', 'd/M/yyyy HH:mm'),
    [6]);
  Logger.log('[RPT] ปราช: A/B ' + out.length + ' · รอโทร ' + todo + ' · เร่ง ' + recall +
             ' · match ' + matched + ' · ตัดออก ' + dropped);
  return out.length;
}

// ---------- รีพอร์ต 2: Recall ด่วน ทุกเซล ----------
function rptBuildRecall() {
  var lg = rptReadPrachLog_(), log = lg.map, st = lg.stat;
  var out = [], matched = 0, skipped = 0;
  for (var s = 0; s < RPT_SALES.length; s++) {
    var name = RPT_SALES[s][0];
    var rows = rptReadSales_(RPT_SALES[s][1]);
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (!rptIsGood_(r[7])) continue;
      if (!rptUntouched_(r)) continue;
      var age = rptAgeDays_(r[0]);
      if (age == null || age < RPT_RECALL_DAYS) continue;
      if (!rptNorm_(r[4]) && !rptNorm_(r[5])) continue;     // ไม่มีทั้งเบอร์และไลน์

      // เช็คกับไฟล์เพจกลาง — ถ้าเคยจบไปแล้วไม่ต้องรีคอล (ไม่คลุกฝุ่น)
      var hit = null, ks = rptPhoneKeys_(r[4]);
      for (var a = 0; a < ks.length && !hit; a++) hit = log[ks[a]] || null;
      var past = '', stage = '';
      if (hit) {
        matched++;
        stage = hit.note ? rptStageFromNote_(hit.note) : '';
        if (RPT_DEAD_STAGES.indexOf(stage) >= 0) { skipped++; continue; }
        past = hit.date + (hit.sale ? ' · ' + hit.sale : '') + (hit.note ? ' · ' + hit.note : '');
      }
      out.push([rptNorm_(r[7]), age, name, rptNorm_(r[3]) || '(ยังไม่ได้ชื่อ)',
                rptPhoneShow_(r[4]), rptNorm_(r[5]), rptNorm_(r[2]), rptNorm_(r[8]),
                rptNorm_(r[9]), stage, past, rptNorm_(r[1])]);
    }
  }
  out.sort(function (a, b) {
    if (a[0] !== b[0]) return a[0] < b[0] ? -1 : 1;
    return b[1] - a[1];
  });
  rptWrite_(RPT_RECALL_FILE, 'Recall',
    ['เกรด', 'อายุเคส(วัน)', 'เซล', 'ชื่อลูกค้า', 'เบอร์โทร', 'LINE', 'เพจ',
     'รายได้/เดือน', 'ภาระผ่อน/เดือน', 'Stage จากเพจกลาง', 'ประวัติจากเพจกลาง', 'Lead ID'],
    out,
    'เคสเกรด A/B ที่ได้ไปเกิน ' + RPT_RECALL_DAYS + ' วันแล้วยังไม่มีใครแตะเลย — รวมทุกเซล · ' +
    'รวม ' + out.length + ' เคส · จับคู่กับเพจกลางได้ ' + matched + ' · ' +
    'ตัดออกเพราะเพจกลางบอกว่าจบไปแล้ว ' + skipped + ' เคส · ' +
    '"ยังไม่แตะ" = สถานะติดต่อ/วันที่ติดต่อ/ครั้งที่ติดต่อ/ผลการคุย/Stage ว่างหมด · ' +
    'อัปเดต ' + Utilities.formatDate(new Date(), 'Asia/Bangkok', 'd/M/yyyy HH:mm'),
    [5]);
  Logger.log('[RPT] recall: ' + out.length + ' · match ' + matched + ' · ตัด ' + skipped);
  return out.length;
}

// ============================================================
// ตัวตรวจ: "ทำไม match เบอร์ได้น้อย" — เขียนลงแท็บ "ตรวจ match"
// ตอบ 3 คำถาม: (A) เพจกลางมีเบอร์เดือนไหนบ้าง
//               (B) ไฟล์เซลแต่ละไฟล์ match ได้กี่เคส
//               (C) กลับด้าน — เบอร์ในเพจกลางอยู่ใน CRM กี่เบอร์
// ============================================================
function rptDiagBuild_() {
  var lg = rptReadPrachLog_(), log = lg.map;
  var out = [];

  // A) เบอร์ในเพจกลาง แยกตามเดือนที่ลงวันที่ไว้
  var byMonth = {}, noDate = 0;
  for (var k in log) {
    var w = log[k].when;
    if (!w) { noDate++; continue; }
    var mk = Utilities.formatDate(new Date(w), 'Asia/Bangkok', 'yyyy-MM');
    byMonth[mk] = (byMonth[mk] || 0) + 1;
  }
  var ms = [];
  for (var mm in byMonth) ms.push(mm);
  ms.sort();
  for (var i = 0; i < ms.length; i++)
    out.push(['A. เพจกลาง — เบอร์ที่ลงวันที่เดือน ' + ms[i], byMonth[ms[i]], '', '', '']);
  out.push(['A. เพจกลาง — อ่านวันที่ไม่ออก', noDate, '', '', '']);
  out.push(['', '', '', '', '']);

  // B) ต่อไฟล์เซล
  var allKeys = {}, gTot = 0, gHit = 0;
  for (var s = 0; s < RPT_SALES.length; s++) {
    var nm = RPT_SALES[s][0], rw = rptReadSales_(RPT_SALES[s][1]);
    var tot = 0, ab = 0, ph = 0, hit = 0, hitAll = 0;
    for (var j = 0; j < rw.length; j++) {
      var r = rw[j];
      if (!rptNorm_(r[1]) && !rptNorm_(r[3]) && !rptNorm_(r[4])) continue;
      tot++;
      var ks = rptPhoneKeys_(r[4]), m = false;
      for (var a = 0; a < ks.length; a++) { allKeys[ks[a]] = 1; if (log[ks[a]]) m = true; }
      if (m) hitAll++;
      if (!rptIsGood_(r[7])) continue;
      ab++;
      if (ks.length) ph++;
      if (m) hit++;
    }
    gTot += tot; gHit += hitAll;
    out.push(['B. ไฟล์เซล ' + nm, 'ทุกเคส ' + tot, 'A/B ' + ab, 'มีเบอร์อ่านได้ ' + ph,
              'match A/B ' + hit + ' · match ทุกเกรด ' + hitAll]);
  }
  out.push(['B. รวมทุกไฟล์', 'ทุกเคส ' + gTot, '', '', 'match ทุกเกรด ' + gHit]);
  out.push(['', '', '', '', '']);

  // C) กลับด้าน — เบอร์ในเพจกลาง อยู่ใน CRM กี่เบอร์
  var lk = 0, lHit = 0, lRecent = 0, lRecentHit = 0;
  var cut = new Date().getTime() - 45 * 86400000;
  for (var k2 in log) {
    lk++;
    var inCrm = !!allKeys[k2];
    if (inCrm) lHit++;
    if (log[k2].when && log[k2].when >= cut) { lRecent++; if (inCrm) lRecentHit++; }
  }
  out.push(['C. เบอร์ในเพจกลางทั้งหมด', lk, 'อยู่ใน CRM ' + lHit,
            'ไม่อยู่ใน CRM ' + (lk - lHit), '']);
  out.push(['C. เพจกลาง เฉพาะ 45 วันล่าสุด', lRecent, 'อยู่ใน CRM ' + lRecentHit,
            'ไม่อยู่ใน CRM ' + (lRecent - lRecentHit), '']);

  rptWrite_(RPT_PRACH_FILE, 'ตรวจ match',
    ['หัวข้อ', 'ค่า 1', 'ค่า 2', 'ค่า 3', 'ค่า 4'], out,
    'ตัวตรวจ: ทำไม match เบอร์ได้น้อย · อัปเดต ' +
    Utilities.formatDate(new Date(), 'Asia/Bangkok', 'd/M/yyyy HH:mm'));
  Logger.log('[RPT] diag: ' + out.length + ' บรรทัด');
  return out.length;
}

function rptBuildAll() {
  var a = 0, b = 0;
  try { a = rptBuildPrach(); } catch (e) { Logger.log('[RPT] ปราช ล้ม: ' + e); }
  try { b = rptBuildRecall(); } catch (e) { Logger.log('[RPT] recall ล้ม: ' + e); }
  try { rptDiagBuild_(); } catch (e) { Logger.log('[RPT] diag ล้ม: ' + e); }
  return 'ปราช ' + a + ' เคส · recall ' + b + ' เคส';
}

// ตัวคุมความถี่ — งานที่เกาะอยู่วิ่งทุก ~5 นาที ไม่ต้องสร้างรีพอร์ตทุกครั้ง
function rptTick_() {
  var p = PropertiesService.getScriptProperties();
  var last = Number(p.getProperty('RPT_LAST') || 0);
  var now = new Date().getTime();
  if (now - last < 15 * 60 * 1000) return 'skip';
  p.setProperty('RPT_LAST', String(now));
  return rptBuildAll();
}
