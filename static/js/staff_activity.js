/*
 * Kota Money CRM - mobile CRM activity reporter.
 *
 * What it reports (to POST /activity/signal/, form-encoded, CSRF token in the body):
 *   kind=heartbeat   "this page is alive"            (every ~60 s while visible; proves nothing about work)
 *   kind=activity    "the person just used the CRM"  (real touches / typing / scrolling / navigation, max 1 per 30 s)
 *   kind=visibility  "this page became hidden/visible" (Page Visibility API; sendBeacon when hiding)
 * The server decides everything (who, when, which state). The page never sends a user id or a time.
 *
 * Deliberately NOT done: guessing what other app is open. A hidden page is only "CRM in background".
 * Heartbeats, polling and timers never count as activity; only trusted user-input events do.
 */
(function () {
  'use strict';
  var root = document.getElementById('actCfg');
  if (!root) return;
  var URL_ = root.dataset.url, TOKEN = root.dataset.token;
  var HEARTBEAT_MS = (+root.dataset.heartbeat || 60) * 1000;
  var REPORT_MIN_MS = (+root.dataset.reportMin || 30) * 1000;
  var warnEl = document.getElementById('actWarn');
  var warnText = document.getElementById('actWarnText');
  var chip = document.getElementById('actChip');
  var lastSent = 0, dirty = false, trailing = null, inflight = false, failures = 0;
  var warnDismissedUntil = 0;

  function visibility() { return document.visibilityState === 'hidden' ? 'hidden' : 'visible'; }

  function body(kind) {
    var p = new URLSearchParams();
    p.set('kind', kind); p.set('visibility', visibility()); p.set('csrfmiddlewaretoken', TOKEN);
    return p;
  }

  function send(kind) {
    if (inflight && kind !== 'visibility') { if (kind === 'activity') dirty = true; return; }   // keep the activity for the next ping
    inflight = true; lastSent = Date.now();
    fetch(URL_, {
      method: 'POST', body: body(kind), credentials: 'same-origin', keepalive: true,
      headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' }
    }).then(function (r) {
      if (r.status === 401 || r.status === 403) { stop(); return null; }   // logged out / locked: stop quietly
      return r.ok ? r.json() : Promise.reject(new Error('http ' + r.status));
    }).then(function (d) { failures = 0; if (d) render(d); })
      .catch(function () { failures++; })                                   // one missed ping is normal on mobile data
      .then(function () { inflight = false; });
  }

  // Best-effort "I'm being hidden" note. Mobile browsers do not guarantee this fires; the server's
  // heartbeat timeout is what actually detects a closed / locked / offline phone.
  function beaconHidden() {
    try { if (navigator.sendBeacon) { navigator.sendBeacon(URL_, body('visibility')); return; } } catch (e) {}
    send('visibility');
  }

  var timer = setInterval(tick, HEARTBEAT_MS);
  function stop() { clearInterval(timer); }
  function tick() {
    if (visibility() !== 'visible') return;
    if (dirty && Date.now() - lastSent >= REPORT_MIN_MS) { dirty = false; send('activity'); }
    else send('heartbeat');
  }

  // ---- meaningful activity: only real (trusted) user input on the CRM, never inside the warning sheet
  function onInteract(e) {
    if (!e.isTrusted || visibility() !== 'visible') return;
    if (e.target && e.target.closest && e.target.closest('[data-activity-ignore]')) return;
    dirty = true;
    var wait = REPORT_MIN_MS - (Date.now() - lastSent);
    if (wait <= 0) { dirty = false; send('activity'); }
    else if (!trailing) trailing = setTimeout(function () { trailing = null; if (dirty && visibility() === 'visible') { dirty = false; send('activity'); } }, wait);
  }
  ['pointerdown', 'touchstart', 'keydown', 'input', 'change'].forEach(function (t) {
    document.addEventListener(t, onInteract, { passive: true, capture: true });
  });
  document.addEventListener('scroll', onInteract, { passive: true, capture: true });

  // A real page navigation (opening a lead, a list, a form ...) is CRM use. A reload by the person is too.
  window.addEventListener('load', function () { if (visibility() === 'visible') { dirty = false; send('activity'); } });

  // ---- mobile lifecycle
  document.addEventListener('visibilitychange', function () {
    if (visibility() === 'hidden') beaconHidden(); else send('visibility');   // visible again: tells the server, NOT activity
  });
  window.addEventListener('pagehide', beaconHidden);                          // best effort only
  window.addEventListener('pageshow', function (e) { if (e.persisted) send('visibility'); });   // back/forward cache restore
  window.addEventListener('online', function () { send('heartbeat'); });      // network came back: reconnect now
  document.addEventListener('resume', function () { send('heartbeat'); });    // Chrome "unfrozen" page

  // ---- UI (server numbers only; the page never works out "15 minutes" by itself)
  function render(d) {
    if (chip) { chip.textContent = d.label || ''; chip.dataset.state = d.state || ''; }
    if (!warnEl) return;
    var show = d.warn && visibility() === 'visible' && Date.now() > warnDismissedUntil;
    if (show) {
      var m = Math.max(1, Math.ceil((d.seconds_to_inactive || 0) / 60));
      warnText.textContent = 'You have not used the CRM recently. You may be marked inactive in about ' + m + ' minute' + (m > 1 ? 's' : '') + '. Please continue working in the CRM.';
      warnEl.hidden = false;
    } else if (!d.warn || d.staff_status === 'inactive') {
      warnEl.hidden = true;
    }
  }
  var okBtn = document.getElementById('actWarnOk');
  if (okBtn) okBtn.addEventListener('click', function () {
    // Closes the notice ONLY. It reports nothing: just opening the CRM or a record counts as activity.
    warnDismissedUntil = Date.now() + 90 * 1000; warnEl.hidden = true;
  });

})();
