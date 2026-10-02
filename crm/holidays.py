"""
Holiday rules and lookups.

One place that knows:
  * how the quick "type" presets (single day / week / month) turn into a
    start..end range,
  * what counts as an overlap (warned about) and what counts as an accidental
    duplicate (refused),
  * how the rest of the CRM asks "is this date a holiday?".

Nothing here decides *who* may manage holidays - the views do that
(admin_required). Only ACTIVE holidays ever count as a holiday; an inactive one
is kept for reference and is ignored by every lookup below.

Dates are plain `datetime.date`s in the business calendar (Asia/Kolkata, see
attendance.business_date()), so a holiday 20/10/2026-27/10/2026 covers each of
those eight calendar days, both ends included.
"""
import calendar
import datetime

from .models import Holiday

# --------------------------------------------------------------- presets
DURATION_CUSTOM = "custom"
DURATION_SINGLE = "single"
DURATION_WEEK = "week"
DURATION_MONTH = "month"
DURATION_CHOICES = [
    (DURATION_CUSTOM, "Date range (pick both dates)"),
    (DURATION_SINGLE, "Single day"),
    (DURATION_WEEK, "One week (7 days)"),
    (DURATION_MONTH, "One month"),
]
PRESET_KINDS = (DURATION_SINGLE, DURATION_WEEK, DURATION_MONTH)

MAX_SPAN_DAYS = 366          # sanity guard against a typo like 2026 -> 2062
MIN_YEAR, MAX_YEAR = 2000, 2100


def add_one_month(day):
    """Same day next month; clamped to that month's last day (31 Jan -> 28/29 Feb)."""
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return datetime.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def end_for_preset(kind, start):
    """
    End date implied by a preset, or None for a custom range.

    One month = start .. (same date next month - 1 day), so starting on the 1st
    gives the whole calendar month (01/11 -> 30/11) and starting on the 10th
    gives 10/11 -> 09/12.
    """
    if kind == DURATION_SINGLE:
        return start
    if kind == DURATION_WEEK:
        return start + datetime.timedelta(days=6)
    if kind == DURATION_MONTH:
        return add_one_month(start) - datetime.timedelta(days=1)
    return None


# --------------------------------------------------------------- conflicts
def find_overlaps(start, end, exclude_pk=None):
    """
    ACTIVE holidays sharing at least one date with start..end. Overlapping is
    allowed (e.g. a public holiday inside a longer company shutdown) but the
    admin is asked to confirm it.
    """
    qs = Holiday.objects.active().overlapping(start, end)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return list(qs)


def find_same_name_conflict(name, start, end, exclude_pk=None):
    """
    A holiday with the same name (case-insensitive) that overlaps these dates is
    almost certainly the same holiday entered twice, so it is refused outright
    rather than warned about. Inactive rows count too - re-activating the
    existing one is the right fix.
    """
    qs = Holiday.objects.filter(name__iexact=name).overlapping(start, end)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs.first()


# --------------------------------------------------------------- lookups (used by the rest of the CRM)
def holidays_on(day):
    """Every ACTIVE holiday covering `day`, in calendar order."""
    return list(Holiday.objects.active().covering(day))


def holiday_for(day):
    """The ACTIVE holiday covering `day`, or None. If several overlap, the earliest-starting one."""
    return Holiday.objects.active().covering(day).first()


def is_holiday(day):
    return Holiday.objects.active().covering(day).exists()


def holiday_map(start, end):
    """{date: [Holiday, ...]} for every holiday date in start..end - one query, for calendars / reports."""
    found = {}
    for h in Holiday.objects.active().overlapping(start, end):
        day = max(h.start_date, start)
        last = min(h.end_date, end)
        while day <= last:
            found.setdefault(day, []).append(h)
            day += datetime.timedelta(days=1)
    return found
