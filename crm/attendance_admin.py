"""
Staff Attendance admin dashboard (Feature 6): querying, derived statuses,
corrections and export rows.

Nothing here decides *who* may call it - the views do that (admin_required).
Nothing here loads a whole table: every list is a queryset that the view
paginates, and the export streams in chunks.

Two query shapes, because "Not Started" / "Absent" are staff with NO row:

  * single day  -> one row per staff member (User-driven, LEFT-JOIN style),
                   so people who haven't started show up too;
  * date range  -> one row per attendance record (record-driven).
"""
import datetime
import re
from dataclasses import dataclass
from typing import Optional

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from . import attendance, filters, holidays as holiday_rules, services
from .models import Attendance, AttendanceCorrection, AttendanceEvent, user_label

User = get_user_model()

# ------------------------------------------------------------------ statuses
ST_PRESENT = "present"
ST_HALF_DAY = "half_day"
ST_ABSENT = "absent"
ST_SUNDAY = "sunday"
ST_HOLIDAY = "holiday"
ST_NOT_STARTED = "not_started"   # today only, while the working day is still open (never a recorded status)
# Record filters/counters kept from the original dashboard. They are NOT day statuses (rows never show them).
ST_WORKING = "working"           # a day that was started and has not ended yet
ST_ENDED = "ended"               # a day that was started and has ended
ST_AUTO_ENDED = "auto_ended"     # a day the 19:00 job / safety net ended

# Display statuses are exactly PRESENT / HALF DAY / ABSENT / SUNDAY / HOLIDAY (the day-status filter adds
# "NOT STARTED" for today only). The status itself always comes from attendance.display_status().
STATUS_CHOICES = [
    (ST_PRESENT, "PRESENT"), (ST_HALF_DAY, "HALF DAY"), (ST_ABSENT, "ABSENT"),
    (ST_SUNDAY, "SUNDAY"), (ST_HOLIDAY, "HOLIDAY"), (ST_NOT_STARTED, "NOT STARTED"),
    (ST_WORKING, "Still working (filter)"), (ST_ENDED, "Day ended (filter)"), (ST_AUTO_ENDED, "Auto-ended (filter)"),
]
STATUS_LABELS = dict(STATUS_CHOICES)
_KEY_FOR = {attendance.PRESENT: ST_PRESENT, attendance.HALF_DAY: ST_HALF_DAY, attendance.ABSENT: ST_ABSENT,
            attendance.SUNDAY: ST_SUNDAY, attendance.HOLIDAY: ST_HOLIDAY, attendance.NOT_STARTED_YET: ST_NOT_STARTED}
LEGACY_STATUS = {"full_day": ST_PRESENT}   # old bookmarked links keep working
MAX_RANGE_DAYS = 93
# Shown next to the normal status when a record exists on a day later declared a holiday.
WORKED_ON_HOLIDAY_LABEL = "Worked — Holiday Declared Later"
VERIFICATION_CHOICES = [
    (Attendance.VERIFY_VERIFIED, "Verified"),
    (Attendance.VERIFY_NOT_CHECKED, "Not checked"),
    (Attendance.VERIFY_OVERRIDE, "Admin override"),
]
YES_NO = [("yes", "Yes"), ("no", "No")]

# Events that describe a day's Start/End activity. Device enrolment is not an attendance anomaly,
# and an event an admin has dismissed no longer counts as one.
ANOMALY_ACTIONS = (AttendanceEvent.ACTION_START, AttendanceEvent.ACTION_END, AttendanceEvent.ACTION_ADMIN)
TYPE_LABELS = dict(AttendanceEvent.TYPE_CHOICES)

MIN_REASON_LEN = 5


# ------------------------------------------------------------------ who is subject to attendance
def eligible_staff():
    """Active staff who are actually subject to attendance (never admins/superusers)."""
    return services.active_staff().exclude(is_superuser=True).exclude(staff_profile__role="admin")


# ------------------------------------------------------------------ filters
@dataclass
class Filters:
    date: datetime.date
    date_to: Optional[datetime.date] = None
    staff: Optional[int] = None
    status: str = ""
    verification: str = ""
    auto_ended: str = ""
    anomaly: str = ""
    q: str = ""

    @property
    def is_range(self):
        return self.date_to is not None and self.date_to != self.date


