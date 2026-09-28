import logging

from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import access, filters, services
from .datefmt import parse_ddmmyyyy
from .decorators import active_account_required, admin_required
from .forms import (
    EmailAuthenticationForm, LeadForm, ProfileForm, RegisterForm, StaffCreateForm, StaffEditForm, TransferForm,
)
from .models import (
    Activity, AssignmentHistory, Contact, DocumentHistory, ImportBatch, Lead, StaffProfile, user_label,
)
from .n8n_integration import send_lead_to_n8n
from .settings_store import get_bool

# =========================================================
# AUTH
# =========================================================


def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if not get_bool("allow_public_registration"):
        messages.error(request, "Self-registration is turned off. Ask an admin to create your account.")
        return redirect("login")

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
            new_user = form.save()
            services.log_audit(request.user, "staff_created", f"Staff created: {user_label(new_user)}",
                               {"email": new_user.email}, "user", new_user.pk)
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
                services.log_audit(request.user, "staff_edited", f"Staff updated: {user_label(target_user)}",
                                   {"role": form.cleaned_data["role"], "status": form.cleaned_data["status"]},
                                   "user", target_user.pk)
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

    if profile.is_account_active:
        # Never silently strand data: if they still hold records, ask what to do first.
        held = _staff_holdings(target_user)
        if held["leads"] or held["contacts"]:
            messages.warning(request, f"{user_label(target_user)} still has assigned records — choose how to handle them.")
            return redirect("staff_remove", user_id=target_user.pk)
    profile.status = (
        StaffProfile.STATUS_INACTIVE if profile.is_account_active else StaffProfile.STATUS_ACTIVE
    )
    profile.save()
    services.log_audit(request.user, "staff_edited", f"{user_label(target_user)} set {profile.status}",
                       {"status": profile.status}, "user", target_user.pk)
    messages.success(request, f"{target_user.get_full_name() or target_user.username} is now {profile.status}.")
    return redirect("staff_list")


# =========================================================
# STAFF — safe removal / deactivation
# =========================================================


def _staff_holdings(user):
    return {
        "leads": Lead.objects.filter(assigned_to=user).count(),
        "contacts": Contact.objects.filter(current_assigned_to=user, is_deleted=False).count(),
        "call_records": user.call_records.count(),
        "call_devices": user.call_devices.count(),
    }


@admin_required
def staff_remove(request, user_id):
    """Deactivate/delete a staff member without ever losing their assigned data."""
    target = get_object_or_404(User, pk=user_id)
    if target == request.user:
        messages.error(request, "You can't remove your own account.")
        return redirect("staff_list")
    held = _staff_holdings(target)
    others = services.active_staff().exclude(pk=target.pk)

    if request.method == "POST":
        choice = request.POST.get("choice")
        dest = others.filter(pk=request.POST.get("reassign_to") or 0).first()
        if choice == "cancel":
            return redirect("staff_list")
        if choice in ("reassign_deactivate", "reassign_delete"):
            if not dest:
                messages.error(request, "Pick who should receive the records.")
                return redirect("staff_remove", user_id=target.pk)
            for name in ("lead", "contact"):
                job = services.new_job(
                    "reassign", f"Reassign {name}s from {user_label(target)}", held[name + "s"],
                    {"model": name, "scope": {"type": "filter", "query": f"assigned_to={target.pk}" if name == "lead" else f"assigned_to={target.pk}"},
                     "method": "one", "staff_ids": [dest.pk], "reason": "Staff member removed"}, request.user)
                services.start_job(job)
        if choice in ("reassign_deactivate", "keep_inactive", "reassign_delete"):
            if choice == "reassign_delete":
                if Lead.objects.filter(assigned_to=target).exists() or Contact.objects.filter(current_assigned_to=target).exists():
                    messages.error(request, "Records are still assigned to this person — deletion was cancelled.")
                    return redirect("staff_remove", user_id=target.pk)
                name = user_label(target)
                services.log_audit(request.user, "staff_deleted", f"Staff deleted: {name}", {"email": target.email}, "user", target.pk)
                target.delete()
                messages.success(request, f"{name} was deleted. Their history stays readable.")
                return redirect("staff_list")
            profile = target.staff_profile
            profile.status = StaffProfile.STATUS_INACTIVE
            profile.save()
            services.log_audit(request.user, "staff_edited", f"{user_label(target)} deactivated",
                               {"kept_records": choice == "keep_inactive", "leads": held["leads"], "contacts": held["contacts"]},
                               "user", target.pk)
            messages.success(request, f"{user_label(target)} is now inactive.")
            return redirect("staff_list")
    return render(request, "staff/staff_remove.html", {
        "target": target, "held": held, "others": others, "active_page": "staff",
    })


