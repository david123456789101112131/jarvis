'use strict';
/* Validation for item bodies: returns { value } or { errors }. */

function validateItem(body, { partial = false } = {}) {
  const errors = {};
  const value = {};
  if (!body || typeof body !== 'object' || Array.isArray(body)) return { errors: { body: 'must be a JSON object' } };
  if (body.name !== undefined || !partial) {
    const name = typeof body.name === 'string' ? body.name.trim() : '';
    if (!name) errors.name = 'required';
    else if (name.length > 120) errors.name = 'at most 120 characters';
    else value.name = name;
  }
  if (body.description !== undefined) {
    if (typeof body.description !== 'string' || body.description.length > 2000) errors.description = 'a string up to 2000 characters';
    else value.description = body.description.trim();
  } else if (!partial) value.description = '';
  if (body.price !== undefined) {
    const price = Number(body.price);
    if (!Number.isFinite(price) || price < 0) errors.price = 'a number >= 0';
    else value.price = Math.round(price * 100) / 100;
  } else if (!partial) value.price = 0;
  if (body.done !== undefined) {
    if (typeof body.done !== 'boolean') errors.done = 'true or false';
    else value.done = body.done;
  } else if (!partial) value.done = false;
  return Object.keys(errors).length ? { errors } : { value };
}

module.exports = { validateItem };
