'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createServer } = require('../server.js');
const { validateItem } = require('../lib/validate.js');

async function start(dataFile) {
  const server = createServer({ dataFile });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const call = async (method, url, body) => {
    const res = await fetch(base + url, { method, headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body) });
    const text = await res.text();
    return { status: res.status, body: text ? JSON.parse(text) : null };
  };
  return { server, call };
}

const tmpFile = () => path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'api-')), 'items.json');

test('health and index', async () => {
  const { server, call } = await start(tmpFile());
  try {
    assert.deepEqual((await call('GET', '/health')).body, { status: 'ok', items: 0 });
    assert.ok((await call('GET', '/')).body.endpoints.includes('/api/items'));
    assert.equal((await call('GET', '/nope')).status, 404);
  } finally {
    server.close();
  }
});

test('create, read, update, delete', async () => {
  const { server, call } = await start(tmpFile());
  try {
    const created = await call('POST', '/api/items', { name: ' Lamp ', price: 19.999 });
    assert.equal(created.status, 201);
    assert.equal(created.body.name, 'Lamp');
    assert.equal(created.body.price, 20);
    const id = created.body.id;
    assert.equal((await call('GET', `/api/items/${id}`)).body.name, 'Lamp');
    assert.equal((await call('PATCH', `/api/items/${id}`, { done: true })).body.done, true);
    const replaced = await call('PUT', `/api/items/${id}`, { name: 'Desk lamp' });
    assert.equal(replaced.body.name, 'Desk lamp');
    assert.equal(replaced.body.price, 0);
    assert.equal((await call('GET', '/api/items?q=desk')).body.total, 1);
    assert.equal((await call('DELETE', `/api/items/${id}`)).status, 204);
    assert.equal((await call('GET', `/api/items/${id}`)).status, 404);
  } finally {
    server.close();
  }
});

test('validation and bad input', async () => {
  const { server, call } = await start(tmpFile());
  try {
    const bad = await call('POST', '/api/items', { name: '', price: -3 });
    assert.equal(bad.status, 422);
    assert.deepEqual(Object.keys(bad.body.fields).sort(), ['name', 'price']);
    assert.equal((await call('POST', '/api/items', '{oops')).status, 400);
    assert.equal((await call('GET', '/api/items/abc')).status, 400);
    assert.equal((await call('DELETE', '/api/items')).status, 405);
  } finally {
    server.close();
  }
});

test('data persists across restarts', async () => {
  const file = tmpFile();
  let { server, call } = await start(file);
  await call('POST', '/api/items', { name: 'kept' });
  server.close();
  ({ server, call } = await start(file));
  try {
    assert.equal((await call('GET', '/api/items')).body.items[0].name, 'kept');
  } finally {
    server.close();
  }
});

test('partial validation only checks the fields sent', () => {
  assert.deepEqual(validateItem({ done: true }, { partial: true }), { value: { done: true } });
  assert.ok(validateItem([], {}).errors);
});