@admin_required
def staff_delete(request, user_id):
    """
    Permanently deletes a DEACTIVATED staff account — only when it is
    genuinely safe to do so.

    Leads/contacts still assigned to them are never silently lost: if any
    remain, this redirects into the existing reassign wizard instead of
    deleting anything. Call history is protected the same way but can't be
    reassigned — CallDevice/CallRecord are both a hard CASCADE straight to
    this User in the schema (by calling-system design, left untouched here),
    so deleting the account would silently delete their call devices and
    every synced call record with it. Rather than change that schema, the
    safe path when call history exists is simply to leave the account
    deactivated (deactivation already fully revokes their access).
    """
    target = get_object_or_404(User, pk=user_id)
    if request.method != "POST":
        return redirect("staff_list")
    if target == request.user:
        messages.error(request, "You can't delete your own account.")
        return redirect("staff_list")
    profile = target.staff_profile
    if profile.is_account_active:
        messages.error(request, f"Deactivate {user_label(target)} first, then delete the account.")
        return redirect("staff_list")

    held = _staff_holdings(target)
    if held["leads"] or held["contacts"]:
        messages.warning(request, f"{user_label(target)} still has assigned records — reassign them first.")
        return redirect("staff_remove", user_id=target.pk)
    if held["call_records"] or held["call_devices"]:
        messages.error(
            request,
            f"{user_label(target)} has {held['call_records']:,} call record(s) and {held['call_devices']} "
            "device(s) linked to this account. Deleting it would also permanently delete that call history, "
            "so the account stays deactivated instead — deactivation already fully blocks their login and access.",
        )
        return redirect("staff_list")

    if target.attendance_records.exists():
        messages.error(
            request,
            f"{user_label(target)} has attendance history, which is kept for records — "
            "the account stays deactivated instead (deactivation already fully blocks their access).",
        )
        return redirect("staff_list")

    name, pk = user_label(target), target.pk
    services.log_audit(request.user, "staff_deleted", f"Staff deleted: {name}", {"email": target.email}, "user", pk)
    target.delete()
    messages.success(request, f"{name}'s account was permanently deleted.")
    return redirect("staff_list")


# =========================================================
# DASHBOARD
# =========================================================


def _followup_counts(model_qs):
    today = timezone.localdate()
    base = model_qs.filter(followup_status="pending", next_followup_date__isnull=False)
    return {
        "today": base.filter(next_followup_date=today).count(),
        "overdue": base.filter(next_followup_date__lt=today).count(),
        "upcoming": base.filter(next_followup_date__gt=today).count(),
    }


