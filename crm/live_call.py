"""
Live assigned-customer-call state for Staff Activity ("System B").

COMPLETELY SEPARATE from the Android CallLog sync (crm/calling_api.py, CallRecord): nothing here
reads or writes CallRecord, lead/contact status, call analytics or the sync watermark. It only answers
"is this staff member on a call, right now, with a customer assigned to THEM?" so that
staff_activity.compute_state can count that time as work.

Android only reports what it sees (session id, number, state). The SERVER decides whether the number
qualifies. No phone number is stored - only the verdict and the matched lead/contact.
Events are idempotent (unique staff+session_id) and an ended session can never be resurrected.
"""
import datetime
import re

from django.conf import settings
from django.db import IntegrityError, transaction

from .models import Contact, Lead, PrivilegedNumber, StaffLiveCallActivity, normalize_phone

CALL_STARTED, CALL_ACTIVE, CALL_ENDED = "CALL_STARTED", "CALL_ACTIVE", "CALL_ENDED"
EVENT_STATES = (CALL_STARTED, CALL_ACTIVE, CALL_ENDED)
_SESSION_RE = re.compile(r"^[A-Za-z0-9._:\-]{8,64}$")


def ttl():
    """The single place the stale-call window is read."""
    return datetime.timedelta(seconds=settings.ACTIVITY_LIVE_CALL_TTL_SECONDS)


def valid_session_id(value):
    return bool(value) and bool(_SESSION_RE.match(value))


def match_assignment(staff_id, phone_raw):
    """
    (qualifies, lead, contact). Same assignment semantics as the calling sync (Lead.assigned_to /
    Contact.current_assigned_to) but evaluated here independently: THIS staff member + THIS number.
    A privileged/excluded number, an unknown number, or a customer assigned to someone else never qualifies.
    """
    norm = normalize_phone(phone_raw)
    if not norm:
        return False, None, None
    if norm in PrivilegedNumber.active_normalized_set():
        return False, None, None
    lead = Lead.objects.filter(phone_normalized=norm, assigned_to_id=staff_id).order_by("-created_at").first()
    contact = None
    if lead is None:
        contact = (Contact.objects.filter(phone_normalized=norm, current_assigned_to_id=staff_id, is_deleted=False)
                   .order_by("-id").first())
    return (lead is not None or contact is not None), lead, contact


def _client_time(ts_ms):
    try:
        return datetime.datetime.fromtimestamp(int(ts_ms) / 1000, tz=datetime.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def ingest(device, session_id, state, phone_raw, ts_ms, now):
    """
    Apply ONE live-call event for `device.staff`. Idempotent. Returns True when the session is
    currently a qualifying, still-open assigned-customer call.
    """
    staff_id = device.staff_id
    with transaction.atomic():
        row = StaffLiveCallActivity.objects.select_for_update().filter(staff_id=staff_id, session_id=session_id).first()
        if row is None:
            try:
                with transaction.atomic():
                    row = StaffLiveCallActivity.objects.create(
                        staff_id=staff_id, device=device, session_id=session_id, started_at=now, last_seen_at=now)
            except IntegrityError:  # a concurrent retry created it first
                row = StaffLiveCallActivity.objects.select_for_update().get(staff_id=staff_id, session_id=session_id)

        if row.ended_at is not None:
            return False  # closed: duplicate / late events change nothing

        if state == CALL_ENDED:
            reported = _client_time(ts_ms)
            ended = now if reported is None else min(now, max(reported, row.started_at))  # never future, never before start
            StaffLiveCallActivity.objects.filter(pk=row.pk).update(
                ended_at=ended, call_state=StaffLiveCallActivity.ENDED, last_seen_at=now)
            return False

        fields = {"last_seen_at": now, "device": device}
        if state == CALL_ACTIVE:
            fields["call_state"] = StaffLiveCallActivity.ACTIVE
            if row.connected_at is None:
                fields["connected_at"] = now
        if phone_raw:  # (re)decided by the server whenever a number is supplied
            qualifies, lead, contact = match_assignment(staff_id, phone_raw)
            fields.update(is_qualifying=qualifies, matched_lead=lead, matched_contact=contact)
        StaffLiveCallActivity.objects.filter(pk=row.pk).update(**fields)
        qualifying = fields.get("is_qualifying", row.is_qualifying)
        return bool(qualifying)


# ------------------------------------------------------------------ read side (used by staff_activity)
def qualifying_sessions(user_id, since):
    """Connected, qualifying sessions that were seen at/after `since` (small, indexed query)."""
    return list(StaffLiveCallActivity.objects.filter(
        staff_id=user_id, is_qualifying=True, connected_at__isnull=False, last_seen_at__gte=since,
    ).order_by("-last_seen_at")[:10])


def is_live(session, now):
    """Open AND refreshed within the TTL. A stale session never counts as live."""
    return session.ended_at is None and (now - session.last_seen_at) <= ttl()


def effective_end(session, now):
    """How far this session can be trusted as 'working time': now if live, else its end / last refresh."""
    if session.ended_at is not None:
        return session.ended_at
    return now if is_live(session, now) else session.last_seen_at
