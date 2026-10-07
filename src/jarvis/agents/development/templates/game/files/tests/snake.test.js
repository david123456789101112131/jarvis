'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Snake = require('../js/snake.js');

test('a new game is centred, alive and has food off the snake', () => {
  const g = Snake.createGame({ cols: 10, rows: 10, seed: 42 });
  assert.equal(g.snake.length, 3);
  assert.deepEqual(g.snake[0], { x: 5, y: 5 });
  assert.ok(g.alive && !g.paused);
  assert.ok(g.food && !g.snake.some((c) => c.x === g.food.x && c.y === g.food.y));
});

test('the same seed gives the same food', () => {
  assert.deepEqual(Snake.createGame({ seed: 7 }).food, Snake.createGame({ seed: 7 }).food);
});

test('moves one cell per step and cannot reverse into itself', () => {
  let g = Snake.createGame({ cols: 10, rows: 10, seed: 1 });
  g = { ...g, food: { x: 0, y: 0 } };
  g = Snake.step(g);
  assert.deepEqual(g.snake[0], { x: 6, y: 5 });
  g = Snake.turn(g, 'left');
  g = Snake.step(g);
  assert.deepEqual(g.snake[0], { x: 7, y: 5 });
  g = Snake.turn(Snake.turn(g, 'up'), 'left');
  assert.equal(g.nextDir, 'up');
  g = Snake.step(g);
  assert.deepEqual(g.snake[0], { x: 7, y: 4 });
});

test('eating grows the snake, scores and moves the food', () => {
  let g = Snake.createGame({ cols: 10, rows: 10, seed: 3 });
  g = { ...g, food: { x: 6, y: 5 } };
  g = Snake.step(g);
  assert.equal(g.snake.length, 4);
  assert.equal(g.score, Snake.POINTS);
  assert.ok(g.food && !(g.food.x === 6 && g.food.y === 5));
});

test('hitting a wall or itself ends the game', () => {
  let g = Snake.createGame({ cols: 6, rows: 6, seed: 2 });
  g = { ...g, food: { x: 0, y: 5 } };
  for (let i = 0; i < 5 && g.alive; i++) g = Snake.step(g);
  assert.equal(g.alive, false);
  let s = { ...Snake.createGame({ cols: 10, rows: 10, seed: 2 }), food: { x: 0, y: 0 },
    snake: [{ x: 5, y: 5 }, { x: 4, y: 5 }, { x: 4, y: 6 }, { x: 5, y: 6 }, { x: 6, y: 6 }, { x: 6, y: 5 }] };
  s = Snake.step(Snake.turn(s, 'down'));
  assert.equal(s.alive, false);
});

test('levels speed the game up', () => {
  assert.equal(Snake.levelFor(0), 1);
  assert.equal(Snake.levelFor(Snake.LEVEL_EVERY), 2);
  assert.equal(Snake.levelFor(100000), 10);
  const slow = Snake.speed({ level: 1 });
  const fast = Snake.speed({ level: 5 });
  assert.ok(fast < slow && Snake.speed({ level: 99 }) >= 55);
});

test('pause stops movement', () => {
  let g = Snake.togglePause(Snake.createGame({ seed: 5 }));
  const before = g.snake[0];
  g = Snake.step(g);
  assert.deepEqual(g.snake[0], before);
  assert.equal(Snake.togglePause(g).paused, false);
});

test('filling the board wins', () => {
  // 5x5 board with every cell but one taken: eating the last food fills the board.
  const snake = [];
  for (let y = 0; y < 5; y++) for (let x = 0; x < 5; x++) snake.push({ x, y });
  const head = { x: 3, y: 4 };
  const body = snake.filter((c) => !(c.x === 4 && c.y === 4) && !(c.x === 3 && c.y === 4));
  let g = { ...Snake.createGame({ cols: 5, rows: 5, seed: 9 }), snake: [head, ...body], dir: 'right', nextDir: 'right', food: { x: 4, y: 4 } };
  g = Snake.step(g);
  assert.equal(g.won, true);
  assert.equal(g.alive, false);
});