@active_account_required
def dashboard(request):
    user = request.user
    admin = access.is_admin(user)
    leads = access.visible_leads(user)
    contacts = access.visible_contacts(user)
    today = timezone.localdate()

    complete = services.filter_doc_status(leads, "complete").count()
    total_leads = leads.count()
    lead_fu, contact_fu = _followup_counts(leads), _followup_counts(contacts)
    transfers = AssignmentHistory.objects.filter(action=AssignmentHistory.ACTION_TRANSFER, lead__isnull=False)
    if not admin:
        transfers = transfers.filter(from_user=user)
    counts = dict(leads.values_list("status").annotate(c=Count("id")).values_list("status", "c"))

    if admin:
        stats = [
            {"label": "Total Leads", "value": total_leads},
            {"label": "Total Contacts", "value": contacts.count()},
            {"label": "Today's Leads", "value": leads.filter(created_at__date=today).count()},
            {"label": "Pending Follow-ups", "value": lead_fu["today"] + lead_fu["overdue"] + contact_fu["today"] + contact_fu["overdue"]},
            {"label": "Documents Pending", "value": total_leads - complete},
            {"label": "Documents Complete", "value": complete},
            {"label": "Processing", "value": counts.get(Lead.STATUS_PROCESSING, 0)},
            {"label": "Approved", "value": counts.get(Lead.STATUS_APPROVED, 0)},
            {"label": "Rejected", "value": counts.get(Lead.STATUS_REJECTED, 0)},
            {"label": "Transferred", "value": transfers.count()},
            {"label": "Total Staff", "value": User.objects.filter(staff_profile__status="active").count()},
            {"label": "Imported Batches", "value": ImportBatch.objects.count()},
        ]
    else:
        stats = [
            {"label": "My Leads", "value": total_leads},
            {"label": "My Contacts", "value": contacts.count()},
            {"label": "Today's Follow-ups", "value": lead_fu["today"] + contact_fu["today"]},
            {"label": "Overdue Follow-ups", "value": lead_fu["overdue"] + contact_fu["overdue"]},
            {"label": "Documents Pending", "value": total_leads - complete},
            {"label": "Documents Complete", "value": complete},
            {"label": "Processing", "value": counts.get(Lead.STATUS_PROCESSING, 0)},
            {"label": "Approved", "value": counts.get(Lead.STATUS_APPROVED, 0)},
            {"label": "Rejected", "value": counts.get(Lead.STATUS_REJECTED, 0)},
            {"label": "Transferred by me", "value": transfers.count()},
        ]
    ctx = {
        "stats": stats,
        "recent_leads": leads.select_related("reference_by", "assigned_to").order_by("-created_at")[:8],
        "active_page": "dashboard",
        "is_admin_view": admin,
    }
    if admin:
        ctx["recent_imports"] = ImportBatch.objects.select_related("uploaded_by")[:5]
        ctx["recent_transfers"] = AssignmentHistory.objects.filter(action="transfer", lead__isnull=False).select_related("lead")[:5]
    return render(request, "dashboard/dashboard.html", ctx)


# =========================================================
# LEAD LISTS
# =========================================================


def _lead_list_context(request, base_qs, active_page, title, subtitle, list_url_name, per_page=50):
    params = request.GET
    admin = access.is_admin(request.user)
    qs = filters.filter_leads(base_qs.select_related("reference_by", "assigned_to"), params)
    page = filters.paginate(request, qs, per_page=per_page)
    progress = services.doc_progress_map([l.pk for l in page.object_list])
    for lead in page.object_list:
        rec, total, status = progress[lead.pk]
        lead.doc_received, lead.doc_total = rec, total
        lead.doc_status_label = services.DOC_STATUS_LABELS[status]
        lead.doc_status_key = status
        lead.can_edit = access.can_edit_lead(request.user, lead)
        lead.can_delete = access.can_delete_lead(request.user, lead)
    sources = list(access.visible_leads(request.user).exclude(source="").values_list("source", flat=True).distinct()[:100])
    return {
        "page": page, "leads": page.object_list, "total_count": page.paginator.count,
        "staff_members": services.active_staff() if admin else [],
        "bulk_staff": services.active_staff(),
        "status_choices": Lead.STATUS_CHOICES, "sources": sources,
        "doc_types": services.active_doc_types(),
        "querystring": filters.querystring_without(request, "page"),
        "list_url": list_url_name, "is_admin_view": admin,
        "can_export": access.can_export(request.user),
        "active_page": active_page, "title": title, "subtitle": subtitle,
        "has_filters": any(k for k in request.GET if k not in ("page", "per_page", "sort")),
        "GET": params,
    }


