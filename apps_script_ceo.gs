
/* ======================================================================
 * รายงาน CEO — Daily / Weekly / Monthly   (Gift 19 ส.ค. 2026)
 * ----------------------------------------------------------------------
 * โจทย์: แถว = ตัวชี้วัด · คอลัมน์ = ช่วงเวลา (วัน/สัปดาห์/เดือน)
 *        ไล่คอลัมน์ไปเพื่อเทียบแนวโน้ม เห็นภาพจากชีตโดยไม่ต้องอ่านทีละเลข
 *
 * สถาปัตยกรรม — สำคัญ อย่าแก้โครงนี้โดยไม่อ่านก่อน
 *   Leads_<เพจ>  ->  _raw_daily (1 แถว = 1 เพจ 1 วัน)  ->  Daily / Weekly / Monthly
 *   ทั้งสามแท็บอ่านจาก _raw_daily ตัวเดียวกัน = เลขตรงกันเสมอ
 *   ถ้าให้แต่ละแท็บวิ่งไปนับจาก Leads เอง สามแท็บจะให้เลขไม่ตรงกันแน่นอน
 *
 * ไฟล์นี้แยกจาก WEC CRM โดยตั้งใจ — CRM แชร์ไม่ได้ (รวมทุกเพจทุกเซล)
 * แต่ Dashboard ไม่มีข้อมูลลูกค้ารายคนเลย จึงส่งให้หุ้นส่วนดูได้
 * ====================================================================== */

var CEO_NAME   = 'WEC CEO Dashboard';
var CEO_KEY    = 'CEO_SHEET_ID';
var CEO_BAR    = 25000;      // เกณฑ์รายได้ที่ยื่นเดี่ยวผ่าน
var CEO_DAYS   = 45;         // จำนวนวันที่โชว์ในแท็บ Daily
var CEO_WEEKS  = 26;
var CEO_MONTHS = 12;
var CEO_TREND  = 30;         // ความยาวเส้น sparkline

// ลำดับนี้คือลำดับแถวในทุกแท็บ — แก้ที่นี่ที่เดียว ทั้งสามแท็บเปลี่ยนตาม
// pct: [ตัวเศษ, ตัวส่วน] · ไม่ใส่ = ตัวเลขดิบ
var CEO_METRICS = [
  { k:'new',     t:'คนทักใหม่' },
  { k:'active',  t:'คนที่คุยทั้งหมด' },
  { k:'qual',    t:'รายได้ถึงเกณฑ์ 25,000' },
  { k:'qualpct', t:'   % ถึงเกณฑ์',   pct:['qual','new'] },
  { k:'contact', t:'ให้ข้อมูลติดต่อ' },
  { k:'ctpct',   t:'   % ได้ช่องทาง', pct:['contact','new'] },
  { k:'ab',      t:'ลีดคุณภาพ (A+B)' },
  { k:'gA',      t:'   เกรด A' },
  { k:'gB',      t:'   เกรด B' },
  { k:'gC',      t:'   เกรด C' },
  { k:'gD',      t:'   เกรด D' },
  { k:'gN',      t:'   เกรด N (ข้อมูลไม่พอ)' },
  { k:'gOR',     t:'   เจ้าของห้อง/ผู้เช่า' }
];

var CEO_RAW_HEAD = ['วันที่','เพจ','สัปดาห์','เดือน',
                    'คนทักใหม่','คนที่คุยทั้งหมด','ถึงเกณฑ์25k','ให้ช่องทางติดต่อ',
                    'A','B','C','D','N','เจ้าของ/ผู้เช่า'];
var CEO_RAW_KEYS = ['new','active','qual','contact','gA','gB','gC','gD','gN','gOR'];
var CEO_ALL = 'รวมทุกเพจ';


