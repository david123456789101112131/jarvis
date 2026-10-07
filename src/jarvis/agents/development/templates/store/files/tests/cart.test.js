'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Cart = require('../js/cart.js');
const { products, config } = require('../data/products.js');

const [first, second] = products;

test('adds items and merges quantities', () => {
  let cart = Cart.empty();
  cart = Cart.addItem(cart, first.id);
  cart = Cart.addItem(cart, first.id, 2);
  cart = Cart.addItem(cart, second.id);
  assert.equal(Cart.count(cart), 4);
  assert.deepEqual(cart.items.map((i) => i.id), [first.id, second.id]);
});

test('respects stock limits and ignores bad quantities', () => {
  let cart = Cart.addItem(Cart.empty(), first.id, 50, 3);
  assert.equal(Cart.count(cart), 3);
  cart = Cart.addItem(cart, first.id, -2);
  assert.equal(Cart.count(cart), 3);
  cart = Cart.addItem(cart, second.id, 1, 0);
  assert.equal(cart.items.length, 1);
});

test('setQty updates and removes at zero', () => {
  let cart = Cart.addItem(Cart.empty(), first.id, 2);
  cart = Cart.setQty(cart, first.id, 5);
  assert.equal(Cart.count(cart), 5);
  cart = Cart.setQty(cart, first.id, 0);
  assert.equal(Cart.count(cart), 0);
});

test('totals: subtotal, VAT, shipping and free-shipping threshold', () => {
  const cfg = { vatRate: 0.18, shippingFee: 30, freeShippingOver: 300, pricesIncludeVat: true };
  const items = [{ id: 'a', price: 100 }, { id: 'b', price: 50 }];
  let cart = Cart.addItem(Cart.empty(), 'a', 1);
  let t = Cart.totals(cart, items, cfg);
  assert.equal(t.subtotal, 100);
  assert.equal(t.shipping, 30);
  assert.equal(t.total, 130);
  assert.equal(t.vat, 15.25);
  assert.equal(t.freeShippingRemaining, 200);
  cart = Cart.addItem(cart, 'a', 2);
  t = Cart.totals(cart, items, cfg);
  assert.equal(t.subtotal, 300);
  assert.equal(t.shipping, 0);
  assert.equal(t.total, 300);
  const net = Cart.totals(cart, items, { vatRate: 0.1, shippingFee: 5, freeShippingOver: 0, pricesIncludeVat: false });
  assert.equal(net.vat, 30);
  assert.equal(net.total, 335);
  assert.equal(Cart.totals(Cart.empty(), items, cfg).total, 0);
});

test('lines skip products that no longer exist', () => {
  let cart = Cart.addItem(Cart.empty(), 'gone');
  cart = Cart.addItem(cart, first.id);
  const lines = Cart.lines(cart, products);
  assert.equal(lines.length, 1);
  assert.equal(lines[0].product.id, first.id);
});

test('load/save round-trip and survive corrupt storage', () => {
  const store = new Map();
  const storage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) };
  Cart.save(storage, 'k', Cart.addItem(Cart.empty(), first.id, 2));
  assert.equal(Cart.count(Cart.load(storage, 'k')), 2);
  store.set('k', '{not json');
  assert.deepEqual(Cart.load(storage, 'k'), Cart.empty());
  assert.deepEqual(Cart.load(null, 'k'), Cart.empty());
});

test('formats money in the store currency', () => {
  const text = Cart.formatMoney(1234.5, config);
  assert.match(text, /1,234\.5|1,234\.50|1234[.,]5/);
});
