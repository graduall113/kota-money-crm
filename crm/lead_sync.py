"""
Lead -> n8n -> Google Sheets synchronisation queue.

The Django database is the source of truth. Google Sheets is a synchronised COPY,
keyed by the permanent Lead ID (``Lead.display_id``, e.g. ``KM-1050``) - never by
phone number. Every relevant change to a Lead becomes one ``LeadSyncEvent`` that is
delivered to the n8n webhook; n8n's Google Sheets node ("Append or Update Row",
matching on the ``Lead ID`` column) then updates the SAME row, or appends one only
when that Lead ID is not in the Sheet yet.

Design rules (all enforced here, not by convention):

* A Lead save NEVER fails because of sync. Queueing is wrapped in its own savepoint,
  delivery happens after the DB commit, and every delivery error is caught.
* Payloads are built when the event is PROCESSED, from the live Lead, so a retry can
  never overwrite the Sheet with stale data.
* At most one PENDING event per Lead (rapid edits coalesce) and at most one
  PROCESSING event per Lead (two sends for one Lead never run in parallel), via the
  partial unique constraints on ``LeadSyncEvent``.
* Automatic events are skipped when nothing synced changed since the last send n8n
  accepted (content hash). ``send_lead_to_n8n()`` (Add Lead / Convert) always sends.
* Failures are retried with back-off (1, 5, 15, 60, 180 min; 6 attempts), recorded on
  the event and on ``Lead.n8n_sync_status / n8n_error / n8n_last_sync``.
"""
import logging
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.utils import timezone

from .models import Lead, LeadSyncEvent

logger = logging.getLogger("crm.n8n")

MAX_ATTEMPTS = 6
BACKOFF_MINUTES = (1, 5, 15, 60, 180)  # wait after failed attempt 1..5
STALE_PROCESSING = timedelta(minutes=5)  # a worker that died mid-send
BUSY = "Another sync for this lead is already in progress."

# Lead fields whose change must reach the Sheet. Anything else (n8n_* bookkeeping,
# last_contacted, checklist ...) never queues an event.
SYNC_FIELDS = frozenset({
    "customer_name", "contact_number", "work_profile", "income", "requirement", "loan_amount",
    "bank_calling", "status", "form_date", "reference_by_name", "assigned_to_name",
    "reference_by", "assigned_to", "email", "city", "source", "interest", "next_followup_date",
    "contact",
})


# ------------------------------------------------------------------ helpers
def touches_sync_fields(update_fields):
    """True when a save() may have changed something the Sheet shows."""
    if update_fields is None:  # full save()
        return True
    names = {f[:-3] if f.endswith("_id") else f for f in update_fields}
    return bool(names & SYNC_FIELDS)


def _reference_id(pk):
    return f"{Lead.LEAD_ID_PREFIX}-{pk}"


def _merge_reasons(*parts):
    seen = []
    for part in parts:
        for r in str(part or "").split(","):
            r = r.strip()
            if r and r not in seen:
                seen.append(r)
    return ",".join(seen)[:300]


# ------------------------------------------------------------------ queueing
def enqueue_lead_sync(lead_or_pk, reason, force=False):
    """
    Record "this Lead must be synced". Coalesces into the Lead's existing PENDING event
    instead of creating a second one. Never raises; returns the event or None.
    Safe to call inside the transaction that saved the Lead (uses a savepoint).
    """
    pk = getattr(lead_or_pk, "pk", lead_or_pk)
    if pk is None:
        return None
    try:
        with transaction.atomic():
            pending = LeadSyncEvent.objects.filter(lead_id=pk, status=LeadSyncEvent.PENDING).first()
            if pending is None:
                try:
                    with transaction.atomic():
                        return LeadSyncEvent.objects.create(
                            lead_id=pk, lead_reference_id=_reference_id(pk),
                            idempotency_key=f"lead-{_reference_id(pk)}", reasons=_merge_reasons(reason), force=force,
                        )
                except IntegrityError:  # a concurrent request queued one first -> coalesce into it
                    pending = LeadSyncEvent.objects.filter(lead_id=pk, status=LeadSyncEvent.PENDING).first()
                    if pending is None:
                        raise
            LeadSyncEvent.objects.filter(pk=pending.pk, status=LeadSyncEvent.PENDING).update(
                reasons=_merge_reasons(pending.reasons, reason),
                force=pending.force or force,
                next_attempt_at=timezone.now(),  # fresh edit -> try now, don't wait out an old back-off
            )
            pending.refresh_from_db()
            return pending
    except Exception:  # noqa: BLE001 - sync bookkeeping must never break a Lead save
        logger.exception("Could not queue lead sync for lead %s (%s)", pk, reason)
        return None