@active_account_required
def all_leads(request):
    """
    All Leads — Admin sees every lead, staff see ONLY the leads they are
    authorized for. Authorization is applied BEFORE any filter/search/sort.
    """
    admin = access.is_admin(request.user)
    ctx = _lead_list_context(
        request, access.visible_leads(request.user), "all_leads", "All Leads",
        "Every lead captured across the team." if admin else "Every lead you are authorized to work on.", "all_leads",
    )
    return render(request, "leads/all_leads.html", ctx)


@active_account_required
def my_leads(request):
    """
    My Leads — leads assigned to me, or (optionally) referred by me.
    scope=assigned|referred narrows it; the default shows both.
    """
    user = request.user
    scope = request.GET.get("scope", "")
    base = access.visible_leads(user)
    if scope == "assigned":
        base = base.filter(assigned_to=user)
    elif scope == "referred":
        base = base.filter(reference_by=user)
    stats_qs = access.visible_leads(user)
    stats = [
        {"label": "Assigned to me", "value": stats_qs.filter(assigned_to=user).count()},
        {"label": "Referred by me", "value": stats_qs.filter(reference_by=user).count()},
        {"label": "Documents Pending", "value": stats_qs.filter(status=Lead.STATUS_DOCS_PENDING).count()},
        {"label": "Documents Complete", "value": stats_qs.filter(status=Lead.STATUS_DOCS_COMPLETE).count()},
        {"label": "Meeting Requested", "value": stats_qs.filter(status=Lead.STATUS_MEETING_REQUESTED).count()},
        {"label": "Approved", "value": stats_qs.filter(status=Lead.STATUS_APPROVED).count()},
    ]
    ctx = _lead_list_context(request, base, "my_leads", "My Leads",
                             "Leads assigned to you and leads you referred.", "my_leads", per_page=25)
    ctx.update({"stats": stats, "scope": scope})
    return render(request, "leads/my_leads.html", ctx)


@active_account_required
def followups(request):
    user = request.user
    today = timezone.localdate()
    leads, contacts = access.visible_leads(user), access.visible_contacts(user)

    def pending(qs):
        return qs.filter(followup_status="pending", next_followup_date__isnull=False)

    sections = []
    for key, label, cond in (
        ("overdue", "Overdue", {"next_followup_date__lt": today}),
        ("today", "Today", {"next_followup_date": today}),
        ("upcoming", "Upcoming", {"next_followup_date__gt": today}),
    ):
        order = "-next_followup_date" if key == "overdue" else "next_followup_date"
        l = pending(leads).filter(**cond).select_related("assigned_to").order_by(order, "next_followup_time")
        c = pending(contacts).filter(**cond).select_related("current_assigned_to").order_by(order, "next_followup_time")
        sections.append({"key": key, "label": label, "lead_count": l.count(), "contact_count": c.count(),
                         "leads": l[:50], "contacts": c[:50]})

    # Preserved from the original page: leads waiting on documents / a meeting.
    waiting = leads.filter(status__in=[Lead.STATUS_DOCS_PENDING, Lead.STATUS_MEETING_REQUESTED]) \
        .select_related("reference_by", "assigned_to").order_by("-updated_at")[:100]
    return render(request, "leads/followups.html", {"sections": sections, "leads": waiting, "active_page": "followups"})


# =========================================================
# LEAD CREATE / EDIT / DELETE / DETAIL
# =========================================================


def _lead_home(user):
    return "all_leads" if access.is_admin(user) else "my_leads"


class _ContactAlreadyConverted(Exception):
    """Raised inside the conversion transaction when another request won the race."""


