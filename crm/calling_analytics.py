"""
Automatic Calling Analytics — every number here comes from CallRecord rows,
which are only ever written by an Android device syncing real call-log
events (see crm/calling_api.py). Nothing in this module reads the old
manual "Call Completed" Activity log — that stays a separate, clearly
distinguished thing on the lead timeline (kind="call" = manual note,
kind="call_auto" = written automatically when a real synced call matches
a lead — see crm/calling_api.py).

Every query here is a single annotate()/aggregate() call — never a Python
loop over the full history — so this stays cheap on a low-resource Render
deployment even with hundreds of thousands of CallRecord rows.
"""
from django.contrib.auth.models import User
from django.db.models import Avg, Count, Max, Min, Q, Sum
from django.utils import timezone

from . import filters
from .models import CallRecord, user_label

NOT_CONNECTED_STATUSES = [
    CallRecord.STATUS_MISSED,
    CallRecord.STATUS_NOT_CONNECTED,
    CallRecord.STATUS_REJECTED,
    CallRecord.STATUS_BUSY,
    CallRecord.STATUS_FAILED,
]


def date_range_for_preset(params):
    """Turns the dashboard's `range` preset (today/yesterday/7d/month/all)
    — or an explicit date_from/date_to — into ISO date strings. Falls back
    to "today" for anything unrecognised, so a bad querystring can never
    accidentally show the whole database."""
    today = timezone.localdate()
    if params.get("date_from") or params.get("date_to"):
        return params.get("date_from"), params.get("date_to")
    preset = (params.get("range") or "today").strip()
    if preset == "yesterday":
        d = today - timezone.timedelta(days=1)
        return d.isoformat(), d.isoformat()
    if preset == "7d":
        return (today - timezone.timedelta(days=6)).isoformat(), today.isoformat()
    if preset == "month":
        return today.replace(day=1).isoformat(), today.isoformat()
    if preset == "all":
        return None, None
    return today.isoformat(), today.isoformat()


def overview_queryset(params, viewer=None):
    """`viewer=None` (admin) sees every telecaller, optionally narrowed by
    ?staff=<id>. A non-None viewer (a staff member's own dashboard) always
    stays locked to their own calls regardless of querystring tampering —
    same "narrow, never widen" rule as access.visible_leads()."""
    qs = CallRecord.objects.all()
    date_from, date_to = date_range_for_preset(params)
    qs = filters._range(qs, "started_at", date_from, date_to)
    if viewer is not None:
        return qs.filter(staff_id=viewer.pk)
    staff_id = params.get("staff")
    if staff_id and str(staff_id).isdigit():
        qs = qs.filter(staff_id=int(staff_id))
    return qs


def _fmt_duration(total_seconds):
    total_seconds = int(total_seconds or 0)
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def totals_card(qs):
    """Every CallRecord counted here already passed the eligibility check in
    crm/calling_api.sync_calls (assigned to this staff, not privileged) —
    so "Successful Contacts" is simply the answered-outgoing count; it is
    never derived from total calls or from duration."""
    agg = qs.aggregate(
        total=Count("id"),
        answered=Count("id", filter=Q(status=CallRecord.STATUS_ANSWERED)),
        not_connected=Count("id", filter=Q(status__in=NOT_CONNECTED_STATUSES)),
        missed=Count("id", filter=Q(status=CallRecord.STATUS_MISSED)),
        rejected=Count("id", filter=Q(status=CallRecord.STATUS_REJECTED)),
        busy=Count("id", filter=Q(status=CallRecord.STATUS_BUSY)),
        failed=Count("id", filter=Q(status=CallRecord.STATUS_FAILED)),
        cancelled=Count("id", filter=Q(status=CallRecord.STATUS_NOT_CONNECTED)),
        successful_contacts=Count(
            "id", filter=Q(status=CallRecord.STATUS_ANSWERED, direction=CallRecord.DIRECTION_OUTGOING)
        ),
        talk_seconds=Sum("duration_seconds", filter=Q(status=CallRecord.STATUS_ANSWERED)),
        avg_seconds=Avg("duration_seconds", filter=Q(status=CallRecord.STATUS_ANSWERED)),
    )
    return {
        "total": agg["total"] or 0,
        "answered": agg["answered"] or 0,
        "not_connected": agg["not_connected"] or 0,
        "missed": agg["missed"] or 0,
        "rejected": agg["rejected"] or 0,
        "busy": agg["busy"] or 0,
        "failed": agg["failed"] or 0,
        "cancelled": agg["cancelled"] or 0,
        "successful_contacts": agg["successful_contacts"] or 0,
        "talk_time": _fmt_duration(agg["talk_seconds"]),
        "avg_duration": _fmt_duration(agg["avg_seconds"]),
    }


