"""
Holidays admin section.

EVERY view here is admin-only and the check is server-side (`admin_required`
-> 403), so it does not matter whether the sidebar link is visible: a staff
member who types the URL, or POSTs to it, is refused and nothing changes.
The only ids taken from the request are the holiday being edited / deleted /
toggled, and those endpoints are themselves admin-only.
"""
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from . import attendance, filters, holidays as holiday_rules, services
from .decorators import active_account_required, admin_required
from .forms import HolidayForm
from .models import Holiday

TABS = [("upcoming", "Upcoming"), ("current", "Current"), ("past", "Past"), ("all", "All")]
TAB_KEYS = {k for k, _ in TABS}


def _fmt(h):
    return f"{h.start_date:%d/%m/%Y}" if h.is_single_day else f"{h.start_date:%d/%m/%Y} - {h.end_date:%d/%m/%Y}"


def _snapshot(h):
    return {"name": h.name, "start_date": h.start_date.isoformat(), "end_date": h.end_date.isoformat(),
            "reason": h.reason, "is_active": h.is_active}


def _tab_queryset(tab, today):
    qs = Holiday.objects.select_related("created_by")
    if tab == "upcoming":
        return qs.filter(start_date__gt=today).order_by("start_date", "end_date", "id")
    if tab == "current":
        return qs.filter(start_date__lte=today, end_date__gte=today).order_by("start_date", "end_date", "id")
    if tab == "past":
        return qs.filter(end_date__lt=today).order_by("-end_date", "-start_date", "-id")
    return qs.order_by("-start_date", "-end_date", "-id")


@active_account_required
@admin_required
@require_http_methods(["GET"])
def holiday_list(request):
    """Summary cards + Upcoming / Current / Past / All tabs. 'Today' is the business (Asia/Kolkata) date."""
    today = attendance.business_date()
    tab = request.GET.get("tab")
    tab = tab if tab in TAB_KEYS else "upcoming"

    counts = Holiday.objects.aggregate(
        upcoming=Count("pk", filter=Q(start_date__gt=today)),
        current=Count("pk", filter=Q(start_date__lte=today, end_date__gte=today)),
        past=Count("pk", filter=Q(end_date__lt=today)),
        all=Count("pk"),
    )
    active = Holiday.objects.active()
    current_holiday = active.covering(today).first()
    next_holiday = active.filter(start_date__gt=today).order_by("start_date", "id").first()
    this_year = active.filter(start_date__year=today.year).count()

    page = filters.paginate(request, _tab_queryset(tab, today), per_page=25)
    rows = []
    for h in page.object_list:
        rows.append({
            "h": h, "status": h.status_on(today),
            "days_until": (h.start_date - today).days if h.start_date > today else None,
        })
    return render(request, "holidays/holiday_list.html", {
        "active_page": "holidays", "today": today,
        "tab": tab, "tabs": [(k, label, counts[k]) for k, label in TABS], "counts": counts,
        "rows": rows, "page": page, "querystring": filters.querystring_without(request, "page"),
        "current_holiday": current_holiday, "next_holiday": next_holiday,
        "next_in_days": (next_holiday.start_date - today).days if next_holiday else None,
        "this_year": this_year,
    })


@active_account_required
@admin_required
@require_http_methods(["GET", "POST"])
def holiday_create(request):
    form = HolidayForm(request.POST) if request.method == "POST" else HolidayForm()
    if request.method == "POST" and form.is_valid():
        holiday = form.save(commit=False)
        holiday.created_by = request.user
        try:
            with transaction.atomic():
                holiday.save()
        except IntegrityError:  # two admins saving the same period at once - the DB constraint is the backstop
            form.add_error(None, "A holiday for exactly these dates already exists.")
        else:
            services.log_audit(request.user, "holiday_created", f"Holiday '{holiday.name}' created ({_fmt(holiday)})",
                               _snapshot(holiday), "holiday", holiday.pk)
            messages.success(request, f"Holiday '{holiday.name}' added for {_fmt(holiday)} ({holiday.duration_days} day{'s' if holiday.duration_days != 1 else ''}).")
            return redirect("holiday_list")
    return render(request, "holidays/holiday_form.html", {"form": form, "mode": "create", "active_page": "holidays"})


@active_account_required
@admin_required
@require_http_methods(["GET", "POST"])
def holiday_edit(request, pk):
    holiday = get_object_or_404(Holiday, pk=pk)
    before = _snapshot(holiday)  # taken first: a bound ModelForm mutates the instance even when invalid
    form = HolidayForm(request.POST, instance=holiday) if request.method == "POST" else HolidayForm(instance=holiday)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                holiday = form.save()
        except IntegrityError:
            form.add_error(None, "A holiday for exactly these dates already exists.")
        else:
            after = _snapshot(holiday)
            changes = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}
            if changes:
                services.log_audit(request.user, "holiday_edited", f"Holiday '{holiday.name}' updated ({_fmt(holiday)})",
                                   {"changes": changes}, "holiday", holiday.pk)
            messages.success(request, f"Holiday '{holiday.name}' updated.")
            return redirect("holiday_list")
    return render(request, "holidays/holiday_form.html", {
        "form": form, "mode": "edit", "holiday": holiday, "holiday_name": before["name"], "active_page": "holidays",
    })


@active_account_required
@admin_required
@require_http_methods(["GET", "POST"])
def holiday_delete(request, pk):
    holiday = get_object_or_404(Holiday, pk=pk)
    if request.method == "POST":
        name, label, snap = holiday.name, _fmt(holiday), _snapshot(holiday)
        holiday.delete()
        services.log_audit(request.user, "holiday_deleted", f"Holiday '{name}' deleted ({label})", snap, "holiday", pk)
        messages.success(request, f"Holiday '{name}' deleted.")
        return redirect("holiday_list")
    return render(request, "holidays/holiday_confirm_delete.html", {
        "holiday": holiday, "status": holiday.status_on(attendance.business_date()), "active_page": "holidays",
    })


@active_account_required
@admin_required
@require_POST
def holiday_toggle(request, pk):
    """Activate / deactivate without deleting (an inactive holiday is ignored everywhere)."""
    holiday = get_object_or_404(Holiday, pk=pk)
    holiday.is_active = not holiday.is_active
    holiday.save(update_fields=["is_active", "updated_at"])
    verb = "activated" if holiday.is_active else "deactivated"
    services.log_audit(request.user, f"holiday_{verb}", f"Holiday '{holiday.name}' {verb} ({_fmt(holiday)})",
                       {"is_active": holiday.is_active}, "holiday", holiday.pk)
    messages.success(request, f"Holiday '{holiday.name}' {verb}.")
    if holiday.is_active:
        clashes = holiday_rules.find_overlaps(holiday.start_date, holiday.end_date, holiday.pk)
        if clashes:
            messages.warning(request, "Note: it overlaps " + ", ".join(f"'{c.name}' ({_fmt(c)})" for c in clashes[:3])
                             + (" and more" if len(clashes) > 3 else "") + ".")
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}) and nxt.startswith(reverse("holiday_list")):
        return redirect(nxt)
    return redirect("holiday_list")
