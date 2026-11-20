// Preferred tasks stay within the existing work; tentative dates never get a countdown.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const context = vm.createContext({ Intl, Date });
vm.runInContext(source.slice(source.indexOf('function isoDate('), source.indexOf('function planDayLabel(')), context);
vm.runInContext(source.slice(source.indexOf('function planPriority('), source.indexOf('function planPage(')), context);
const tasks = [
  { id: 'java', section: 'course', kind: 'exercise' },
  { id: 'homework', section: 'course', kind: 'deadline' },
  { id: 'preview', section: 'course', kind: 'lecture' },
  { id: 'tomorrow', section: 'course', kind: 'exercise' },
  { id: 'research', section: 'research', kind: 'deadline' },
];
const ids = rows => Array.from(rows, row => row.id);
assert.deepEqual(ids(context.planPriority(tasks)), ['homework', 'java', 'preview']);
assert.deepEqual(ids(context.planPriority(tasks, ['tomorrow', 'homework', 'java'])), ['tomorrow', 'homework', 'java']);
assert.deepEqual(ids(context.planPriority(tasks, ['tomorrow', 'homework', 'java'], t => t.id === 'tomorrow')), ['homework', 'java', 'preview']);
assert.equal(context.upcomingTiming({ date_text: '11月底至12月初' }, '2026-11-09').days, null);
assert.equal(context.upcomingTiming({ date_text: '11月底至12月初' }, '2026-11-09').label, '11月底至12月初');
assert.equal(context.upcomingTiming({ date: '2026-11-10', time: '12:00' }, '2026-11-09').days, 1);
assert.equal(context.upcomingTiming({ date: '2026-11-08' }, '2026-11-09').days, -1);
console.log('Plan priority and tentative date checks passed.');