def parse_filters(params, today=None):
    """Bad or tampered values are ignored, never raised."""
    today = today or attendance.business_date()
    d = filters._date(params.get("date")) or today
    d2 = filters._date(params.get("date_to"))
    if d2 is not None and d2 < d:
        d, d2 = d2, d
    if d2 == d:
        d2 = None

    def pick(name, allowed):
        v = (params.get(name) or "").strip()
        v = LEGACY_STATUS.get(v, v) if name == "status" else v
        return v if v in allowed else ""

    return Filters(
        date=d, date_to=d2,
        staff=filters._int(params.get("staff")),
        status=pick("status", STATUS_LABELS),
        verification=pick("verification", dict(VERIFICATION_CHOICES)),
        auto_ended=pick("auto_ended", dict(YES_NO)),
        anomaly=pick("anomaly", dict(YES_NO)),
        q=(params.get("q") or "").strip()[:100],
    )


def _record_q(f):
    """Conditions on an attendance RECORD (verification / auto-ended). Status is decided per day, not here."""
    q = Q()
    if f.verification:
        q &= Q(start_verification=f.verification)
    if f.auto_ended == "yes":
        q &= Q(auto_ended=True)
    elif f.auto_ended == "no":
        q &= Q(auto_ended=False)
    return q


def _search_q(text, prefix=""):
    """
    Name / username / staff code (StaffProfile.reference_code, labelled
    "Staff Code" at registration). Every word must match one of those fields.
    Email and phone are deliberately NOT searchable.
    """
    q = Q()
    for token in text.split():
        q &= (
            Q(**{f"{prefix}first_name__icontains": token})
            | Q(**{f"{prefix}last_name__icontains": token})
            | Q(**{f"{prefix}username__icontains": token})
            | Q(**{f"{prefix}staff_profile__reference_code__icontains": token})
        )
    return q


def _anomaly_exists(user_ref, date_ref):
    """
    Subquery: this staff member has a (non-dismissed) Start/End/Admin event on this
    business date. `user_ref` / `date_ref` are OuterRefs or literals.
    """
    events = (
        AttendanceEvent.objects.filter(action__in=ANOMALY_ACTIONS)
        .exclude(review_status=AttendanceEvent.REVIEW_DISMISSED)
        .annotate(local_day=TruncDate("created_at", tzinfo=attendance.business_tz()))
    )
    return Exists(events.filter(user_id=user_ref, local_day=date_ref))


def _next_day_start(d):
    return datetime.datetime.combine(d + datetime.timedelta(days=1), datetime.time.min, tzinfo=attendance.business_tz())


def build_queryset(f, today=None, anomaly_flag=False):
    """
    Returns a lazy, ordered queryset for the table/export:
      single day -> Users (each row carries that day's record via prefetch)
      range      -> Attendance records
    """
    today = today or attendance.business_date()
    if f.is_range:
        return _record_queryset(f, anomaly_flag)
    return _day_queryset(f, today, anomaly_flag)


def _day_queryset(f, today, anomaly_flag):
    d = f.date
    day_recs = Attendance.objects.filter(user=OuterRef("pk"), work_date=d)
    has_rec = Exists(day_recs)

    # Staff with no record are only listed for today/past days, and only if they existed by then.
    show_virtual = d <= today
    qs = User.objects.select_related("staff_profile")
    listed = Q(pk__in=[])
    if show_virtual:
        listed = Q(pk__in=eligible_staff().values("pk"), date_joined__lt=_next_day_start(d))
    qs = qs.filter(Q(has_rec) | listed) if show_virtual else qs.filter(has_rec)

    if f.staff is not None:
        qs = qs.filter(pk=f.staff)
    if f.q:
        qs = qs.filter(_search_q(f.q))

    qs = _status_day_filter(qs, f, d, today, has_rec, day_recs, show_virtual)
    rq = _record_q(f)
    if rq:
        qs = qs.filter(Exists(day_recs.filter(rq)))

    anomaly = _anomaly_exists(OuterRef("pk"), d)
    if f.anomaly == "yes":
        qs = qs.filter(anomaly)
    elif f.anomaly == "no":
        qs = qs.filter(~anomaly)
    if anomaly_flag:
        qs = qs.annotate(anomaly_flag=anomaly)

    return qs.prefetch_related(
        Prefetch("attendance_records", queryset=Attendance.objects.filter(work_date=d).select_related("start_device", "end_device"),
                 to_attr="_day_recs")
    ).order_by("first_name", "username", "pk")