/* ---------- ไฟล์ปลายทาง (จำ id ไว้ ไม่สร้างซ้ำ) ---------- */
function ceoSS_(){
  var p = PropertiesService.getScriptProperties();
  var id = p.getProperty(CEO_KEY);
  if (id) { try { return SpreadsheetApp.openById(id); } catch(e){} }
  var it = DriveApp.getFilesByName(CEO_NAME), ss;
  if (it.hasNext()) ss = SpreadsheetApp.openById(it.next().getId());
  else {
    ss = SpreadsheetApp.create(CEO_NAME);
    ss.getSheets()[0].setName('README');
  }
  p.setProperty(CEO_KEY, ss.getId());
  return ss;
}

function ceoUrl(){ return ceoSS_().getUrl(); }


/* ---------- อ่านวันที่จากเซล (เป็นได้ทั้ง Date และข้อความ) ---------- */
function ceoDay_(v){
  if (!v) return '';
  if (Object.prototype.toString.call(v) === '[object Date]')
    return Utilities.formatDate(v, P4_TZ, 'yyyy-MM-dd');
  var m = String(v).trim().match(/^[0-9]{4}-[0-9]{2}-[0-9]{2}/);
  return m ? m[0] : '';
}

/* สัปดาห์แบบ ISO (จันทร์เป็นวันแรก) — คีย์รวมยอดรายสัปดาห์ */
function ceoWeek_(ymd){
  var p = ymd.split('-');
  var d = new Date(Number(p[0]), Number(p[1])-1, Number(p[2]));
  var day = (d.getDay() + 6) % 7;                 // จ.=0 ... อา.=6
  d.setDate(d.getDate() - day + 3);               // เลื่อนไปวันพฤหัสของสัปดาห์นั้น
  var year = d.getFullYear();
  var jan4 = new Date(year, 0, 4);
  var wk = 1 + Math.round(((d - jan4) / 86400000 - 3 + ((jan4.getDay()+6)%7)) / 7);
  return year + '-W' + (wk < 10 ? '0'+wk : wk);
}

function ceoBump_(map, day, page, key, n){
  var names = [CEO_ALL, page];
  for (var i=0; i<names.length; i++){
    var pn = names[i];
    var k = day + '||' + pn;
    if (!map[k]) map[k] = { day:day, page:pn };
    map[k][key] = (map[k][key] || 0) + n;
  }
}


/* ======================================================================
 * 1) สแนปช็อต — สร้าง _raw_daily ใหม่ทั้งก้อนจาก Leads ทุกเพจ
 * ----------------------------------------------------------------------
 * สร้างใหม่ทั้งก้อนทุกรอบโดยตั้งใจ (ไม่ใช่ต่อท้าย) เพราะเกรดของเคสเดิม
 * เปลี่ยนได้ทีหลัง — เซลอัปเดตสถานะ บอทได้ข้อมูลเพิ่มแล้วตีเกรดใหม่
 * ถ้าเก็บแบบต่อท้ายอย่างเดียว ตัวเลขย้อนหลังจะค้างที่ค่าเก่าตลอดไป
 * ====================================================================== */
