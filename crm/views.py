from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from .decorators import active_account_required, admin_required
from .forms import EmailAuthenticationForm, LeadForm, ProfileForm, RegisterForm, StaffCreateForm, StaffEditForm
from .models import Lead, StaffProfile
from .n8n_integration import send_lead_to_n8n

# =========================================================
# AUTH
# =========================================================


def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Account created. You can now sign in.")
            return redirect("login")
    else:
        form = RegisterForm()

    return render(request, "auth/register.html", {"form": form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        form = EmailAuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            profile = getattr(user, "staff_profile", None)
            if profile and not profile.is_account_active:
                messages.error(request, "This account has been deactivated. Contact an admin.")
            else:
                auth_login(request, user)
                if not form.cleaned_data.get("remember_me"):
                    request.session.set_expiry(0)  # expires when the browser closes
                next_url = request.POST.get("next") or request.GET.get("next") or "dashboard"
                return redirect(next_url)
    else:
        form = EmailAuthenticationForm(request)

    return render(request, "auth/login.html", {"form": form})


@login_required
def logout_view(request):
    auth_logout(request)
    messages.success(request, "You've been signed out.")
    return redirect("login")


@active_account_required
def profile_view(request):
    if request.method == "POST":
        form = ProfileForm(request.POST, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated.")
            return redirect("profile")
    else:
        form = ProfileForm(user=request.user)

    return render(request, "auth/profile.html", {"form": form, "active_page": "profile"})


# =========================================================
# ADMIN — STAFF MANAGEMENT
# =========================================================


@admin_required
def staff_list(request):
    staff_members = User.objects.select_related("staff_profile").order_by(
        "-staff_profile__role", "first_name", "username"
    )
    return render(
        request,
        "staff/staff_list.html",
        {"staff_members": staff_members, "active_page": "staff"},
    )


@admin_required
def staff_add(request):
    if request.method == "POST":
        form = StaffCreateForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Staff account created.")
            return redirect("staff_list")
    else:
        form = StaffCreateForm()

    return render(
        request,
        "staff/staff_form.html",
        {"form": form, "active_page": "staff", "mode": "add"},
    )


@admin_required
def staff_edit(request, user_id):
    target_user = get_object_or_404(User, pk=user_id)

    if request.method == "POST":
        form = StaffEditForm(request.POST, instance=target_user)
        if form.is_valid():
            # An admin cannot demote/deactivate themselves by accident and
            # lock everyone (including themselves) out of Staff Management.
            if target_user == request.user and form.cleaned_data["role"] != StaffProfile.ROLE_ADMIN:
                messages.error(request, "You can't remove your own admin role.")
            else:
                form.save()
                messages.success(request, "Staff account updated.")
                return redirect("staff_list")
    else:
        form = StaffEditForm(instance=target_user)

    return render(
        request,
        "staff/staff_form.html",
        {"form": form, "active_page": "staff", "mode": "edit", "target_user": target_user},
    )


@admin_required
def staff_toggle_status(request, user_id):
    target_user = get_object_or_404(User, pk=user_id)
    profile = target_user.staff_profile

    if target_user == request.user:
        messages.error(request, "You can't deactivate your own account.")
        return redirect("staff_list")

    profile.status = (
        StaffProfile.STATUS_INACTIVE if profile.is_account_active else StaffProfile.STATUS_ACTIVE
    )
    profile.save()
    messages.success(request, f"{target_user.get_full_name() or target_user.username} is now {profile.status}.")
    return redirect("staff_list")


# =========================================================
# DASHBOARD
# =========================================================


@active_account_required
def dashboard(request):
    profile = request.user.staff_profile

    if profile.is_admin:
        leads = Lead.objects.all()
    else:
        leads = Lead.objects.filter(reference_by=request.user)

    stats = [
        {"label": "Total leads", "value": leads.count()},
        {"label": "New", "value": leads.filter(status=Lead.STATUS_NEW).count()},
        {"label": "Pending follow-ups", "value": leads.filter(
            status__in=[Lead.STATUS_DOCS_PENDING, Lead.STATUS_MEETING_REQUESTED]
        ).count()},
        {"label": "Approved", "value": leads.filter(status=Lead.STATUS_APPROVED).count()},
    ]
    recent_leads = leads.order_by("-created_at")[:8]

    return render(
        request,
        "dashboard/dashboard.html",
        {"stats": stats, "recent_leads": recent_leads, "active_page": "dashboard"},
    )


# =========================================================
# LEADS
# =========================================================


@admin_required
def all_leads(request):
    leads = Lead.objects.select_related("reference_by", "assigned_to").all()

    reference_by = request.GET.get("reference_by")
    assigned_to = request.GET.get("assigned_to")
    status = request.GET.get("status")
    requirement = request.GET.get("requirement")

    if reference_by:
        leads = leads.filter(reference_by_id=reference_by)
    if assigned_to:
        leads = leads.filter(assigned_to_id=assigned_to)
    if status:
        leads = leads.filter(status=status)
    if requirement:
        leads = leads.filter(requirement__icontains=requirement)

    staff_members = User.objects.select_related("staff_profile").order_by("first_name", "username")

    return render(
        request,
        "leads/all_leads.html",
        {
            "leads": leads,
            "staff_members": staff_members,
            "status_choices": Lead.STATUS_CHOICES,
            "active_page": "all_leads",
        },
    )


@active_account_required
def my_leads(request):
    # THIS IS THE IMPORTANT PART: "My Leads" = leads REFERRED BY the
    # logged-in user, never leads merely ASSIGNED to them.
    leads = Lead.objects.select_related("reference_by", "assigned_to").filter(
        reference_by=request.user
    )

    stats = [
        {"label": "Total Referred", "value": leads.count()},
        {"label": "New", "value": leads.filter(status=Lead.STATUS_NEW).count()},
        {"label": "Documents Pending", "value": leads.filter(status=Lead.STATUS_DOCS_PENDING).count()},
        {"label": "Documents Complete", "value": leads.filter(status=Lead.STATUS_DOCS_COMPLETE).count()},
        {"label": "Meeting Requested", "value": leads.filter(status=Lead.STATUS_MEETING_REQUESTED).count()},
        {"label": "Approved", "value": leads.filter(status=Lead.STATUS_APPROVED).count()},
    ]

    return render(
        request,
        "leads/my_leads.html",
        {"leads": leads, "stats": stats, "active_page": "my_leads"},
    )


@active_account_required
def followups(request):
    profile = request.user.staff_profile
    pending_statuses = [Lead.STATUS_DOCS_PENDING, Lead.STATUS_MEETING_REQUESTED]

    leads = Lead.objects.filter(status__in=pending_statuses)
    if not profile.is_admin:
        # Staff follow-ups cover leads they referred OR are handling.
        leads = leads.filter(Q(reference_by=request.user) | Q(assigned_to=request.user))

    return render(
        request,
        "leads/followups.html",
        {"leads": leads.select_related("reference_by", "assigned_to"), "active_page": "followups"},
    )


@active_account_required
def lead_create(request):
    """
    The CRM's own native "Add New Lead" form.

    Flow: validate -> save to the Django DB (source of truth) -> POST the
    saved lead to n8n's webhook so the existing Gmail / Google Sheets /
    WhatsApp automation keeps running. If the n8n webhook is slow, down,
    or errors out, the lead still stays saved — send_lead_to_n8n() never
    raises, and the user just sees a "sync is pending" message instead of
    an error page.
    """
    if request.method == "POST":
        form = LeadForm(request.POST)
        if form.is_valid():
            # reference_by here is the CRM's internal ownership FK (drives
            # "My Leads" + edit/delete permissions) — it is NOT on this form
            # (see LeadForm docstring), so set it explicitly to whoever is
            # submitting the form, same as the old initial={"reference_by":
            # request.user} did for the previous dropdown-based version.
            lead = form.save(commit=False)
            lead.reference_by = request.user
            lead.save()

            synced, error_message = send_lead_to_n8n(lead)
            if synced:
                messages.success(request, "Lead created successfully and automation started.")
            else:
                messages.warning(
                    request, "Lead saved successfully, but automation sync is pending."
                )

            return redirect("all_leads" if request.user.staff_profile.is_admin else "my_leads")
    else:
        form = LeadForm()

    return render(
        request,
        "leads/lead_form.html",
        {"form": form, "active_page": "add_lead"},
    )


def _can_manage_lead(user, lead):
    """
    Admins can edit/delete any lead. Staff can only edit/delete leads they
    referred — same ownership boundary "My Leads" already uses
    (reference_by=request.user), never assigned_to. Enforced here so it
    applies regardless of which URL is hit directly, not just hidden buttons.
    """
    profile = user.staff_profile
    return profile.is_admin or lead.reference_by_id == user.id


@active_account_required
def lead_edit(request, lead_id):
    """
    Edit Lead — reuses the exact same LeadForm as Add New Lead, so the
    field set, dropdown options, and required-field rules never drift
    between the two. Does not re-fire the n8n webhook; that only happens
    once, at creation (crm/views.py:lead_create).
    """
    lead = get_object_or_404(Lead, pk=lead_id)
    if not _can_manage_lead(request.user, lead):
        raise PermissionDenied("You can only edit leads you referred.")

    if request.method == "POST":
        form = LeadForm(request.POST, instance=lead)
        if form.is_valid():
            form.save()
            messages.success(request, "Lead updated successfully.")
            return redirect("all_leads" if request.user.staff_profile.is_admin else "my_leads")
    else:
        form = LeadForm(instance=lead)

    return render(
        request,
        "leads/lead_form.html",
        {"form": form, "active_page": "add_lead", "mode": "edit", "lead": lead},
    )


@active_account_required
def lead_delete(request, lead_id):
    """
    Delete Lead — GET shows a confirmation page naming the lead (never a
    one-click delete); the actual deletion only happens on a CSRF-protected
    POST. Same ownership rule as edit: admins can delete any lead, staff
    only leads they referred.
    """
    lead = get_object_or_404(Lead, pk=lead_id)
    if not _can_manage_lead(request.user, lead):
        raise PermissionDenied("You can only delete leads you referred.")

    if request.method == "POST":
        customer_name = lead.customer_name
        lead.delete()
        messages.success(request, f"Lead for {customer_name} was deleted.")
        return redirect("all_leads" if request.user.staff_profile.is_admin else "my_leads")

    return render(
        request,
        "leads/lead_confirm_delete.html",
        {"lead": lead, "active_page": "add_lead"},
    )


@admin_required
def reports(request):
    leads = Lead.objects.all()
    by_status = [
        {"label": label, "value": leads.filter(status=key).count()}
        for key, label in Lead.STATUS_CHOICES
    ]
    return render(
        request,
        "reports.html",
        {"by_status": by_status, "total": leads.count(), "active_page": "reports"},
    )


@active_account_required
def settings_page(request):
    return render(request, "settings.html", {"active_page": "settings_page"})
