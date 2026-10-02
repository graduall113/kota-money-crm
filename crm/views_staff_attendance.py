"""
Staff Attendance admin section (Feature 6).

EVERY view here is admin-only and the check is server-side (`admin_required`
-> 403), so it does not matter whether the sidebar link is visible. Nothing
here reads a staff id from a form for authorization; the only ids taken from
the request are the attendance record being viewed / corrected, and those
endpoints are themselves admin-only.
"""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import attendance, attendance_admin as aa, attendance_verify as verify_mod, exports, filters, holidays as holiday_rules, services
from .decorators import active_account_required, admin_required
from .models import Attendance, user_label


@active_account_required
@admin_required
@require_GET
def staff_attendance(request):
    """Today cards + filterable, paginated attendance table."""
    today = attendance.business_date()
    f = aa.parse_filters(request.GET, today)
    source = aa.range_rows(f, today) if f.is_range else aa.build_queryset(f, today)
    page = filters.paginate(request, source, per_page=25)  # only one page is ever loaded
    rows = aa.rows_for_page(page.object_list, f, today)
    return render(request, "attendance/admin_dashboard.html", {
        "active_page": "staff_attendance",
        "holiday": holiday_rules.holiday_for(f.date),
        "summary": aa.today_summary(today),
        "f": f, "rows": rows, "page": page,
        "querystring": filters.querystring_without(request, "page"),
        "staff_options": aa.eligible_staff().select_related(None).only("id", "first_name", "last_name", "username"),
        "status_choices": aa.STATUS_CHOICES, "verification_choices": aa.VERIFICATION_CHOICES, "yes_no": aa.YES_NO,
        "export_qs": filters.querystring_without(request, "page", "per_page"),
    })


@active_account_required
@admin_required
@require_GET
def staff_attendance_detail(request, pk):
    rec = get_object_or_404(
        Attendance.objects.select_related("user", "user__staff_profile", "start_device", "end_device", "override_by"), pk=pk)
    cfg = verify_mod.load_config()
    events = list(aa.events_for(rec))
    return render(request, "attendance/admin_detail.html", {
        "active_page": "staff_attendance",
        "rec": rec, "staff_name": user_label(rec.user),
        "staff_code": getattr(getattr(rec.user, "staff_profile", None), "reference_code", ""),
        "row": aa.Row(rec.user, rec.work_date, rec, attendance.business_date(), holiday=holiday_rules.holiday_for(rec.work_date)),
        "start_verdict": aa.location_verdict(rec.start_distance_from_office, rec.start_accuracy, cfg),
        "end_verdict": aa.location_verdict(rec.end_distance_from_office, rec.end_accuracy, cfg),
        "started_hhmm": rec.start_time.astimezone(attendance.business_tz()).strftime("%H:%M"),
        "ended_hhmm": rec.end_time.astimezone(attendance.business_tz()).strftime("%H:%M") if rec.end_time else "",
        "events": events, "anomaly_count": sum(1 for e in events if e.review_status != "dismissed"),
        "corrections": rec.corrections.all(),
        "correctable_statuses": list(aa.CORRECTABLE_STATUSES.items()),
        "min_reason": aa.MIN_REASON_LEN,
    })


@active_account_required
@admin_required
@require_POST
def staff_attendance_correct(request, pk):
    rec = get_object_or_404(Attendance, pk=pk)
    try:
        changes = aa.apply_correction(
            rec.pk, request.user,
            start=request.POST.get("start_time", ""), end=request.POST.get("end_time", ""),
            status=request.POST.get("status", ""), reason=request.POST.get("reason", ""),
        )
    except aa.CorrectionError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, f"Attendance corrected ({len(changes)} field{'s' if len(changes) != 1 else ''} changed). The change is in the audit history.")
    return redirect("staff_attendance_detail", pk=rec.pk)


@active_account_required
@admin_required
@require_GET
def staff_attendance_export(request):
    """CSV (default) or Excel (?format=xlsx) of the CURRENT filters - all pages, streamed in chunks."""
    today = attendance.business_date()
    f = aa.parse_filters(request.GET, today)
    fmt = "xlsx" if request.GET.get("format") == "xlsx" else "csv"
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    resp = exports.export_queryset(aa.RowStream(f, today), aa.export_columns(), fmt, f"kota-money-staff-attendance-{stamp}")
    services.log_audit(request.user, "attendance_export", f"Staff attendance export ({fmt})",
                       {"query": request.GET.urlencode()[:300]}, "attendance")
    return resp
