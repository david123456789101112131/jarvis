/* Snake game rules: pure functions over a plain state object (no DOM), tested with node --test. */
(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.Snake = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DIRS = { up: { x: 0, y: -1 }, down: { x: 0, y: 1 }, left: { x: -1, y: 0 }, right: { x: 1, y: 0 } };
  var OPPOSITE = { up: 'down', down: 'up', left: 'right', right: 'left' };
  var POINTS = 10;
  var LEVEL_EVERY = 50;
  var MAX_LEVEL = 10;

  // mulberry32: a tiny seeded random generator, so games are reproducible in tests.
  function nextRandom(seed) {
    var t = (seed + 0x6D2B79F5) >>> 0;
    var r = Math.imul(t ^ (t >>> 15), 1 | t);
    r = (r + Math.imul(r ^ (r >>> 7), 61 | r)) ^ r;
    return { seed: t, value: ((r ^ (r >>> 14)) >>> 0) / 4294967296 };
  }

  function key(cell) { return cell.x + ',' + cell.y; }

  function placeFood(state) {
    var taken = {};
    state.snake.forEach(function (cell) { taken[key(cell)] = true; });
    var free = [];
    for (var y = 0; y < state.rows; y++) {
      for (var x = 0; x < state.cols; x++) {
        if (!taken[x + ',' + y]) free.push({ x: x, y: y });
      }
    }
    if (!free.length) return Object.assign({}, state, { food: null });
    var roll = nextRandom(state.seed);
    return Object.assign({}, state, { food: free[Math.floor(roll.value * free.length)], seed: roll.seed });
  }

  function createGame(options) {
    var opts = options || {};
    var cols = Math.max(5, opts.cols || 20);
    var rows = Math.max(5, opts.rows || 20);
    var cx = Math.floor(cols / 2);
    var cy = Math.floor(rows / 2);
    var state = {
      cols: cols, rows: rows,
      snake: [{ x: cx, y: cy }, { x: cx - 1, y: cy }, { x: cx - 2, y: cy }],
      dir: 'right', nextDir: 'right', food: null, score: 0, level: 1, alive: true, won: false, paused: false,
      ticks: 0, seed: (opts.seed === undefined ? Math.floor(Math.random() * 4294967296) : opts.seed) >>> 0
    };
    return placeFood(state);
  }

  function turn(state, dir) {
    if (!DIRS[dir] || !state.alive) return state;
    // Compare with the direction actually moved last, so two quick turns cannot reverse into the neck.
    if (dir === OPPOSITE[state.dir] || dir === state.dir) return Object.assign({}, state, { nextDir: dir === state.dir ? dir : state.nextDir });
    return Object.assign({}, state, { nextDir: dir });
  }

  function levelFor(score) {
    return Math.min(MAX_LEVEL, 1 + Math.floor(score / LEVEL_EVERY));
  }

  function speed(state) {
    return Math.max(55, 150 - (state.level - 1) * 10);
  }

  function step(state) {
    if (!state.alive || state.paused) return state;
    var dir = state.nextDir;
    var d = DIRS[dir];
    var head = { x: state.snake[0].x + d.x, y: state.snake[0].y + d.y };
    var base = Object.assign({}, state, { dir: dir, ticks: state.ticks + 1 });
    if (head.x < 0 || head.y < 0 || head.x >= state.cols || head.y >= state.rows) {
      return Object.assign(base, { alive: false });
    }
    var eating = state.food && head.x === state.food.x && head.y === state.food.y;
    var body = eating ? state.snake : state.snake.slice(0, -1);
    if (body.some(function (cell) { return cell.x === head.x && cell.y === head.y; })) {
      return Object.assign(base, { alive: false });
    }
    var snake = [head].concat(body);
    if (!eating) return Object.assign(base, { snake: snake });
    var score = state.score + POINTS * state.level;
    var next = placeFood(Object.assign(base, { snake: snake, score: score, level: levelFor(score) }));
    if (!next.food) return Object.assign(next, { alive: false, won: true });
    return next;
  }

  function togglePause(state) {
    if (!state.alive) return state;
    return Object.assign({}, state, { paused: !state.paused });
  }

  return {
    DIRS: DIRS, POINTS: POINTS, LEVEL_EVERY: LEVEL_EVERY, createGame: createGame, turn: turn, step: step,
    togglePause: togglePause, speed: speed, levelFor: levelFor, placeFood: placeFood, nextRandom: nextRandom
  };
});
