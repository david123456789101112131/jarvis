/* Task manager UI: renders the list, wires the forms, filters, shortcuts and storage. */
(function () {
  'use strict';
  var Tasks = window.Tasks;
  var ui = window.APP_UI || {};
  var cfg = window.APP_CONFIG || {};
  var KEY = (cfg.storageKey || 'tasks') + '-tasks';
  var THEME_KEY = (cfg.storageKey || 'tasks') + '-theme';
  var state = { tasks: [], status: 'all', priority: 'all', search: '', sort: 'smart', editing: null };

  function $(id) { return document.getElementById(id); }
  function t(key, vars) {
    var s = ui[key] || key;
    Object.keys(vars || {}).forEach(function (k) { s = s.replace('{' + k + '}', vars[k]); });
    return s;
  }
  function storage() { try { return window.localStorage; } catch (err) { return null; } }
  function load() {
    try {
      var raw = storage() && storage().getItem(KEY);
      state.tasks = raw ? Tasks.deserialize(raw) : [];
    } catch (err) {
      state.tasks = [];
    }
  }
  function save() {
    try { if (storage()) storage().setItem(KEY, Tasks.serialize(state.tasks)); } catch (err) { /* full or disabled */ }
  }
  function commit(list) {
    state.tasks = list;
    save();
    render();
  }
  var toastTimer;
  function toast(message) {
    var box = $('toast');
    box.textContent = message;
    box.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { box.classList.remove('show'); }, 2000);
  }
  function el(tag, attrs, kids) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'text') node.textContent = attrs[k];
      else if (k === 'class') node.className = attrs[k];
      else if (k.indexOf('on') === 0) node.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== undefined && attrs[k] !== null && attrs[k] !== false) node.setAttribute(k, attrs[k] === true ? '' : attrs[k]);
    });
    (kids || []).forEach(function (kid) { if (kid) node.appendChild(kid); });
    return node;
  }
  function formatDate(iso) {
    if (!iso) return t('no_due');
    if (iso === Tasks.today()) return t('today');
    try {
      return new Date(iso + 'T00:00:00').toLocaleDateString(cfg.locale === 'he' ? 'he-IL' : 'en-US', { day: 'numeric', month: 'short' });
    } catch (err) {
      return iso;
    }
  }
  function prioritySelect(value) {
    var select = el('select', { name: 'priority', 'aria-label': t('priority') });
    Tasks.PRIORITIES.forEach(function (p) {
      select.appendChild(el('option', { value: p, text: t(p), selected: p === value }));
    });
    return select;
  }

  function taskItem(task) {
    var item = el('li', { class: 'task ' + task.priority + (task.done ? ' done' : '') });
    if (state.editing === task.id) {
      var title = el('input', { name: 'title', value: task.title, maxlength: '200', 'aria-label': t('edit'), required: true });
      var form = el('form', { onsubmit: function (event) {
        event.preventDefault();
        var data = new FormData(form);
        state.editing = null;
        commit(Tasks.update(state.tasks, task.id, { title: data.get('title'), due: data.get('due'), priority: data.get('priority') }));
      } }, [
        title,
        el('input', { name: 'due', type: 'date', value: task.due, 'aria-label': t('due') }),
        prioritySelect(task.priority),
        el('button', { class: 'btn btn-primary', type: 'submit', text: t('save') }),
        el('button', { class: 'btn btn-ghost', type: 'button', text: t('cancel'), onclick: function () { state.editing = null; render(); } })
      ]);
      item.appendChild(form);
      setTimeout(function () { title.focus(); title.select(); }, 0);
      return item;
    }
    var meta = el('div', { class: 'meta' }, [
      el('span', { class: Tasks.isOverdue(task) ? 'late' : '', text: '📅 ' + formatDate(task.due) }),
      el('span', { text: '● ' + t(task.priority) })
    ]);
    item.appendChild(el('input', { type: 'checkbox', checked: task.done, 'aria-label': task.title,
      onchange: function () { commit(Tasks.toggle(state.tasks, task.id)); } }));
    item.appendChild(el('div', {}, [el('div', { class: 'title', text: task.title }), meta]));
    item.appendChild(el('div', { class: 'actions' }, [
      el('button', { type: 'button', text: t('edit'), onclick: function () { state.editing = task.id; render(); } }),
      el('button', { type: 'button', text: t('delete'), onclick: function () {
        if (window.confirm(t('confirm_delete'))) commit(Tasks.remove(state.tasks, task.id));
      } })
    ]));
    return item;
  }

  function render() {
    var visible = Tasks.sort(Tasks.filter(state.tasks, state), state.sort);
    var list = $('list');
    list.textContent = '';
    visible.forEach(function (task) { list.appendChild(taskItem(task)); });
    $('empty').hidden = visible.length > 0;
    var s = Tasks.stats(state.tasks);
    $('stat-total').textContent = s.total;
    $('stat-done').textContent = s.done;
    $('stat-overdue').textContent = s.overdue;
    $('progress-value').textContent = s.progress + '%';
    $('progress-ring').style.setProperty('--p', s.progress);
    document.querySelectorAll('#status-tabs [data-status]').forEach(function (tab) {
      tab.setAttribute('aria-selected', String(tab.getAttribute('data-status') === state.status));
    });
  }

  function init() {
    var theme = null;
    try { theme = storage() && storage().getItem(THEME_KEY); } catch (err) { theme = null; }
    if (theme) document.documentElement.setAttribute('data-theme', theme);
    $('today-label').textContent = new Date().toLocaleDateString(cfg.locale === 'he' ? 'he-IL' : 'en-US',
      { weekday: 'long', day: 'numeric', month: 'long' });
    load();
    $('composer').addEventListener('submit', function (event) {
      event.preventDefault();
      var data = new FormData(event.target);
      try {
        commit(Tasks.create(state.tasks, { title: data.get('title'), due: data.get('due'), priority: data.get('priority') }));
        event.target.reset();
        $('new-priority').value = 'medium';
        $('new-title').focus();
      } catch (err) {
        $('new-title').focus();
      }
    });
    $('status-tabs').addEventListener('click', function (event) {
      var status = event.target.getAttribute && event.target.getAttribute('data-status');
      if (status) { state.status = status; render(); }
    });
    $('search').addEventListener('input', function (event) { state.search = event.target.value; render(); });
    $('priority-filter').addEventListener('change', function (event) { state.priority = event.target.value; render(); });
    $('sort').addEventListener('change', function (event) { state.sort = event.target.value; render(); });
    $('clear-done').addEventListener('click', function () { commit(Tasks.clearDone(state.tasks)); });
    $('theme-toggle').addEventListener('click', function () {
      var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try { if (storage()) storage().setItem(THEME_KEY, next); } catch (err) { /* ignore */ }
    });
    $('export').addEventListener('click', function () {
      var blob = new Blob([Tasks.serialize(state.tasks)], { type: 'application/json' });
      var link = el('a', { href: URL.createObjectURL(blob), download: 'tasks.json' });
      document.body.appendChild(link);
      link.click();
      link.remove();
    });
    $('import').addEventListener('change', function (event) {
      var file = event.target.files && event.target.files[0];
      if (!file) return;
      file.text().then(function (text) {
        var imported = Tasks.deserialize(text);
        var ids = {};
        state.tasks.forEach(function (task) { ids[task.id] = true; });
        commit(state.tasks.concat(imported.filter(function (task) { return !ids[task.id]; })));
        toast(t('imported', { n: imported.length }));
      }).catch(function () { toast(t('bad_file')); });
      event.target.value = '';
    });
    document.addEventListener('keydown', function (event) {
      var typing = /INPUT|TEXTAREA|SELECT/.test(event.target.tagName);
      if (event.key === 'Escape') {
        if (state.editing) { state.editing = null; render(); }
        else if (typing) event.target.blur();
        return;
      }
      if (typing || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === 'n' || event.key === 'N' || event.key === 'מ') { event.preventDefault(); $('new-title').focus(); }
      if (event.key === '/' || event.key === '.') { event.preventDefault(); $('search').focus(); }
    });
    render();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