def _converted_lead_for(contact):
    """
    The Lead this Contact was already converted into, or None if it hasn't been.
    A contact counts as converted when its status says so OR any Lead already
    points at it (Lead.contact) — either one alone must block a second conversion.
    Returns (already_converted: bool, lead_or_None).
    """
    lead = Lead.objects.filter(contact_id=contact.pk).order_by("-created_at", "-id").first()
    return (contact.status == Contact.STATUS_CONVERTED or lead is not None), lead


def _already_converted_response(request, contact):
    """Friendly message + link to the existing Lead (when this user may see it)."""
    _, lead = _converted_lead_for(contact)
    visible = access.visible_leads(request.user).filter(pk=lead.pk).first() if lead else None
    if visible:
        messages.warning(request, f"{contact.name} was already converted to lead {visible.display_id}. "
                                  "Opening the existing lead instead of creating a duplicate.")
        return redirect("lead_detail", lead_id=visible.pk)
    messages.warning(request, f"{contact.name} has already been converted to a lead, so it can't be converted again.")
    return redirect("contact_detail", contact_id=contact.pk)


@active_account_required
def lead_create(request):
    """
    The CRM's own native "Add New Lead" form — and, with ?from_contact=<id>,
    the SAME form/template/validation used as "Convert Contact to Lead".

    Flow: validate -> (one DB transaction: claim the contact, save the lead,
    link it, write history) -> POST the saved lead to n8n's webhook so the
    existing Gmail / Google Sheets / WhatsApp automation keeps running. n8n is
    called only AFTER the transaction has committed, and send_lead_to_n8n()
    never raises, so a slow/down n8n can never roll back a saved Lead — the
    user just sees a "sync is pending" warning.

    Converting a contact is guarded on the server (not just in JS): an
    already-converted contact is refused, and the contact row is claimed with a
    single conditional UPDATE inside the transaction, so a double-click or two
    simultaneous submits can only ever create ONE lead.

    reference_by = whoever creates it (never changes later).
    assigned_to  = creator (staff) or the staff member chosen by an admin.
    """
    admin = access.is_admin(request.user)
    contact = None
    if request.GET.get("from_contact") or request.POST.get("from_contact"):
        contact = access.get_visible_contact_or_404(request.user, request.GET.get("from_contact") or request.POST.get("from_contact"))
        if not access.can_edit_contact(request.user, contact):
            raise PermissionDenied("You can't convert this contact.")
        if _converted_lead_for(contact)[0]:
            return _already_converted_response(request, contact)

    if request.method == "POST":
        form = LeadForm(request.POST, is_admin=admin)
        if form.is_valid():
            try:
                with transaction.atomic():
                    if contact:
                        # Claim the contact atomically. Only one concurrent request can flip
                        # it to "converted"; the other gets 0 rows and is turned away.
                        claimed = (Contact.objects.filter(pk=contact.pk, is_deleted=False)
                                   .exclude(status=Contact.STATUS_CONVERTED)
                                   .update(status=Contact.STATUS_CONVERTED, updated_at=timezone.now()))
                        if not claimed or Lead.objects.filter(contact_id=contact.pk).exists():
                            raise _ContactAlreadyConverted()

                    lead = form.save(commit=False)
                    lead.created_by = request.user
                    lead.reference_by = (contact.reference_by if contact and contact.reference_by_id else request.user)
                    owner = form.cleaned_data.get("assigned_to_user") if admin else request.user
                    if admin and owner is None and contact and contact.current_assigned_to_id:
                        owner = contact.current_assigned_to
                    lead.assigned_to = owner
                    lead.original_assigned_to = owner
                    if contact:
                        lead.contact, lead.import_batch = contact, contact.import_batch
                    lead.save()
                    services.log_activity(request.user, "created", "Lead created" + (f" from contact #{contact.pk}" if contact else ""), lead=lead)
                    if owner:
                        AssignmentHistory.objects.create(
                            lead=lead, action=AssignmentHistory.ACTION_ASSIGN, to_user=owner, to_name=user_label(owner),
                            changed_by=request.user, changed_by_name=user_label(request.user), reason="Lead created")
                        services.log_activity(request.user, "assign", f"Assigned to {user_label(owner)}", lead=lead)
                    services.log_audit(request.user, "lead_created", f"{lead.display_id} created ({lead.customer_name})",
                                       {"reference_by": user_label(lead.reference_by), "assigned_to": user_label(owner)}, "lead", lead.pk)
                    if contact:
                        services.log_activity(request.user, "converted", f"Converted to lead {lead.display_id}", contact=contact)
                        services.log_audit(request.user, "contact_converted",
                                           f"Contact #{contact.pk} ({contact.name}) converted to {lead.display_id}",
                                           {"contact_id": contact.pk, "lead_id": lead.pk}, "contact", contact.pk)
            except _ContactAlreadyConverted:
                return _already_converted_response(request, contact)

            # Lead + contact conversion are committed. From here on nothing may undo them.
            try:
                synced, error_message = send_lead_to_n8n(lead)
            except Exception:  # send_lead_to_n8n() promises not to raise; belt and braces
                logging.getLogger("crm.n8n").exception("Unexpected n8n error for lead %s", lead.pk)
                synced = False
            # The Lead ID only exists now that the row is saved, so this is the
            # first place it can honestly be shown to the user.
            if synced:
                messages.success(request, f"Lead {lead.display_id} created successfully and automation started."
                                 if not contact else f"{contact.name} converted to lead {lead.display_id} and automation started.")
            else:
                messages.warning(request, f"Lead {lead.display_id} saved successfully, but automation sync is pending."
                                 if not contact else f"{contact.name} was converted to lead {lead.display_id}, "
                                                     "but automation sync is pending.")
            return redirect("lead_detail", lead_id=lead.pk)
    else:
        initial = {}
        if contact:
            initial = {
                "form_date": timezone.localdate(), "customer_name": contact.name, "contact_number": contact.phone,
                "work_profile": contact.work_profile, "income": contact.income, "requirement": contact.requirement,
                "loan_amount": contact.loan_amount, "email": contact.email, "city": contact.city,
                "source": contact.source, "assigned_to_user": contact.current_assigned_to_id,
            }
        form = LeadForm(initial=initial, is_admin=admin)

    return render(request, "leads/lead_form.html",
                  {"form": form, "active_page": "add_lead", "from_contact": contact, "is_admin_view": admin})