function ceoSnapshot(){
  var ss = p4SS();
  var map = {};
  for (var i=0; i<P4_PAGES.length; i++){
    var pg = P4_PAGES[i];
    var sh = ss.getSheetByName(pg.tab);
    if (!sh) continue;
    var last = sh.getLastRow();
    if (last < 2) continue;
    var wide = Math.min(P4_COLS, sh.getLastColumn());
    var v = sh.getRange(2, 1, last-1, wide).getValues();
    for (var r=0; r<v.length; r++){
      var row = v[r];
      if (!String(row[P4_C.PSID-1] || '').trim()) continue;   // แถวว่าง
      var seen  = ceoDay_(row[P4_C.SEEN-1]);
      var first = ceoDay_(row[P4_C.FIRST-1]) || ceoDay_(row[P4_C.OPENED-1]);
      if (seen) ceoBump_(map, seen, pg.name, 'active', 1);
      if (!first) continue;
      ceoBump_(map, first, pg.name, 'new', 1);

      // ถึงเกณฑ์รายได้ — ใช้ช่องตัวเลขที่บอทส่งมา (นับรวมผู้กู้ร่วมแล้ว)
      // ห้ามไปแกะเลขจากข้อความ "พนักงานประจำ รายได้ 60k ค่ะ" เด็ดขาด
      if (String(row[P4_C.QUAL-1] || '') === '1')
        ceoBump_(map, first, pg.name, 'qual', 1);

      var phone = String(row[P4_C.PHONE-1] || '').trim();
      var line  = String(row[P4_C.LINE-1]  || '').trim();
      if (phone || (line && line !== '-'))
        ceoBump_(map, first, pg.name, 'contact', 1);

      var g = String(row[P4_C.GRADE-1] || '').trim().toUpperCase();
      if (g === 'A' || g === 'B' || g === 'C' || g === 'D' || g === 'N')
        ceoBump_(map, first, pg.name, 'g'+g, 1);
      else if (g === 'O' || g === 'R')
        ceoBump_(map, first, pg.name, 'gOR', 1);
    }
  }

  var keys = Object.keys(map).sort();
  var out = [];
  for (var j=0; j<keys.length; j++){
    var o = map[keys[j]];
    var line2 = [o.day, o.page, ceoWeek_(o.day), o.day.slice(0,7)];
    for (var m2=0; m2<CEO_RAW_KEYS.length; m2++) line2.push(o[CEO_RAW_KEYS[m2]] || 0);
    out.push(line2);
  }

  var ds = ceoSS_();
  var raw = ds.getSheetByName('_raw_daily') || ds.insertSheet('_raw_daily');
  raw.clear();
  // บังคับคอลัมน์วันที่/เดือนเป็นข้อความ ไม่งั้น Sheets แปลงเป็น Date
  // แล้วตอนอ่านกลับมาจะได้ object ไม่ใช่ '2026-08-19' -> สร้างมุมมองพัง
  raw.getRange(1,1,raw.getMaxRows(),1).setNumberFormat('@');
  raw.getRange(1,3,raw.getMaxRows(),2).setNumberFormat('@');
  raw.getRange(1,1,1,CEO_RAW_HEAD.length).setValues([CEO_RAW_HEAD])
     .setFontWeight('bold').setBackground('#17203D').setFontColor('#FFFFFF');
  if (out.length) raw.getRange(2,1,out.length,CEO_RAW_HEAD.length).setValues(out);
  raw.setFrozenRows(1);
  return 'raw rows: ' + out.length;
}


/* ---------- ดึง _raw_daily มาใช้สร้างมุมมอง ---------- */
/* เซลอาจคืนค่าเป็น Date ถ้า Sheets เผลอแปลงชนิดข้อมูล — บังคับให้เป็นข้อความเสมอ */
function ceoStr_(v, fmt){
  if (Object.prototype.toString.call(v) === '[object Date]')
    return Utilities.formatDate(v, P4_TZ, fmt);
  return String(v || '');
}

function ceoLoadRaw_(){
  var ds = ceoSS_();
  var raw = ds.getSheetByName('_raw_daily');
  if (!raw || raw.getLastRow() < 2) return { rows:[], pages:[] };
  var v = raw.getRange(2, 1, raw.getLastRow()-1, CEO_RAW_HEAD.length).getValues();
  for (var q=0; q<v.length; q++){
    v[q][0] = ceoStr_(v[q][0], 'yyyy-MM-dd');
    v[q][2] = ceoStr_(v[q][2], 'yyyy-MM');
    v[q][3] = ceoStr_(v[q][3], 'yyyy-MM');
  }
  var pages = [CEO_ALL];
  for (var i=0; i<P4_PAGES.length; i++) pages.push(P4_PAGES[i].name);
  return { rows:v, pages:pages };
}

/* รวมยอดตามคีย์เวลา (วัน / สัปดาห์ / เดือน) */
function ceoAgg_(rows, colIdx){
  var agg = {};
  for (var i=0; i<rows.length; i++){
    var r = rows[i];
    var pg = r[1], b = r[colIdx];
    if (!agg[pg]) agg[pg] = {};
    if (!agg[pg][b]) agg[pg][b] = {};
    for (var m=0; m<CEO_RAW_KEYS.length; m++){
      var k = CEO_RAW_KEYS[m];
      agg[pg][b][k] = (agg[pg][b][k] || 0) + Number(r[4+m] || 0);
    }
  }
  return agg;
}

