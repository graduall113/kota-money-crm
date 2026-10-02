"""
Mobile CRM activity endpoints.

  POST /activity/signal/        heartbeat / activity / visibility report from the phone's browser
  POST /activity/lunch/start|end/
  GET  /staff-activity/         admin board (who is CRM-active right now, inactivity periods)

Identity ALWAYS comes from request.user (the authenticated session). Nothing in the body can
name a user, a time or a state: the browser only says WHAT KIND of signal this is and whether
the page is visible. Every timestamp is the server's.
"""
import datetime

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from . import attendance, services, staff_activity as sa
from .decorators import active_account_required, admin_required
from .models import StaffInactivityPeriod, StaffLiveCallActivity, StaffPresence, user_label


def _payload(d):
    """What the page may see: state + the numbers its warning needs. No internal timestamps."""
    return {
        "monitored": True,
        "state": d["state"], "label": d["label"], "staff_status": d["staff_status"],
        "reason": d.get("reason"), "reason_label": d.get("reason_label"),
        "warn": bool(d.get("warn")), "seconds_to_inactive": d.get("seconds_to_inactive"),
        "inactive_minutes": d.get("inactive_minutes", 0),
        "heartbeat_seconds": sa.cfg().heartbeat,
    }


@never_cache
@require_POST
def activity_signal(request):
    user = request.user
    if not user.is_authenticated:
        return JsonResponse({"error": "not_authenticated"}, status=401)
    profile = getattr(user, "staff_profile", None)
    if profile is None or not profile.is_account_active:
        return JsonResponse({"error": "account_inactive"}, status=403)
    if not sa.is_monitored(user):
        return JsonResponse({"monitored": False})
    kind = request.POST.get("kind", "")
    if kind not in sa.KINDS:
        return JsonResponse({"error": "bad_kind"}, status=400)
    visibility = request.POST.get("visibility", StaffPresence.VISIBLE)
    if visibility not in (StaffPresence.VISIBLE, StaffPresence.HIDDEN):
        return JsonResponse({"error": "bad_visibility"}, status=400)
    return JsonResponse(_payload(sa.record_signal(user, kind, visibility, request.session.session_key)))


# ------------------------------------------------------------------ lunch (staff)
@active_account_required
@require_POST
def lunch_start(request):
    if not attendance.requires_attendance(request.user):
        return redirect("dashboard")
    try:
        lunch = sa.start_lunch(request.user)
        messages.success(request, f"Lunch started at {attendance.fmt_clock(lunch.started_at)}. "
                                  f"Inactivity is not counted for up to {sa.cfg().lunch_max.seconds // 60} minutes.")
    except sa.LunchError as exc:
        messages.error(request, str(exc))
    return redirect("dashboard")


@active_account_required
@require_POST
def lunch_end(request):
    if not attendance.requires_attendance(request.user):
        return redirect("dashboard")
    try:
        lunch = sa.end_lunch(request.user)
        messages.success(request, f"Welcome back. Lunch ended at {attendance.fmt_clock(lunch.ended_at)}.")
    except sa.LunchError as exc:
        messages.error(request, str(exc))
    return redirect("dashboard")


# ------------------------------------------------------------------ admin board
@active_account_required
@admin_required
@require_GET
def staff_activity_board(request):
    now = attendance._now()
    today = attendance.business_date(now)
    staff = list(services.active_staff().exclude(is_superuser=True).exclude(staff_profile__role="admin"))
    periods_today = {}
    for p in StaffInactivityPeriod.objects.filter(work_date=today):
        periods_today.setdefault(p.user_id, []).append(p)
    live_by_user = {}
    for x in StaffLiveCallActivity.objects.filter(
            staff__in=staff, is_qualifying=True, connected_at__isnull=False,
            last_seen_at__gte=now - datetime.timedelta(days=1)).order_by("-last_seen_at"):
        live_by_user.setdefault(x.staff_id, []).append(x)
    rows = []
    for u in staff:
        state, rec = attendance.get_state(u)
        if state == attendance.STATE_ACTIVE:
            d = sa.compute_state(u, now, record=rec, live=live_by_user.get(u.pk, []))
        else:
            d = {"state": sa.NOT_WORKING, "label": sa.LABELS[sa.NOT_WORKING], "staff_status": "not_working"}
        presence = StaffPresence.objects.filter(user=u).first()
        mine = periods_today.get(u.pk, [])
        rows.append({
            "user": u, "name": user_label(u), "d": d, "presence": presence,
            "active_label": attendance.fmt_duration(datetime.timedelta(seconds=d.get("active_seconds", 0))) if state == attendance.STATE_ACTIVE else "—",
            "periods": mine,
            "inactive_total": attendance.fmt_duration(datetime.timedelta(seconds=sum(
                max(int(((p.ended_at or now) - p.started_at).total_seconds()) - p.call_overlap_seconds, 0) for p in mine))),
        })
    order = {"inactive": 0, "working": 1, "lunch": 2, "unknown": 3, "not_working": 4}
    rows.sort(key=lambda r: (order.get(r["d"]["staff_status"], 9), r["name"].lower()))
    recent = (StaffInactivityPeriod.objects.select_related("user")
              .filter(work_date__gte=today - datetime.timedelta(days=7)).order_by("-started_at")[:100])
    return render(request, "attendance/activity_board.html", {
        "active_page": "staff_activity", "rows": rows, "recent": recent, "now": now,
        "threshold_minutes": int(sa.cfg().inactivity.total_seconds() // 60),
    })