def _status_day_filter(qs, f, d, today, has_rec, day_recs, show_virtual):
    """Applies the status filter for ONE day, in the same priority order as attendance.display_status()."""
    if not f.status:
        return qs
    if f.status == ST_WORKING:
        return qs.filter(Exists(day_recs.filter(end_time__isnull=True)))
    if f.status == ST_ENDED:
        return qs.filter(Exists(day_recs.filter(end_time__isnull=False)))
    if f.status == ST_AUTO_ENDED:
        return qs.filter(Exists(day_recs.filter(auto_ended=True)))
    kind = ST_SUNDAY if attendance.is_sunday(d) else (ST_HOLIDAY if holiday_rules.is_holiday(d) else "")
    if f.status in (ST_SUNDAY, ST_HOLIDAY):
        return qs if kind == f.status else qs.none()
    if kind:                      # a Sunday / holiday is never present, half day, absent or "not started"
        return qs.none()
    day_over = d < today or (d == today and attendance._now() >= attendance.work_end_at(d))
    short = Exists(day_recs.filter(attendance_status=Attendance.STATUS_SHORT_DAY))
    if f.status == ST_PRESENT:
        return qs.filter(Exists(day_recs.filter(Q(end_time__isnull=True) | Q(attendance_status=Attendance.STATUS_FULL_DAY))))
    if f.status == ST_HALF_DAY:
        return qs.filter(Exists(day_recs.filter(attendance_status=Attendance.STATUS_HALF_DAY)))
    if f.status == ST_ABSENT:      # no Start Day on a finished working day, or too little worked to count
        return qs.filter(~has_rec | short) if (day_over and show_virtual) else qs.filter(short)
    if f.status == ST_NOT_STARTED:
        return qs.filter(~has_rec) if (not day_over and d == today) else qs.none()
    return qs


def range_rows(f, today=None):
    """
    Range view: one Row per (staff, calendar day) for every day up to today - so working days with no
    Start Day show ABSENT, Sundays SUNDAY and holidays HOLIDAY even though no Attendance row exists.
    Derived on the fly (nothing is stored); capped at MAX_RANGE_DAYS days.
    """
    today = today or attendance.business_date()
    lo, hi = f.date, min(f.date_to, today, f.date + datetime.timedelta(days=MAX_RANGE_DAYS - 1))
    if hi < lo:
        return []
    recs = Attendance.objects.filter(work_date__gte=lo, work_date__lte=hi).select_related(
        "start_device", "end_device", "user", "user__staff_profile")
    by_key = {(r.user_id, r.work_date): r for r in recs}
    users = User.objects.select_related("staff_profile").filter(
        Q(pk__in=eligible_staff().values("pk")) | Q(pk__in={u for u, _ in by_key}))
    if f.staff is not None:
        users = users.filter(pk=f.staff)
    if f.q:
        users = users.filter(_search_q(f.q))
    users = list(users.order_by("first_name", "username", "pk"))
    hmap = holiday_rules.holiday_map(lo, hi)
    rows, day = [], hi
    while day >= lo:
        joined_before = _next_day_start(day)
        for u in users:
            rec = by_key.get((u.pk, day))
            if rec is None and u.date_joined >= joined_before:
                continue
            rows.append(Row(u, day, rec, today, holiday=_first(hmap, day)))
        day -= datetime.timedelta(days=1)
    if f.status == ST_WORKING:
        rows = [r for r in rows if r.rec and r.rec.end_time is None]
    elif f.status == ST_ENDED:
        rows = [r for r in rows if r.rec and r.rec.end_time is not None]
    elif f.status == ST_AUTO_ENDED:
        rows = [r for r in rows if r.auto_ended]
    elif f.status:
        rows = [r for r in rows if r.status == f.status]
    if f.verification:
        rows = [r for r in rows if r.rec and r.rec.start_verification == f.verification]
    if f.auto_ended:
        rows = [r for r in rows if bool(r.auto_ended) == (f.auto_ended == "yes")]
    attach_anomalies(rows)
    if f.anomaly:
        rows = [r for r in rows if r.has_anomaly == (f.anomaly == "yes")]
    return rows


