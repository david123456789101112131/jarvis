'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Cart = require('../js/cart.js');
const Checkout = require('../js/checkout.js');
const { products, config } = require('../data/products.js');

const good = { name: 'Dana Levi', email: 'dana@example.com', phone: '050-123-4567', address: '1 Main St', city: 'Haifa' };

test('valid form has no errors', () => {
  assert.deepEqual(Checkout.validate(good), {});
});

test('reports each invalid field', () => {
  const errors = Checkout.validate({ name: 'D', email: 'nope', phone: '12', address: '', city: '' });
  assert.equal(errors.name, 'short');
  assert.equal(errors.email, 'email');
  assert.equal(errors.phone, 'phone');
  assert.equal(errors.address, 'required');
  assert.equal(errors.city, 'required');
  assert.equal(Checkout.validate(undefined).name, 'required');
});

test('creates an order with lines and totals', () => {
  const cart = Cart.addItem(Cart.empty(), products[0].id, 2);
  const order = Checkout.createOrder(cart, products, config, good, Date.UTC(2026, 0, 15), () => 0.5);
  assert.match(order.id, /^ORD-2601\d\d-[0-9A-F]{4}$/);
  assert.equal(order.lines.length, 1);
  assert.equal(order.lines[0].qty, 2);
  assert.equal(order.totals.subtotal, Cart.round2(products[0].price * 2));
  assert.equal(order.paid, false);
});

test('refuses an empty cart or an invalid form', () => {
  assert.throws(() => Checkout.createOrder(Cart.empty(), products, config, good), /empty cart/);
  const cart = Cart.addItem(Cart.empty(), products[0].id);
  assert.throws(() => Checkout.createOrder(cart, products, config, { ...good, email: 'x' }), /invalid/);
});

test('orders are appended to storage', () => {
  const store = new Map();
  const storage = { getItem: (k) => store.get(k) || null, setItem: (k, v) => store.set(k, v) };
  Checkout.saveOrder(storage, 'orders', { id: 1 });
  const all = Checkout.saveOrder(storage, 'orders', { id: 2 });
  assert.equal(all.length, 2);
  assert.equal(JSON.parse(store.get('orders')).length, 2);
});
