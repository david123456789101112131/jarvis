'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { validateContact, mailtoLink } = require('../js/validate.js');

const good = { name: 'Dana', email: 'dana@example.com', phone: '', message: 'I would like to hear more about your services.' };

test('a complete form is valid', () => {
  assert.deepEqual(validateContact(good), {});
  assert.deepEqual(validateContact({ ...good, phone: '+972 50-123-4567' }), {});
});

test('missing and invalid fields are reported', () => {
  const errors = validateContact({ name: '', email: 'bad@', phone: '123', message: 'hi' });
  assert.deepEqual(errors, { name: 'required', email: 'email', phone: 'phone', message: 'short' });
  assert.equal(validateContact(null).email, 'required');
});

test('mailto link carries subject and body', () => {
  const link = mailtoLink('team@example.com', 'Hello there', good);
  assert.ok(link.startsWith('mailto:team%40example.com?subject=Hello%20there&body='));
  assert.ok(decodeURIComponent(link).includes(good.message));
  assert.ok(decodeURIComponent(link).includes('dana@example.com'));
});
