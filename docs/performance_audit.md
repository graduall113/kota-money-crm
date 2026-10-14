# Performance audit (Oct 2026)

> **Status: code changes were verified by `compileall`, a static undefined-name scan and `node --check` only.**
> Django/PostgreSQL could not be installed in the authoring sandbox (package index blocked), so **no Django check,
> migration check, existing test or new test was executed.** Run `python manage.py check`, `python manage.py test`
> and `python manage.py makemigrations --check --dry-run` before deploying. No migration is added by this change.

## What was already good (left alone)
Server-side pagination on All Leads / My Leads / Contacts / Segment detail / Import lists / Audit log / Attendance
tables / Holidays; streamed CSV export (`iterator(chunk_size=2000)`); bulk operations and imports already run in worker
threads with progress rows; Lead -> n8n -> Sheets already goes through a retrying queue (`LeadSyncEvent`) drained in a
background thread or by `manage.py process_lead_sync`; attendance state is memoised per request; extensive DB indexes.

## Changes
| Area | Change | File(s) |
|---|---|---|
| ORM | Settings table read once per HTTP request instead of once per `get_setting()` call (was several SELECTs per page: `visible_leads`, context processor, export check...). Snapshot is discarded at the end of every request, dropped on any Setting write, and never used outside a request. | `settings_store.py`, `apps.py` |
| ORM | Dashboard: ~16 COUNT queries -> 3 aggregates (leads, contacts, + doc-status). Same numbers. | `views.py` |
| ORM | My Leads stat cards: 6 COUNTs -> 1. Follow-ups page: 6 COUNTs -> 2. | `views.py` |
| ORM | Staff detail page computed performance for *every* telecaller to show one; now scoped to that user. | `analytics.py`, `views_admin.py` |
| ORM | Segment list (staff view) loaded every visible contact id into Python and ran an `IN (...)` per segment; now one subquery-annotated query. | `views_segments.py` |
| ORM | N+1 removed: bulk-action confirm (owner lookup per group, user lookup per staff id), staff activity board (presence per person). | `views_contacts.py`, `views_activity.py` |
| ORM / bug | Source filter dropdown used `DISTINCT` + the model's default `-created_at` ordering, so it returned repeated values from the 100 newest rows; now truly distinct + alphabetical. Same for contacts. | `views.py`, `views_contacts.py` |
| ORM / bug | Bulk-action confirmation "from" counts grouped by the filter's sort column (wrong per-owner counts); `.order_by()` added. | `views_contacts.py` |
| Pagination | Staff list is now paginated (50/page, stable order, existing pagination partial). | `views.py`, `staff_list.html` |
| Caching | `cache_utils.py` (fail-safe wrapper) + `CACHES` (LocMem; DummyCache under tests). Cached: source/batch dropdown options (per user, 5 min / 60 s) and admin analytics (60 s). Invalidated by model signals, finished bulk jobs and imports. | `cache_utils.py`, `signals.py`, `jobs.py`, `importer.py`, `settings.py` |
| Frontend | Import / job progress polling slows to 8 s while the tab is hidden. Google Fonts stylesheet no longer render-blocking (`media=print` swap + `<noscript>`; `display=swap` was already set). | `job_detail.html`, `import_detail.html`, `base.html`, `auth_base.html` |
| Tests | `crm/tests_performance.py` (not executed here). | |

## Caching strategy and limits
* Backend: per-process `LocMemCache` - no Redis, no cost. With N gunicorn workers there are N independent caches, so
  every entry is short-lived and `bump()` invalidation only reaches the current process (others expire by TTL).
* **Never cached:** lead/contact rows, statuses, assignments, permissions, attendance state, settings (those use a
  per-request snapshot only), anything from `access.visible_*()` results.
* Keys include user id (dropdowns) or the exact report parameters (analytics, admin-only).
* Worst-case staleness: analytics 60 s; batch dropdown 60 s; source dropdown 5 min (labels only).
* If the cache raises, the value is computed fresh (page still works).

## Lazy-loading / frontend audit
| Page / component | Result |
|---|---|
| Login, register, password reset, profile (`auth_base`) | Font stylesheet made non-blocking. No images/scripts to defer. Auth/security untouched. |
| Base layout (all pages) | Same font change. `staff_activity.js` already `defer`. Inline scripts are tiny and at end of body - left. `auth.css` (3 KB, cached) left: shared selectors may be used by `profile.html`. |
| Dashboard, Reports | No charts/images exist (plain tables/cards). Nothing to lazy-load; server-side numbers reduced instead. |
| Leads / Contacts / Segments lists | Already server-paginated (<=200 rows/page); search is a submitted form (no per-keystroke AJAX) so debounce does not apply. "More filters" is a native `<details>` already collapsed. |
| Lead / contact detail | Timeline/history already capped (`[:200]`, `[:100]`); collapsing them would change the UI, so left. |
| Import detail, Job detail | Polling made visibility-aware. |
| Staff, Attendance, Holidays, Calling, Audit, Settings | Paginated or small bounded lists; no heavy hidden tabs (settings sections are separate requests). |
| `<img>` tags | None exist in the project, so `loading="lazy"` has nothing to apply to. |

## Background tasks audit
* Existing infrastructure kept: `BackgroundJob` threads (>1,500 records; <=1,500 run inline for instant feedback), import
  phases in daemon threads, `LeadSyncEvent` queue with retries/back-off + idempotent Sheet upsert.
* **Not changed:** `lead_create` still calls n8n synchronously (<=15 s timeout) because the success/"sync pending"
  message the user sees depends on that result and moving it would change user-visible semantics. Making it
  asynchronous is possible with the existing queue (`enqueue_lead_sync` + `schedule_drain`) but is a product decision.
* Emails: only password reset (console backend) - nothing to move. Exports are streamed; `.xlsx` is built in memory
  (capped at 200,000 rows) and is the one remaining memory-heavy path.
* No Celery/Redis/worker was added or assumed. Thread-based jobs die if the dyno restarts mid-run (pre-existing);
  a Render cron running `python manage.py process_lead_sync` every minute is the cheap reliability upgrade.

## Database indexes
No migration added (cannot be verified without a database here). Candidates to evaluate with `EXPLAIN ANALYZE` on
production-sized data: `Lead(status)` for the admin status counts, `Lead(followup_status, next_followup_date)` and
`Contact(followup_status, next_followup_date)` for follow-up buckets. Existing indexes already cover assignee/status,
reference/status, created/updated, phone, source, import batch.

## Remaining risks / unmeasured
* No before/after timings: no Postgres and no Django in the sandbox. Query-count reductions above are by inspection.
* Per-request settings snapshot assumes `request_started`/`request_finished` fire per request (WSGI and ASGI both do).
* `Count(..., filter=...)` aggregates need SQLite >= 3.30 (dev only); PostgreSQL is fine.