def _record_queryset(f, anomaly_flag):
    qs = Attendance.objects.filter(work_date__gte=f.date, work_date__lte=f.date_to).select_related(
        "user", "user__staff_profile", "start_device", "end_device")
    # NOTE: the range view now uses range_rows(); this record-only queryset is kept for compatibility.
    if f.staff is not None:
        qs = qs.filter(user_id=f.staff)
    if f.q:
        qs = qs.filter(_search_q(f.q, "user__"))
    rq = _record_q(f)
    if rq:
        qs = qs.filter(rq)
    anomaly = _anomaly_exists(OuterRef("user_id"), OuterRef("work_date"))
    if f.anomaly == "yes":
        qs = qs.filter(anomaly)
    elif f.anomaly == "no":
        qs = qs.filter(~anomaly)
    if anomaly_flag:
        qs = qs.annotate(anomaly_flag=anomaly)
    return qs.order_by("-work_date", "user__first_name", "user__username", "pk")


# ------------------------------------------------------------------ rows (display + export)
class Row:
    """One table/export row: a staff member on a date, with their record if any."""

    def __init__(self, user, work_date, rec, today, has_anomaly=False, holiday=None):
        self.user, self.work_date, self.rec, self.today = user, work_date, rec, today
        self.has_anomaly = has_anomaly
        self.holiday = holiday  # the ACTIVE holiday covering work_date, or None
        self.anomaly_types = []
        # Snapshot the status when the row is built, so a page/export never shows two different
        # statuses for the same row if the clock crosses the end of the working day while rendering.
        self._display = attendance.display_status(work_date, rec, self.holiday, today)

    # -- identity
    @property
    def staff_name(self):
        return user_label(self.user)

    @property
    def staff_code(self):
        profile = getattr(self.user, "staff_profile", None)
        return profile.reference_code if profile else ""

    # -- status (ONE source: attendance.display_status; nothing is re-derived here)
    @property
    def display(self):
        return self._display

    @property
    def status(self):
        return _KEY_FOR[self.display]

    @property
    def status_label(self):
        return self.display

    @property
    def badge(self):
        """Status colour class (.badge.present / .half / .absent / .sunday / .holiday - see style.css)."""
        return attendance.BADGE_CLASS[self.display]

    @property
    def row_class(self):
        return f"att-row-{self.badge}"

    # -- holiday
    @property
    def holiday_period(self):
        """'20/10/2026 – 27/10/2026' (or the single date) of the holiday on this row's date, else ''."""
        return attendance.holiday_label(self.holiday) if self.holiday is not None else ""

    @property
    def holiday_name(self):
        return self.holiday.name if self.holiday is not None else ""

    @property
    def worked_on_holiday(self):
        """A record exists on a day that is a holiday: the record is kept and flagged, never deleted."""
        return self.rec is not None and self.holiday is not None

    @property
    def holiday_note(self):
        return WORKED_ON_HOLIDAY_LABEL if self.worked_on_holiday else ""

    @property
    def status_export_label(self):
        """Status text for CSV/Excel: the normal status, plus the holiday note when there is one."""
        return f"{self.status_label} ({self.holiday_note})" if self.worked_on_holiday else self.status_label

    @property
    def auto_ended(self):
        return bool(self.rec and self.rec.auto_ended)

    # -- times
    @property
    def start_label(self):
        return attendance.fmt_clock(self.rec.start_time) if self.rec else ""

    @property
    def end_label(self):
        return attendance.fmt_clock(self.rec.end_time) if self.rec and self.rec.end_time else ""

    @property
    def worked_label(self):
        if not self.rec:
            return ""
        if self.rec.end_time is None:  # still running: elapsed so far
            return attendance.fmt_duration(attendance._now() - self.rec.start_time)
        return attendance.fmt_duration(self.rec.worked_duration)

    @property
    def worked_hours(self):
        """Decimal hours for spreadsheets; blank while the day is still open."""
        if self.rec and self.rec.worked_duration is not None:
            return round(self.rec.worked_duration.total_seconds() / 3600, 2)
        return ""

    # -- verification / device
    @property
    def verification_label(self):
        return self.rec.get_start_verification_display() if self.rec else ""

    @property
    def ip(self):
        return (self.rec.start_ip if self.rec else "") or ""

    @property
    def device_label(self):
        d = self.rec.start_device if self.rec else None
        return (d.label or "Registered device") if d else ""


