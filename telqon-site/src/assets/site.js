/* telqon.com behaviour. Settings from config.js (written by build.py):
 *   window.SITE = { hubspotPortal, hubspotForm, contactEmail }
 */
(function () {
  'use strict';
  var S = window.SITE || {};
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.documentElement.classList.add('js');

  // menu
  var header = $('.site-header'), menu = $('.menu-btn');
  if (menu && header) {
    menu.addEventListener('click', function () {
      var open = header.classList.toggle('open');
      menu.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }

  // reveal on scroll (content is visible without it; this only adds a small lift)
  if ('IntersectionObserver' in window && !reduce) {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
    }, { rootMargin: '0px 0px -6% 0px' });
    $$('.reveal').forEach(function (el) { io.observe(el); });
  } else {
    $$('.reveal').forEach(function (el) { el.classList.add('in'); });
  }

  // copy buttons
  $$('[data-copy]').forEach(function (b) {
    b.addEventListener('click', function () {
      var text = b.getAttribute('data-copy');
      var done = function () { b.textContent = 'Copied'; setTimeout(function () { b.textContent = 'Copy'; }, 1500); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { selectText(b.previousElementSibling); });
      } else { selectText(b.previousElementSibling); }
    });
  });
  function selectText(el) {
    if (!el) return;
    var r = document.createRange(); r.selectNodeContents(el);
    var s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
  }

  // contact form: HubSpot when configured, else an email to the contact address
  var email = S.contactEmail || 'info@telqon.com';
  $$('[data-email]').forEach(function (el) { el.textContent = email; });
  $$('form[data-form]').forEach(function (form) {
    var msg = $('.form-msg', form), btn = $('button[type="submit"]', form);
    var interest = new URLSearchParams(location.search).get('interest');
    if (interest && form.elements.interest) {
      Array.prototype.forEach.call(form.elements.interest.options, function (o) { if (o.value === interest) o.selected = true; });
    }
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      if (!form.reportValidity()) return;
      var d = new FormData(form);
      if (d.get('website')) return;                       // honeypot
      var say = function (t, cls) { msg.textContent = t; msg.className = 'form-msg ' + (cls || ''); };
      if (S.hubspotPortal && S.hubspotForm) {
        var fields = [];
        d.forEach(function (v, k) { if (k !== 'website' && v) fields.push({ name: k, value: String(v) }); });
        btn.disabled = true; say('Sending…');
        fetch('https://api.hsforms.com/submissions/v3/integration/submit/' + encodeURIComponent(S.hubspotPortal) + '/' +
              encodeURIComponent(S.hubspotForm), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ fields: fields, context: { pageUri: location.href, pageName: document.title } })
        }).then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          form.reset(); say('Thanks. An engineer will reply within one working day.', 'ok');
        }).catch(function () {
          say('That didn\'t send. Please email ' + email + ' instead.', 'err');
        }).then(function () { btn.disabled = false; });
      } else {
        var body = ['Name: ' + d.get('firstname') + ' ' + (d.get('lastname') || ''), 'Company: ' + (d.get('company') || ''),
                    'Phone: ' + (d.get('phone') || ''), 'Interested in: ' + (d.get('interest') || ''), '', d.get('message') || ''].join('\n');
        location.href = 'mailto:' + email + '?subject=' + encodeURIComponent('Enquiry: ' + (d.get('interest') || 'Telqon')) +
                        '&body=' + encodeURIComponent(body);
        say('Your email app should open with the message filled in. If it doesn\'t, write to ' + email + '.', 'ok');
      }
    });
  });
})();
