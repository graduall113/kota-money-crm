from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import attendance, attendance_verify as verify_mod, filters, services
from .decorators import active_account_required, admin_required
from .models import Attendance, AttendanceEvent, TrustedDevice


def _staff_only(request):
    """Admins aren't subject to attendance; they get bounced to the dashboard."""
    if not attendance.requires_attendance(request.user):
        messages.info(request, "Attendance applies to staff accounts only.")
        return redirect("dashboard")
    return None


def _attendance_staff():
    """Staff who are actually subject to attendance (never admins/superusers)."""
    return services.active_staff().exclude(is_superuser=True).exclude(staff_profile__role="admin")


# ------------------------------------------------------------------ staff: page
@active_account_required
def attendance_page(request):
    blocked = _staff_only(request)
    if blocked:
        return blocked
    state, record = attendance.request_state(request)
    history = Attendance.objects.filter(user=request.user).order_by("-work_date")[:10]
    rows = [
        {
            "date": r.work_date,
            "started": attendance.fmt_clock(r.start_time),
            "ended": attendance.fmt_clock(r.end_time) if r.end_time else "—",
            "duration": attendance.fmt_duration(r.worked_duration) if r.worked_duration is not None else "—",
            "status": r.get_attendance_status_display(),
            "auto_ended": r.auto_ended,
            "override": r.start_verification == Attendance.VERIFY_OVERRIDE,
        }
        for r in history
    ]
    cfg = verify_mod.load_config()
    device, _problem = verify_mod.resolve_device(request, request.user)
    return render(request, "attendance/attendance.html", {
        "active_page": "attendance", "history": rows,
        "verify_geofence": cfg.geofence_active, "verify_ip": cfg.ip_active,
        "verify_device": cfg.device_active, "device_trusted": device is not None,
    })


# ------------------------------------------------------------------ staff: Start Day
@active_account_required
@require_POST
def attendance_start(request):
    blocked = _staff_only(request)
    if blocked:
        return blocked
    user = request.user  # the ONLY source of "who". No staff id is read from the request.
    verify_mod.record_unexpected_input(user, verify_mod.ACTION_START, request)  # ignored, but noted

    # 1) State first (fresh read, not the cached one): a repeat Start is a duplicate, not a verification matter.
    state, _rec = attendance.get_state(user)
    if state != attendance.STATE_NOT_STARTED:
        exc = attendance.AlreadyEnded() if state == attendance.STATE_ENDED else attendance.AlreadyStarted()
        ev = verify_mod.Evidence(action=verify_mod.ACTION_START, ip=verify_mod.client_ip(request))
        verify_mod.record_event(
            user, verify_mod.ACTION_START, AttendanceEvent.TYPE_DUPLICATE_START, AttendanceEvent.OUTCOME_REJECTED,
            exc.message, ev,
        )
        messages.error(request, exc.message)
        return redirect("attendance")

    # 2) Cool-down after a burst of rejected attempts (temporary, not a lock-out).
    try:
        verify_mod.check_throttle(user, verify_mod.ACTION_START)
    except attendance.AttendanceError as exc:
        messages.error(request, exc.message)
        return redirect("attendance")

    # 3) Verification (geofence / accuracy / IP / device), judged on the server.
    evidence = verify_mod.verify(request, user, verify_mod.ACTION_START)
    if evidence.blocking:
        verify_mod.record_failures(user, evidence, evidence.blocking, AttendanceEvent.OUTCOME_REJECTED)
        verify_mod.note_repeated_attempts(user, evidence, verify_mod.load_config().repeat_threshold)
        messages.error(request, verify_mod.VerificationFailed(evidence).message)
        return redirect("attendance")

    # 4) The guarded state transition (transaction + row lock + unique constraint).
    try:
        rec = attendance.start_day(user, evidence=evidence)  # times/date come from the server clock
    except attendance.AttendanceError as exc:
        if isinstance(exc, (attendance.AlreadyStarted, attendance.AlreadyEnded)):  # lost a race with another tab
            verify_mod.record_event(
                user, verify_mod.ACTION_START, AttendanceEvent.TYPE_DUPLICATE_START, AttendanceEvent.OUTCOME_REJECTED,
                exc.message, evidence,
            )
        messages.error(request, exc.message)
        return redirect("attendance")

    if evidence.flagged:
        verify_mod.record_failures(user, evidence, evidence.flagged, AttendanceEvent.OUTCOME_FLAGGED, rec)
    verify_mod.touch_device(evidence.device, evidence.ip, request)
    messages.success(request, f"Day started at {attendance.fmt_clock(rec.start_time)}.")
    return redirect("dashboard")


