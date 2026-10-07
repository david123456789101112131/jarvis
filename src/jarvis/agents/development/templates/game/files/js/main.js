/* Snake: drawing, game loop and controls (keyboard, swipes, on-screen pad). */
(function () {
  'use strict';
  var Snake = window.Snake;
  var ui = window.GAME_UI || {};
  var colors = window.GAME_COLORS || {};
  var BEST_KEY = (window.GAME_KEY || 'snake') + '-best';
  var canvas = document.getElementById('canvas');
  var ctx = canvas.getContext('2d');
  var overlay = document.getElementById('overlay');
  var COLS = 20;
  var ROWS = 20;
  var game = Snake.createGame({ cols: COLS, rows: ROWS });
  var running = false;
  var timer = null;
  var best = 0;

  try { best = Number(window.localStorage.getItem(BEST_KEY)) || 0; } catch (err) { best = 0; }

  function $(id) { return document.getElementById(id); }

  function resize() {
    var size = Math.floor(canvas.getBoundingClientRect().width * (window.devicePixelRatio || 1));
    if (size > 0 && canvas.width !== size) {
      canvas.width = size;
      canvas.height = size;
    }
  }

  function roundRect(x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function draw() {
    resize();
    var cell = canvas.width / COLS;
    ctx.fillStyle = '#0f1522';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = 'rgba(255,255,255,0.035)';
    for (var y = 0; y < ROWS; y++) {
      for (var x = (y % 2); x < COLS; x += 2) ctx.fillRect(x * cell, y * cell, cell, cell);
    }
    if (game.food) {
      var fx = (game.food.x + 0.5) * cell;
      var fy = (game.food.y + 0.5) * cell;
      var glow = ctx.createRadialGradient(fx, fy, 1, fx, fy, cell);
      glow.addColorStop(0, colors.accent || '#f59e0b');
      glow.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = glow;
      ctx.fillRect(fx - cell, fy - cell, cell * 2, cell * 2);
      ctx.fillStyle = colors.accent || '#f59e0b';
      ctx.beginPath();
      ctx.arc(fx, fy, cell * 0.32, 0, Math.PI * 2);
      ctx.fill();
    }
    game.snake.forEach(function (part, i) {
      var t = i / Math.max(1, game.snake.length - 1);
      ctx.globalAlpha = 1 - t * 0.45;
      ctx.fillStyle = i === 0 ? (colors.secondary || '#22d3ee') : (colors.primary || '#6366f1');
      var pad = cell * 0.08;
      roundRect(part.x * cell + pad, part.y * cell + pad, cell - pad * 2, cell - pad * 2, cell * 0.28);
      ctx.fill();
    });
    ctx.globalAlpha = 1;
    var head = game.snake[0];
    var d = Snake.DIRS[game.dir];
    ctx.fillStyle = '#0b0f17';
    [-1, 1].forEach(function (side) {
      var ex = (head.x + 0.5 + d.x * 0.18 + (d.y !== 0 ? side * 0.2 : 0)) * cell;
      var ey = (head.y + 0.5 + d.y * 0.18 + (d.x !== 0 ? side * 0.2 : 0)) * cell;
      ctx.beginPath();
      ctx.arc(ex, ey, cell * 0.08, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  function hud() {
    $('score').textContent = game.score;
    $('level').textContent = game.level;
    $('best').textContent = Math.max(best, game.score);
  }

  function showOverlay(title, text, button) {
    $('overlay-title').textContent = title;
    $('overlay-text').textContent = text;
    $('overlay-btn').textContent = button;
    overlay.hidden = false;
    $('overlay-btn').focus();
  }

  function tick() {
    game = Snake.step(game);
    draw();
    hud();
    if (!game.alive) {
      running = false;
      var record = game.score > best;
      if (record) {
        best = game.score;
        try { window.localStorage.setItem(BEST_KEY, String(best)); } catch (err) { /* ignore */ }
      }
      showOverlay(game.won ? ui.won : ui.game_over,
        (record ? ui.new_best + ' ' : '') + ui.score + ': ' + game.score, ui.again);
      return;
    }
    timer = setTimeout(tick, Snake.speed(game));
  }

  function start() {
    clearTimeout(timer);
    game = Snake.createGame({ cols: COLS, rows: ROWS });
    running = true;
    overlay.hidden = true;
    draw();
    hud();
    timer = setTimeout(tick, Snake.speed(game));
  }

  function pause() {
    if (!running) return;
    game = Snake.togglePause(game);
    if (game.paused) {
      clearTimeout(timer);
      showOverlay(ui.paused, ui.how, ui.resume);
    } else {
      overlay.hidden = true;
      timer = setTimeout(tick, Snake.speed(game));
    }
  }

  function steer(dir) {
    if (running && !game.paused) game = Snake.turn(game, dir);
  }

  var KEYS = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right', KeyW: 'up', KeyS: 'down', KeyA: 'left', KeyD: 'right' };
  document.addEventListener('keydown', function (event) {
    if (KEYS[event.code]) {
      event.preventDefault();
      steer(KEYS[event.code]);
    } else if (event.code === 'Space' || event.code === 'KeyP') {
      event.preventDefault();
      if (running) pause();
      else start();
    } else if (event.code === 'Enter' && !running) {
      event.preventDefault();
      start();
    }
  });

  $('overlay-btn').addEventListener('click', function () {
    if (running && game.paused) pause();
    else start();
  });
  $('pause').addEventListener('click', pause);
  document.querySelectorAll('.pad [data-dir]').forEach(function (btn) {
    btn.addEventListener('click', function () { steer(btn.getAttribute('data-dir')); });
  });

  var touch = null;
  canvas.addEventListener('touchstart', function (event) {
    var t = event.changedTouches[0];
    touch = { x: t.clientX, y: t.clientY };
  }, { passive: true });
  canvas.addEventListener('touchend', function (event) {
    if (!touch) return;
    var t = event.changedTouches[0];
    var dx = t.clientX - touch.x;
    var dy = t.clientY - touch.y;
    if (Math.max(Math.abs(dx), Math.abs(dy)) > 24) {
      steer(Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'right' : 'left') : (dy > 0 ? 'down' : 'up'));
    }
    touch = null;
  }, { passive: true });

  window.addEventListener('resize', draw);
  document.addEventListener('visibilitychange', function () {
    if (document.hidden && running && !game.paused) pause();
  });
  draw();
  hud();
})();
