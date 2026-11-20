// Date boundaries must follow Berlin even when the browser uses another time zone.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const helpers = source.slice(source.indexOf('function isoDate('), source.indexOf('function planDayLabel('));
const context = vm.createContext({ Intl, Date });
vm.runInContext(helpers, context);
assert.equal(context.daysBetween('2026-03-28', '2026-03-30'), 2);
assert.equal(context.daysBetween('2026-10-24', '2026-10-26'), 2);
assert.equal(context.daysBetween('2026-11-06', '2026-11-05'), -1);
assert.equal(context.berlinDate(new Date('2026-11-05T23:30:00Z')), '2026-11-06');
assert.equal(context.berlinDate(new Date('2026-07-05T22:30:00Z')), '2026-07-06');
assert.equal(context.berlinDate(new Date('2026-10-25T00:30:00Z')), '2026-10-25');
assert.equal(context.berlinDate(new Date('2026-10-25T01:30:00Z')), '2026-10-25');
console.log('Plan date checks passed (midnight, summer time, winter time, overdue).');
