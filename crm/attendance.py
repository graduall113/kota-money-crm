"""
Server-side staff attendance: the ONLY place attendance state changes.

Everything here derives user, date and times from the authenticated user and
the server clock. Nothing (user id, date, time, duration, status) is ever
accepted from the browser.
"""
import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import holidays as holiday_rules
from .access import is_admin
from .models import Attendance

STATE_NOT_STARTED = "not_started"
STATE_ACTIVE = "active"
STATE_ENDED = "ended"


class AttendanceError(Exception):
    """Base: a rejected transition. `message` is safe to show to the user."""

    message = "Attendance action not allowed."

    def __init__(self, message=None):
        super().__init__(message or self.message)
        self.message = message or self.message


class AlreadyStarted(AttendanceError):
    message = "Your day has already started."


class AlreadyEnded(AttendanceError):
    message = "Your day has already ended."


class NotStarted(AttendanceError):
    message = "You haven't started your day yet."


class OutsideWorkingHours(AttendanceError):
    message = "Working hours are over for today, so the day can't be started."


class HolidayBlocked(AttendanceError):
    """Start Day refused because today is an ACTIVE company holiday."""

    message = "Today is a holiday. Attendance can't be started."

    def __init__(self, holiday=None):
        self.holiday = holiday
        if holiday is None:
            super().__init__()
        else:
            super().__init__(f"Today is a holiday: {holiday.name} ({holiday_label(holiday)}). Attendance can't be started.")


# ------------------------------------------------------------------ config
def _now():
    """Single clock for the whole module (tests patch this)."""
    return timezone.now()


def business_tz():
    return ZoneInfo(settings.ATTENDANCE_TIMEZONE)


def _parse_hhmm(value):
    hh, mm = str(value).split(":")
    return datetime.time(int(hh), int(mm))


def work_start_at(work_date):
    return datetime.datetime.combine(work_date, _parse_hhmm(settings.ATTENDANCE_WORK_START), tzinfo=business_tz())


def work_end_at(work_date):
    return datetime.datetime.combine(work_date, _parse_hhmm(settings.ATTENDANCE_WORK_END), tzinfo=business_tz())


def business_date(now=None):
    return (now or _now()).astimezone(business_tz()).date()


def compute_status(duration):
    """
    Stored status of a FINISHED day, from actual worked time only (the one place thresholds are applied).
    full_day -> PRESENT, half_day -> HALF DAY, short_day -> ABSENT (see display_status()).
    """
    minutes = duration.total_seconds() / 60
    if minutes >= settings.ATTENDANCE_FULL_DAY_MIN_MINUTES:
        return Attendance.STATUS_FULL_DAY
    if minutes >= settings.ATTENDANCE_HALF_DAY_MIN_MINUTES:
        return Attendance.STATUS_HALF_DAY
    return Attendance.STATUS_SHORT_DAY


# ------------------------------------------------------------------ the five display statuses
PRESENT = "PRESENT"
HALF_DAY = "HALF DAY"
ABSENT = "ABSENT"
SUNDAY = "SUNDAY"
HOLIDAY = "HOLIDAY"
NOT_STARTED_YET = "NOT STARTED"   # only for today (before working hours end) / future: nothing to judge yet
BADGE_CLASS = {PRESENT: "present", HALF_DAY: "half", ABSENT: "absent", SUNDAY: "sunday",
               HOLIDAY: "holiday", NOT_STARTED_YET: "pending"}
_STORED_TO_DISPLAY = {
    Attendance.STATUS_FULL_DAY: PRESENT,
    Attendance.STATUS_HALF_DAY: HALF_DAY,
    Attendance.STATUS_SHORT_DAY: ABSENT,
    Attendance.STATUS_IN_PROGRESS: PRESENT,   # day started and still running
}


def is_sunday(day):
    off = settings.ATTENDANCE_WEEKLY_OFF_WEEKDAY      # None disables the weekly off (used by legacy test fixtures)
    return off is not None and day.weekday() == off


