/* Contact form validation and the mailto: link (pure functions, tested with node --test). */
(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.SiteForm = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

  function clean(value) {
    return String(value === undefined || value === null ? '' : value).trim();
  }

  function validateContact(form) {
    var data = form || {};
    var errors = {};
    var name = clean(data.name);
    var email = clean(data.email);
    var phone = clean(data.phone);
    var message = clean(data.message);
    if (!name) errors.name = 'required';
    else if (name.length < 2) errors.name = 'short';
    if (!email) errors.email = 'required';
    else if (!EMAIL.test(email)) errors.email = 'email';
    if (phone && !/^\+?\d{9,15}$/.test(phone.replace(/[\s\-().]/g, ''))) errors.phone = 'phone';
    if (!message) errors.message = 'required';
    else if (message.length < 10) errors.message = 'short';
    return errors;
  }

  function mailtoLink(to, subject, form) {
    var data = form || {};
    var lines = [clean(data.message), '', '— ' + clean(data.name), clean(data.email)];
    if (clean(data.phone)) lines.push(clean(data.phone));
    return 'mailto:' + encodeURIComponent(to) + '?subject=' + encodeURIComponent(subject) +
      '&body=' + encodeURIComponent(lines.join('\n'));
  }

  return { validateContact: validateContact, mailtoLink: mailtoLink };
});
