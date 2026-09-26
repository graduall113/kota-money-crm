"""
Calling Analytics — admin sees every telecaller's real, synced calls;
a staff member sees only their own. Nothing on this page is ever a manual
CRM click; every number comes from crm/calling_analytics.py, built off
CallRecord rows that only crm/calling_api.py (the Android sync endpoint)
writes.
"""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from . import access, services
from . import calling_analytics as analytics
from .decorators import active_account_required, admin_required
from .models import CallDevice, PrivilegedNumber


@active_account_required
def calling_dashboard(request):
    is_admin = access.is_admin(request.user)
    params = request.GET
    qs = analytics.overview_queryset(params, viewer=None if is_admin else request.user)
    ctx = {
        "active_page": "calling",
        "is_admin_view": is_admin,
        "totals": analytics.totals_card(qs),
        "staff_rows": analytics.staff_cards(params) if is_admin else [],
        "calls": analytics.recent_calls(params, request.user, is_admin),
        "staff_members": services.active_staff() if is_admin else None,
        "GET": params,
        "range": params.get("range") or "today",
    }
    return render(request, "calling/dashboard.html", ctx)


@admin_required
def device_list(request):
    devices = CallDevice.objects.select_related("staff").order_by("-created_at")
    return render(request, "calling/device_list.html", {
        "devices": devices,
        "staff_members": services.active_staff(),
        "active_page": "calling_devices",
    })


@admin_required
def device_pair_new(request):
    if request.method != "POST":
        return redirect("calling_devices")
    staff = get_object_or_404(services.active_staff(), pk=request.POST.get("staff"))
    label = request.POST.get("label", "").strip()[:100]
    device = CallDevice.objects.create(staff=staff, label=label, created_by=request.user)
    device.start_pairing()
    messages.success(
        request,
        f"Pairing code for {staff.get_full_name() or staff.username}: {device.pairing_code} "
        f"— valid for 10 minutes. Enter this in the Android app's setup screen.",
    )
    return redirect("calling_devices")


@admin_required
def device_toggle(request, device_id):
    device = get_object_or_404(CallDevice, pk=device_id)
    device.is_active = not device.is_active
    device.save(update_fields=["is_active"])
    messages.success(request, f"Device {'enabled' if device.is_active else 'revoked'}.")
    return redirect("calling_devices")


@admin_required
def privileged_numbers(request):
    if request.method == "POST":
        phone = request.POST.get("phone_number", "").strip()[:20]
        label = request.POST.get("label", "").strip()[:100]
        if phone:
            PrivilegedNumber.objects.create(phone_number=phone, label=label, created_by=request.user)
            messages.success(request, f"{phone} added — calls to/from this number will never appear in CRM analytics.")
        return redirect("calling_privileged_numbers")
    numbers = PrivilegedNumber.objects.all()
    return render(request, "calling/privileged_numbers.html", {
        "numbers": numbers,
        "active_page": "calling_privileged",
    })


@admin_required
def privileged_number_toggle(request, number_id):
    number = get_object_or_404(PrivilegedNumber, pk=number_id)
    number.is_active = not number.is_active
    number.save(update_fields=["is_active"])
    messages.success(request, f"{number.phone_number} {'re-enabled' if number.is_active else 'disabled'}.")
    return redirect("calling_privileged_numbers")