def enqueue_many(pks, reason, chunk=1000):
    """Bulk version for queryset.update() paths (bulk assign / status / follow-up). Returns #new events."""
    pks = list(pks)
    created = 0
    try:
        for i in range(0, len(pks), chunk):
            part = pks[i:i + chunk]
            with transaction.atomic():
                existing = set(LeadSyncEvent.objects.filter(status=LeadSyncEvent.PENDING, lead_id__in=part)
                               .values_list("lead_id", flat=True))
                if existing:
                    LeadSyncEvent.objects.filter(status=LeadSyncEvent.PENDING, lead_id__in=existing).update(
                        next_attempt_at=timezone.now())
                new = [LeadSyncEvent(lead_id=pk, lead_reference_id=_reference_id(pk),
                                     idempotency_key=f"lead-{_reference_id(pk)}", reasons=_merge_reasons(reason))
                       for pk in part if pk not in existing]
                LeadSyncEvent.objects.bulk_create(new, ignore_conflicts=True)
                created += len(new)
    except Exception:  # noqa: BLE001
        logger.exception("Could not queue bulk lead sync (%s)", reason)
    return created


# ------------------------------------------------------------------ processing
def _claim(ev):
    """Atomically pending -> processing. False if someone else got it / the Lead is mid-send."""
    try:
        with transaction.atomic():
            n = LeadSyncEvent.objects.filter(pk=ev.pk, status=LeadSyncEvent.PENDING).update(
                status=LeadSyncEvent.PROCESSING, started_at=timezone.now(), attempts=F("attempts") + 1)
    except IntegrityError:  # uniq_processing_sync_per_lead: this Lead is already being sent
        return False
    return n == 1


def _record_lead(lead_pk, instance, **fields):
    """Write n8n_* bookkeeping without firing signals or bumping updated_at; mirror onto the caller's object."""
    Lead.objects.filter(pk=lead_pk).update(**fields)
    if instance is not None:
        for k, v in fields.items():
            setattr(instance, k, v)


def _requeue_or_supersede(ev, when, error, http_status):
    """Put a failed/stale event back to pending, unless a newer pending event already exists for the Lead."""
    try:
        with transaction.atomic():
            LeadSyncEvent.objects.filter(pk=ev.pk).update(
                status=LeadSyncEvent.PENDING, next_attempt_at=when, last_error=error[:2000],
                http_status=http_status, started_at=None)
    except IntegrityError:
        LeadSyncEvent.objects.filter(pk=ev.pk).update(
            status=LeadSyncEvent.FAILED, finished_at=timezone.now(), http_status=http_status,
            last_error=(f"Superseded by a newer sync event. Last error: {error}")[:2000])


def _fail(ev, instance, error, http_status=None):
    now = timezone.now()
    _record_lead(ev.lead_id, instance, n8n_sync_status=Lead.N8N_SYNC_FAILED, n8n_error=error[:2000], n8n_last_sync=now)
    if ev.attempts >= MAX_ATTEMPTS:
        LeadSyncEvent.objects.filter(pk=ev.pk).update(
            status=LeadSyncEvent.FAILED, finished_at=now, last_error=error[:2000], http_status=http_status)
        logger.error("Lead sync %s gave up after %s attempts: %s", ev.lead_reference_id, ev.attempts, error)
        return
    delay = BACKOFF_MINUTES[min(ev.attempts, len(BACKOFF_MINUTES)) - 1]
    logger.warning("Lead sync %s attempt %s failed, retry in %s min: %s", ev.lead_reference_id, ev.attempts, delay, error)
    _requeue_or_supersede(ev, now + timedelta(minutes=delay), error, http_status)


def _process_event(ev, instance=None):
    """Returns (ok, error, sent). `sent` is False when nothing was POSTed (busy / skipped)."""
    from . import n8n_integration as n8n
    from .settings_store import get_n8n_webhook_url

    if not _claim(ev):
        return False, BUSY, False
    try:
        ev.refresh_from_db()
    except LeadSyncEvent.DoesNotExist:  # Lead deleted meanwhile (cascade)
        return True, "", False
    lead = Lead.objects.select_related("assigned_to", "reference_by").filter(pk=ev.lead_id).first()
    if lead is None:
        LeadSyncEvent.objects.filter(pk=ev.pk).update(
            status=LeadSyncEvent.SKIPPED, finished_at=timezone.now(), last_error="Lead no longer exists.")
        return True, "", False

    payload = n8n.build_sync_payload(lead, ev.reasons)
    content_hash = n8n.payload_content_hash(payload)
    if (not ev.force and lead.n8n_sync_status == Lead.N8N_SYNC_SUCCESS and lead.n8n_payload_hash == content_hash):
        LeadSyncEvent.objects.filter(pk=ev.pk).update(
            status=LeadSyncEvent.SKIPPED, finished_at=timezone.now(), last_error="")
        return True, "", False

    url = get_n8n_webhook_url()
    if not any(r in n8n.CREATION_REASONS for r in ev.reasons.split(",")):
        # Optional: updates can go to a separate n8n workflow that ONLY upserts the Sheet
        # (so edits never re-trigger the new-lead Gmail / WhatsApp branch).
        url = getattr(settings, "N8N_LEAD_UPDATE_WEBHOOK_URL", "") or url
    if not url:
        error = "N8N_LEAD_WEBHOOK_URL is not configured."
        logger.error("n8n sync skipped for lead %s: %s", lead.pk, error)
        _fail(ev, instance, error)
        return False, error, False

    logger.info("Sending lead %s (%s) to n8n [%s] attempt %s", lead.pk, payload["lead_reference_id"], ev.reasons, ev.attempts)
    try:
        response = n8n.post_payload(url, payload)
    except Exception as exc:  # noqa: BLE001 - network error, non-2xx, anything: recorded, never raised
        error = str(exc)[:2000]
        _fail(ev, instance, error, getattr(getattr(exc, "response", None), "status_code", None))
        return False, error, True

    now = timezone.now()
    LeadSyncEvent.objects.filter(pk=ev.pk).update(
        status=LeadSyncEvent.SUCCESS, finished_at=now, last_error="", http_status=response.status_code)
    _record_lead(lead.pk, instance, n8n_sync_status=Lead.N8N_SYNC_SUCCESS, n8n_error="", n8n_last_sync=now,
                 n8n_payload_hash=content_hash)
    logger.info("n8n webhook accepted lead %s (HTTP %s)", lead.pk, response.status_code)
    return True, "", True