def staff_cards(params):
    """One card per telecaller with at least one call in range — admin view only."""
    qs = overview_queryset(params, viewer=None)
    rows = qs.exclude(staff=None).values("staff").annotate(
        total=Count("id"),
        answered=Count("id", filter=Q(status=CallRecord.STATUS_ANSWERED)),
        not_connected=Count("id", filter=Q(status__in=NOT_CONNECTED_STATUSES)),
        talk_seconds=Sum("duration_seconds", filter=Q(status=CallRecord.STATUS_ANSWERED)),
        avg_seconds=Avg("duration_seconds", filter=Q(status=CallRecord.STATUS_ANSWERED)),
        longest=Max("duration_seconds", filter=Q(status=CallRecord.STATUS_ANSWERED)),
        shortest=Min("duration_seconds", filter=Q(status=CallRecord.STATUS_ANSWERED)),
    ).order_by("-total")
    names = {u.pk: user_label(u) for u in User.objects.filter(pk__in=[r["staff"] for r in rows])}
    out = []
    for r in rows:
        out.append({
            "staff_id": r["staff"], "name": names.get(r["staff"], "—"),
            "total": r["total"], "answered": r["answered"], "not_connected": r["not_connected"],
            "talk_time": _fmt_duration(r["talk_seconds"]),
            "avg_duration": _fmt_duration(r["avg_seconds"]),
            "longest": _fmt_duration(r["longest"]), "shortest": _fmt_duration(r["shortest"]),
        })
    return out


def recent_calls(params, viewer, admin_view, limit=200):
    qs = overview_queryset(params, viewer=None if admin_view else viewer)
    return qs.select_related("staff", "lead", "contact").order_by("-started_at")[:limit]


def daily_report(params):
    """Machine-readable per-staff rollup for the n8n-facing API
    (crm/calling_api.daily_report) — matches the field names in the
    master prompt's example JSON exactly."""
    qs = overview_queryset(params, viewer=None)
    rows = qs.exclude(staff=None).values("staff").annotate(
        total_calls=Count("id"),
        answered_calls=Count("id", filter=Q(status=CallRecord.STATUS_ANSWERED)),
        not_connected_calls=Count("id", filter=Q(status__in=NOT_CONNECTED_STATUSES)),
        total_talk_time_seconds=Sum("duration_seconds", filter=Q(status=CallRecord.STATUS_ANSWERED)),
        average_call_duration_seconds=Avg("duration_seconds", filter=Q(status=CallRecord.STATUS_ANSWERED)),
    )
    names = {u.pk: user_label(u) for u in User.objects.filter(pk__in=[r["staff"] for r in rows])}
    out = []
    for r in rows:
        out.append({
            "staff": names.get(r["staff"], ""),
            "staff_id": r["staff"],
            "total_calls": r["total_calls"],
            "answered_calls": r["answered_calls"],
            "not_connected_calls": r["not_connected_calls"],
            "total_talk_time_seconds": int(r["total_talk_time_seconds"] or 0),
            "average_call_duration_seconds": round(r["average_call_duration_seconds"] or 0),
        })
    return out
