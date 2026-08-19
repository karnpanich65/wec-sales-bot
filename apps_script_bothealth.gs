
/* ======================================================================
 * Bot Health Scan — จับ "บอทตอบพลาด" เองทุกคืน  (Gift 19 ส.ค. 2026)
 * ----------------------------------------------------------------------
 * ที่ผ่านมาเรารู้ว่าบอทพลาดก็ต่อเมื่อมีคนบังเอิญเห็นแล้วแคปหน้าจอมาให้
 * ช้า และได้เฉพาะที่คนเห็น — เคส "AI ใช่ไหมเนี้ย" กว่าจะรู้ก็ผ่านไปครึ่งวัน
 *
 * ตัวนี้ไล่อ่าน Chat_Log ของทุกเพจ แล้วตั้งธงเองตาม 5 สัญญาณ
 * เขียนลงแท็บ Bot_Health ในไฟล์ CEO Dashboard (ไฟล์เดียวกับรายงาน)
 *
 * ⚠️ ตัวนี้ "อ่านอย่างเดียว" จาก CRM — ไม่แก้อะไรในชีตลีดเด็ดขาด
 * ====================================================================== */

var BH_TAB = 'Bot_Health';
var BH_DAYS = 3;          // ย้อนหลังกี่วันต่อรอบสแกน
var BH_MAX_ROWS = 4000;   // อ่านท้ายตารางแค่นี้พอ กัน timeout

// 1) ลูกค้าจับได้ว่าคุยกับบอท — สัญญาณแดง เสียลีดทันที
var BH_BOT_DETECT = [
  'ai ใช่ไหม', 'aiใช่ไหม', 'เป็นบอท', 'บอทใช่ไหม', 'บอทรึเปล่า', 'ai รึเปล่า',
  'ไม่ใช่คน', 'คุยกับคนได้ไหม', 'ขอคุยกับคน', 'ตอบไม่ตรง', 'ถามอะไรตอบไม่ตรง',
  'ตอบมั่ว', 'งงมาก', 'ระบบตอบ'
];

// 5) ข้อความสำรองที่บอทใช้ตอนตอบไม่ได้ — เจอบ่อย = มีช่องว่างความรู้
var BH_FALLBACK_HINT = [
  'ขออนุญาตสอบถามเพิ่มนิดนึง', 'ที่ปรึกษารับเรื่องไว้แล้ว',
  'เดี๋ยวที่ปรึกษาติดต่อกลับ'
];


