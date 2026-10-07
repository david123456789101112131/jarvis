/* Site behaviour: mobile menu, current page, contact form. */
(function () {
  'use strict';
  var ui = window.SITE_UI || {};

  function initMenu() {
    var toggle = document.getElementById('menu-toggle');
    var nav = document.getElementById('site-nav');
    if (!toggle || !nav) return;
    toggle.addEventListener('click', function () {
      var open = nav.classList.toggle('open');
      toggle.setAttribute('aria-expanded', String(open));
    });
    nav.addEventListener('click', function (event) {
      if (event.target.tagName === 'A') {
        nav.classList.remove('open');
        toggle.setAttribute('aria-expanded', 'false');
      }
    });
  }

  function initContact() {
    var form = document.getElementById('contact-form');
    if (!form || !window.SiteForm) return;
    var done = document.getElementById('contact-done');
    var messages = { required: ui.err_required, email: ui.err_email, phone: ui.err_phone, short: ui.err_short };
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var data = {};
      new FormData(form).forEach(function (value, key) { data[key] = value; });
      var errors = window.SiteForm.validateContact(data);
      form.querySelectorAll('[data-error-for]').forEach(function (slot) {
        var field = slot.getAttribute('data-error-for');
        slot.textContent = errors[field] ? messages[errors[field]] || errors[field] : '';
        if (form.elements[field]) form.elements[field].setAttribute('aria-invalid', errors[field] ? 'true' : 'false');
      });
      var first = Object.keys(errors)[0];
      if (first) {
        form.elements[first].focus();
        return;
      }
      try {
        var saved = JSON.parse(window.localStorage.getItem('contact-messages') || '[]');
        saved.push({ at: new Date().toISOString(), name: data.name, email: data.email, phone: data.phone, message: data.message });
        window.localStorage.setItem('contact-messages', JSON.stringify(saved));
      } catch (err) { /* storage disabled */ }
      var link = document.getElementById('mail-link');
      link.href = window.SiteForm.mailtoLink(form.getAttribute('data-to'), ui.subject || '', data);
      form.hidden = true;
      done.hidden = false;
      done.focus();
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    initMenu();
    initContact();
  });
})();