def rows_for_page(objects, f, today=None):
    """Turns a page of Users/Attendance into Rows and attaches each row's anomaly types (one query)."""
    today = today or attendance.business_date()
    objects = list(objects)
    if f.is_range:                     # range_rows() already produced finished Rows (anomalies attached)
        return objects
    hmap = _holidays_for(f, objects)
    rows = []
    for o in objects:
        if f.is_range:
            rows.append(Row(o.user, o.work_date, o, today, holiday=_first(hmap, o.work_date)))
        else:
            rows.append(Row(o, f.date, (o._day_recs[0] if o._day_recs else None), today, holiday=_first(hmap, f.date)))
    attach_anomalies(rows)
    return rows


def _first(hmap, day):
    found = hmap.get(day)
    return found[0] if found else None


def _holidays_for(f, objects=None):
    """{date: [Holiday]} for the dates a table/export covers - a single query."""
    if not f.is_range:
        return holiday_rules.holiday_map(f.date, f.date)
    if objects is not None:
        if not objects:
            return {}
        days = [o.work_date for o in objects]
        return holiday_rules.holiday_map(min(days), max(days))
    return holiday_rules.holiday_map(f.date, f.date_to)


def _day_bounds(lo, hi):
    tz = attendance.business_tz()
    return (datetime.datetime.combine(lo, datetime.time.min, tzinfo=tz), _next_day_start(hi))


def attach_anomalies(rows):
    if not rows:
        return
    start, end = _day_bounds(min(r.work_date for r in rows), max(r.work_date for r in rows))
    events = (
        AttendanceEvent.objects.filter(user_id__in={r.user.pk for r in rows}, action__in=ANOMALY_ACTIONS,
                                       created_at__gte=start, created_at__lt=end)
        .exclude(review_status=AttendanceEvent.REVIEW_DISMISSED)
        .order_by("created_at").values_list("user_id", "event_type", "created_at")
    )
    tz = attendance.business_tz()
    found = {}
    for uid, etype, created in events:
        labels = found.setdefault((uid, created.astimezone(tz).date()), [])
        label = TYPE_LABELS.get(etype, etype)
        if label not in labels:
            labels.append(label)
    for r in rows:
        r.anomaly_types = found.get((r.user.pk, r.work_date), [])
        r.has_anomaly = bool(r.anomaly_types)


class RowStream:
    """
    Duck-types the bit of QuerySet that exports.export_queryset() uses
    (`.iterator(chunk_size)`), so the existing CSV/XLSX export code is reused as-is.
    """

    def __init__(self, f, today=None):
        self.f = f
        self.today = today or attendance.business_date()

    def iterator(self, chunk_size=2000):
        if self.f.is_range:
            yield from range_rows(self.f, self.today)
            return
        qs = build_queryset(self.f, self.today, anomaly_flag=True)
        hmap = _holidays_for(self.f)
        for o in qs.iterator(chunk_size=chunk_size):
            if self.f.is_range:
                yield Row(o.user, o.work_date, o, self.today, o.anomaly_flag, holiday=_first(hmap, o.work_date))
            else:
                yield Row(o, self.f.date, (o._day_recs[0] if o._day_recs else None), self.today, o.anomaly_flag,
                          holiday=_first(hmap, self.f.date))


def export_columns():
    """Exactly the requested columns. No IP, device, coordinates, tokens or contact details."""
    tz = attendance.business_tz

    def hhmm(dt):
        return dt.astimezone(tz()).strftime("%H:%M") if dt else ""

    return [
        ("Staff", lambda r: r.staff_name),
        ("Date", lambda r: r.work_date.isoformat()),
        ("Start", lambda r: hhmm(r.rec.start_time) if r.rec else ""),
        ("End", lambda r: hhmm(r.rec.end_time) if r.rec else ""),
        ("Worked Hours", lambda r: r.worked_hours),
        ("Status", lambda r: r.status_export_label),
        ("Auto Ended", lambda r: "Yes" if r.auto_ended else "No"),
        ("Verification", lambda r: r.verification_label),
        ("Anomaly", lambda r: "Yes" if r.has_anomaly else "No"),
    ]