def process_event(ev, instance=None):
    ok, error, _sent = _process_event(ev, instance)
    return ok, error


def sync_lead_now(lead, reason="created"):
    """
    Queue + deliver one Lead immediately, in the caller's thread (used by Add New Lead and
    Convert Contact so the user still gets the "automation started / pending" message).
    Always sends (force). Returns (success, error_message); never raises.
    """
    from .settings_store import get_bool

    if lead.pk is None:
        return False, "Lead has not been saved yet, so it has no Lead ID."
    if not get_bool("n8n_enabled"):
        return False, "n8n integration is switched off in Settings."
    try:
        ev = enqueue_lead_sync(lead, reason, force=True)
        if ev is None:
            return False, "Could not queue the sync; it will be retried."
        return process_event(ev, instance=lead)
    except Exception as exc:  # noqa: BLE001 - belt and braces: the caller promises the user a clean message
        logger.exception("Unexpected n8n error for lead %s", lead.pk)
        return False, str(exc)[:2000]


# ------------------------------------------------------------------ draining (retry driver)
def recover_stale():
    """Events left 'processing' by a worker that died are put back in the queue."""
    cutoff = timezone.now() - STALE_PROCESSING
    for ev in LeadSyncEvent.objects.filter(status=LeadSyncEvent.PROCESSING, started_at__lt=cutoff):
        _requeue_or_supersede(ev, timezone.now(), "Worker stopped mid-send; retrying.", ev.http_status)


def drain_due_events(limit=50, min_interval=None):
    """
    Send every pending event that is due, oldest first. Paced (default 1 send/second,
    CRM_LEAD_SYNC_MIN_INTERVAL) so a bulk operation cannot exceed Google Sheets' write quota.
    Returns the number of events handled.
    """
    from .settings_store import get_bool

    if not get_bool("n8n_enabled"):
        return 0
    if min_interval is None:
        min_interval = float(getattr(settings, "CRM_LEAD_SYNC_MIN_INTERVAL", 1.0))
    recover_stale()
    ids = list(LeadSyncEvent.objects.filter(status=LeadSyncEvent.PENDING, next_attempt_at__lte=timezone.now())
               .order_by("id").values_list("pk", flat=True)[:limit])
    handled, sent_before = 0, False
    for pk in ids:
        ev = LeadSyncEvent.objects.filter(pk=pk, status=LeadSyncEvent.PENDING).first()
        if ev is None:
            continue
        if sent_before and min_interval:
            time.sleep(min_interval)
        _ok, error, sent = _process_event(ev)
        if error != BUSY:
            handled += 1
        sent_before = sent
    return handled


_drain_lock = threading.Lock()
_drain_again = threading.Event()


def _drain_worker():
    if not _drain_lock.acquire(blocking=False):
        _drain_again.set()  # a worker is running in this process; ask it to make another pass
        return
    try:
        while True:
            _drain_again.clear()
            drain_due_events()
            if not _drain_again.is_set():
                break
    except Exception:  # noqa: BLE001
        logger.exception("Lead sync drain failed")
    finally:
        _drain_lock.release()
        connection.close()  # a worker thread owns its DB connection
    if _drain_again.is_set():
        schedule_drain()


def schedule_drain():
    """
    Deliver queued events without blocking the request: a daemon thread (same approach as the
    bulk jobs). Runs inline when CRM_JOBS_INLINE is on (tests). Does nothing when
    CRM_LEAD_SYNC_AUTODISPATCH is off (tests / a dedicated `process_lead_sync` worker).
    Pending retries that are not due yet are picked up by the next drain or by
    `python manage.py process_lead_sync` (run it every minute from cron for guaranteed retries).
    """
    if not getattr(settings, "CRM_LEAD_SYNC_AUTODISPATCH", True):
        return
    if getattr(settings, "CRM_JOBS_INLINE", False):
        drain_due_events(min_interval=0)
        return
    threading.Thread(target=_drain_worker, daemon=True, name="lead-sync-drain").start()
