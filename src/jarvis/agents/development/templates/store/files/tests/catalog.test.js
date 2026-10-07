'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Catalog = require('../js/catalog.js');
const { products } = require('../data/products.js');

test('the generated catalog is valid', () => {
  assert.ok(products.length >= 4);
  const ids = new Set();
  for (const p of products) {
    assert.equal(typeof p.id, 'string');
    assert.ok(!ids.has(p.id), `duplicate id ${p.id}`);
    ids.add(p.id);
    assert.ok(p.name && p.category && p.image);
    assert.ok(Number.isFinite(p.price) && p.price > 0);
  }
});

test('categories are unique and keep catalog order', () => {
  const cats = Catalog.categories(products);
  assert.equal(new Set(cats).size, cats.length);
  assert.equal(cats[0], products[0].category);
});

test('filter by category and search text', () => {
  const cat = products[0].category;
  const inCat = Catalog.filterProducts(products, { category: cat });
  assert.ok(inCat.length >= 1 && inCat.every((p) => p.category === cat));
  const word = products[1].name.split(' ')[0];
  const found = Catalog.filterProducts(products, { query: word.toUpperCase() });
  assert.ok(found.some((p) => p.id === products[1].id));
  assert.equal(Catalog.filterProducts(products, { query: 'zzzz-no-such-product' }).length, 0);
});

test('sorting by price, rating and featured', () => {
  const asc = Catalog.sortProducts(products, 'price-asc');
  for (let i = 1; i < asc.length; i++) assert.ok(asc[i - 1].price <= asc[i].price);
  const desc = Catalog.sortProducts(products, 'price-desc');
  for (let i = 1; i < desc.length; i++) assert.ok(desc[i - 1].price >= desc[i].price);
  const rated = Catalog.sortProducts(products, 'rating');
  for (let i = 1; i < rated.length; i++) assert.ok((rated[i - 1].rating || 0) >= (rated[i].rating || 0));
  const featured = Catalog.sortProducts(products, 'unknown-sort');
  assert.equal(featured[0].featured, true);
  assert.equal(products.length, Catalog.sortProducts(products, 'name').length);
});

test('find, related and stock state', () => {
  const p = products[0];
  assert.equal(Catalog.findProduct(products, p.id), p);
  assert.equal(Catalog.findProduct(products, 'missing'), null);
  const rel = Catalog.related(products, p, 3);
  assert.ok(rel.length <= 3 && rel.every((r) => r.id !== p.id));
  assert.equal(Catalog.stockState({ stock: 0 }), 'out');
  assert.equal(Catalog.stockState({ stock: 3 }), 'low');
  assert.equal(Catalog.stockState({ stock: 30 }), 'in');
});

test('search ignores Hebrew niqqud and case', () => {
  const list = [{ id: 'x', name: 'שָׁלוֹם Coffee', description: '', category: 'c' }];
  assert.equal(Catalog.filterProducts(list, { query: 'שלום coffee' }).length, 1);
});