function bhNorm_(s) {
  return String(s || '')
    .replace(/[​-‍﻿ ]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
}

function bhHit_(text, words) {
  var t = bhNorm_(text);
  for (var i = 0; i < words.length; i++) if (t.indexOf(words[i]) >= 0) return words[i];
  return '';
}

function bhDay_(v) {
  if (!v) return '';
  if (Object.prototype.toString.call(v) === '[object Date]')
    return Utilities.formatDate(v, P4_TZ, 'yyyy-MM-dd');
  var m = String(v).trim().match(/^[0-9]{4}-[0-9]{2}-[0-9]{2}/);
  return m ? m[0] : '';
}

function bhTime_(v) {
  if (Object.prototype.toString.call(v) === '[object Date]') return v.getTime();
  var s = String(v || '').trim().replace(' ', 'T');
  var d = new Date(s);
  return isNaN(d.getTime()) ? 0 : d.getTime();
}


/* ---------------------------------------------------------------- scan ---- */
function botHealthScan() {
  var ss = p4SS();
  var cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - BH_DAYS);
  var cutoffDay = Utilities.formatDate(cutoff, P4_TZ, 'yyyy-MM-dd');

  var flags = [];
  var seenTabs = {};

  for (var pi = 0; pi < P4_PAGES.length; pi++) {
    var pg = P4_PAGES[pi];
    var tab = pg.log || 'Chat_Log';
    if (seenTabs[tab]) continue;         // หลายเพจใช้แท็บเดียวกันได้
    seenTabs[tab] = 1;
    var sh = ss.getSheetByName(tab);
    if (!sh) continue;
    var last = sh.getLastRow();
    if (last < 2) continue;
    var from = Math.max(2, last - BH_MAX_ROWS);
    var v = sh.getRange(from, 1, last - from + 1, 11).getValues();

    // จัดกลุ่มตามคนคุย
    var conv = {};
    for (var r = 0; r < v.length; r++) {
      var day = bhDay_(v[r][0]);
      if (!day || day < cutoffDay) continue;
      var psid = String(v[r][2] || '').trim();
      if (!psid) continue;
      if (!conv[psid]) conv[psid] = [];
      conv[psid].push({
        t: bhTime_(v[r][0]), day: day, stage: String(v[r][3] || ''),
        role: String(v[r][5] || ''), text: String(v[r][6] || ''),
        lead: String(v[r][9] || ''), page: String(v[r][10] || '') || pg.id
      });
    }

    for (var psid in conv) {
      var rows = conv[psid];
      rows.sort(function (a, b) { return a.t - b.t; });
      var pageName = p4PageName(rows[rows.length - 1].page) || pg.name;
      var lead = '';
      for (var q = rows.length - 1; q >= 0; q--) if (rows[q].lead) { lead = rows[q].lead; break; }

      var botMsgs = [], lastBotIdx = -1, burst = {}, sawDetect = false;

      for (var i = 0; i < rows.length; i++) {
        var row = rows[i];
        if (row.role === 'user' || row.role === 'customer' || row.role === 'cust') {
          var hit = bhHit_(row.text, BH_BOT_DETECT);
          if (hit && !sawDetect) {
            sawDetect = true;
            flags.push([row.day, pageName, '🔴 ลูกค้าจับได้ว่าเป็นบอท', psid, lead,
                        row.text.slice(0, 200), 'พบคำว่า "' + hit + '"']);
          }
        } else {
          botMsgs.push(bhNorm_(row.text).slice(0, 60));
          lastBotIdx = i;
          var k = row.day + '|' + Math.floor(row.t / 1000);
          burst[k] = (burst[k] || 0) + 1;
        }
      }

      // 2) ถามซ้ำ — ข้อความบอทตัวเดิมโผล่ 2 ครั้งใน 5 บับเบิลติดกัน
      for (var b = 0; b < botMsgs.length; b++) {
        for (var c = b + 1; c < Math.min(b + 5, botMsgs.length); c++) {
          if (botMsgs[b] && botMsgs[b] === botMsgs[c]) {
            flags.push([rows[rows.length - 1].day, pageName, '⚠️ บอทถามซ้ำข้อเดิม', psid, lead,
                        botMsgs[b], 'ซ้ำภายใน 5 บับเบิล']);
            b = botMsgs.length; break;
          }
        }
      }

      // 3) ยิงรัว — ส่ง 3 บับเบิลขึ้นไปในวินาทีเดียวกัน
      for (var kk in burst) {
        if (burst[kk] >= 3) {
          flags.push([kk.split('|')[0], pageName, '⚠️ ยิงรัว ' + burst[kk] + ' บับเบิล',
                      psid, lead, '', 'กำแพงข้อความ — ลูกค้าสแกนผ่าน']);
          break;
        }
      }

      // 4) ลูกค้าหายหลังบอทตอบ — ข้อความสุดท้ายเป็นของบอท + เงียบเกิน 24 ชม.
      var lastRow = rows[rows.length - 1];
      var quietHrs = (new Date().getTime() - lastRow.t) / 3600000;
      if (lastBotIdx === rows.length - 1 && quietHrs >= 24 && quietHrs <= 24 * BH_DAYS) {
        flags.push([lastRow.day, pageName, 'ℹ️ ลูกค้าเงียบหลังบอทตอบ', psid, lead,
                    lastRow.text.slice(0, 160),
                    'จุดที่หลุด: ' + (lastRow.stage || '-')]);
      }

      // 5) ตอบด้วยข้อความสำรองซ้ำหลายครั้ง = มีช่องว่างความรู้
      var fbCount = 0;
      for (var f = 0; f < botMsgs.length; f++) if (bhHit_(botMsgs[f], BH_FALLBACK_HINT)) fbCount++;
      if (fbCount >= 3) {
        flags.push([lastRow.day, pageName, '⚠️ ตอบด้วยข้อความสำรอง ' + fbCount + ' ครั้ง',
                    psid, lead, '', 'บอทไม่รู้จะตอบอะไร — เติม FAQ']);
      }
    }
  }

  bhWrite_(flags);
  return 'Bot_Health: ' + flags.length + ' ธง (' + BH_DAYS + ' วันล่าสุด)';
}


