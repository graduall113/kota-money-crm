/* Responsive tables (mobile/tablet only).
 *
 * Progressive enhancement for every `table.data-table`:
 *   1. copies each header's text onto its cells as `data-label` (the CSS prints it as the field name on a card);
 *   2. switches the table into card mode with the `rt-card` class when the viewport is
 *      <= 767px (every table) or <= 1023px (tables with 7+ columns, which cannot be read as a table there);
 *   3. in card mode, restores table semantics with ARIA roles because `display:block` strips them.
 * At >= 1024px no class is added, so the desktop table is byte-for-byte the existing markup + CSS.
 * Without JavaScript nothing changes: tables keep their existing scroll container.
 * Rows, links, forms and checkboxes are never moved or re-created, so bulk actions, delete
 * confirmations and select-all keep working exactly as before. */
(function () {
  'use strict';
  var WIDE_COLS = 7;
  var mqPhone = window.matchMedia('(max-width: 767px)');
  var mqTablet = window.matchMedia('(max-width: 1023px)');
  var tables = Array.prototype.slice.call(document.querySelectorAll('table.data-table'));
  if (!tables.length) return;

  function headerLabels(table) {
    var row = table.querySelector('thead tr');
    var labels = [];
    if (!row) return labels;
    Array.prototype.forEach.call(row.children, function (th) {
      var text = (th.textContent || '').replace(/\s+/g, ' ').trim();
      if (!text) {
        var box = th.querySelector('input[type="checkbox"]');
        text = box ? '' : (th.getAttribute('aria-label') || '');
      }
      var span = parseInt(th.getAttribute('colspan') || '1', 10) || 1;
      for (var i = 0; i < span; i++) labels.push(text);
    });
    return labels;
  }

  function label(table) {
    var labels = headerLabels(table);
    table.setAttribute('data-cols', String(labels.length));
    if (labels.length >= WIDE_COLS) table.classList.add('rt-wide');
    Array.prototype.forEach.call(table.querySelectorAll('tbody tr'), function (tr) {
      var idx = 0;
      Array.prototype.forEach.call(tr.children, function (td) {
        var span = parseInt(td.getAttribute('colspan') || '1', 10) || 1;
        if (span === 1 && labels[idx] !== undefined && !td.hasAttribute('data-label')) {
          td.setAttribute('data-label', labels[idx]);
        }
        idx += span;
      });
    });
  }

  var ROLES = [['table', 'table'], ['thead,tbody', 'rowgroup'], ['tr', 'row'], ['th', 'columnheader'], ['td', 'cell']];
  function setRoles(table, on) {
    ROLES.forEach(function (pair) {
      var nodes = table.matches(pair[0]) ? [table] : [];
      nodes = nodes.concat(Array.prototype.slice.call(table.querySelectorAll(pair[0])));
      nodes.forEach(function (n) {
        if (on) { if (!n.hasAttribute('role')) { n.setAttribute('role', pair[1]); n.setAttribute('data-rt-role', '1'); } }
        else if (n.getAttribute('data-rt-role')) { n.removeAttribute('role'); n.removeAttribute('data-rt-role'); }
      });
    });
  }

  // True when the table, shown as a normal table, would need horizontal scrolling in its wrapper.
  function overflows(table) {
    var had = table.classList.contains('rt-card');
    if (had) { table.classList.remove('rt-card'); }
    var box = table.parentElement;
    var over = !!box && box.scrollWidth > box.clientWidth + 1;
    if (had) { table.classList.add('rt-card'); }
    return over;
  }

  function apply() {
    tables.forEach(function (table) {
      var wide = table.classList.contains('rt-wide');
      var on = mqPhone.matches || (wide && mqTablet.matches);
      if (!on && mqTablet.matches) on = overflows(table);   // few columns but long content (e.g. emails)
      if (on === table.classList.contains('rt-card')) return;
      table.classList.toggle('rt-card', on);
      setRoles(table, on);
    });
  }

  tables.forEach(label);
  document.documentElement.classList.add('rt-ready');
  apply();
  [mqPhone, mqTablet].forEach(function (mq) {
    if (mq.addEventListener) mq.addEventListener('change', apply); else if (mq.addListener) mq.addListener(apply);
  });
})();
