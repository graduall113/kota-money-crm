/**
 * Kota Money CRM - Google Sheet row colouring (Apps Script web app)
 *
 * n8n calls this AFTER its Google Sheets "Append or Update Row" node.
 * The row is found by the permanent Lead ID (e.g. KM-1001) - never by row number -
 * and the WHOLE data row is coloured from the Status:
 *
 *   Approved   -> light green
 *   Rejected   -> light red
 *   Processing -> light yellow
 *   anything else (New, Documents Pending, blank, unknown ...) -> default / no fill
 *
 * The header row is never touched. Safe to call repeatedly (idempotent).
 *
 * Setup: see docs/n8n_sheet_row_colour_setup.md
 *   Script property required : WEBHOOK_SECRET   (same value n8n sends as "secret")
 *   Script property optional : SPREADSHEET_ID   (only if this script is NOT bound to the Sheet)
 */

var CONFIG = {
  SHEET_NAME: '',               // '' = first tab. Otherwise the exact tab name n8n writes to.
  HEADER_ROW: 1,                // data starts on the row after this one
  LEAD_ID_HEADER: 'Lead ID',    // header text of the Lead ID column (found by name, not position)
  STATUS_HEADER: 'Status',      // header text of the Status column (only used if n8n sends no status)
  COLOURS: {                    // keys are lower-case, single-spaced status text
    'approved':   '#D9EAD3',    // light green
    'rejected':   '#F4CCCC',    // light red
    'processing': '#FFF2CC'     // light yellow
  }
  // Text colour is never changed; these pastel fills keep black text readable.
};

/* ------------------------------------------------------------------ web app */

function doPost(e) {
  var lock = LockService.getScriptLock();
  try {
    var body = parseBody_(e);
    checkSecret_(body.secret);
    lock.waitLock(30000);                       // one colouring job at a time
    return json_(colourLeadRow_(body.lead_reference_id, body.status));
  } catch (err) {
    return json_({ ok: false, error: String((err && err.message) || err) });
  } finally {
    try { lock.releaseLock(); } catch (ignore) {}
  }
}

/** Opening the /exec URL in a browser just proves the deployment is alive. */
function doGet() {
  return json_({ ok: true, service: 'Kota Money row colouring', use: 'POST' });
}

/* -------------------------------------------------------------- core logic */

function colourLeadRow_(leadId, status) {
  leadId = normaliseId_(leadId);
  if (!leadId) throw new Error('lead_reference_id is missing.');

  var sheet = getSheet_();
  var lastCol = sheet.getLastColumn();
  var lastRow = sheet.getLastRow();
  var headers = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, lastCol).getDisplayValues()[0];
  var idCol = findHeader_(headers, CONFIG.LEAD_ID_HEADER);
  if (!idCol) throw new Error('Header "' + CONFIG.LEAD_ID_HEADER + '" not found in row ' + CONFIG.HEADER_ROW + '.');

  var firstData = CONFIG.HEADER_ROW + 1;
  var rows = [];
  if (lastRow >= firstData) {
    var ids = sheet.getRange(firstData, idCol, lastRow - CONFIG.HEADER_ROW, 1).getDisplayValues();
    for (var i = 0; i < ids.length; i++) {
      if (normaliseId_(ids[i][0]) === leadId) rows.push(firstData + i);   // exact match: KM-100 never matches KM-1001
    }
  }
  if (!rows.length) {
    return { ok: false, code: 'NOT_FOUND', error: 'Lead ID ' + leadId + ' is not in the Sheet (yet).' };
  }

  if (status === undefined || status === null || status === '') {      // fall back to the Sheet's own Status cell
    var statusCol = findHeader_(headers, CONFIG.STATUS_HEADER);
    status = statusCol ? sheet.getRange(rows[0], statusCol).getDisplayValue() : '';
  }
  var colour = colourFor_(status);

  rows.forEach(function (r) {
    sheet.getRange(r, 1, 1, lastCol).setBackground(colour);            // null = default (no fill)
  });

  return { ok: true, lead_id: leadId, status: String(status || ''), colour: colour || 'none',
           rows_coloured: rows.length, duplicate_lead_id: rows.length > 1 };
}

/** Status text -> hex colour, or null (= reset to default) for anything unknown. */
function colourFor_(status) {
  var key = String(status === undefined || status === null ? '' : status).toLowerCase().replace(/\s+/g, ' ').trim();
  return Object.prototype.hasOwnProperty.call(CONFIG.COLOURS, key) ? CONFIG.COLOURS[key] : null;
}

/* ---------------------------------------------- one-off / maintenance tools */

/** Recolour EVERY data row from its Status cell (existing rows, or after editing CONFIG.COLOURS). */
function recolourAllRows() {
  var sheet = getSheet_();
  var lastCol = sheet.getLastColumn();
  var lastRow = sheet.getLastRow();
  var firstData = CONFIG.HEADER_ROW + 1;
  if (lastRow < firstData) return 0;
  var headers = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, lastCol).getDisplayValues()[0];
  var statusCol = findHeader_(headers, CONFIG.STATUS_HEADER);
  if (!statusCol) throw new Error('Header "' + CONFIG.STATUS_HEADER + '" not found.');
  var statuses = sheet.getRange(firstData, statusCol, lastRow - CONFIG.HEADER_ROW, 1).getDisplayValues();
  var backgrounds = statuses.map(function (s) {
    var c = colourFor_(s[0]), row = [];
    for (var i = 0; i < lastCol; i++) row.push(c);
    return row;
  });
  sheet.getRange(firstData, 1, backgrounds.length, lastCol).setBackgrounds(backgrounds);
  return backgrounds.length;
}

function onOpen() {
  try {
    SpreadsheetApp.getUi().createMenu('Kota Money')
      .addItem('Recolour all rows by Status', 'recolourAllRows').addToUi();
  } catch (ignore) {}   // standalone script: no UI
}

/* ----------------------------------------------------------------- helpers */

function getSpreadsheet_() {
  var id = PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID');
  var ss = id ? SpreadsheetApp.openById(id) : SpreadsheetApp.getActiveSpreadsheet();
  if (!ss) throw new Error('No spreadsheet: bind this script to the Sheet or set SPREADSHEET_ID.');
  return ss;
}

function getSheet_() {
  var ss = getSpreadsheet_();
  var sheet = CONFIG.SHEET_NAME ? ss.getSheetByName(CONFIG.SHEET_NAME) : ss.getSheets()[0];
  if (!sheet) throw new Error('Sheet tab "' + CONFIG.SHEET_NAME + '" not found.');
  return sheet;
}

function findHeader_(headers, name) {
  var want = String(name).toLowerCase().replace(/\s+/g, ' ').trim();
  for (var i = 0; i < headers.length; i++) {
    if (String(headers[i]).toLowerCase().replace(/\s+/g, ' ').trim() === want) return i + 1;
  }
  return 0;
}

function normaliseId_(v) { return String(v === undefined || v === null ? '' : v).trim().toUpperCase(); }

function parseBody_(e) {
  if (!e || !e.postData || !e.postData.contents) throw new Error('Empty request body.');
  try { return JSON.parse(e.postData.contents); } catch (x) { throw new Error('Body is not valid JSON.'); }
}

function checkSecret_(given) {
  var expected = PropertiesService.getScriptProperties().getProperty('WEBHOOK_SECRET');
  if (!expected) throw new Error('WEBHOOK_SECRET is not set in Script properties.');   // fail closed
  if (String(given || '') !== expected) throw new Error('Unauthorized.');
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