function bhWrite_(flags) {
  var ds = ceoSS_();
  var sh = ds.getSheetByName(BH_TAB) || ds.insertSheet(BH_TAB);
  sh.clear();
  sh.clearConditionalFormatRules();

  var stamp = Utilities.formatDate(new Date(), P4_TZ, 'd MMM yyyy HH:mm');
  sh.getRange(1, 1).setValue('สุขภาพบอท — ธงที่ระบบจับได้เอง')
    .setFontSize(14).setFontWeight('bold');
  sh.getRange(1, 3).setValue('สแกนอัตโนมัติ ' + stamp + ' น. · ย้อนหลัง ' + BH_DAYS + ' วัน')
    .setFontColor('#888888');

  // สรุปนับตามประเภท — อ่านบรรทัดเดียวรู้ว่าคืนนี้มีอะไรต้องดู
  var count = {};
  for (var i = 0; i < flags.length; i++) {
    var kind = flags[i][2].replace(/ [0-9]+ .*$/, '');
    count[kind] = (count[kind] || 0) + 1;
  }
  var summary = [];
  for (var k in count) summary.push(k + ' ' + count[k]);
  sh.getRange(2, 1).setValue(summary.length ? summary.join('  ·  ') : '✅ ไม่พบธงผิดปกติ')
    .setFontWeight('bold');

  var head = ['วันที่', 'เพจ', 'ประเภทธง', 'PSID', 'Lead ID', 'ข้อความที่เจอ', 'หมายเหตุ'];
  sh.getRange(4, 1, 1, head.length).setValues([head])
    .setFontWeight('bold').setBackground('#17203D').setFontColor('#FFFFFF');
  sh.setFrozenRows(4);

  if (flags.length) {
    // แดงขึ้นก่อนเสมอ
    flags.sort(function (a, b) {
      var ra = a[2].indexOf('🔴') === 0 ? 0 : 1, rb = b[2].indexOf('🔴') === 0 ? 0 : 1;
      return ra - rb || (a[0] < b[0] ? 1 : -1);
    });
    sh.getRange(5, 1, flags.length, head.length).setValues(flags);
  }
  sh.setColumnWidth(1, 90);
  sh.setColumnWidth(2, 140);
  sh.setColumnWidth(3, 210);
  sh.setColumnWidth(4, 120);
  sh.setColumnWidth(5, 130);
  sh.setColumnWidth(6, 420);
  sh.setColumnWidth(7, 260);
}


/* ---------- ติดตั้ง trigger รายคืน (ตี 2 หลัง ceoRefreshAll ตี 1) ---------- */
function RUN_ONCE_botHealthSetup() {
  var out = botHealthScan();
  var have = false;
  var tg = ScriptApp.getProjectTriggers();
  for (var i = 0; i < tg.length; i++)
    if (tg[i].getHandlerFunction() === 'botHealthScan') have = true;
  if (!have)
    ScriptApp.newTrigger('botHealthScan').timeBased().atHour(2).everyDays(1).create();
  return out;
}
