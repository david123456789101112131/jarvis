/* Page behaviour for the store (catalog, product, cart, checkout). Data comes from data/products.js. */
(function () {
  'use strict';

  var products = window.STORE_PRODUCTS || [];
  var config = window.STORE_CONFIG || {};
  var ui = window.STORE_UI || {};
  var Cart = window.StoreCart;
  var Catalog = window.StoreCatalog;
  var Checkout = window.StoreCheckout;
  var CART_KEY = config.storageKey || 'store-cart';
  var ORDERS_KEY = CART_KEY + '-orders';
  var storage = (function () {
    try {
      var probe = '__probe__';
      window.localStorage.setItem(probe, probe);
      window.localStorage.removeItem(probe);
      return window.localStorage;
    } catch (err) {
      return null;
    }
  })();

  function $(selector, scope) { return (scope || document).querySelector(selector); }
  function text(key, vars) {
    var value = ui[key] || key;
    Object.keys(vars || {}).forEach(function (name) { value = value.replace('{' + name + '}', vars[name]); });
    return value;
  }
  function money(amount) { return Cart.formatMoney(amount, config); }
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (name) {
      if (name === 'text') node.textContent = attrs[name];
      else if (name === 'class') node.className = attrs[name];
      else if (name.indexOf('on') === 0) node.addEventListener(name.slice(2), attrs[name]);
      else node.setAttribute(name, attrs[name]);
    });
    (children || []).forEach(function (child) { if (child) node.appendChild(child); });
    return node;
  }

  function getCart() { return Cart.load(storage, CART_KEY); }
  function setCart(cart) {
    var saved = Cart.save(storage, CART_KEY, cart);
    updateBadge(saved);
    return saved;
  }
  function updateBadge(cart) {
    var n = Cart.count(cart || getCart());
    document.querySelectorAll('[data-cart-count]').forEach(function (badge) {
      badge.textContent = String(n);
      badge.hidden = n === 0;
    });
  }

  var toastTimer = null;
  function toast(message) {
    var box = $('#toast');
    if (!box) return;
    box.textContent = message;
    box.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { box.classList.remove('show'); }, 2200);
  }

  function addToCart(product, qty) {
    if (Catalog.stockState(product) === 'out') return;
    setCart(Cart.addItem(getCart(), product.id, qty || 1, product.stock));
    toast(text('added') + ' · ' + product.name);
  }

  function stars(rating) {
    var full = Math.round(Number(rating) || 0);
    var box = el('span', { class: 'stars', 'aria-label': text('rating') + ' ' + rating });
    for (var i = 1; i <= 5; i++) box.appendChild(el('span', { class: i <= full ? 'on' : '', text: '★' }));
    return box;
  }

  function stockLabel(product) {
    var state = Catalog.stockState(product);
    var label = state === 'out' ? text('out_of_stock') : state === 'low' ? text('low_stock', { n: product.stock }) : text('in_stock');
    return el('span', { class: 'stock stock-' + state, text: label });
  }

  function productCard(product) {
    var link = 'product.html?id=' + encodeURIComponent(product.id);
    var add = el('button', { class: 'btn btn-primary btn-sm', type: 'button', text: text('add'),
      onclick: function () { addToCart(product, 1); } });
    if (Catalog.stockState(product) === 'out') add.disabled = true;
    return el('article', { class: 'product-card' }, [
      el('a', { class: 'thumb', href: link, 'aria-label': product.name }, [
        el('img', { src: product.image, alt: product.name, loading: 'lazy', width: '400', height: '400' })
      ]),
      el('div', { class: 'body' }, [
        el('span', { class: 'chip', text: product.category }),
        el('h3', {}, [el('a', { href: link, text: product.name })]),
        stars(product.rating),
        el('div', { class: 'row' }, [el('strong', { class: 'price', text: money(product.price) }), add])
      ])
    ]);
  }

  // -------------------------------------------------------------------------------- catalog
  function initCatalog() {
    var params = new URLSearchParams(window.location.search);
    var state = { query: params.get('q') || '', category: params.get('cat') || 'all', sort: params.get('sort') || 'featured',
      locale: config.locale };
    var search = $('#search');
    var sort = $('#sort');
    var chips = $('#categories');
    var grid = $('#grid');
    var results = $('#results');
    search.value = state.query;
    sort.value = state.sort;

    function renderChips() {
      chips.textContent = '';
      ['all'].concat(Catalog.categories(products)).forEach(function (cat) {
        chips.appendChild(el('button', {
          type: 'button', class: 'chip-btn' + (state.category === cat ? ' active' : ''),
          'aria-pressed': String(state.category === cat), text: cat === 'all' ? text('all') : cat,
          onclick: function () { state.category = cat; update(); }
        }));
      });
    }

    function update() {
      var list = Catalog.query(products, state);
      grid.textContent = '';
      list.forEach(function (p) { grid.appendChild(productCard(p)); });
      results.textContent = list.length ? text('results', { n: list.length }) : text('no_results');
      renderChips();
      try {
        var next = new URLSearchParams();
        if (state.query) next.set('q', state.query);
        if (state.category !== 'all') next.set('cat', state.category);
        if (state.sort !== 'featured') next.set('sort', state.sort);
        var qs = next.toString();
        window.history.replaceState(null, '', qs ? '?' + qs : window.location.pathname);
      } catch (err) { /* some file:// setups refuse history changes */ }
    }

    search.addEventListener('input', function () { state.query = search.value; update(); });
    sort.addEventListener('change', function () { state.sort = sort.value; update(); });
    update();
  }

  // -------------------------------------------------------------------------------- product
  function initProduct() {
    var id = new URLSearchParams(window.location.search).get('id');
    var product = Catalog.findProduct(products, id) || null;
    var box = $('#product');
    if (!product) {
      box.appendChild(el('div', { class: 'empty' }, [
        el('h1', { text: text('not_found') }), el('a', { class: 'btn btn-primary', href: 'index.html', text: text('back') })
      ]));
      return;
    }
    document.title = product.name + ' · ' + document.title;
    var qty = el('input', { type: 'number', min: '1', max: String(Math.max(1, product.stock || 1)), value: '1',
      'aria-label': text('qty'), class: 'qty-input' });
    var add = el('button', { class: 'btn btn-primary', type: 'button', text: text('add'),
      onclick: function () { addToCart(product, Number(qty.value) || 1); } });
    if (Catalog.stockState(product) === 'out') add.disabled = true;
    box.appendChild(el('div', { class: 'product-detail' }, [
      el('div', { class: 'gallery' }, [el('img', { src: product.image, alt: product.name, width: '400', height: '400' })]),
      el('div', { class: 'info' }, [
        el('a', { class: 'back', href: 'index.html', text: '‹ ' + text('back') }),
        el('span', { class: 'chip', text: product.category }),
        el('h1', { text: product.name }),
        stars(product.rating),
        el('p', { class: 'price big', text: money(product.price) }),
        el('p', { class: 'desc', text: product.description }),
        stockLabel(product),
        el('div', { class: 'buy' }, [qty, add])
      ])
    ]));
    var rel = $('#related');
    Catalog.related(products, product, 4).forEach(function (p) { rel.appendChild(productCard(p)); });
  }

  // -------------------------------------------------------------------------------- cart
  function summaryRows(t) {
    var rows = [
      [text('subtotal'), money(t.subtotal)],
      [config.pricesIncludeVat ? text('vat_included') : text('vat'), money(t.vat)],
      [text('shipping'), t.shipping === 0 && t.subtotal > 0 ? text('free') : money(t.shipping)]
    ];
    var list = el('dl', { class: 'summary-rows' });
    rows.forEach(function (row) {
      list.appendChild(el('dt', { text: row[0] }));
      list.appendChild(el('dd', { text: row[1] }));
    });
    list.appendChild(el('dt', { class: 'total', text: text('total') }));
    list.appendChild(el('dd', { class: 'total', text: money(t.total) }));
    return list;
  }

  function initCart() {
    var box = $('#cart-lines');
    var summary = $('#summary');
    function render() {
      var cart = getCart();
      var lines = Cart.lines(cart, products);
      var t = Cart.totals(cart, products, config);
      box.textContent = '';
      summary.textContent = '';
      if (!lines.length) {
        box.appendChild(el('div', { class: 'empty' }, [
          el('p', { text: text('cart_empty') }), el('a', { class: 'btn btn-primary', href: 'index.html', text: text('continue') })
        ]));
        summary.hidden = true;
        return;
      }
      summary.hidden = false;
      lines.forEach(function (line) {
        var p = line.product;
        var change = function (delta) { return function () { setCart(Cart.setQty(getCart(), p.id, line.qty + delta)); render(); }; };
        box.appendChild(el('div', { class: 'cart-line' }, [
          el('img', { src: p.image, alt: '', width: '80', height: '80' }),
          el('div', { class: 'grow' }, [el('a', { href: 'product.html?id=' + encodeURIComponent(p.id), text: p.name }),
            el('div', { class: 'muted', text: money(p.price) })]),
          el('div', { class: 'stepper', role: 'group', 'aria-label': text('qty') }, [
            el('button', { type: 'button', 'aria-label': '-', text: '−', onclick: change(-1) }),
            el('span', { text: String(line.qty) }),
            el('button', { type: 'button', 'aria-label': '+', text: '+', onclick: change(1) })
          ]),
          el('strong', { class: 'line-total', text: money(line.total) }),
          el('button', { type: 'button', class: 'link-btn', text: text('remove'),
            onclick: function () { setCart(Cart.removeItem(getCart(), p.id)); render(); } })
        ]));
      });
      summary.appendChild(el('h2', { text: text('summary') }));
      summary.appendChild(summaryRows(t));
      summary.appendChild(el('p', { class: 'hint', text: t.freeShippingRemaining > 0
        ? text('free_shipping_hint', { amount: money(t.freeShippingRemaining) }) : text('free_shipping_ok') }));
      summary.appendChild(el('a', { class: 'btn btn-primary wide', href: 'checkout.html', text: text('to_checkout') }));
    }
    render();
  }

  // -------------------------------------------------------------------------------- checkout
  function initCheckout() {
    var form = $('#checkout-form');
    var summary = $('#order-summary');
    var done = $('#confirmation');
    var cart = getCart();
    if (!Cart.lines(cart, products).length) {
      form.hidden = true;
      summary.appendChild(el('div', { class: 'empty' }, [el('p', { text: text('err_cart') }),
        el('a', { class: 'btn btn-primary', href: 'index.html', text: text('continue') })]));
      return;
    }
    Cart.lines(cart, products).forEach(function (line) {
      summary.appendChild(el('div', { class: 'mini-line' }, [el('span', { text: line.qty + ' × ' + line.product.name }),
        el('span', { text: money(line.total) })]));
    });
    summary.appendChild(summaryRows(Cart.totals(cart, products, config)));

    var messages = { required: text('err_required'), email: text('err_email'), phone: text('err_phone'), short: text('err_short') };
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var data = {};
      new FormData(form).forEach(function (value, key) { data[key] = value; });
      var errors = Checkout.validate(data);
      form.querySelectorAll('[data-error-for]').forEach(function (slot) {
        var field = slot.getAttribute('data-error-for');
        slot.textContent = errors[field] ? messages[errors[field]] : '';
        var input = form.elements[field];
        if (input) input.setAttribute('aria-invalid', errors[field] ? 'true' : 'false');
      });
      var first = Object.keys(errors)[0];
      if (first) {
        if (form.elements[first]) form.elements[first].focus();
        return;
      }
      var order = Checkout.createOrder(getCart(), products, config, data);
      Checkout.saveOrder(storage, ORDERS_KEY, order);
      setCart(Cart.empty());
      form.hidden = true;
      summary.hidden = true;
      done.hidden = false;
      $('#order-id').textContent = order.id;
      $('#order-total').textContent = money(order.totals.total);
      done.focus();
    });
  }

  function initMenu() {
    var toggle = $('#menu-toggle');
    var nav = $('#site-nav');
    if (!toggle || !nav) return;
    toggle.addEventListener('click', function () {
      var open = nav.classList.toggle('open');
      toggle.setAttribute('aria-expanded', String(open));
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    initMenu();
    updateBadge();
    var page = document.body.getAttribute('data-page');
    if (page === 'catalog') initCatalog();
    else if (page === 'product') initProduct();
    else if (page === 'cart') initCart();
    else if (page === 'checkout') initCheckout();
    window.addEventListener('storage', function (event) { if (event.key === CART_KEY) updateBadge(); });
  });
})();
