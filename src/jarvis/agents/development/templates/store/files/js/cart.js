/* Shopping cart logic: pure functions (no DOM), used by the pages and by the tests. */
(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.StoreCart = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var MAX_QTY = 99;

  function round2(value) {
    return Math.round((value + Number.EPSILON) * 100) / 100;
  }

  function empty() {
    return { items: [] };
  }

  function normalize(cart) {
    if (!cart || !Array.isArray(cart.items)) return empty();
    var seen = {};
    var items = [];
    cart.items.forEach(function (item) {
      if (!item || typeof item.id !== 'string') return;
      var qty = Math.floor(Number(item.qty));
      if (!isFinite(qty) || qty <= 0) return;
      if (seen[item.id]) {
        seen[item.id].qty = Math.min(MAX_QTY, seen[item.id].qty + qty);
        return;
      }
      var clean = { id: item.id, qty: Math.min(MAX_QTY, qty) };
      seen[item.id] = clean;
      items.push(clean);
    });
    return { items: items };
  }

  function addItem(cart, id, qty, maxQty) {
    var add = qty === undefined ? 1 : Math.floor(Number(qty));
    if (!isFinite(add) || add <= 0) return normalize(cart);
    var limit = Math.min(MAX_QTY, maxQty === undefined ? MAX_QTY : Math.max(0, maxQty));
    var next = normalize(cart);
    var found = next.items.find(function (item) { return item.id === id; });
    if (found) {
      found.qty = Math.min(limit, found.qty + add);
    } else if (limit > 0) {
      next.items.push({ id: id, qty: Math.min(limit, add) });
    }
    return next;
  }

  function setQty(cart, id, qty) {
    var value = Math.floor(Number(qty));
    var next = normalize(cart);
    if (!isFinite(value) || value <= 0) return removeItem(next, id);
    next.items.forEach(function (item) {
      if (item.id === id) item.qty = Math.min(MAX_QTY, value);
    });
    return next;
  }

  function removeItem(cart, id) {
    var next = normalize(cart);
    next.items = next.items.filter(function (item) { return item.id !== id; });
    return next;
  }

  function count(cart) {
    return normalize(cart).items.reduce(function (sum, item) { return sum + item.qty; }, 0);
  }

  function lines(cart, products) {
    var byId = {};
    (products || []).forEach(function (p) { byId[p.id] = p; });
    return normalize(cart).items
      .filter(function (item) { return byId[item.id]; })
      .map(function (item) {
        var product = byId[item.id];
        return { product: product, qty: item.qty, total: round2(product.price * item.qty) };
      });
  }

  function totals(cart, products, config) {
    var cfg = config || {};
    var rate = Number(cfg.vatRate) || 0;
    var subtotal = round2(lines(cart, products).reduce(function (sum, line) { return sum + line.total; }, 0));
    var freeOver = Number(cfg.freeShippingOver);
    var fee = Number(cfg.shippingFee) || 0;
    var shipping = subtotal === 0 || (freeOver > 0 && subtotal >= freeOver) ? 0 : fee;
    var vat;
    var total;
    if (cfg.pricesIncludeVat) {
      vat = round2(subtotal - subtotal / (1 + rate));
      total = round2(subtotal + shipping);
    } else {
      vat = round2(subtotal * rate);
      total = round2(subtotal + vat + shipping);
    }
    return {
      subtotal: subtotal,
      vat: vat,
      shipping: round2(shipping),
      total: total,
      items: count(cart),
      freeShippingRemaining: freeOver > 0 && subtotal > 0 && subtotal < freeOver ? round2(freeOver - subtotal) : 0
    };
  }

  function formatMoney(amount, config) {
    var cfg = config || {};
    try {
      return new Intl.NumberFormat(cfg.locale || 'en-US', {
        style: 'currency', currency: cfg.currencyCode || 'USD', maximumFractionDigits: 2
      }).format(amount);
    } catch (err) {
      return (cfg.currency || '$') + Number(amount).toFixed(2);
    }
  }

  function load(storage, key) {
    try {
      var raw = storage && storage.getItem(key);
      return raw ? normalize(JSON.parse(raw)) : empty();
    } catch (err) {
      return empty();
    }
  }

  function save(storage, key, cart) {
    var clean = normalize(cart);
    try {
      if (storage) storage.setItem(key, JSON.stringify(clean));
    } catch (err) {
      /* storage full or disabled: the cart still works for this page view */
    }
    return clean;
  }

  return {
    MAX_QTY: MAX_QTY, empty: empty, normalize: normalize, addItem: addItem, setQty: setQty,
    removeItem: removeItem, count: count, lines: lines, totals: totals, formatMoney: formatMoney,
    load: load, save: save, round2: round2
  };
});