def display_status(day, rec=None, holiday=None, today=None, now=None):
    """
    THE authoritative day status (templates only display this). Order:
      1 Sunday -> SUNDAY   2 active holiday (and no Start Day) -> HOLIDAY   3 no Start Day -> ABSENT
      4 started: classify the actual worked duration -> PRESENT / HALF DAY (ABSENT if below half-day minimum)
    `holiday` is the ACTIVE Holiday covering `day` (or None); `rec` the Attendance row (or None).
    A day that has not ended yet (today, no record, before working hours end) is NOT_STARTED_YET, not Absent.
    """
    if is_sunday(day):
        return SUNDAY
    if holiday is not None and rec is None:
        return HOLIDAY            # nobody started: Holiday, never Absent. (Work done before a holiday was declared still counts.)
    if rec is None:
        now = now or _now()
        today = today or business_date(now)
        if day > today or (day == today and now < work_end_at(day)):
            return NOT_STARTED_YET
        return ABSENT
    if rec.end_time is None:
        return PRESENT
    return _STORED_TO_DISPLAY.get(rec.attendance_status) or _STORED_TO_DISPLAY[compute_status(rec.worked_duration)]


# ------------------------------------------------------------------ display helpers
def fmt_clock(dt):
    return dt.astimezone(business_tz()).strftime("%I:%M %p") if dt else ""


def fmt_duration(td):
    total = max(int(td.total_seconds()), 0)
    return f"{total // 3600:02d}h {(total % 3600) // 60:02d}m"


def holiday_label(holiday):
    """'20/10/2026' for a single day, '20/10/2026 – 27/10/2026' for a range."""
    if holiday.is_single_day:
        return f"{holiday.start_date:%d/%m/%Y}"
    return f"{holiday.start_date:%d/%m/%Y} – {holiday.end_date:%d/%m/%Y}"


def todays_holiday(now=None):
    """The ACTIVE holiday covering today's business date, or None."""
    return holiday_rules.holiday_for(business_date(now))


# ------------------------------------------------------------------ who is subject to attendance
def requires_attendance(user):
    """Staff are gated. Admins/superusers never are."""
    if not settings.ATTENDANCE_ENFORCED:
        return False
    return bool(user.is_authenticated and not user.is_superuser and not is_admin(user))


# ------------------------------------------------------------------ core transitions
def _finalize(record_pk, end_time, auto, evidence=None):
    """
    ACTIVE -> ENDED, as one guarded UPDATE: it only matches a row that is still
    open, so a second finalize (double click, second tab, cron racing a manual
    End) matches nothing and can never overwrite an already-final record.
    Returns the fresh record, or None if it was not open.
    """
    rec = Attendance.objects.get(pk=record_pk)
    end_time = max(end_time, rec.start_time)
    duration = end_time - rec.start_time
    extra = evidence.end_fields() if evidence is not None else {}
    updated = Attendance.objects.filter(pk=record_pk, end_time__isnull=True).update(
        end_time=end_time,
        worked_duration=duration,
        attendance_status=compute_status(duration),
        auto_ended=auto,
        updated_at=timezone.now(),
        **extra,
    )
    return Attendance.objects.get(pk=record_pk) if updated else None


def _settle_overdue(user, now):
    """Auto-end this user's open records whose 19:00 has passed (lazy safety net)."""
    for rec in Attendance.objects.select_for_update().filter(user=user, end_time__isnull=True):
        limit = work_end_at(rec.work_date)
        if now >= limit:
            _finalize(rec.pk, limit, auto=True)


def start_day(user, evidence=None, override_by=None, override_reason=""):
    """
    NOT_STARTED -> ACTIVE.

    `evidence` is what crm.attendance_verify measured (already judged by the
    caller; this function only stores it). `override_by` is set only by the
    admin-override endpoint. `user` is always the authenticated staff member
    (or, for an override, the staff member the admin chose) - never a
    browser-supplied id.
    """
    now = _now()
    work_date = business_date(now)
    if now >= work_end_at(work_date):
        raise OutsideWorkingHours()
    extra = {}
    if override_by is not None:
        extra = {
            "start_verification": Attendance.VERIFY_OVERRIDE,
            "override_by": override_by,
            "override_reason": (override_reason or "")[:255],
        }
        if evidence is not None:
            extra.update({k: v for k, v in evidence.start_fields().items() if k != "start_verification"})
    elif evidence is not None:
        extra = evidence.start_fields()
    with transaction.atomic():
        # Per-user lock: serialises simultaneous Start/End requests from two
        # tabs (row lock on Postgres; SQLite serialises writers itself).
        get_user_model().objects.select_for_update().get(pk=user.pk)
        _settle_overdue(user, now)
        existing = Attendance.objects.filter(user=user, work_date=work_date).first()
        if existing:
            raise AlreadyEnded() if existing.end_time else AlreadyStarted()
        # THE holiday gate. Every way of starting a day (staff Start Day, direct POST, admin
        # override) ends up here, so nothing the browser sends can get around it. It sits inside
        # the transaction and only guards *creating* a record: an existing record is never touched.
        holiday = holiday_rules.holiday_for(work_date)
        if holiday is not None:
            raise HolidayBlocked(holiday)
        try:
            with transaction.atomic():  # savepoint, so an IntegrityError doesn't poison the outer txn
                return Attendance.objects.create(user=user, work_date=work_date, start_time=now, **extra)
        except IntegrityError:  # the DB unique constraint is the final backstop
            raise AlreadyStarted()


