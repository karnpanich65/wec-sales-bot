/**
 * WEC Bot — Google Apps Script Web App
 * Sheet: WEC CRM — ฐานข้อมูลลูกค้า  |  Tab: Facebook Leads
 *
 * วิธี Deploy (ทำครั้งเดียว):
 * 1. ไปที่ https://script.google.com -> New project
 * 2. Paste code นี้ -> Save
 * 3. Deploy -> New deployment -> Type: Web app
 *    - Execute as: Me (karnpanich.phutrakul@gmail.com)
 *    - Who has access: Anyone
 * 4. Copy Web app URL -> ใส่ใน Railway: APPS_SCRIPT_URL=<url>
 */

var SHEET_ID   = '1TyOIHDmyjcvpWepaLOOJ8MAzfLB3wAzTLqb4QU15h8s';
var SHEET_TAB  = 'Facebook Leads';
var CALENDAR_ID = 'primary';

function doPost(e) {
  try {
    var payload = JSON.parse(e.postData.contents);
    var leadId  = appendLead(payload);
    var eventId = createCalendarEvent(payload, leadId);
    return ContentService
      .createTextOutput(JSON.stringify({ status: 'ok', leadId: leadId, eventId: eventId }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ status: 'error', message: err.message }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

function doGet(e) {
  return ContentService
    .createTextOutput(JSON.stringify({ status: 'alive', script: 'WEC Bot Sheet+Calendar Handler' }))
    .setMimeType(ContentService.MimeType.JSON);
}

function appendLead(data) {
  var ss    = SpreadsheetApp.openById(SHEET_ID);
  var sheet = ss.getSheetByName(SHEET_TAB);
  var now   = new Date();
  var tz    = 'Asia/Bangkok';

  var lastRow = sheet.getLastRow();
  var seq     = String(lastRow).padStart(3, '0');
  var leadId  = 'FB-' + Utilities.formatDate(now, tz, 'yyyyMMdd') + '-' + seq;

  var contact = (data.contact || '').trim();
  var phone   = '';
  var lineId  = '';
  if (/^0[0-9]{8,9}$/.test(contact.replace(/[-\s]/g, ''))) {
    phone = contact;
  } else {
    lineId = contact;
  }

  var callbackTime = getCallbackTime(now, data.grade);
  var callbackStr  = Utilities.formatDate(callbackTime, tz, 'yyyy-MM-dd HH:mm');
  var actionNote   = buildActionNote(data);

  // Row: A-V (22 columns)
  var row = [
    leadId,
    '',
    phone,
    data.facebook_psid || '',
    lineId,
    'Facebook Messenger',
    'New Lead - Grade ' + (data.grade || 'C'),
    Utilities.formatDate(now, tz, 'yyyy-MM-dd'),
    Utilities.formatDate(now, tz, 'yyyy-MM-dd'),
    callbackStr,
    actionNote,
    data.income || '',
    '',
    '',
    '',
    '',
    '',
    '',
    '',
    '',
    data.debt || '',
    'via WEC Bot'
  ];

  sheet.appendRow(row);
  return leadId;
}

function createCalendarEvent(data, leadId) {
  var now          = new Date();
  var tz           = 'Asia/Bangkok';
  var callbackTime = getCallbackTime(now, data.grade);
  var endTime      = new Date(callbackTime.getTime() + 30 * 60 * 1000);

  var contact = (data.contact || '').trim();
  var grade   = data.grade || 'C';
  var title   = '[WEC] โทรกลับ Grade ' + grade + ' — ' + (contact || data.facebook_psid || 'Unknown');

  var description =
    'Lead ID: ' + leadId + '\n' +
    'Grade: ' + grade + '\n' +
    'Facebook PSID: ' + (data.facebook_psid || '-') + '\n' +
    'ติดต่อ: ' + (contact || '-') + '\n\n' +
    'วัตถุประสงค์ (Q1): ' + (data.objective || '-') + '\n' +
    'รายได้/อาชีพ (Q2): ' + (data.income || '-') + '\n' +
    'บูโร/หนี้ (Q3): ' + (data.debt || '-') + '\n\n' +
    'แหล่งที่มา: Facebook Messenger Bot';

  var calendar = CalendarApp.getCalendarById(CALENDAR_ID) || CalendarApp.getDefaultCalendar();
  var event    = calendar.createEvent(title, callbackTime, endTime, { description: description });

  event.addPopupReminder(10); // แจ้งเตือน 10 นาทีก่อน
  return event.getId();
}

function getCallbackTime(now, grade) {
  var t = new Date(now.getTime());
  if (grade === 'A') {
    t.setMinutes(t.getMinutes() + 30);
  } else if (grade === 'B') {
    t.setHours(t.getHours() + 1);
  } else {
    t.setDate(t.getDate() + 1);
    t.setHours(9, 0, 0, 0);
  }
  return t;
}

function buildActionNote(data) {
  var parts = [];
  if (data.objective) parts.push('วัตถุประสงค์: ' + data.objective);
  var g = data.grade || 'C';
  if (g === 'A')      parts.push('โทรกลับภายใน 30 นาที');
  else if (g === 'B') parts.push('โทรกลับภายใน 1-2 ชั่วโมง');
  else                parts.push('ติดต่อกลับเร็วๆ นี้');
  return parts.join(' | ');
}
