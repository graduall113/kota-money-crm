"""
Staff CRM activity monitoring (mobile web). The ONLY place presence / inactivity state changes.

Facts the server can really know, all from its own clock and the authenticated session:
  * last heartbeat   - the page / session is talking to the server (says nothing about work)
  * last activity    - the last meaningful CRM interaction the page reported while VISIBLE
  * reported visibility ("visible"/"hidden") from the Page Visibility API

What it can NOT know, and therefore never claims: which other phone app is in use, whether the
screen is locked, the browser was closed or the network dropped (all look like "silence").
Those surface only as the factual states below.

Inactivity is evaluated lazily AND by `manage.py monitor_staff_activity`, and always against the
timestamps (retroactively), so a missed cron run or a silent phone never loses a period.
"""
import datetime
import hashlib
from collections import namedtuple

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import attendance, live_call
from .models import CallRecord, LunchBreak, StaffInactivityPeriod, StaffPresence

# ---- factual CRM states (never "using WhatsApp" etc.)
ACTIVE = "active"                      # CRM visible and meaningful interaction very recently
TEMP_INACTIVE = "temp_inactive"        # CRM visible, no interaction for a short while (< 15 min)
BACKGROUND = "background"              # CRM page reported hidden (app switch / lock / other tab)
DISCONNECTED = "disconnected"          # no heartbeat for DISCONNECT_SECONDS (closed / asleep / offline)
UNKNOWN = "unknown"                    # not enough information
LUNCH = "lunch"
NOT_WORKING = "not_working"            # no open attendance day

LABELS = {
    ACTIVE: "CRM Active", TEMP_INACTIVE: "CRM Temporarily Inactive", BACKGROUND: "CRM in Background / Not Visible",
    DISCONNECTED: "CRM Session Disconnected", UNKNOWN: "Unknown (no recent signal)", LUNCH: "Lunch Break",
    NOT_WORKING: "Not working (day not started / ended)",
}

# ---- human-readable reasons (admin board). The state itself stays the factual CRM state above.
REASONS = {
    "assigned_call": "Assigned customer call", "crm_activity": "CRM activity", "browser_hidden": "CRM browser hidden",
    "lunch": "Lunch break", "no_recent_activity": "No recent CRM activity", "no_heartbeat": "CRM session not responding",
    "no_signal": "No recent signal from CRM", "not_working": "Day not started / ended",
}
_DEFAULT_REASON = {ACTIVE: "crm_activity", BACKGROUND: "browser_hidden", LUNCH: "lunch", TEMP_INACTIVE: "no_recent_activity",
                   DISCONNECTED: "no_heartbeat", UNKNOWN: "no_signal", NOT_WORKING: "not_working"}

KIND_HEARTBEAT, KIND_ACTIVITY, KIND_VISIBILITY = "heartbeat", "activity", "visibility"
KINDS = (KIND_HEARTBEAT, KIND_ACTIVITY, KIND_VISIBILITY)

Config = namedtuple("Config", "heartbeat inactivity warning active_window credit grace disconnect lunch_max server_min")


def cfg():
    s = settings
    return Config(
        heartbeat=s.ACTIVITY_HEARTBEAT_SECONDS,
        inactivity=datetime.timedelta(minutes=s.ACTIVITY_INACTIVITY_MINUTES),
        warning=datetime.timedelta(minutes=s.ACTIVITY_WARNING_MINUTES),
        active_window=datetime.timedelta(seconds=s.ACTIVITY_ACTIVE_WINDOW_SECONDS),
        credit=datetime.timedelta(seconds=s.ACTIVITY_CREDIT_SECONDS),
        grace=datetime.timedelta(seconds=s.ACTIVITY_NETWORK_GRACE_SECONDS),
        disconnect=datetime.timedelta(seconds=s.ACTIVITY_DISCONNECT_SECONDS),
        lunch_max=datetime.timedelta(minutes=s.ACTIVITY_LUNCH_MAX_MINUTES),
        server_min=datetime.timedelta(seconds=s.ACTIVITY_SERVER_MIN_SECONDS),
    )


def _now():
    return attendance._now()  # one clock for attendance + monitoring (tests patch it)


