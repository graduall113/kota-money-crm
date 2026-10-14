/* Searchable "Assigned To" box. Two modes, same component:
 *  - FILTER (All Leads / My Leads: .assignee-filter[data-autosubmit]): choosing a suggestion submits the filter form;
 *  - FIELD  (Add / Edit Lead: .assignee-filter.assignee-field): choosing only fills the input. Suggestions are a
 *    convenience - ANY name may be typed and saved, whether or not the person is registered CRM staff.
 * (see below for the details common to both)
 * - debounced (250 ms) requests, one in flight at a time (earlier ones are aborted), per-page memo so retyping the
 *   same text or reopening the list never re-requests;
 * - keyboard (Up/Down/Enter/Esc) and touch friendly (44px rows), ARIA combobox/listbox;
 * - choosing a suggestion fills the box with the canonical name and submits the filter form;
 * - the box is a plain text input named "assignee", so the form works (typed text is matched server-side) even if
 *   this script fails to load. */
(function () {
  'use strict';
  Array.prototype.forEach.call(document.querySelectorAll('.assignee-filter'), init);

  function init(root) {
  var url = root.getAttribute('data-suggest-url');
  var input = root.querySelector('input[type="text"]');
  var list = root.querySelector('.assignee-list');
  var clear = root.querySelector('.assignee-clear');
  if (!input || !list || !url) return;
  var form = input.form;
  var fieldMode = root.classList.contains('assignee-field');
  var memo = Object.create(null), timer = null, controller = null, active = -1, items = [], seq = 0;

  function norm(v) { return (v || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
  function open() { list.hidden = false; input.setAttribute('aria-expanded', 'true'); }
  function close() { list.hidden = true; input.setAttribute('aria-expanded', 'false'); active = -1; }
  function syncClear() { if (clear) clear.hidden = !input.value; }

  function setActive(i) {
    var nodes = list.querySelectorAll('[role="option"]');
    Array.prototype.forEach.call(nodes, function (n, k) {
      var on = k === i;
      n.classList.toggle('is-active', on);
      n.setAttribute('aria-selected', on ? 'true' : 'false');
      if (on) { input.setAttribute('aria-activedescendant', n.id); n.scrollIntoView({ block: 'nearest' }); }
    });
    if (i < 0) input.removeAttribute('aria-activedescendant');
    active = i;
  }

  function choose(name) {
    input.value = name;
    syncClear();
    close();
    if (form && !fieldMode) form.submit();
  }

  function render(results, query) {
    items = results;
    list.textContent = '';
    if (!results.length) {
      var li = document.createElement('li');
      li.className = 'assignee-empty';
      li.setAttribute('role', 'option');
      li.setAttribute('aria-disabled', 'true');
      li.textContent = fieldMode
        ? (query ? 'No existing match. Keep typing: any name can be used.' : 'No assignees yet. Type a name.')
        : (query ? 'No matching assignee. Press Apply to search the text as typed.' : 'No assignees yet.');
      list.appendChild(li);
    }
    results.forEach(function (r, i) {
      var li = document.createElement('li');
      li.id = 'assignee-opt-' + i;
      li.setAttribute('role', 'option');
      li.setAttribute('aria-selected', 'false');
      var name = document.createElement('span');
      name.className = 'assignee-name';
      name.textContent = r.name;
      var meta = document.createElement('span');
      meta.className = 'assignee-meta';
      meta.textContent = (r.match === 'similar' ? 'Did you mean? · ' : '') + r.count + ' lead' + (r.count === 1 ? '' : 's');
      li.appendChild(name); li.appendChild(meta);
      // mousedown (not click) so the input's blur handler cannot close the list first; works for touch as well.
      li.addEventListener('mousedown', function (e) { e.preventDefault(); choose(r.name); });
      list.appendChild(li);
    });
    active = -1;
    open();
  }

  function fetchSuggestions(query) {
    var key = norm(query);
    if (memo[key]) { render(memo[key], key); return; }
    if (controller) controller.abort();
    controller = window.AbortController ? new AbortController() : null;
    var mine = ++seq;
    fetch(url + '?q=' + encodeURIComponent(query), {
      credentials: 'same-origin', headers: { 'Accept': 'application/json' }, signal: controller ? controller.signal : undefined
    }).then(function (r) { return r.ok ? r.json() : { results: [] }; })
      .then(function (data) {
        if (mine !== seq) return;           // a newer request superseded this one
        memo[key] = data.results || [];
        render(memo[key], key);
      }).catch(function () { /* aborted or offline: leave the typed text usable */ });
  }

  input.addEventListener('input', function () {
    syncClear();
    clearTimeout(timer);
    timer = setTimeout(function () { fetchSuggestions(input.value); }, 250);
  });
  input.addEventListener('focus', function () {
    if (!list.hidden) return;
    fetchSuggestions(input.value);
  });
  input.addEventListener('blur', function () { setTimeout(close, 120); });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (list.hidden) { fetchSuggestions(input.value); return; }
      e.preventDefault();
      var n = items.length;
      if (!n) return;
      setActive(e.key === 'ArrowDown' ? (active + 1) % n : (active <= 0 ? n - 1 : active - 1));
    } else if (e.key === 'Enter') {
      if (!list.hidden && active >= 0 && items[active]) { e.preventDefault(); choose(items[active].name); }
    } else if (e.key === 'Escape') {
      close();
    }
  });
  if (clear) clear.addEventListener('click', function () {
    input.value = ''; syncClear(); close();
    if (form && !fieldMode) form.submit();   // an empty "assignee" is ignored by the server: other filters stay applied
    else input.focus();
  });
  document.addEventListener('click', function (e) { if (!root.contains(e.target)) close(); });
  }
})();