@active_account_required
def lead_edit(request, lead_id):
    """
    Edit Lead — same LeadForm as Add New Lead. Does not re-fire the n8n
    webhook (that only happens once, at creation).
    """
    lead = access.get_visible_lead_or_404(request.user, lead_id)
    if not access.can_edit_lead(request.user, lead):
        raise PermissionDenied("You can't edit this lead.")
    admin = access.is_admin(request.user)

    if request.method == "POST":
        old_status = lead.status
        form = LeadForm(request.POST, instance=lead, is_admin=admin)
        if form.is_valid():
            changed = [f for f in form.changed_data if f != "assigned_to_user"]
            lead = form.save(commit=False)
            new_owner = form.cleaned_data.get("assigned_to_user") if admin else None
            lead.save()
            if admin and "assigned_to_user" in form.changed_data and new_owner != lead.assigned_to:
                try:
                    services.assign_lead(lead, new_owner, request.user, action=AssignmentHistory.ACTION_REASSIGN,
                                         reason="Edited by admin")
                except services.AssignmentError as exc:
                    messages.error(request, str(exc))
            if lead.status != old_status:
                labels = dict(Lead.STATUS_CHOICES)
                services.log_activity(request.user, "status", f"Status changed: {labels.get(old_status)} → {labels.get(lead.status)}", lead=lead)
                services.log_audit(request.user, "lead_status_changed",
                                   f"{lead.display_id}: {labels.get(old_status)} → {labels.get(lead.status)}",
                                   {"from": old_status, "to": lead.status}, "lead", lead.pk)
            if changed:
                services.log_activity(request.user, "edited", "Lead edited: " + ", ".join(sorted(changed)), lead=lead)
                services.log_audit(request.user, "lead_edited", f"{lead.display_id} edited",
                                   {"fields": sorted(changed)}, "lead", lead.pk)
            messages.success(request, "Lead updated successfully.")
            return redirect("lead_detail", lead_id=lead.pk)
    else:
        form = LeadForm(instance=lead, is_admin=admin)

    return render(request, "leads/lead_form.html",
                  {"form": form, "active_page": "add_lead", "mode": "edit", "lead": lead, "is_admin_view": admin})