def is_monitored(user):
    """Staff subject to attendance are monitored; admins / superusers never are."""
    return bool(settings.ACTIVITY_MONITORING_ENABLED and user.is_authenticated and attendance.requires_attendance(user))


def session_fingerprint(session_key):
    return hashlib.sha256((session_key or "").encode()).hexdigest()[:12] if session_key else ""


# ------------------------------------------------------------------ lunch
def _lunch_today(user, work_date):
    return LunchBreak.objects.filter(user=user, work_date=work_date).first()


def lunch_is_valid(lunch, now, c=None):
    """An OPEN lunch is valid for lunch_max after it started; after that it no longer excuses inactivity."""
    c = c or cfg()
    return bool(lunch and lunch.ended_at is None and now <= lunch.started_at + c.lunch_max)


class LunchError(Exception):
    pass


def start_lunch(user):
    now = _now()
    with transaction.atomic():
        state, rec = attendance.get_state(user)
        if state != attendance.STATE_ACTIVE:
            raise LunchError("Start your day before taking lunch.")
        presence = _lock_presence(user, rec.work_date, now)
        settle(user, now, presence=presence, record=rec)  # an idle stretch before lunch is recorded first
        try:
            with transaction.atomic():
                lunch = LunchBreak.objects.create(user=user, work_date=rec.work_date, started_at=now)
        except IntegrityError:
            raise LunchError("You have already taken your lunch break today.")
        settle(user, now, presence=presence, record=rec)  # closes any open period (ended_by lunch)
        return lunch


def end_lunch(user):
    now = _now()
    with transaction.atomic():
        state, rec = attendance.get_state(user)
        presence = _lock_presence(user, rec.work_date if rec else attendance.business_date(now), now)
        lunch = LunchBreak.objects.select_for_update().filter(user=user, ended_at__isnull=True).first()
        if lunch is None:
            raise LunchError("You are not on a lunch break.")
        settle(user, now, presence=presence, record=rec)  # records idle time after the valid lunch window
        LunchBreak.objects.filter(pk=lunch.pk, ended_at__isnull=True).update(ended_at=max(now, lunch.started_at))
        settle(user, now, presence=presence, record=rec)
        return LunchBreak.objects.get(pk=lunch.pk)


# ------------------------------------------------------------------ presence
def _lock_presence(user, work_date, now):
    """Get/create the user's row (row-locked) and roll the daily counters over at a new business date."""
    presence = StaffPresence.objects.select_for_update().filter(user=user).first()
    if presence is None:
        try:
            with transaction.atomic():
                presence = StaffPresence.objects.create(user=user, work_date=work_date)
        except IntegrityError:
            pass
        presence = StaffPresence.objects.select_for_update().get(user=user)
    if presence.work_date != work_date:
        presence.work_date = work_date
        presence.active_seconds = 0
        presence.credited_until = None
        presence.save(update_fields=["work_date", "active_seconds", "credited_until"])
    return presence


def _last_call_end(user, since, now):
    """Latest end (or start+duration) of a synced call that overlaps [since, now]. Retroactive by nature."""
    best = None
    for rec in CallRecord.objects.filter(staff=user, started_at__lte=now).order_by("-started_at")[:20]:
        end = rec.ended_at or (rec.started_at + datetime.timedelta(seconds=rec.duration_seconds or 0))
        if end >= since and end <= now and (best is None or end > best):
            best = end
    return best


def _live_sessions(user, record):
    return live_call.qualifying_sessions(user.pk, record.start_time) if record is not None else []


def baseline(user, presence, record, lunch, now, c=None, live=None):
    """
    The moment the 15-minute inactivity clock counts from, and why:
    the latest of  day start / last meaningful activity / valid-lunch end / end of a synced call /
    end (or "now" while live) of a qualifying assigned-customer live call.
    Heartbeats are deliberately NOT part of it.  `live` = pre-fetched qualifying sessions (None = query).
    """
    c = c or cfg()
    parts = []
    if record is not None:
        parts.append((record.start_time, "day_start"))
    if presence is not None and presence.last_activity_at:
        parts.append((presence.last_activity_at, "activity"))
    if lunch is not None:
        valid_until = lunch.started_at + c.lunch_max
        if lunch.ended_at is not None:
            parts.append((min(lunch.ended_at, valid_until), "lunch"))
        elif now > valid_until:
            parts.append((valid_until, "lunch"))  # lunch overstayed: the clock restarts when its allowance ran out
    if record is not None:
        call_end = _last_call_end(user, record.start_time, now)
        if call_end:
            parts.append((call_end, "call"))
        if live is None:
            live = _live_sessions(user, record)
        ends = [live_call.effective_end(x, now) for x in live]
        ends = [e for e in ends if e >= record.start_time and e <= now]
        if ends:
            parts.append((max(ends), "live_call"))
    if not parts:
        return None, ""
    return max(parts, key=lambda p: p[0])


