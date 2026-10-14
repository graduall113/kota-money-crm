# Mobile / tablet responsive pass (Oct 2026)

> **Verification status (read first).** Django, PostgreSQL and the package index were unavailable in the authoring
> sandbox, so **`manage.py check`, migrations and the Django test-suite were NOT run.** Responsive behaviour was tested
> in headless Chromium against the project's *real templates rendered through a Jinja2 shim with mock data*
> (8 pages: My Leads, Contacts, Lead detail, Staff Attendance, Staff list, Audit log, Analytics, Dashboard).
> That is an approximation of Django rendering, not a live run. **Please smoke-test on a deployed/staging copy.**

## How it works
* `static/css/mobile.css` (new, linked after `auth.css` in `base.html`): every rule is inside a `max-width` media query
  (<= 1023px), so >= 1024px is untouched. No `overflow-x:hidden` on body.
* `static/js/responsive_tables.js` (new, `defer`): copies each `<th>` text to its cells as `data-label` and adds `rt-card`
  to a `table.data-table` when: viewport <= 767px (all tables); <= 1023px and the table has 7+ columns or would otherwise
  scroll sideways. Rows/forms/checkboxes are not moved, so bulk actions, select-all, delete confirmations are unchanged.
  ARIA table roles are restored while in card mode. Without JS, tables keep their existing scroll wrapper.
* Lead list and Contact list (`partials/lead_table.html`, `contacts/contact_list.html`): class hooks only
  (`lead-cards`, `c-check`, `c-name`, `c-status`, `c-id`, `c-actions`, `c-full`) plus an `aria-label` on row checkboxes.
  Card layout follows the reference: bold title, status pill + ID, compact "Label: value" rows, tap-sized actions.
* Detail pages (<= 767px): `.kv-grid` becomes compact inline "Label: value" rows; panels use tighter padding.
  Existing single-column stacking (<= 1080px) is unchanged.

## Results (Chromium, mock data)
Inner/page horizontal scroll before -> after at 320/375/430/768/820: tables inside `.table-scroll` scrolled sideways
(1 per list page) -> 0 on all 8 tested pages. Page-level overflow: none before or after.
Desktop (1024/1280/1366/1920): full-page screenshots pixel-identical before vs after on all 8 pages.

## Page checklist
Legend: **T** = rendered and measured in Chromium (mock data) · **G** = covered by the same generic table/card or
existing responsive rules, not individually rendered.
| Area | Pages | Status |
|---|---|---|
| Auth | login, register, password reset x4, profile | G - own `auth.css` already responsive; no tables |
| Dashboard | dashboard | T |
| Leads | all_leads, my_leads (lead_table), lead_form, lead_detail, followups, lead_confirm_delete | my_leads T, lead_detail T, others G |
| Contacts | contact_list, contact_detail, contact_form | contact_list T, others G |
| Segments | list/detail/form/delete | G |
| Staff | staff_list, staff_detail, staff_form, staff_remove | staff_list T, others G |
| Attendance | attendance, admin_dashboard, admin_detail, admin_events, admin_devices, activity_board | admin_dashboard T, others G |
| Holidays | list/form/delete | G |
| Calling | dashboard, device_list, privileged_numbers | G |
| Imports / jobs | list, new, map, review, detail, errors, job_detail | G |
| Reports / analytics / audit | reports, performance, documents, audit_log | audit T, performance T, others G |
| Settings | settings, settings_documents, assign_page, bulk_confirm, 403 | G |
| Shell | sidebar drawer, topbar, user menu, confirm modal | drawer capped to 86vw with dim backdrop, 44px targets (CSS only; not interaction-tested) |

## Documented exceptions / risks
* 1024-~1300px (desktop breakpoint): wide tables (12-13 columns) still scroll inside their own `.table-scroll` wrapper,
  exactly as before - intentionally unchanged ("desktop stays as it is").
* No-JS browsers keep the old contained scroll on tables.
* Cards show every column of the original table (no field removed); order of facts follows the table's column order.
* `lead_form.html` embeds an n8n iframe (`min-height` 900-1000px, `width:100%`); not changed.
* Pages marked G were not individually rendered; any odd cell content (nested forms/inputs) may need a small tweak.

## Lazy-loading audit
| Item | Finding / change |
|---|---|
| Images / `<img>` / charts / canvas | None exist in templates (icons are inline SVG). Nothing to lazy-load; no fake work added. |
| Off-screen rendering | `content-visibility:auto` + `contain-intrinsic-size` on mobile cards and below-the-fold panels (<= 1023px only): browser skips layout/paint until near the viewport. Data is still server-rendered, permissions unchanged. |
| Lists | Already backend-paginated (prior audit); unchanged. Exports/bulk ops unaffected. |
| JS | New script uses `defer`; `staff_activity.js` already deferred; heartbeat already visibility-aware; fonts already non-blocking. |
| Tabs / secondary data on demand | Not done: would need new endpoints/permission checks that cannot be tested without Django. Timeline/history are already capped (200/100 rows). |
| ORM | `select_related` already used on list/dashboard querysets (prior audit); no query changes made in this pass. |
| Search debounce | Search is a submitted form, no per-keystroke requests. |

## Local test / deploy
```
pip install -r requirements.txt
python manage.py check && python manage.py makemigrations --check --dry-run && python manage.py test
python manage.py collectstatic --no-input && python manage.py runserver
```
Then open Chrome DevTools device mode (320, 375, 430, 768, 820) on My Leads, Contacts, a lead, Staff Attendance.
Deploy via your normal Render flow (`build.sh` runs `collectstatic`). Nothing was deployed. No migrations added.