# ------------------------------------------------------------------ staff: End Day
@active_account_required
@require_POST
def attendance_end(request):
    blocked = _staff_only(request)
    if blocked:
        return blocked
    user = request.user
    verify_mod.record_unexpected_input(user, verify_mod.ACTION_END, request)
    # End Day is NEVER blocked by verification: anything odd is recorded for review instead.
    evidence = verify_mod.verify(request, user, verify_mod.ACTION_END)
    try:
        rec = attendance.end_day(user, evidence=evidence)
    except attendance.AttendanceError as exc:
        if isinstance(exc, attendance.AlreadyEnded):
            etype = AttendanceEvent.TYPE_DUPLICATE_END
        elif isinstance(exc, attendance.NotStarted):
            etype = AttendanceEvent.TYPE_SUSPICIOUS_TRANSITION
        else:
            etype = None
        if etype:
            verify_mod.record_event(user, verify_mod.ACTION_END, etype, AttendanceEvent.OUTCOME_REJECTED, exc.message, evidence)
        messages.error(request, exc.message)
        return redirect("attendance")
    if evidence.failures:
        verify_mod.record_failures(user, evidence, evidence.failures, AttendanceEvent.OUTCOME_FLAGGED, rec)
    verify_mod.touch_device(evidence.device, evidence.ip, request)
    messages.success(request, f"Day ended. Worked {attendance.fmt_duration(rec.worked_duration)} · {rec.get_attendance_status_display()}.")
    return redirect("attendance")


# ------------------------------------------------------------------ staff: trusted-device enrolment
@active_account_required
@require_POST
def attendance_device_enroll(request):
    """Staff redeem the admin-issued code on the device they want trusted."""
    blocked = _staff_only(request)
    if blocked:
        return blocked
    try:
        device, raw_token = verify_mod.redeem_enrollment(request, request.user, request.POST.get("code", ""))
    except attendance.AttendanceError as exc:
        messages.error(request, exc.message)
        return redirect("attendance")
    services.log_audit(request.user, "attendance_device_enrolled", f"Trusted device enrolled for {request.user.username}",
                       {"device": device.pk}, "trusted_device", device.pk)
    messages.success(request, "This device is now registered for attendance.")
    response = redirect("attendance")
    response.set_cookie(verify_mod.DEVICE_COOKIE, raw_token, **verify_mod.device_cookie_kwargs(request))
    return response


# ================================================================== ADMIN
@admin_required
def admin_events(request):
    """Neutral anomaly/audit trail + the admin override form."""
    p = request.GET
    qs = AttendanceEvent.objects.select_related("device")
    if p.get("type"):
        qs = qs.filter(event_type=p["type"])
    if p.get("review"):
        qs = qs.filter(review_status=p["review"])
    if p.get("outcome"):
        qs = qs.filter(outcome=p["outcome"])
    if p.get("staff", "").isdigit():
        qs = qs.filter(user_id=int(p["staff"]))
    if p.get("q"):
        qs = qs.filter(Q(user_name__icontains=p["q"].strip()) | Q(message__icontains=p["q"].strip()))
    qs = filters._range(qs, "created_at", p.get("from"), p.get("to"))
    page = filters.paginate(request, qs, per_page=50)

    today = attendance.business_date()
    started_ids = set(Attendance.objects.filter(work_date=today).values_list("user_id", flat=True))
    staff_members = list(_attendance_staff())
    return render(request, "attendance/admin_events.html", {
        "page": page, "events": page.object_list, "querystring": filters.querystring_without(request, "page"),
        "types": AttendanceEvent.TYPE_CHOICES, "reviews": AttendanceEvent.REVIEW_CHOICES,
        "outcomes": AttendanceEvent.OUTCOME_CHOICES, "GET": p,
        "staff_members": staff_members,
        "override_candidates": [u for u in staff_members if u.pk not in started_ids],
        "active_page": "attendance_events",
        "open_count": AttendanceEvent.objects.filter(review_status="open").count(),
    })


