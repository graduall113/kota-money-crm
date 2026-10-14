/* On-demand secondary panels (lead / contact detail).
 * A panel's rows are fetched from its data-lazy-url the FIRST time it is opened, then kept: closing and reopening
 * just shows/hides them (no second request). A second click while the request is in flight is ignored. A failed
 * request shows a Retry button and can be repeated. Page navigation / form posts reload the page, so a reopened
 * panel always reflects the latest data. */
(function () {
  'use strict';
  function init(panel) {
    var btn = panel.querySelector('.lazy-toggle');
    var body = panel.querySelector('.lazy-body');
    var url = panel.getAttribute('data-lazy-url');
    if (!btn || !body || !url) return;
    var state = 'idle'; // idle | loading | loaded

    function setOpen(open) {
      body.hidden = !open;
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      btn.textContent = open ? 'Hide' : 'Show';
    }
    function fail() {
      state = 'idle';
      body.textContent = '';
      var wrap = document.createElement('div');
      wrap.className = 'panel-body muted';
      wrap.textContent = 'Could not load this section. ';
      var retry = document.createElement('button');
      retry.type = 'button'; retry.className = 'btn-secondary btn-sm'; retry.textContent = 'Retry';
      retry.addEventListener('click', load);
      wrap.appendChild(retry);
      body.appendChild(wrap);
    }
    function load() {
      if (state === 'loading') return;
      state = 'loading';
      body.textContent = '';
      var wait = document.createElement('div');
      wait.className = 'panel-body muted'; wait.textContent = 'Loading…';
      body.appendChild(wait);
      fetch(url, { credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' } })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
        .then(function (html) {
          body.innerHTML = html;   // server-rendered, auto-escaped Django template output from our own origin
          state = 'loaded';
          document.dispatchEvent(new CustomEvent('lazy-panel-loaded', { detail: { panel: panel } }));
        })
        .catch(fail);
    }
    btn.addEventListener('click', function () {
      var willOpen = body.hidden;
      setOpen(willOpen);
      if (willOpen && state === 'idle') load();
    });
  }
  Array.prototype.forEach.call(document.querySelectorAll('.lazy-panel'), init);
})();