# ------------------------------------------------------------------ today's cards
def today_summary(today=None):
    """Cards for today. Counts follow the table's rule: Sunday/holiday first, then the day's records."""
    today = today or attendance.business_date()
    eligible_ids = eligible_staff().values("pk")
    total = eligible_staff().count()
    counts = Attendance.objects.filter(work_date=today, user_id__in=eligible_ids).aggregate(
        started=Count("id"),
        working=Count("id", filter=Q(end_time__isnull=True)),
        ended=Count("id", filter=Q(end_time__isnull=False)),
        present=Count("id", filter=Q(end_time__isnull=True) | Q(attendance_status=Attendance.STATUS_FULL_DAY)),
        half_day=Count("id", filter=Q(attendance_status=Attendance.STATUS_HALF_DAY)),
        short=Count("id", filter=Q(attendance_status=Attendance.STATUS_SHORT_DAY)),
        auto_ended=Count("id", filter=Q(auto_ended=True)),
    )
    anomalies = build_queryset(Filters(date=today, anomaly="yes"), today).filter(pk__in=eligible_ids).count()
    holiday = holiday_rules.holiday_for(today)
    sunday = attendance.is_sunday(today)
    waiting = max(total - counts["started"], 0)          # staff with no record yet
    day_over = attendance._now() >= attendance.work_end_at(today)
    hol = holiday is not None and not sunday             # a holiday only replaces "no Start Day"
    return {
        "date": today, "total": total, "started": counts["started"], "holiday": holiday, "is_sunday": sunday,
        "working": counts["working"], "ended": counts["ended"],
        "present": 0 if sunday else counts["present"], "half_day": 0 if sunday else counts["half_day"],
        "absent": 0 if sunday else counts["short"] + (waiting if (day_over and holiday is None) else 0),
        "sunday": total if sunday else 0,
        "on_holiday": waiting if hol else 0,
        "not_started": 0 if (sunday or holiday is not None or day_over) else waiting,
        "auto_ended": counts["auto_ended"], "anomalies": anomalies,
    }


# ------------------------------------------------------------------ detail helpers
def events_for(rec):
    """The Start/End/Admin events this staff member generated on the record's business date."""
    start, end = _day_bounds(rec.work_date, rec.work_date)
    return (AttendanceEvent.objects.filter(user_id=rec.user_id, action__in=ANOMALY_ACTIONS,
                                           created_at__gte=start, created_at__lt=end)
            .select_related("device").order_by("created_at", "id"))


def location_verdict(distance, accuracy, cfg):
    """Human wording for one Start/End measurement, judged against the CURRENT geofence settings."""
    if distance is None:
        return "No location captured"
    parts = ["Inside office area" if distance <= cfg.radius else "Outside office area"]
    if accuracy is not None and accuracy > cfg.max_accuracy:
        parts.append("low GPS accuracy")
    return " · ".join(parts)