@active_account_required
def lead_delete(request, lead_id):
    """GET shows a confirmation page; the deletion only happens on a CSRF-protected POST."""
    lead = access.get_visible_lead_or_404(request.user, lead_id)
    if not access.can_delete_lead(request.user, lead):
        raise PermissionDenied("You can't delete this lead.")

    if request.method == "POST":
        customer_name, code = lead.customer_name, lead.display_id
        lead.delete()
        services.log_audit(request.user, "lead_deleted", f"{code} deleted ({customer_name})", {}, "lead", lead_id)
        messages.success(request, f"Lead for {customer_name} was deleted.")
        return redirect(_lead_home(request.user))

    return render(request, "leads/lead_confirm_delete.html", {"lead": lead, "active_page": "add_lead"})


@active_account_required
def lead_detail(request, lead_id):
    lead = access.get_visible_lead_or_404(request.user, lead_id)
    items = services.lead_checklist(lead)
    summary = services.checklist_summary(items)
    attached_ids = {i["type"].pk for i in items}
    extra_types = services.active_doc_types().filter(is_default=False).exclude(pk__in=attached_ids)
    can_transfer = access.can_transfer_lead(request.user, lead)
    ctx = {
        "lead": lead, "items": items, "summary": summary, "extra_types": extra_types,
        "timeline": lead.activities.all()[:200],
        "assignments": lead.assignment_history.all(),
        "doc_history": lead.document_history.all()[:100],
        "can_edit": access.can_edit_lead(request.user, lead),
        "can_delete": access.can_delete_lead(request.user, lead),
        "can_transfer": can_transfer,
        "transfer_form": TransferForm() if can_transfer else None,
        "transfer_targets": services.active_staff().exclude(pk=lead.assigned_to_id) if can_transfer else [],
        "active_page": "all_leads" if access.is_admin(request.user) else "my_leads",
        "is_admin_view": access.is_admin(request.user),
        "interest_choices": Lead.INTEREST_CHOICES,
        "status_choices": Lead.STATUS_CHOICES,
    }
    return render(request, "leads/lead_detail.html", ctx)


