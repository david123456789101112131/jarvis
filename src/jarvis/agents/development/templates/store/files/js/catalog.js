/* Catalog logic: search, category filter and sorting (pure functions). */
(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.StoreCatalog = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var SORTS = ['featured', 'price-asc', 'price-desc', 'name', 'rating'];

  function fold(text) {
    return String(text || '')
      .toLowerCase()
      .normalize('NFKD')
      .replace(/[֑-ׇ̀-ͯ]/g, '') // Hebrew niqqud and Latin accents
      .trim();
  }

  function categories(products) {
    var seen = [];
    (products || []).forEach(function (p) {
      if (p.category && seen.indexOf(p.category) === -1) seen.push(p.category);
    });
    return seen;
  }

  function matches(product, query) {
    if (!query) return true;
    var hay = fold([product.name, product.description, product.category].join(' '));
    return fold(query).split(/\s+/).every(function (word) { return hay.indexOf(word) !== -1; });
  }

  function filterProducts(products, options) {
    var opts = options || {};
    return (products || []).filter(function (p) {
      if (opts.category && opts.category !== 'all' && p.category !== opts.category) return false;
      return matches(p, opts.query);
    });
  }

  function sortProducts(products, sort, locale) {
    var list = (products || []).slice();
    var key = SORTS.indexOf(sort) === -1 ? 'featured' : sort;
    var order = {};
    (products || []).forEach(function (p, i) { order[p.id] = i; });
    var byOrder = function (a, b) { return order[a.id] - order[b.id]; };
    var compare = {
      featured: function (a, b) { return (b.featured ? 1 : 0) - (a.featured ? 1 : 0) || byOrder(a, b); },
      'price-asc': function (a, b) { return a.price - b.price || byOrder(a, b); },
      'price-desc': function (a, b) { return b.price - a.price || byOrder(a, b); },
      name: function (a, b) { return String(a.name).localeCompare(String(b.name), locale || undefined) || byOrder(a, b); },
      rating: function (a, b) { return (b.rating || 0) - (a.rating || 0) || byOrder(a, b); }
    }[key];
    return list.sort(compare);
  }

  function query(products, options) {
    var opts = options || {};
    return sortProducts(filterProducts(products, opts), opts.sort, opts.locale);
  }

  function findProduct(products, id) {
    return (products || []).find(function (p) { return p.id === id; }) || null;
  }

  function related(products, product, limit) {
    if (!product) return [];
    var same = (products || []).filter(function (p) { return p.id !== product.id && p.category === product.category; });
    var others = (products || []).filter(function (p) { return p.id !== product.id && p.category !== product.category; });
    return same.concat(others).slice(0, limit || 4);
  }

  function stockState(product) {
    var stock = Number(product && product.stock);
    if (!isFinite(stock)) return 'in';
    if (stock <= 0) return 'out';
    if (stock <= 5) return 'low';
    return 'in';
  }

  return {
    SORTS: SORTS, fold: fold, categories: categories, filterProducts: filterProducts, sortProducts: sortProducts,
    query: query, findProduct: findProduct, related: related, stockState: stockState
  };
});