function ceoBuckets_(rows, colIdx, limit){
  var seen = {}, list = [];
  for (var i=0; i<rows.length; i++){
    var b = rows[i][colIdx];
    if (b && !seen[b]) { seen[b] = 1; list.push(b); }
  }
  list.sort();
  list.reverse();                       // ล่าสุดอยู่ซ้าย ไม่ต้องเลื่อนไปหา
  return list.slice(0, limit);
}

function ceoVal_(cell, k){
  if (!cell) return 0;
  if (k === 'ab') return (cell.gA||0) + (cell.gB||0);
  return cell[k] || 0;
}


/* ======================================================================
 * 2) สร้างมุมมอง — แถว = ตัวชี้วัด · คอลัมน์ = ช่วงเวลา
 * ====================================================================== */
function ceoBuildView_(tabName, colIdx, limit, title, fmtHead){
  var d = ceoLoadRaw_();
  var agg = ceoAgg_(d.rows, colIdx);
  var buckets = ceoBuckets_(d.rows, colIdx, limit);
  var nb = Math.max(buckets.length, 1);

  var ds = ceoSS_();
  var sh = ds.getSheetByName(tabName) || ds.insertSheet(tabName);
  sh.clear();
  sh.clearConditionalFormatRules();

  var stamp = Utilities.formatDate(new Date(), P4_TZ, 'd MMM yyyy HH:mm');
  sh.getRange(1,1).setValue(title).setFontSize(14).setFontWeight('bold');
  sh.getRange(1,3).setValue('อัปเดตอัตโนมัติ ' + stamp + ' น.').setFontColor('#888888');

  // ---- แถบสรุปอ่าน 10 วินาที (เฉพาะแท็บ Daily) ----
  var top = 3;
  if (tabName === 'Daily'){
    var lines = ceoHeadline_(agg, buckets);
    for (var i=0; i<lines.length; i++)
      sh.getRange(2+i, 1).setValue(lines[i]).setFontWeight(i === 0 ? 'bold' : 'normal');
    top = 2 + lines.length + 1;
  }

  var head = ['ตัวชี้วัด', 'เทรนด์'];
  for (var b=0; b<buckets.length; b++) head.push(fmtHead(buckets[b]));
  sh.getRange(top, 1, 1, head.length).setValues([head])
    .setFontWeight('bold').setBackground('#17203D').setFontColor('#FFFFFF');
  sh.setFrozenRows(top);
  sh.setFrozenColumns(2);
  sh.setColumnWidth(1, 210);
  sh.setColumnWidth(2, 90);

  var row = top + 1;
  for (var p=0; p<d.pages.length; p++){
    var pg = d.pages[p];
    if (!agg[pg]) continue;

    sh.getRange(row, 1, 1, head.length).setBackground(p === 0 ? '#DCE6F5' : '#F1F3F4');
    sh.getRange(row, 1).setValue((p === 0 ? '■ ' : '□ ') + pg).setFontWeight('bold');
    row++;

    var blockStart = row;
    for (var mi=0; mi<CEO_METRICS.length; mi++){
      var M = CEO_METRICS[mi];
      var vals = [];
      for (var bb=0; bb<buckets.length; bb++){
        var cell = agg[pg][buckets[bb]];
        if (M.pct){
          var num = ceoVal_(cell, M.pct[0]), den = ceoVal_(cell, M.pct[1]);
          vals.push(den ? num/den : '');
        } else {
          vals.push(ceoVal_(cell, M.k));
        }
      }
      sh.getRange(row, 1).setValue(M.t);
      if (buckets.length) sh.getRange(row, 3, 1, buckets.length).setValues([vals]);
      if (M.pct){
        sh.getRange(row, 3, 1, nb).setNumberFormat('0%');
        sh.getRange(row, 1, 1, head.length).setFontColor('#666666');
      } else {
        // เส้นเทรนด์จิ๋ว — ตารางเรียงใหม่->เก่า แต่กราฟต้องอ่านเก่า->ใหม่ จึงกลับด้าน
        var n = Math.min(CEO_TREND, buckets.length);
        if (n > 1){
          var a1 = sh.getRange(row, 3, 1, n).getA1Notation();
          sh.getRange(row, 2).setFormula(
            '=SPARKLINE(TRANSPOSE(SORT(TRANSPOSE(' + a1 + '),SEQUENCE(COLUMNS(' + a1 + ')),FALSE)),' +
            '{"charttype","line";"linewidth",2;"color","#2B5CE6";"empty","zero"})');
        }
      }
      row++;
    }

    // ระบายสีตามค่าเฉพาะบล็อกนี้ — วันที่ตกผิดปกติจะเด้งออกมาเอง
    if (buckets.length){
      var rng = sh.getRange(blockStart, 3, CEO_METRICS.length, buckets.length);
      var rules = sh.getConditionalFormatRules();
      rules.push(SpreadsheetApp.newConditionalFormatRule()
        .setGradientMinpointWithValue('#FFFFFF', SpreadsheetApp.InterpolationType.NUMBER, '0')
        .setGradientMaxpoint('#7CA9F5')
        .setRanges([rng]).build());
      sh.setConditionalFormatRules(rules);
    }
    row++;   // เว้นบรรทัดคั่นบล็อก
  }
  return tabName + ' ' + buckets.length + ' คอลัมน์';
}