# ------------------------------------------------------------------ state
def compute_state(user, now=None, presence=None, record=None, lunch=None, live=None):
    """Display-ready, server-computed picture. Pure read (no writes)."""
    now = now or _now()
    c = cfg()
    if record is None:
        state, record = attendance.get_state(user)
        if state != attendance.STATE_ACTIVE:
            return _state(NOT_WORKING, now)
    if presence is None:
        presence = StaffPresence.objects.filter(user=user).first()
    if lunch is None:
        lunch = _lunch_today(user, record.work_date)
    if live is None:
        live = _live_sessions(user, record)
    base, base_reason = baseline(user, presence, record, lunch, now, c, live=live)
    idle = (now - base) if base else None
    info = {"since_activity_seconds": int(idle.total_seconds()) if idle is not None else None,
            "last_activity_at": presence.last_activity_at if presence else None,
            "last_heartbeat_at": presence.last_heartbeat_at if presence else None,
            "visibility": presence.reported_visibility if presence else None,
            "active_seconds": presence.active_seconds if presence and presence.work_date == record.work_date else 0}

    if lunch_is_valid(lunch, now, c):
        return _state(LUNCH, now, **info, staff_status="lunch")
    if any(live_call.is_live(x, now) for x in live):
        # Qualifying assigned-customer call in progress: legitimate work. Overrides browser hidden /
        # background / disconnected and the inactivity clock for as long as the call is fresh (TTL).
        info.update(inactive_minutes=0, seconds_to_inactive=int(c.inactivity.total_seconds()), warn=False, on_assigned_call=True)
        return _state(ACTIVE, now, **info, staff_status="working", reason="assigned_call")
    inactive = idle is not None and idle >= c.inactivity
    staff_status = "inactive" if inactive else "working"
    info["inactive_minutes"] = int(idle.total_seconds() // 60) if inactive else 0
    if idle is not None:
        left = (c.inactivity - idle).total_seconds()
        info["seconds_to_inactive"] = max(int(left), 0)
        info["warn"] = bool(not inactive and idle >= c.warning)

    if presence is None or presence.last_heartbeat_at is None or presence.work_date != record.work_date:
        return _state(UNKNOWN, now, **info, staff_status=staff_status)
    age = now - presence.last_heartbeat_at
    if age > c.disconnect:
        key = DISCONNECTED
    elif presence.reported_visibility == StaffPresence.HIDDEN:
        key = BACKGROUND
    elif age > c.grace:
        key = UNKNOWN  # one or two missed pings (mobile network) is not an event
    elif idle is not None and idle <= c.active_window and presence.last_activity_at and (now - presence.last_activity_at) <= c.active_window:
        key = ACTIVE
    else:
        key = TEMP_INACTIVE
    return _state(key, now, **info, staff_status=staff_status)


def _state(key, now, **extra):
    reason = extra.pop("reason", None) or _DEFAULT_REASON[key]
    d = {"state": key, "label": LABELS[key], "staff_status": extra.pop("staff_status", "not_working" if key == NOT_WORKING else "unknown")}
    d.update(extra)
    d.setdefault("on_assigned_call", False)
    # The 15-minute Android warning is due exactly when the (authoritative) inactivity threshold is reached.
    d["warning_due"] = d["staff_status"] == "inactive"
    if d["staff_status"] == "inactive":
        d["detail"] = f"CRM inactive for {extra.get('inactive_minutes', 0)} minutes."
        reason = "inactive"
        d["reason_label"] = f"No CRM activity for {extra.get('inactive_minutes', 0)} minutes"
    else:
        d["reason_label"] = REASONS[reason]
    d["reason"] = reason
    return d


# ------------------------------------------------------------------ inactivity periods (the only writer)
def settle(user, now=None, presence=None, record=None):
    """
    Open / close this user's inactivity period from the timestamps. Idempotent; safe from any
    number of concurrent requests (one-open-period DB constraint). Call inside transaction.atomic().
    """
    now = now or _now()
    c = cfg()
    if record is None:
        _s, record = attendance.get_state(user)
    open_p = StaffInactivityPeriod.objects.select_for_update().filter(user=user, ended_at__isnull=True).first()
    if record is None:
        return open_p
    ended_day = record.end_time is not None
    eff_now = min(now, record.end_time) if ended_day else now
    if presence is None:
        presence = StaffPresence.objects.filter(user=user).first()
    lunch = _lunch_today(user, record.work_date)

    if open_p is not None and ended_day and open_p.work_date == record.work_date:
        _close(open_p, max(record.end_time, open_p.started_at), StaffInactivityPeriod.END_DAY)
        return None

    base, why = baseline(user, presence, record, lunch, eff_now, c)
    if base is None:
        return open_p
    start = base + c.inactivity

    # An OPEN lunch that began after the baseline cuts the idle stretch short: inactivity only counts
    # BEFORE the lunch started (an overstayed lunch moves the baseline past its start, so it skips this).
    if lunch is not None and lunch.ended_at is None and lunch.started_at > base:
        if start < lunch.started_at and presence is not None:
            period = open_p if (open_p is not None and open_p.last_activity_at == base) else None
            if period is None:
                if open_p is not None:
                    _close(open_p, max(lunch.started_at, open_p.started_at), StaffInactivityPeriod.END_LUNCH)
                if not StaffInactivityPeriod.objects.filter(user=user, started_at=start).exists():  # already recorded
                    _open(user, record, base, start, presence, now, c,
                          ended=(lunch.started_at, StaffInactivityPeriod.END_LUNCH))
            else:
                _close(period, max(lunch.started_at, period.started_at), StaffInactivityPeriod.END_LUNCH)
        elif open_p is not None:
            _close(open_p, max(lunch.started_at, open_p.started_at), StaffInactivityPeriod.END_LUNCH)
        return None

    if eff_now >= start:
        if open_p is not None and open_p.last_activity_at == base:
            return open_p  # same stretch, still running
        if open_p is not None:  # the clock was reset since, and has run out again
            _close(open_p, max(base, open_p.started_at), _reason(why))
        return _open(user, record, base, start, presence, now, c)
    if open_p is not None:
        _close(open_p, max(base, open_p.started_at), _reason(why))
    return None


def _reason(why):
    return {"lunch": StaffInactivityPeriod.END_LUNCH, "call": StaffInactivityPeriod.END_CALL,
            "live_call": StaffInactivityPeriod.END_LIVE_CALL}.get(why, StaffInactivityPeriod.END_ACTIVITY)


def _close(period, when, reason):
    StaffInactivityPeriod.objects.filter(pk=period.pk, ended_at__isnull=True).update(ended_at=when, ended_by=reason)
    period.ended_at, period.ended_by = when, reason
    _apply_call_overlap(period)


def _open(user, record, base, start, presence, now, c, ended=None):
    snapshot = compute_state(user, now, presence=presence, record=record, lunch=_lunch_today(user, record.work_date))
    try:
        with transaction.atomic():
            period = StaffInactivityPeriod.objects.create(
                user=user, work_date=record.work_date, last_activity_at=base, started_at=start,
                session_state=snapshot["state"], visibility_state=(presence.reported_visibility if presence else ""),
            )
    except IntegrityError:
        return StaffInactivityPeriod.objects.filter(user=user, started_at=start).first()
    if ended:
        _close(period, ended[0], ended[1])
    return period


def _apply_call_overlap(period):
    """Seconds of a recorded inactivity period that a (later-synced) call covers. Informational."""
    end = period.ended_at or _now()
    total = 0
    for rec in CallRecord.objects.filter(staff_id=period.user_id, started_at__lt=end):
        rec_end = rec.ended_at or (rec.started_at + datetime.timedelta(seconds=rec.duration_seconds or 0))
        lo, hi = max(rec.started_at, period.started_at), min(rec_end, end)
        if hi > lo:
            total += int((hi - lo).total_seconds())
    if total != period.call_overlap_seconds:
        StaffInactivityPeriod.objects.filter(pk=period.pk).update(call_overlap_seconds=total)
        period.call_overlap_seconds = total


def reconcile_calls(user):
    """
    Called after a call record is synced (Android sync arrives AFTER the call ended).
    Re-evaluates: an open period that a call now covers is closed at the call's end, and
    recorded periods get their covered-seconds updated. No live-call detection is faked.
    """
    now = _now()
    with transaction.atomic():
        state, rec = attendance.get_state(user)
        for p in StaffInactivityPeriod.objects.filter(user=user, work_date__gte=attendance.business_date(now) - datetime.timedelta(days=1)):
            _apply_call_overlap(p)
        if rec is not None:
            settle(user, now, record=rec)


# ------------------------------------------------------------------ the signal from the phone
def record_signal(user, kind, visibility, session_key=""):
    """
    Handle one heartbeat / activity / visibility report. `user` is request.user; nothing about
    WHO is ever read from the request body. Returns the display dict (also sent back to the page).
    """
    now = _now()
    c = cfg()
    visibility = StaffPresence.HIDDEN if visibility == StaffPresence.HIDDEN else StaffPresence.VISIBLE
    with transaction.atomic():
        state, rec = attendance.get_state(user)
        if state != attendance.STATE_ACTIVE:
            return compute_state(user, now, record=None)  # not working: nothing is recorded
        presence = _lock_presence(user, rec.work_date, now)
        settle(user, now, presence=presence, record=rec)  # judge the silence BEFORE this signal counts

        fields = {"last_heartbeat_at": now, "session_hash": session_fingerprint(session_key)}
        if presence.reported_visibility != visibility:
            fields.update(reported_visibility=visibility, visibility_changed_at=now)
        counted = (kind == KIND_ACTIVITY and visibility == StaffPresence.VISIBLE
                   and (presence.last_activity_at is None or now - presence.last_activity_at >= c.server_min))
        if counted:
            fields["last_activity_at"] = now
            start = max(now, presence.credited_until or now)
            new_until = now + c.credit
            fields["active_seconds"] = presence.active_seconds + max(int((new_until - start).total_seconds()), 0)
            fields["credited_until"] = new_until
        StaffPresence.objects.filter(pk=presence.pk).update(**fields)
        for k, v in fields.items():
            setattr(presence, k, v)
        settle(user, now, presence=presence, record=rec)
        return compute_state(user, now, presence=presence, record=rec)


# ------------------------------------------------------------------ live assigned-call + Android warning (System B)
def reconcile_live_call(user):
    """After a live-call event: re-settle so an open inactivity period is closed the moment a qualifying call starts."""
    now = _now()
    with transaction.atomic():
        _state_key, rec = attendance.get_state(user)
        if rec is not None:
            settle(user, now, record=rec)


def device_status(user, ack_warning_id=None):
    """
    What the Android app polls: has the 15-minute warning become due for a NEW inactivity period?
    Django decides; the phone only displays. One warning per period (device_notified_at, set on ack).
    """
    now = _now()
    with transaction.atomic():
        if ack_warning_id:
            StaffInactivityPeriod.objects.filter(pk=ack_warning_id, user=user, device_notified_at__isnull=True).update(device_notified_at=now)
        state, rec = attendance.get_state(user)
        if state != attendance.STATE_ACTIVE:
            return {"state": NOT_WORKING, "reason": "not_working", "warning_id": None, "seconds_to_inactive": None}
        presence = StaffPresence.objects.filter(user=user).first()
        settle(user, now, presence=presence, record=rec)
        d = compute_state(user, now, presence=presence, record=rec)
        warning_id = None
        if d["warning_due"]:
            p = StaffInactivityPeriod.objects.filter(user=user, ended_at__isnull=True, device_notified_at__isnull=True).first()
            warning_id = p.pk if p else None
        return {"state": d["state"], "reason": d["reason"], "warning_id": warning_id,
                "seconds_to_inactive": d.get("seconds_to_inactive")}