@active_account_required
def lead_action(request, lead_id):
    """All small POST actions on a lead (transfer, checklist, follow-up, call log, status)."""
    if request.method != "POST":
        return redirect("lead_detail", lead_id=lead_id)
    lead = access.get_visible_lead_or_404(request.user, lead_id)
    user, action = request.user, request.POST.get("action")
    back = redirect("lead_detail", lead_id=lead.pk)

    if action == "transfer":
        if not access.can_transfer_lead(user, lead):
            raise PermissionDenied("You can't transfer this lead.")
        form = TransferForm(request.POST)
        if form.is_valid():
            try:
                services.transfer_lead(lead, form.cleaned_data["to_user"], user, form.cleaned_data["reason"])
                messages.success(request, f"Lead transferred to {user_label(form.cleaned_data['to_user'])}. Reference By is unchanged.")
                # the sender may lose access; go somewhere safe
                if not access.visible_leads(user).filter(pk=lead.pk).exists():
                    return redirect(_lead_home(user))
            except services.AssignmentError as exc:
                messages.error(request, str(exc))
        else:
            messages.error(request, "Choose a staff member to transfer to.")
        return back

    if not access.can_edit_lead(user, lead):
        raise PermissionDenied("You can't change this lead.")

    if action == "checklist":
        wanted = {int(i) for i in request.POST.getlist("docs") if i.isdigit()}
        changes = services.save_lead_checklist(lead, wanted, user)
        messages.success(request, "Checklist updated." if changes else "No checklist changes.")
    elif action == "add_doc":
        dt = services.active_doc_types().filter(pk=request.POST.get("doc_type") or 0, is_default=False).first()
        if dt:
            services.attach_extra_document(lead, dt, user)
            messages.success(request, f"{dt.name} added to this lead's checklist.")
    elif action == "followup":
        import datetime as _dt

        raw_date, raw_time = request.POST.get("next_followup_date", ""), request.POST.get("next_followup_time", "")
        try:
            date = parse_ddmmyyyy(raw_date, required=False)
            time = _dt.time.fromisoformat(raw_time) if raw_time else None
        except ValueError:
            messages.error(request, "Enter a valid follow-up date (DD/MM/YYYY) and time.")
            return back
        lead.next_followup_date, lead.next_followup_time = date, time
        lead.followup_notes = request.POST.get("followup_notes", "")[:1000]
        lead.followup_status = Lead.FOLLOWUP_PENDING if date else ""
        lead.save(update_fields=["next_followup_date", "next_followup_time", "followup_notes", "followup_status", "updated_at"])
        services.log_activity(user, "followup", f"Follow-up scheduled for {date:%d/%m/%Y}" if date else "Follow-up cleared", lead=lead)
        messages.success(request, "Follow-up saved.")
    elif action == "followup_done":
        lead.followup_status, lead.last_contacted = Lead.FOLLOWUP_DONE, timezone.now()
        lead.save(update_fields=["followup_status", "last_contacted", "updated_at"])
        services.log_activity(user, "followup", "Follow-up marked done", lead=lead)
        messages.success(request, "Follow-up marked done.")
    elif action == "log_call":
        lead.last_contacted = timezone.now()
        interest = request.POST.get("interest", "")
        if interest in ("interested", "not_interested"):
            lead.interest = interest
        lead.save(update_fields=["last_contacted", "interest", "updated_at"])
        note = request.POST.get("note", "").strip()[:300]
        services.log_activity(user, "call", "Call completed" + (f" — {note}" if note else "") +
                              (f" ({dict(Lead.INTEREST_CHOICES).get(lead.interest)})" if lead.interest else ""), lead=lead)
        messages.success(request, "Call logged.")
    elif action == "status":
        new = request.POST.get("status")
        if new in dict(Lead.STATUS_CHOICES) and new != lead.status:
            labels = dict(Lead.STATUS_CHOICES)
            old = lead.status
            lead.status = new
            lead.save(update_fields=["status", "updated_at"])
            services.log_activity(user, "status", f"Status changed: {labels[old]} → {labels[new]}", lead=lead)
            services.log_audit(user, "lead_status_changed", f"{lead.display_id}: {labels[old]} → {labels[new]}",
                               {"from": old, "to": new}, "lead", lead.pk)
            messages.success(request, "Status updated.")
    return back


# =========================================================
# REPORTS
# =========================================================


@admin_required
def reports(request):
    leads = Lead.objects.all()
    counts = dict(leads.values_list("status").annotate(c=Count("id")).values_list("status", "c"))
    by_status = [{"label": label, "value": counts.get(key, 0)} for key, label in Lead.STATUS_CHOICES]
    return render(request, "reports.html", {"by_status": by_status, "total": leads.count(), "active_page": "reports"})
