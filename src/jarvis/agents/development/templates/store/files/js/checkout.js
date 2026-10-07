/* Checkout: form validation and order creation (pure functions; no real payment). */
(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) {
    module.exports = factory(require('./cart.js'));
  } else {
    root.StoreCheckout = factory(root.StoreCart);
  }
})(typeof self !== 'undefined' ? self : this, function (Cart) {
  'use strict';

  var EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
  var REQUIRED = ['name', 'email', 'phone', 'address', 'city'];

  function clean(value) {
    return String(value === undefined || value === null ? '' : value).trim();
  }

  function validate(form) {
    var data = form || {};
    var errors = {};
    REQUIRED.forEach(function (field) {
      if (!clean(data[field])) errors[field] = 'required';
    });
    if (!errors.name && clean(data.name).length < 2) errors.name = 'short';
    if (!errors.email && !EMAIL.test(clean(data.email))) errors.email = 'email';
    if (!errors.phone) {
      var digits = clean(data.phone).replace(/[\s\-().]/g, '');
      if (!/^\+?\d{9,15}$/.test(digits)) errors.phone = 'phone';
    }
    if (!errors.address && clean(data.address).length < 3) errors.address = 'short';
    return errors;
  }

  function orderId(now, random) {
    var d = new Date(now);
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    var stamp = String(d.getFullYear()).slice(2) + pad(d.getMonth() + 1) + pad(d.getDate());
    var suffix = Math.floor(random() * 0xffff).toString(16).toUpperCase();
    while (suffix.length < 4) suffix = '0' + suffix;
    return 'ORD-' + stamp + '-' + suffix;
  }

  function createOrder(cart, products, config, customer, now, random) {
    var errors = validate(customer);
    if (Object.keys(errors).length) {
      var err = new Error('invalid checkout form');
      err.errors = errors;
      throw err;
    }
    var lines = Cart.lines(cart, products);
    if (!lines.length) throw new Error('empty cart');
    var time = now === undefined ? Date.now() : now;
    var info = {};
    REQUIRED.concat(['notes']).forEach(function (field) { info[field] = clean(customer[field]); });
    return {
      id: orderId(time, random || Math.random),
      createdAt: new Date(time).toISOString(),
      customer: info,
      lines: lines.map(function (line) {
        return { id: line.product.id, name: line.product.name, price: line.product.price, qty: line.qty, total: line.total };
      }),
      totals: Cart.totals(cart, products, config),
      paid: false
    };
  }

  function saveOrder(storage, key, order) {
    var orders = [];
    try {
      orders = JSON.parse((storage && storage.getItem(key)) || '[]');
      if (!Array.isArray(orders)) orders = [];
    } catch (err) {
      orders = [];
    }
    orders.push(order);
    try {
      if (storage) storage.setItem(key, JSON.stringify(orders));
    } catch (err) {
      /* storage unavailable: the confirmation is still shown */
    }
    return orders;
  }

  return { REQUIRED: REQUIRED, validate: validate, createOrder: createOrder, saveOrder: saveOrder, orderId: orderId };
});
