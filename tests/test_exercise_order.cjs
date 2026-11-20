// Artemis exercises: sorted by release date, grouped by release week (Berlin time), the week
// labelled with the lesson most of its titles name; at the same moment T before H.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const context = vm.createContext({ Intl, Date });
vm.runInContext(source.slice(source.indexOf('function isoDate('), source.indexOf('function planDayLabel(')), context);
vm.runInContext(source.slice(source.indexOf('const EXERCISE_KIND_ORDER'), source.indexOf('function exerciseRow(')), context);
// Release dates as seen live on 2026-11-13 (FoP), in the server's due-date order.
const rows = [
  ['L01E01 - Quiz', '2026-10-22T12:00:00Z'], ['L02E01 - Quiz', '2026-10-29T13:30:00Z'],
  ['T02E01 - Pokédex Manager', '2026-10-29T13:00:00Z'], ['T02E02 - Mensa Rush Hour', '2026-10-29T13:00:00Z'],
  ['H01E01 - Course Participant', '2026-10-22T12:50:00Z'], ['H01E02 - Library Management', '2026-10-22T12:35:00Z'],
  ['T01E01 - TUM Mensa Card', '2026-10-22T12:35:00Z'], ['T01E02 - Service Point Tracker', '2026-10-23T07:55:00Z'],
  ['H02E02 - Dungeon', '2026-10-29T13:00:00Z'], ['H01E02 - The Dragons Accountant', '2026-10-29T13:00:00Z'],
  ['Registration - Supervised Exercise Sheet 1', '2026-11-05T12:00:00Z'], ['L04E01-Quiz', '2026-11-12T13:18:52Z'],
  ['T04E01 - Music Festival', '2026-11-12T13:00:00Z'], ['T04E03 - SkyPort Modelling', '2026-11-11T19:00:00Z'],
  ['H04E01 - Maze Bank', '2026-11-12T12:00:00Z'], ['P04E04 - Fairytale', '2026-11-11T23:00:00Z'],
  ['RISC-V Programming Exercise - Tribonacci', null],
];
const exercises = rows.map(([title, release_date], id) => ({ id, title, release_date }));
const groups = context.exerciseGroups(exercises);
const shown = groups.map(g => [context.exerciseGroupTitle(g), g.items.map(e => e.title.split(/ - |-Quiz/)[0])]);
assert.deepEqual(JSON.parse(JSON.stringify(shown)), [
  ['第 1 课 · 10/19–10/25 发布', ['L01E01', 'T01E01', 'H01E02', 'H01E01', 'T01E02']],
  // The mistyped "H01E02" released in week 2 stays in week 2.
  ['第 2 课 · 10/26–11/1 发布', ['T02E01', 'T02E02', 'H02E02', 'H01E02', 'L02E01']],
  ['11/2–11/8 发布', ['Registration']],
  ['第 4 课 · 11/9–11/15 发布', ['T04E03', 'P04E04', 'H04E01', 'T04E01', 'L04E01']],
  ['发布时间不明', ['RISC-V Programming Exercise']],
]);
// With a filter only the Dragons homework is left: its week label still comes from all exercises.
const left = context.exerciseGroups([exercises[9]], exercises);
assert.equal(context.exerciseGroupTitle(left[0]), '第 2 课 · 10/26–11/1 发布');
// Sunday 23:30 UTC is already Monday in Berlin: next week.
assert.equal(context.releaseWeek(new Date('2026-10-25T23:30:00Z')), '2026-10-26');
assert.equal(context.exerciseCode('h03e02 - lower case').kind, 'H');
assert.equal(context.exerciseCode('H123E01 - three digits'), null);
console.log('Exercise order checks passed.');