/* ---------- แถบสรุปบนสุด — สิ่งที่ต้องอ่านก่อนอย่างอื่น ---------- */
function ceoHeadline_(agg, buckets){
  if (!buckets.length) return ['ยังไม่มีข้อมูล'];
  var A = agg[CEO_ALL] || {};
  var d0 = buckets[0];
  var all = A[d0] || {};
  var cur7 = 0, prev7 = 0, i;
  for (i=0; i<7 && i<buckets.length; i++)  cur7  += ceoVal_(A[buckets[i]], 'new');
  for (i=7; i<14 && i<buckets.length; i++) prev7 += ceoVal_(A[buckets[i]], 'new');
  var trend = prev7 ? Math.round((cur7-prev7)/prev7*100) : null;

  var best = '-', bestN = -1, worst = '-', worstN = 1e9;
  for (var pg in agg){
    if (pg === CEO_ALL) continue;
    var s = 0, t = 0;
    for (var k=0; k<7 && k<buckets.length; k++){
      s += ceoVal_(agg[pg][buckets[k]], 'ab');
      t += ceoVal_(agg[pg][buckets[k]], 'new');
    }
    if (!t) continue;
    if (s > bestN)  { bestN = s;  best  = pg + ' (' + s + ' ใบ)'; }
    if (s < worstN) { worstN = s; worst = pg + ' (' + s + ' ใบ)'; }
  }

  return [
    'สรุปวันที่ ' + d0,
    'คนทักใหม่ ' + ceoVal_(all,'new') + ' คน · ถึงเกณฑ์รายได้ ' + ceoVal_(all,'qual') +
      ' · ให้ช่องทางติดต่อ ' + ceoVal_(all,'contact') +
      ' · ลีดคุณภาพ A+B ' + ceoVal_(all,'ab') + ' ใบ',
    '7 วันล่าสุด คนทักใหม่ ' + cur7 + ' คน  ' +
      (trend === null || prev7 < 5
        ? '(ยังไม่มีข้อมูล 7 วันก่อนหน้ามากพอจะเทียบ)'
        : (trend >= 0 ? '▲ +' : '▼ ') + trend + '% เทียบ 7 วันก่อนหน้า'),
    'ลีดคุณภาพสูงสุด 7 วัน: ' + best + '   ·   ต่ำสุด: ' + worst
  ];
}


