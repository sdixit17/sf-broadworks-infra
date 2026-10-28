/* Telcom-Company website behaviour. Settings come from config.js (written at deploy time):
 *   window.SITE = { demoNumber, hubspotPortal, hubspotForms: { demo, partner, contact }, contactEmail }
 */
(function () {
  'use strict';
  var S = window.SITE || {};
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  // ------------------------------------------------------------ demo number ("Call our AI now")
  var number = (S.demoNumber || '').trim();
  var tel = number.replace(/[^\d+]/g, '');
  $$('[data-demo-number]').forEach(function (el) {
    el.textContent = number || 'Ask us for the demo line';
  });
  $$('[data-demo-link]').forEach(function (el) {
    if (tel) {
      el.setAttribute('href', 'tel:' + tel);
    } else {
      el.setAttribute('href', 'contact.html#demo');   // no line configured yet: book a demo instead
    }
  });

  // ------------------------------------------------------------ header + mobile menu
  var header = $('.site-header');
  var onScroll = function () { if (header) header.classList.toggle('scrolled', window.scrollY > 8); };
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();
  var menu = $('.menu-btn');
  if (menu) {
    menu.addEventListener('click', function () {
      var open = header.classList.toggle('open');
      menu.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }

  // ------------------------------------------------------------ reveal on scroll
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
      });
    }, { rootMargin: '0px 0px -8% 0px' });
    $$('.reveal').forEach(function (el) { io.observe(el); });
  } else {
    $$('.reveal').forEach(function (el) { el.classList.add('in'); });
  }

  // ------------------------------------------------------------ hero conversation (plays, then loops)
  var convo = $('[data-convo]');
  if (convo) {
    var bubbles = $$('.bubble', convo);
    var extras = $$('[data-after]');
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var play = function () {
      bubbles.forEach(function (b) { b.classList.remove('show'); });
      extras.forEach(function (x) { x.classList.remove('in'); });
      bubbles.forEach(function (b, i) { setTimeout(function () { b.classList.add('show'); }, 500 + i * 1400); });
      extras.forEach(function (x) {
        setTimeout(function () { x.classList.add('in'); }, 500 + bubbles.length * 1400 + Number(x.getAttribute('data-after') || 0));
      });
    };
    if (reduce) {
      bubbles.forEach(function (b) { b.classList.add('show'); });
      extras.forEach(function (x) { x.classList.add('in'); });
    } else {
      play();
      setInterval(play, 500 + bubbles.length * 1400 + 6000);
    }
  }

  // ------------------------------------------------------------ forms: HubSpot when configured, else a clear message
  $$('form[data-form]').forEach(function (form) {
    var kind = form.getAttribute('data-form');
    var msg = $('.form-msg', form);
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      if (!form.reportValidity()) return;
      var data = new FormData(form);
      var btn = $('button[type="submit"]', form);
      var guid = (S.hubspotForms || {})[kind];
      var done = function (ok, text) {
        msg.className = 'form-msg ' + (ok ? 'ok' : 'err');
        msg.textContent = text;
        if (btn) btn.disabled = false;
        if (ok) form.reset();
      };
      if (btn) btn.disabled = true;
      if (S.hubspotPortal && guid) {
        var fields = [];
        data.forEach(function (v, k) { if (k !== 'consent') fields.push({ name: k, value: String(v) }); });
        fetch('https://api.hsforms.com/submissions/v3/integration/submit/' + encodeURIComponent(S.hubspotPortal) + '/' +
              encodeURIComponent(guid), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ fields: fields, context: { pageUri: location.href, pageName: document.title } })
        }).then(function (r) {
          done(r.ok, r.ok ? 'Thank you, we will be in touch within one working day.'
                          : 'Sorry, that did not go through. Please try again or call us.');
        }).catch(function () { done(false, 'Sorry, that did not go through. Please check your connection and try again.'); });
      } else if (S.contactEmail) {
        var body = [];
        data.forEach(function (v, k) { if (k !== 'consent') body.push(k + ': ' + v); });
        location.href = 'mailto:' + S.contactEmail + '?subject=' + encodeURIComponent('Website: ' + kind) +
          '&body=' + encodeURIComponent(body.join('\n'));
        done(true, 'Your email app has opened with the details. Send it and we will be in touch.');
      } else {
        done(true, 'Thank you. (Preview site: this form is not connected to HubSpot yet.)');
      }
    });
  });

  // ------------------------------------------------------------ ROI calculator
  var calc = $('[data-calc]');
  if (calc) {
    var gbp = function (n) { return '£' + Math.round(n).toLocaleString('en-GB'); };
    var inputs = $$('input[type="range"]', calc);
    var update = function () {
      var v = {};
      inputs.forEach(function (i) {
        v[i.name] = Number(i.value);
        var out = $('output[for="' + i.id + '"]', calc);
        if (out) out.textContent = (i.getAttribute('data-prefix') || '') + Number(i.value).toLocaleString('en-GB') + (i.getAttribute('data-suffix') || '');
      });
      var aiCalls = v.calls * v.share / 100;                           // calls the AI finishes without an agent
      var minutesSaved = aiCalls * v.handle;                           // agent minutes no longer needed
      var hours = minutesSaved / 60;
      var saving = hours * v.cost;
      var afterHours = v.calls * v.after / 100;                        // calls answered that used to go to voicemail
      $('[data-out="saving"]', calc).textContent = gbp(saving);
      $('[data-out="hours"]', calc).textContent = Math.round(hours).toLocaleString('en-GB') + ' hours';
      $('[data-out="ai"]', calc).textContent = Math.round(aiCalls).toLocaleString('en-GB') + ' calls';
      $('[data-out="after"]', calc).textContent = Math.round(afterHours).toLocaleString('en-GB') + ' calls';
      $('[data-out="year"]', calc).textContent = gbp(saving * 12);
    };
    inputs.forEach(function (i) { i.addEventListener('input', update); });
    update();
  }

  var year = $('[data-year]');
  if (year) year.textContent = new Date().getFullYear();
})();
