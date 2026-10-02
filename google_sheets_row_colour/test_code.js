// Runs Code.gs against a mock Google Sheet:  node test_code.js
const fs = require('fs'), vm = require('vm'), assert = require('assert');

function makeSheet(data) {
  const bg = data.map(r => r.map(() => null));
  const sheet = {
    bg, data,
    getLastRow: () => data.length, getLastColumn: () => data[0].length,
    getRange(r, c, nr = 1, nc = 1) {
      return {
        getDisplayValues: () => data.slice(r - 1, r - 1 + nr).map(x => x.slice(c - 1, c - 1 + nc).map(String)),
        getDisplayValue: () => String(data[r - 1][c - 1]),
        setBackground(col) { for (let i = 0; i < nr; i++) for (let j = 0; j < nc; j++) bg[r - 1 + i][c - 1 + j] = col; },
        setBackgrounds(g) { g.forEach((row, i) => row.forEach((v, j) => bg[r - 1 + i][c - 1 + j] = v)); },
      };
    },
  };
  return sheet;
}

function load(sheet, props) {
  const ctx = {
    SpreadsheetApp: { getActiveSpreadsheet: () => ({ getSheets: () => [sheet], getSheetByName: () => sheet }), openById() {}, getUi() { throw 0; } },
    PropertiesService: { getScriptProperties: () => ({ getProperty: k => props[k] }) },
    LockService: { getScriptLock: () => ({ waitLock() {}, releaseLock() {} }) },
    ContentService: { MimeType: { JSON: 'json' }, createTextOutput: s => ({ text: s, setMimeType() { return this; } }) },
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(__dirname + '/Code.gs', 'utf8'), ctx);
  return { post: body => JSON.parse(ctx.doPost({ postData: { contents: JSON.stringify(body) } }).text), ctx };
}

const G = '#D9EAD3', R = '#F4CCCC', Y = '#FFF2CC';
const sheet = makeSheet([
  ['Lead ID', 'Date', 'Name', 'Contact No.', 'Status'],
  ['KM-1001', '01/10/2026', 'Rahul', '9870000001', 'New'],
  ['KM-1002', '01/10/2026', 'Amit', '9870000002', 'New'],
  ['KM-100',  '01/10/2026', 'Short', '9870000003', 'New'],
]);
const { post, ctx } = load(sheet, { WEBHOOK_SECRET: 's3cret' });
const call = (id, status) => post({ secret: 's3cret', lead_reference_id: id, status });
const rowBg = i => sheet.bg[i];
const allEq = (arr, v) => arr.every(x => x === v);

let r = call('KM-1001', 'Approved');
assert(r.ok && r.rows_coloured === 1); assert(allEq(rowBg(1), G), 'approved -> green whole row');
assert(allEq(rowBg(0), null), 'header untouched'); assert(allEq(rowBg(2), null) && allEq(rowBg(3), null), 'other rows untouched (KM-100 != KM-1001)');

call('KM-1001', 'Processing'); assert(allEq(rowBg(1), Y), 'green -> yellow, same row');
call('KM-1001', 'Rejected');   assert(allEq(rowBg(1), R), 'yellow -> red, same row');
call('KM-1001', 'Rejected');   call('KM-1001', 'Rejected'); assert(allEq(rowBg(1), R), 'repeat stable');
assert.strictEqual(sheet.data.length, 4, 'no rows added');
call('KM-1001', 'Documents Pending'); assert(allEq(rowBg(1), null), 'unknown -> neutral');
call('km-1001', '  approved  ');       assert(allEq(rowBg(1), G), 'case/space tolerant');
r = call('KM-1002', undefined);        assert(r.ok && allEq(rowBg(2), null), 'no status sent -> reads Sheet cell (New) -> neutral');
sheet.data[2][4] = 'Approved'; call('KM-1002');  assert(allEq(rowBg(2), G), 'falls back to Sheet Status cell');

r = call('KM-9999', 'Approved'); assert(!r.ok && r.code === 'NOT_FOUND');
assert(!post({ secret: 'bad', lead_reference_id: 'KM-1001', status: 'Approved' }).ok, 'wrong secret rejected');
assert(!post({ lead_reference_id: 'KM-1001', status: 'Approved' }).ok, 'missing secret rejected');
assert(!load(sheet, {}).post({ secret: '', lead_reference_id: 'KM-1001' }).ok, 'no WEBHOOK_SECRET configured -> fail closed');

// row moves (someone inserts/sorts rows): still found by Lead ID, colour follows
sheet.data.splice(1, 0, ['KM-2000', 'x', 'Inserted', '1', 'New']); sheet.bg.splice(1, 0, [null, null, null, null, null]);
r = call('KM-1002', 'Rejected'); assert(r.ok && allEq(sheet.bg[3], R), 'KM-1002 found after row shift, coloured red');
assert(allEq(sheet.bg[2], G) && allEq(sheet.bg[1], null), 'KM-1001 keeps green, inserted row untouched');

// recolourAllRows
sheet.data[1][4] = 'Processing'; sheet.data[3][4] = 'Approved';
assert.strictEqual(ctx.recolourAllRows(), 4);
assert(allEq(sheet.bg[0], null) && allEq(sheet.bg[1], Y) && allEq(sheet.bg[3], G), 'recolourAll works, header skipped');
console.log('ALL TESTS PASSED');
