'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Tasks = require('../js/tasks.js');

const NOW = new Date(2026, 4, 10, 12, 0, 0).getTime();

function sample() {
  let list = [];
  list = Tasks.create(list, { title: 'Write report', priority: 'high', due: '2026-05-09' }, NOW - 3000);
  list = Tasks.create(list, { title: 'Buy milk', priority: 'low', due: '2026-05-10' }, NOW - 2000);
  list = Tasks.create(list, { title: 'Call the bank', priority: 'medium' }, NOW - 1000);
  return list;
}

test('create cleans input and rejects empty titles', () => {
  const list = Tasks.create([], { title: '   Plan   the   week  ', priority: 'urgent', due: 'tomorrow' });
  assert.equal(list[0].title, 'Plan the week');
  assert.equal(list[0].priority, 'medium');
  assert.equal(list[0].due, '');
  assert.throws(() => Tasks.create([], { title: '  ' }), /title/);
});

test('toggle, update and remove', () => {
  let list = sample();
  const id = list[0].id;
  list = Tasks.toggle(list, id, NOW);
  assert.equal(list[0].done, true);
  assert.equal(list[0].doneAt, NOW);
  list = Tasks.update(list, id, { title: 'Call the bank today', done: false });
  assert.equal(list[0].title, 'Call the bank today');
  assert.equal(list[0].doneAt, null);
  list = Tasks.update(list, id, { title: '' });
  assert.equal(list[0].title, 'Call the bank today');
  list = Tasks.remove(list, id);
  assert.equal(list.length, 2);
});

test('filters by status, priority and search', () => {
  let list = sample();
  list = Tasks.toggle(list, list.find((t) => t.title === 'Buy milk').id, NOW);
  assert.equal(Tasks.filter(list, { status: 'done' }, NOW).length, 1);
  assert.equal(Tasks.filter(list, { status: 'active' }, NOW).length, 2);
  assert.deepEqual(Tasks.filter(list, { status: 'overdue' }, NOW).map((t) => t.title), ['Write report']);
  assert.equal(Tasks.filter(list, { priority: 'high' }, NOW).length, 1);
  assert.deepEqual(Tasks.filter(list, { search: 'BANK' }, NOW).map((t) => t.title), ['Call the bank']);
});

test('smart sort puts open, earliest-due, most important first', () => {
  let list = sample();
  list = Tasks.toggle(list, list.find((t) => t.title === 'Write report').id, NOW);
  const order = Tasks.sort(list, 'smart').map((t) => t.title);
  assert.deepEqual(order, ['Buy milk', 'Call the bank', 'Write report']);
  assert.equal(Tasks.sort(list, 'priority')[0].priority, 'high');
  assert.equal(Tasks.sort(list, 'created')[0].title, 'Call the bank');
});

test('stats count progress and overdue tasks', () => {
  let list = sample();
  list = Tasks.toggle(list, list[0].id, NOW);
  assert.deepEqual(Tasks.stats(list, NOW), { total: 3, done: 1, active: 2, overdue: 1, progress: 33 });
  assert.equal(Tasks.stats([], NOW).progress, 0);
});

test('serialize and deserialize round-trip, rejecting junk', () => {
  const list = sample();
  const back = Tasks.deserialize(Tasks.serialize(list));
  assert.deepEqual(back.map((t) => t.title), list.map((t) => t.title));
  assert.throws(() => Tasks.deserialize('{"nope": 1}'), /task list/);
  assert.equal(Tasks.deserialize('[{"title": ""}, {"id": "a", "title": "x"}, {"id": "a", "title": "dup"}]').length, 1);
});