# ------------------------------------------------------------------ corrections
class CorrectionError(Exception):
    """A rejected correction. `message` is safe to show to the admin."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


_HHMM = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")
CORRECTABLE_STATUSES = {
    Attendance.STATUS_FULL_DAY: "PRESENT",
    Attendance.STATUS_HALF_DAY: "HALF DAY",
    Attendance.STATUS_SHORT_DAY: "ABSENT",
}


def _parse_hhmm(raw, label):
    raw = (raw or "").strip()
    if not raw:
        return None
    m = _HHMM.match(raw)
    if m:
        try:
            return datetime.time(int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    raise CorrectionError(f"{label} must be a time like 10:30.")


def _local(dt):
    return dt.astimezone(attendance.business_tz())


def _fmt_dt(dt):
    return _local(dt).strftime("%Y-%m-%d %H:%M") if dt else "Not ended"


def apply_correction(attendance_id, admin, *, start="", end="", status="", reason=""):
    """
    Corrects one attendance record and writes the audit trail.

    * A reason (>= 5 chars) is mandatory.
    * Times are HH:MM on the record's own work date (business timezone), never in the future,
      and end >= start. An ended day can't be re-opened (the state machine stays terminal).
    * Worked duration and status are recomputed from the times (same rule as End Day) unless
      the admin picks a different status explicitly.
    * Every changed field becomes one AttendanceCorrection row (old value, new value, reason,
      admin, staff, date, timestamp) plus one entry in the CRM audit log - all in one transaction.
    Returns the list of AttendanceCorrection rows created.
    """
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON_LEN:
        raise CorrectionError(f"Please give a reason for the correction (at least {MIN_REASON_LEN} characters).")
    if len(reason) > 500:
        raise CorrectionError("The reason is too long (500 characters maximum).")

    with transaction.atomic():
        rec = Attendance.objects.select_for_update().select_related("user").get(pk=attendance_id)
        now = attendance._now()
        tz = attendance.business_tz()
        wd = rec.work_date
        is_open = rec.end_time is None

        def resolve(raw, current, label):
            t = _parse_hhmm(raw, label)
            if t is None:
                return current, False
            if current is not None and _local(current).strftime("%H:%M") == t.strftime("%H:%M"):
                return current, False  # unchanged: keep the original seconds
            return datetime.datetime.combine(wd, t, tzinfo=tz), True

        new_start, start_changed = resolve(start, rec.start_time, "Start time")
        new_end, end_changed = resolve(end, rec.end_time, "End time")

        for label, value, changed in (("Start time", new_start, start_changed), ("End time", new_end, end_changed)):
            if changed and value > now:
                raise CorrectionError(f"{label} can't be in the future.")
        if new_end is not None and new_end < new_start:
            raise CorrectionError("End time can't be before the start time.")

        status = (status or "").strip()
        status_requested = bool(status) and status != rec.attendance_status
        if status_requested and status not in CORRECTABLE_STATUSES:
            raise CorrectionError("Choose a valid status.")
        closing = is_open and new_end is not None
        if is_open and not closing and status_requested:
            raise CorrectionError("Status can only be changed on a finished day. Set an end time to finish this day first.")

        duration = (new_end - new_start) if new_end is not None else None
        new_status = rec.attendance_status
        if new_end is not None:
            if status_requested:
                new_status = status  # explicit admin choice wins
            elif start_changed or end_changed:
                new_status = attendance.compute_status(duration)  # follows the corrected times
        new_auto = rec.auto_ended and not end_changed  # an admin-set end time is no longer the automatic 19:00 one

        changes = []
        if start_changed:
            changes.append((AttendanceCorrection.FIELD_START, _fmt_dt(rec.start_time), _fmt_dt(new_start)))
        if end_changed:
            changes.append((AttendanceCorrection.FIELD_END, _fmt_dt(rec.end_time), _fmt_dt(new_end)))
        if new_status != rec.attendance_status:
            label = dict(Attendance.STATUS_CHOICES)
            changes.append((AttendanceCorrection.FIELD_STATUS, label[rec.attendance_status], label[new_status]))
        if new_auto != rec.auto_ended:
            changes.append((AttendanceCorrection.FIELD_AUTO_ENDED, "Yes" if rec.auto_ended else "No", "Yes" if new_auto else "No"))
        if not changes:
            raise CorrectionError("Nothing to change: the values are the same as the current record.")

        rec.start_time, rec.end_time, rec.worked_duration = new_start, new_end, duration
        rec.attendance_status, rec.auto_ended = new_status, new_auto
        rec.save(update_fields=["start_time", "end_time", "worked_duration", "attendance_status", "auto_ended", "updated_at"])

        stamp = timezone.now()
        rows = AttendanceCorrection.objects.bulk_create([
            AttendanceCorrection(
                attendance=rec, staff=rec.user, staff_name=user_label(rec.user)[:150], work_date=wd,
                admin=admin, admin_name=user_label(admin)[:150], field=field, old_value=old, new_value=new,
                reason=reason, created_at=stamp,
            )
            for field, old, new in changes
        ])
        services.log_audit(
            admin, "attendance_correction",
            f"Attendance corrected for {user_label(rec.user)} on {wd.isoformat()}: " + ", ".join(c[0] for c in changes),
            {"attendance": rec.pk, "staff": rec.user_id, "work_date": wd.isoformat(), "reason": reason,
             "changes": [{"field": f, "old": o, "new": n} for f, o, n in changes]},
            "attendance", rec.pk,
        )
        return rows