@admin_required
@require_POST
def admin_event_review(request, event_id):
    event = get_object_or_404(AttendanceEvent, pk=event_id)
    status = request.POST.get("status")
    if status not in dict(AttendanceEvent.REVIEW_CHOICES):
        messages.error(request, "Choose a valid review status.")
        return redirect("attendance_events")
    event.review_status = status
    event.reviewed_by = request.user
    event.reviewed_at = timezone.now()
    event.review_note = request.POST.get("note", "").strip()[:255]
    event.save(update_fields=["review_status", "reviewed_by", "reviewed_at", "review_note"])
    services.log_audit(request.user, "attendance_event_reviewed",
                       f"{event.get_event_type_display()} for {event.user_name or 'unknown'} marked {status}",
                       {"event": event.pk, "note": event.review_note}, "attendance_event", event.pk)
    messages.success(request, "Review saved.")
    return redirect("attendance_events")


@admin_required
@require_POST
def admin_override_start(request):
    """
    Admin starts the day for a staff member who can't verify (dead phone, GPS
    failure, ...). Explicit, reasoned, audited. Only the server clock is used:
    an admin cannot back-date or pick the duration/status either.
    """
    target = get_object_or_404(_attendance_staff(), pk=request.POST.get("staff") or 0)
    reason = request.POST.get("reason", "").strip()
    if len(reason) < 5:
        messages.error(request, "Please give a short reason for the override (at least 5 characters).")
        return redirect("attendance_events")
    try:
        rec = attendance.start_day(target, override_by=request.user, override_reason=reason)
    except attendance.AttendanceError as exc:
        messages.error(request, exc.message)
        return redirect("attendance_events")
    ev = verify_mod.record_event(
        target, AttendanceEvent.ACTION_ADMIN, AttendanceEvent.TYPE_ADMIN_OVERRIDE, AttendanceEvent.OUTCOME_OVERRIDE,
        f"Day started by {request.user.get_full_name() or request.user.username}: {reason}", attendance_rec=rec,
        details={"by": request.user.pk, "reason": reason},
    )
    ev.review_status = AttendanceEvent.REVIEW_REVIEWED  # the admin who did it has, by definition, seen it
    ev.reviewed_by, ev.reviewed_at = request.user, timezone.now()
    ev.save(update_fields=["review_status", "reviewed_by", "reviewed_at"])
    services.log_audit(request.user, "attendance_override",
                       f"Attendance start override for {target.get_full_name() or target.username}",
                       {"reason": reason, "attendance": rec.pk}, "attendance", rec.pk)
    messages.success(request, f"Day started for {target.get_full_name() or target.username} (admin override recorded).")
    return redirect("attendance_events")


@admin_required
def admin_devices(request):
    devices = TrustedDevice.objects.select_related("user").order_by("-created_at")
    return render(request, "attendance/admin_devices.html", {
        "devices": devices, "staff_members": _attendance_staff(), "active_page": "attendance_devices",
    })


@admin_required
@require_POST
def admin_device_new(request):
    staff = get_object_or_404(_attendance_staff(), pk=request.POST.get("staff") or 0)
    device, code = verify_mod.create_enrollment(request.user, staff, request.POST.get("label", "").strip())
    services.log_audit(request.user, "attendance_device_code_issued",
                       f"Attendance device enrolment code issued for {staff.get_full_name() or staff.username}",
                       {"device": device.pk}, "trusted_device", device.pk)
    messages.success(
        request,
        f"Enrolment code for {staff.get_full_name() or staff.username}: {code} — valid for 30 minutes, single use. "
        f"They enter it on the Attendance page of the device to trust.",
    )
    return redirect("attendance_devices")


@admin_required
@require_POST
def admin_device_revoke(request, device_id):
    device = get_object_or_404(TrustedDevice, pk=device_id)
    if verify_mod.revoke_device(device, request.user):
        services.log_audit(request.user, "attendance_device_revoked",
                           f"Trusted device revoked for {device.user.get_full_name() or device.user.username}",
                           {"device": device.pk}, "trusted_device", device.pk)
        messages.success(request, "Device revoked. It no longer counts for attendance.")
    else:
        messages.info(request, "That device was already revoked.")
    return redirect("attendance_devices")