/* ---------- ปุ่มเรียกใช้ ---------- */
function ceoBuildDaily(){
  return ceoBuildView_('Daily', 0, CEO_DAYS, 'รายงานรายวัน — WEC ทุกเพจ', function(b){
    var p = b.split('-');
    return Number(p[2]) + '/' + Number(p[1]);
  });
}
function ceoBuildWeekly(){
  return ceoBuildView_('Weekly', 2, CEO_WEEKS, 'รายงานรายสัปดาห์ — WEC ทุกเพจ', function(b){
    return 'W' + b.split('-W')[1];
  });
}
function ceoBuildMonthly(){
  var TH = ['ม.ค.','ก.พ.','มี.ค.','เม.ย.','พ.ค.','มิ.ย.',
            'ก.ค.','ส.ค.','ก.ย.','ต.ค.','พ.ย.','ธ.ค.'];
  return ceoBuildView_('Monthly', 3, CEO_MONTHS, 'รายงานรายเดือน — WEC ทุกเพจ', function(b){
    var p = b.split('-');
    return TH[Number(p[1])-1] + ' ' + String(Number(p[0])+543).slice(2);
  });
}

/* รันทั้งชุด — ตัวที่ trigger เรียกทุกคืน */
function ceoRefreshAll(){
  var out = [ceoSnapshot(), ceoBuildDaily(), ceoBuildWeekly(), ceoBuildMonthly()];
  ceoReadme_();
  return out.join(' | ');
}

function ceoReadme_(){
  var ds = ceoSS_();
  var sh = ds.getSheetByName('README') || ds.insertSheet('README');
  sh.clear();
  var L = [
    ['WEC CEO Dashboard',''],
    ['อัปเดตอัตโนมัติทุกคืน ตี 1 — ไม่ต้องกดอะไร',''],
    ['',''],
    ['Daily','รายวัน 45 วันล่าสุด · ล่าสุดอยู่คอลัมน์ซ้ายสุด'],
    ['Weekly','รายสัปดาห์ 26 สัปดาห์ล่าสุด (สัปดาห์เริ่มวันจันทร์)'],
    ['Monthly','รายเดือน 12 เดือนล่าสุด'],
    ['',''],
    ['วิธีอ่าน',''],
    ['คอลัมน์ "เทรนด์"','เส้นกราฟจิ๋ว 30 ช่วงล่าสุด อ่านซ้ายไปขวา = เก่าไปใหม่'],
    ['สีพื้นในตาราง','เข้ม = ตัวเลขสูง · จาง = ต่ำ ดูรูปทรงได้โดยไม่ต้องอ่านเลข'],
    ['บล็อก ■ รวมทุกเพจ','ภาพรวมทั้งบริษัท · บล็อก □ คือรายเพจ เลื่อนลงดู'],
    ['',''],
    ['นิยามตัวเลข',''],
    ['คนทักใหม่','คนที่ทักเข้ามาครั้งแรกในวันนั้น (ใช้วัดผลโฆษณา)'],
    ['คนที่คุยทั้งหมด','ทุกคนที่มีความเคลื่อนไหวในวันนั้น รวมคนเก่าคุยต่อ (ใช้วัดภาระงาน)'],
    ['ถึงเกณฑ์ 25,000','รายได้รวมกับผู้กู้ร่วมแล้วไม่ต่ำกว่า 25,000 · เคสที่ยังไม่บอกรายได้ไม่นับทั้งสองฝั่ง'],
    ['ให้ข้อมูลติดต่อ','ได้เบอร์โทรหรือ LINE ID อย่างน้อยหนึ่งอย่าง'],
    ['ลีดคุณภาพ (A+B)','A = กู้ได้เลย · B = ปิดภาระก่อนแล้วกู้ได้ (เคสบริดจ์)'],
    ['',''],
    ['ทุกตัวเลขผูกกับ "วันที่ทักครั้งแรก" ของลูกค้าคนนั้น',''],
    ['เช่น ทักวันจันทร์ ให้เบอร์วันพุธ จะนับเบอร์ให้วันจันทร์',''],
    ['เพื่อให้ % แปลงเป็นลูกค้าของแต่ละวันเทียบกันได้จริง',''],
    ['',''],
    ['ไฟล์นี้ไม่มีข้อมูลลูกค้ารายคน ส่งให้หุ้นส่วนดูได้ · WEC CRM ห้ามแชร์เด็ดขาด','']
  ];
  sh.getRange(1,1,L.length,2).setValues(L);
  sh.getRange(1,1).setFontSize(16).setFontWeight('bold');
  sh.setColumnWidth(1, 220);
  sh.setColumnWidth(2, 620);
}