def end_day(user, evidence=None):
    now = _now()
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        _settle_overdue(user, now)
        rec = Attendance.objects.select_for_update().filter(user=user, end_time__isnull=True).first()
        if rec is None:
            if Attendance.objects.filter(user=user, work_date=business_date(now)).exists():
                raise AlreadyEnded()
            raise NotStarted()
        done = _finalize(rec.pk, now, auto=False, evidence=evidence)
        if done is None:
            raise AlreadyEnded()
        return done


def auto_end_overdue(now=None):
    """
    Ends EVERY staff member's open record whose work-end time has passed,
    stamping end_time at the official 19:00 (not the moment the job happened
    to run) and auto_ended=True. Idempotent and safe to run any number of times.
    """
    now = now or _now()
    ended = 0
    for pk, work_date in list(Attendance.objects.filter(end_time__isnull=True).values_list("pk", "work_date")):
        limit = work_end_at(work_date)
        if now < limit:
            continue
        with transaction.atomic():
            rec = Attendance.objects.select_for_update().filter(pk=pk, end_time__isnull=True).first()
            if rec and _finalize(rec.pk, limit, auto=True):
                ended += 1
    return ended


# ------------------------------------------------------------------ state lookup
def get_state(user):
    """Returns (state, record). Lazily auto-ends an overdue open record first."""
    now = _now()
    today = business_date(now)
    rows = list(Attendance.objects.filter(user=user).filter(Q(work_date=today) | Q(end_time__isnull=True)))
    if any(r.end_time is None and now >= work_end_at(r.work_date) for r in rows):
        with transaction.atomic():
            _settle_overdue(user, now)
        rows = list(Attendance.objects.filter(user=user).filter(Q(work_date=today) | Q(end_time__isnull=True)))
    active = next((r for r in rows if r.end_time is None), None)
    if active:
        return STATE_ACTIVE, active
    todays = next((r for r in rows if r.work_date == today), None)
    if todays:
        return STATE_ENDED, todays
    return STATE_NOT_STARTED, None


def request_state(request):
    """get_state() cached on the request so middleware + sidebar share one lookup."""
    cached = getattr(request, "_attendance_cache", None)
    if cached is None:
        cached = get_state(request.user)
        request._attendance_cache = cached
    return cached


def describe(state, record):
    """Display-ready dict for templates (server-computed; the browser only ticks it)."""
    now = _now()
    today = business_date(now)
    info = {
        "state": state,
        "work_start_label": fmt_clock(work_start_at(today)),
        "work_end_label": fmt_clock(work_end_at(today)),
        "can_start": state == STATE_NOT_STARTED and now < work_end_at(today),
    }
    holiday = holiday_rules.holiday_for(today)
    if holiday is not None:
        info["holiday"] = holiday
        info["holiday_label"] = holiday_label(holiday)
        info["is_holiday"] = True
        info["can_start"] = False  # display only - start_day() enforces it independently
    if record:
        info["started_label"] = fmt_clock(record.start_time)
        if record.end_time is None:
            elapsed = now - record.start_time
            info["worked_label"] = fmt_duration(elapsed)
            info["elapsed_seconds"] = max(int(elapsed.total_seconds()), 0)
            info["seconds_to_auto_end"] = max(int((work_end_at(record.work_date) - now).total_seconds()), 0)
        else:
            info["ended_label"] = fmt_clock(record.end_time)
            info["duration_label"] = fmt_duration(record.worked_duration)
            info["status_label"] = display_status(record.work_date, record, holiday_rules.holiday_for(record.work_date))
            info["auto_ended"] = record.auto_ended
    return info
