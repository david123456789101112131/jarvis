/* Task logic: pure functions over plain objects (no DOM), tested with node --test. */
(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.Tasks = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var PRIORITIES = ['high', 'medium', 'low'];
  var RANK = { high: 0, medium: 1, low: 2 };
  var DATE = /^\d{4}-\d{2}-\d{2}$/;

  function today(now) {
    var d = now ? new Date(now) : new Date();
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  function newId(random) {
    return 't' + Date.now().toString(36) + Math.floor((random || Math.random)() * 1e9).toString(36);
  }

  function clean(task) {
    var title = String((task && task.title) || '').replace(/\s+/g, ' ').trim().slice(0, 200);
    if (!title) return null;
    return {
      id: String(task.id || newId()),
      title: title,
      notes: String(task.notes || '').slice(0, 2000),
      priority: PRIORITIES.indexOf(task.priority) === -1 ? 'medium' : task.priority,
      due: DATE.test(task.due || '') ? task.due : '',
      done: Boolean(task.done),
      createdAt: Number(task.createdAt) || Date.now(),
      doneAt: task.done ? Number(task.doneAt) || Date.now() : null
    };
  }

  function create(list, fields, now) {
    var task = clean(Object.assign({ createdAt: now || Date.now() }, fields, { id: newId() }));
    if (!task) throw new Error('A task needs a title');
    return [task].concat(list || []);
  }

  function update(list, id, changes, now) {
    return (list || []).map(function (task) {
      if (task.id !== id) return task;
      var next = clean(Object.assign({}, task, changes, { id: task.id, createdAt: task.createdAt }));
      if (!next) return task;
      if (next.done && !task.done) next.doneAt = now || Date.now();
      if (!next.done) next.doneAt = null;
      return next;
    });
  }

  function toggle(list, id, now) {
    var task = (list || []).find(function (t) { return t.id === id; });
    return task ? update(list, id, { done: !task.done }, now) : list;
  }

  function remove(list, id) {
    return (list || []).filter(function (task) { return task.id !== id; });
  }

  function clearDone(list) {
    return (list || []).filter(function (task) { return !task.done; });
  }

  function isOverdue(task, now) {
    return !task.done && Boolean(task.due) && task.due < today(now);
  }

  function fold(text) {
    return String(text || '').toLowerCase().normalize('NFKD').replace(/[֑-ׇ̀-ͯ]/g, '');
  }

  function filter(list, options, now) {
    var opts = options || {};
    var words = fold(opts.search).split(/\s+/).filter(Boolean);
    return (list || []).filter(function (task) {
      if (opts.status === 'active' && task.done) return false;
      if (opts.status === 'done' && !task.done) return false;
      if (opts.status === 'overdue' && !isOverdue(task, now)) return false;
      if (opts.status === 'today' && (task.done || task.due !== today(now))) return false;
      if (opts.priority && opts.priority !== 'all' && task.priority !== opts.priority) return false;
      var hay = fold(task.title + ' ' + task.notes);
      return words.every(function (w) { return hay.indexOf(w) !== -1; });
    });
  }

  function sort(list, by) {
    var items = (list || []).slice();
    var due = function (t) { return t.due || '9999-99-99'; };
    var compare = {
      due: function (a, b) { return due(a) < due(b) ? -1 : due(a) > due(b) ? 1 : 0; },
      priority: function (a, b) { return RANK[a.priority] - RANK[b.priority]; },
      created: function (a, b) { return b.createdAt - a.createdAt; },
      smart: function (a, b) {
        return (a.done - b.done) || compare.due(a, b) || compare.priority(a, b) || compare.created(a, b);
      }
    };
    return items.sort(compare[by] || compare.smart);
  }

  function stats(list, now) {
    var all = list || [];
    var done = all.filter(function (t) { return t.done; }).length;
    return {
      total: all.length,
      done: done,
      active: all.length - done,
      overdue: all.filter(function (t) { return isOverdue(t, now); }).length,
      progress: all.length ? Math.round((done / all.length) * 100) : 0
    };
  }

  function serialize(list) {
    return JSON.stringify({ version: 1, tasks: list || [] });
  }

  function deserialize(text) {
    var data = JSON.parse(text);
    var raw = Array.isArray(data) ? data : data && Array.isArray(data.tasks) ? data.tasks : null;
    if (!raw) throw new Error('Not a task list');
    var seen = {};
    return raw.map(clean).filter(function (task) {
      if (!task || seen[task.id]) return false;
      seen[task.id] = true;
      return true;
    });
  }

  return {
    PRIORITIES: PRIORITIES, today: today, clean: clean, create: create, update: update, toggle: toggle,
    remove: remove, clearDone: clearDone, isOverdue: isOverdue, filter: filter, sort: sort, stats: stats,
    serialize: serialize, deserialize: deserialize
  };
});