/* ---------- ติดตั้งครั้งเดียว ---------- */
function RUN_ONCE_ceoSetup(){
  var out = ceoRefreshAll();
  var have = false;
  var tg = ScriptApp.getProjectTriggers();
  for (var i=0; i<tg.length; i++)
    if (tg[i].getHandlerFunction() === 'ceoRefreshAll') have = true;
  if (!have)
    ScriptApp.newTrigger('ceoRefreshAll').timeBased().atHour(1).everyDays(1).create();
  return out + ' | ' + ceoSS_().getUrl();
}


/* ======================================================================
 * 3) เติมตัวเลขรายได้ย้อนหลัง — ลีดเก่าที่เก็บไว้เป็นข้อความล้วน
 * ----------------------------------------------------------------------
 * เขียนเฉพาะแถวที่ช่องตัวเลขยังว่าง — ของที่บอทส่งมาแล้วห้ามทับ
 * แกะไม่ออก = ปล่อยว่างไว้ ห้ามเดา
 * (เลขเดาแล้วเข้ารายงาน CEO อันตรายกว่าเลขหาย เพราะดูสมเหตุสมผล)
 * ====================================================================== */
function ceoParseIncome_(txt){
  var s = String(txt||'').replace(/,/g,'').toLowerCase();
  if (!s) return null;
  var m = s.match(/([0-9]+(?:\.[0-9]+)?)\s*(ล้าน|แสน|หมื่น|พัน|k|m)/);
  if (m){
    var n = parseFloat(m[1]), u = m[2];
    if (u === 'ล้าน' || u === 'm') n *= 1000000;
    else if (u === 'แสน')  n *= 100000;
    else if (u === 'หมื่น') n *= 10000;
    else if (u === 'พัน' || u === 'k') n *= 1000;
    return Math.round(n);
  }
  var best = null;
  var all = s.match(/[0-9]+/g) || [];
  for (var i=0; i<all.length; i++){
    var v = Number(all[i]);
    if (v >= 3000 && v <= 3000000 && (best === null || v > best)) best = v;
  }
  return best;
}

function RUN_ONCE_ceoBackfillIncome(){
  var ss = p4SS(), done = 0, skip = 0;
  for (var i=0; i<P4_PAGES.length; i++){
    var sh = p4LeadSheet(P4_PAGES[i].tab);
    if (!sh) continue;
    var last = sh.getLastRow();
    if (last < 2) continue;
    var inc  = sh.getRange(2, P4_C.INCOME,   last-1, 1).getValues();
    var numR = sh.getRange(2, P4_C.INCOME_N, last-1, 1);
    var qR   = sh.getRange(2, P4_C.QUAL,     last-1, 1);
    var num  = numR.getValues(), q = qR.getValues();
    for (var r=0; r<inc.length; r++){
      if (num[r][0] !== '' && num[r][0] !== null) continue;   // บอทเขียนไว้แล้ว
      var n = ceoParseIncome_(inc[r][0]);
      if (n === null) { skip++; continue; }
      num[r][0] = n;
      q[r][0] = (n >= CEO_BAR) ? '1' : '0';
      done++;
    }
    numR.setValues(num);
    qR.setValues(q);
  }
  // เติมเสร็จแล้วสร้างรายงานใหม่ทันที ไม่ต้องรอ trigger รอบดึก
  var re = ceoRefreshAll();
  return 'เติมได้ ' + done + ' แถว · แกะไม่ออก ' + skip + ' แถว (ปล่อยว่างไว้ ไม่เดา) | ' + re;
}
