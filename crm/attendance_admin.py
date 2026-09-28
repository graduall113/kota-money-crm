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

from . import attendance, filters, services
from .models import Attendance, AttendanceCorrection, AttendanceEvent, user_label

User = get_user_model()

# ------------------------------------------------------------------ statuses
ST_NOT_STARTED = "not_started"
ST_WORKING = "working"
ST_FULL_DAY = "full_day"
ST_HALF_DAY = "half_day"
ST_ENDED = "ended"
ST_ABSENT = "absent"
ST_AUTO_ENDED = "auto_ended"

STATUS_CHOICES = [
    (ST_NOT_STARTED, "Not Started"),
    (ST_WORKING, "Working"),
    (ST_FULL_DAY, "Full Day"),
    (ST_HALF_DAY, "Half Day"),
    (ST_ENDED, "Ended"),
    (ST_ABSENT, "Absent"),
    (ST_AUTO_ENDED, "Auto Ended"),
]
STATUS_LABELS = dict(STATUS_CHOICES)
# Colour of the status badge (classes already defined in style.css).
STATUS_BADGE = {
    ST_NOT_STARTED: "", ST_WORKING: "info", ST_FULL_DAY: "ok", ST_HALF_DAY: "warn",
    ST_ENDED: "", ST_ABSENT: "bad", ST_AUTO_ENDED: "warn",
}
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
    """Conditions on an attendance RECORD (Attendance field names)."""
    q = Q()
    if f.status == ST_WORKING:
        q &= Q(end_time__isnull=True)
    elif f.status in (ST_FULL_DAY, ST_HALF_DAY):
        q &= Q(attendance_status=f.status)
    elif f.status == ST_ENDED:
        q &= Q(end_time__isnull=False)
    elif f.status == ST_AUTO_ENDED:
        q &= Q(auto_ended=True)
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

    if f.status == ST_NOT_STARTED:
        qs = qs.filter(~has_rec) if d == today else qs.none()
    elif f.status == ST_ABSENT:
        qs = qs.filter(~has_rec) if d < today else qs.none()
    else:
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


def _record_queryset(f, anomaly_flag):
    qs = Attendance.objects.filter(work_date__gte=f.date, work_date__lte=f.date_to).select_related(
        "user", "user__staff_profile", "start_device", "end_device")
    if f.status in (ST_NOT_STARTED, ST_ABSENT):
        return qs.none()  # "no record" states only exist in the single-day view
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

    def __init__(self, user, work_date, rec, today, has_anomaly=False):
        self.user, self.work_date, self.rec, self.today = user, work_date, rec, today
        self.has_anomaly = has_anomaly
        self.anomaly_types = []

    # -- identity
    @property
    def staff_name(self):
        return user_label(self.user)

    @property
    def staff_code(self):
        profile = getattr(self.user, "staff_profile", None)
        return profile.reference_code if profile else ""

    # -- status
    @property
    def status(self):
        r = self.rec
        if r is None:
            return ST_NOT_STARTED if self.work_date >= self.today else ST_ABSENT
        if r.end_time is None:
            return ST_WORKING
        if r.attendance_status == Attendance.STATUS_FULL_DAY:
            return ST_FULL_DAY
        if r.attendance_status == Attendance.STATUS_HALF_DAY:
            return ST_HALF_DAY
        return ST_ENDED  # a short day

    @property
    def status_label(self):
        return STATUS_LABELS[self.status]

    @property
    def badge(self):
        return STATUS_BADGE[self.status]

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
    rows = []
    for o in objects:
        if f.is_range:
            rows.append(Row(o.user, o.work_date, o, today))
        else:
            rows.append(Row(o, f.date, (o._day_recs[0] if o._day_recs else None), today))
    attach_anomalies(rows)
    return rows


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
        qs = build_queryset(self.f, self.today, anomaly_flag=True)
        for o in qs.iterator(chunk_size=chunk_size):
            if self.f.is_range:
                yield Row(o.user, o.work_date, o, self.today, o.anomaly_flag)
            else:
                yield Row(o, self.f.date, (o._day_recs[0] if o._day_recs else None), self.today, o.anomaly_flag)


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
        ("Status", lambda r: r.status_label),
        ("Auto Ended", lambda r: "Yes" if r.auto_ended else "No"),
        ("Verification", lambda r: r.verification_label),
        ("Anomaly", lambda r: "Yes" if r.has_anomaly else "No"),
    ]


# ------------------------------------------------------------------ today's cards
def today_summary(today=None):
    today = today or attendance.business_date()
    eligible_ids = eligible_staff().values("pk")
    total = eligible_staff().count()
    counts = Attendance.objects.filter(work_date=today, user_id__in=eligible_ids).aggregate(
        started=Count("id"),
        working=Count("id", filter=Q(end_time__isnull=True)),
        ended=Count("id", filter=Q(end_time__isnull=False)),
        full_day=Count("id", filter=Q(attendance_status=Attendance.STATUS_FULL_DAY)),
        half_day=Count("id", filter=Q(attendance_status=Attendance.STATUS_HALF_DAY)),
        auto_ended=Count("id", filter=Q(auto_ended=True)),
    )
    anomalies = build_queryset(Filters(date=today, anomaly="yes"), today).filter(pk__in=eligible_ids).count()
    return {
        "date": today, "total": total, "started": counts["started"],
        "not_started": max(total - counts["started"], 0),
        "working": counts["working"], "ended": counts["ended"], "full_day": counts["full_day"],
        "half_day": counts["half_day"], "auto_ended": counts["auto_ended"], "anomalies": anomalies,
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
    Attendance.STATUS_FULL_DAY: "Full Day",
    Attendance.STATUS_HALF_DAY: "Half Day",
    Attendance.STATUS_SHORT_DAY: "Short Day",
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
