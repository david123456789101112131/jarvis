'use strict';
/* A small REST API with no dependencies. Start: node server.js (PORT=3000 by default). */
const http = require('node:http');
const path = require('node:path');
const { Store } = require('./lib/store.js');
const { validateItem } = require('./lib/validate.js');

const MAX_BODY = 1024 * 1024;
const ALLOWED_ORIGIN = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/;

function send(res, status, data, headers = {}) {
  const body = data === undefined ? '' : JSON.stringify(data);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', ...headers });
  res.end(body);
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY) {
        reject(Object.assign(new Error('Body too large'), { status: 413 }));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      const text = Buffer.concat(chunks).toString('utf8');
      if (!text) return resolve({});
      try {
        resolve(JSON.parse(text));
      } catch (err) {
        reject(Object.assign(new Error('Invalid JSON'), { status: 400 }));
      }
    });
    req.on('error', reject);
  });
}

function createServer({ dataFile = path.join(__dirname, 'data', 'items.json'), title = {{ title|json }} } = {}) {
  const store = new Store(dataFile);

  async function route(req, res) {
    const url = new URL(req.url, 'http://localhost');
    const parts = url.pathname.replace(/\/+$/, '').split('/').filter(Boolean);
    const origin = req.headers.origin;
    const cors = origin && ALLOWED_ORIGIN.test(origin)
      ? { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type' }
      : {};
    if (req.method === 'OPTIONS') return send(res, 204, undefined, cors);
    if (parts.length === 0) return send(res, 200, { name: title, endpoints: ['/health', '/api/items'] }, cors);
    if (parts[0] === 'health' && parts.length === 1) return send(res, 200, { status: 'ok', items: store.list().length }, cors);
    if (parts[0] !== 'api' || parts[1] !== 'items' || parts.length > 3) return send(res, 404, { error: 'Not found' }, cors);

    if (parts.length === 2) {
      if (req.method === 'GET') {
        const q = (url.searchParams.get('q') || '').toLowerCase();
        const items = store.list().filter((i) => !q || i.name.toLowerCase().includes(q) || (i.description || '').toLowerCase().includes(q));
        return send(res, 200, { items, total: items.length }, cors);
      }
      if (req.method === 'POST') {
        const { value, errors } = validateItem(await readJson(req));
        if (errors) return send(res, 422, { error: 'Invalid item', fields: errors }, cors);
        return send(res, 201, store.create(value), cors);
      }
      return send(res, 405, { error: 'Method not allowed' }, { Allow: 'GET, POST', ...cors });
    }

    const id = Number(parts[2]);
    if (!Number.isInteger(id) || id < 1) return send(res, 400, { error: 'Bad id' }, cors);
    if (req.method === 'GET') {
      const item = store.get(id);
      return item ? send(res, 200, item, cors) : send(res, 404, { error: 'Item not found' }, cors);
    }
    if (req.method === 'PUT' || req.method === 'PATCH') {
      const { value, errors } = validateItem(await readJson(req), { partial: req.method === 'PATCH' });
      if (errors) return send(res, 422, { error: 'Invalid item', fields: errors }, cors);
      const item = store.update(id, value);
      return item ? send(res, 200, item, cors) : send(res, 404, { error: 'Item not found' }, cors);
    }
    if (req.method === 'DELETE') {
      return store.remove(id) ? send(res, 204, undefined, cors) : send(res, 404, { error: 'Item not found' }, cors);
    }
    return send(res, 405, { error: 'Method not allowed' }, { Allow: 'GET, PUT, PATCH, DELETE', ...cors });
  }

  return http.createServer((req, res) => {
    route(req, res).catch((err) => {
      if (!res.headersSent) send(res, err.status || 500, { error: err.status ? err.message : 'Internal error' });
      if (!err.status) console.error(err);
    });
  });
}

if (require.main === module) {
  const port = Number(process.env.PORT) || 3000;
  const host = process.env.HOST || '127.0.0.1';
  createServer().listen(port, host, () => console.log(`API ready on http://${host}:${port}/api/items`));
}

module.exports = { createServer };
