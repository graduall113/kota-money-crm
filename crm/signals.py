from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from . import lead_sync
from .models import CallRecord, Lead, StaffProfile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_staff_profile(sender, instance, created, **kwargs):
    """
    Every User gets a StaffProfile automatically — including superusers
    created with `createsuperuser`, who default to the admin role so
    there's always a working admin account after first setup.
    """
    if created and not hasattr(instance, "staff_profile"):
        StaffProfile.objects.create(
            user=instance,
            role=StaffProfile.ROLE_ADMIN if instance.is_superuser else StaffProfile.ROLE_STAFF,
        )


@receiver(post_save, sender=Lead, dispatch_uid="crm_lead_sync_enqueue")
def queue_lead_sync_on_save(sender, instance, created, raw=False, update_fields=None, **kwargs):
    """
    Any Lead save that can change what the Google Sheet shows queues ONE (coalesced) sync
    event, in the same transaction as the save. Covers Add Lead, Convert Contact, Edit Lead
    (My Leads + All Leads), status / assignment / Reference By / follow-up changes, the
    Django admin ... whatever calls Lead.save(). Bulk queryset.update() paths queue
    explicitly (see services.py). This can never make the save fail (enqueue_lead_sync
    swallows its own errors) and never delivers inside the transaction.

    Optional attributes callers may set on the instance before save():
      _sync_reason    comma-separated trigger names sent as `sync_reason`
      _sync_dispatch  "inline" -> caller delivers it itself right after commit (lead_create)
    """
    if raw:  # loaddata fixtures
        return
    if created and instance.external_submission_id:
        # Born from the n8n form (api.py). That workflow writes its own Sheet row, so do not
        # push it straight back; later edits DO sync (upsert by Lead ID).
        return
    if not lead_sync.touches_sync_fields(update_fields):
        return
    reason = getattr(instance, "_sync_reason", "") or (
        ("converted" if instance.contact_id else "created") if created else "lead_saved")
    event = lead_sync.enqueue_lead_sync(instance, reason)
    if event is not None and getattr(instance, "_sync_dispatch", "") != "inline":
        transaction.on_commit(lead_sync.schedule_drain)


@receiver(post_save, sender=CallRecord, dispatch_uid="crm_activity_reconcile_call")
def reconcile_activity_on_call(sender, instance, created, raw=False, **kwargs):
    """
    The Android app syncs a call AFTER it ended. A synced CallRecord (started_at -> ended_at) is
    verified work, so any inactivity period it overlaps is re-evaluated. Never breaks a call sync.
    """
    if raw or not created:
        return
    try:
        from . import staff_activity
        if staff_activity.is_monitored(instance.staff):
            staff_activity.reconcile_calls(instance.staff)
    except Exception:  # noqa: BLE001 - monitoring must never fail a call sync
        import logging
        logging.getLogger("crm.activity").exception("Could not reconcile activity for call %s", instance.pk)
