"""Contacts module + shared bulk-action / export endpoints for leads and contacts."""
import datetime

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.contrib.auth.models import User
from django.db.models import Count
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import access, exports, filters, jobs, services
from .datefmt import parse_ddmmyyyy
from .decorators import active_account_required, admin_required
from .models import AssignmentHistory, BackgroundJob, Contact, ImportBatch, Lead, Segment, user_label


# ------------------------------------------------------------------ contacts list / detail
@active_account_required
def contacts_list(request):
    user = request.user
    admin = access.is_admin(user)
    qs = filters.filter_contacts(
        access.visible_contacts(user).select_related("reference_by", "current_assigned_to", "import_batch").prefetch_related("segments"), request.GET)
    page = filters.paginate(request, qs)
    visible = access.visible_contacts(user)
    return render(request, "contacts/contact_list.html", {
        "page": page, "contacts": page.object_list, "total_count": page.paginator.count,
        "staff_members": services.active_staff() if admin else [],
        "bulk_staff": services.active_staff(),
        "status_choices": Contact.STATUS_CHOICES,
        "batches": ImportBatch.objects.filter(pk__in=visible.exclude(import_batch=None).values("import_batch")[:1000]).order_by("-created_at")[:100],
        "segments": Segment.objects.filter(is_active=True).order_by("name"),
        "sources": list(visible.exclude(source="").values_list("source", flat=True).distinct()[:100]),
        "querystring": filters.querystring_without(request, "page"),
        "is_admin_view": admin, "can_export": access.can_export(user),
        "can_manage_segments": access.can_manage_segments(user),
        "active_page": "contacts", "GET": request.GET,
        "has_filters": any(k for k in request.GET if k not in ("page", "per_page", "sort")),
    })


@active_account_required
def contact_detail(request, contact_id):
    contact = access.get_visible_contact_or_404(request.user, contact_id)
    admin = access.is_admin(request.user)
    return render(request, "contacts/contact_detail.html", {
        "contact": contact, "history": contact.assignment_history.all(),
        "timeline": contact.activities.all()[:100], "leads": contact.leads.all(),
        "can_edit": access.can_edit_contact(request.user, contact),
        "staff_members": services.active_staff() if admin else [],
        "can_manage_segments": access.can_manage_segments(request.user),
        "all_segments": Segment.objects.filter(is_active=True).order_by("name") if access.can_manage_segments(request.user) else [],
        "status_choices": Contact.STATUS_CHOICES, "active_page": "contacts", "is_admin_view": admin,
    })


@active_account_required
def contact_action(request, contact_id):
    if request.method != "POST":
        return redirect("contact_detail", contact_id=contact_id)
    contact = access.get_visible_contact_or_404(request.user, contact_id)
    user, action = request.user, request.POST.get("action")
    back = redirect("contact_detail", contact_id=contact.pk)

    if action == "assign":  # admin only
        if not access.is_admin(user):
            raise PermissionDenied("Only admins can assign contacts.")
        target = services.active_staff().filter(pk=request.POST.get("to_user") or 0).first()
        try:
            services.assign_contact(contact, target, user,
                                    action=AssignmentHistory.ACTION_REASSIGN if contact.current_assigned_to_id else AssignmentHistory.ACTION_ASSIGN,
                                    reason=request.POST.get("reason", ""))
            messages.success(request, "Contact assignment updated.")
        except services.AssignmentError as exc:
            messages.error(request, str(exc))
        return back

    if not access.can_edit_contact(user, contact):
        raise PermissionDenied("You can't change this contact.")

    if action == "update":
        status = request.POST.get("status")
        old = contact.status
        if status in dict(Contact.STATUS_CHOICES) and status != Contact.STATUS_CONVERTED:
            contact.status = status
        contact.notes = request.POST.get("notes", "")[:2000]
        raw_date = request.POST.get("next_followup_date", "")
        raw_time = request.POST.get("next_followup_time", "")
        try:
            contact.next_followup_date = parse_ddmmyyyy(raw_date, required=False)
            contact.next_followup_time = datetime.time.fromisoformat(raw_time) if raw_time else None
        except ValueError:
            messages.error(request, "Enter a valid follow-up date (DD/MM/YYYY) and time.")
            return back
        contact.followup_notes = request.POST.get("followup_notes", "")[:1000]
        contact.followup_status = "pending" if contact.next_followup_date else ""
        if contact.status != Contact.STATUS_NEW and old == Contact.STATUS_NEW:
            contact.last_contacted = timezone.now()
        contact.save()
        if old != contact.status:
            services.log_activity(user, "status", f"Status changed: {dict(Contact.STATUS_CHOICES)[old]} → {contact.get_status_display()}", contact=contact)
        messages.success(request, "Contact updated.")
    elif action == "log_call":
        contact.last_contacted = timezone.now()
        if contact.status == Contact.STATUS_NEW:
            contact.status = Contact.STATUS_CONTACTED
        contact.save()
        services.log_activity(user, "call", "Call completed", contact=contact)
        messages.success(request, "Call logged.")
    elif action == "followup_done":
        contact.followup_status, contact.last_contacted = "done", timezone.now()
        contact.save()
        services.log_activity(user, "followup", "Follow-up marked done", contact=contact)
    return back


# ------------------------------------------------------------------ export
def _export(request, model_name):
    if not access.can_export(request.user):
        raise PermissionDenied("Export is not enabled for your role.")
    fmt = "xlsx" if request.GET.get("format") == "xlsx" else "csv"
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    if model_name == "lead":
        qs = filters.filter_leads(access.visible_leads(request.user).select_related("reference_by", "assigned_to"), request.GET)
        resp = exports.export_queryset(qs, exports.lead_columns(), fmt, f"kota-money-leads-{stamp}")
    else:
        qs = filters.filter_contacts(access.visible_contacts(request.user).select_related("reference_by", "current_assigned_to", "import_batch"), request.GET)
        resp = exports.export_queryset(qs, exports.contact_columns(), fmt, f"kota-money-contacts-{stamp}")
    services.log_audit(request.user, "export", f"{model_name.title()} export ({fmt})",
                       {"query": request.GET.urlencode()[:300]}, model_name)
    return resp


@active_account_required
def leads_export(request):
    return _export(request, "lead")


@active_account_required
def contacts_export(request):
    return _export(request, "contact")


# ------------------------------------------------------------------ bulk actions (leads + contacts)
STAFF_ACTIONS = {"status", "followup", "transfer"}
ADMIN_ACTIONS = STAFF_ACTIONS | {"assign", "reassign", "delete", "add_to_segment"}
CONTACT_ONLY_ACTIONS = {"add_to_segment"}  # segments are a Contact concept, not a Lead one
ACTION_LABELS = {
    "assign": "assign", "reassign": "reassign", "transfer": "transfer", "status": "change the status of",
    "followup": "set a follow-up on", "delete": "permanently delete", "add_to_segment": "add to a segment",
}


def _scope_from_post(request):
    kind = request.POST.get("scope_type")
    if kind == "filter":
        return {"type": "filter", "query": request.POST.get("query", "")}
    if kind == "batch":
        return {"type": "batch", "batch_id": int(request.POST.get("batch_id") or 0),
                "only_unassigned": request.POST.get("only_unassigned") == "1"}
    return {"type": "ids", "ids": [i for i in request.POST.getlist("ids") if i.isdigit()][:5000]}


@active_account_required
def bulk_action(request, model_name):
    """
    Two-step flow: POST (build + validate) -> confirmation page showing exact
    counts -> POST with confirmed=1 -> job. Every step re-derives the records
    from the acting user's authorized set, so nothing here can reach data the
    user isn't allowed to see.
    """
    if request.method != "POST" or model_name not in ("lead", "contact"):
        raise Http404
    user = request.user
    admin = access.is_admin(user)
    action = request.POST.get("action", "")
    allowed = ADMIN_ACTIONS if admin else STAFF_ACTIONS
    if action not in allowed:
        raise PermissionDenied("This bulk action isn't available to you.")
    if action in CONTACT_ONLY_ACTIONS and model_name != "contact":
        raise Http404
    model = jobs.MODELS[model_name]
    scope = _scope_from_post(request)
    if scope["type"] == "batch" and not admin:
        raise PermissionDenied("Batch operations are for admins.")

    params = {"model": model_name, "scope": scope, "reason": request.POST.get("reason", "")[:300]}
    problems = []
    if action in ("assign", "reassign", "transfer"):
        method = request.POST.get("method", "one")
        staff_ids = [int(i) for i in request.POST.getlist("staff_ids") if i.isdigit()]
        if not admin:
            method = "one"
            staff_ids = staff_ids[:1]
        if method == "one" and not staff_ids and request.POST.get("to_user", "").isdigit():
            staff_ids = [int(request.POST["to_user"])]
        params.update({"method": method, "staff_ids": staff_ids,
                       "counts": {k[6:]: v for k, v in request.POST.items() if k.startswith("count_")},
                       "percents": {k[8:]: v for k, v in request.POST.items() if k.startswith("percent_")}})
        if not staff_ids:
            problems.append("Choose at least one staff member.")
        elif method == "one":
            params["staff_ids"] = staff_ids[:1]
    elif action == "status":
        valid = dict(model.STATUS_CHOICES)
        if request.POST.get("status") not in valid:
            problems.append("Choose a valid status.")
        params["status"] = request.POST.get("status")
    elif action == "followup":
        try:
            due = parse_ddmmyyyy(request.POST.get("date", ""))
        except ValueError:
            problems.append("Enter a valid follow-up date (DD/MM/YYYY).")
        else:
            params["date"] = due.isoformat()
        params.update({"time": request.POST.get("time", ""), "notes": request.POST.get("notes", "")[:1000]})
    elif action == "add_to_segment":
        new_name = request.POST.get("new_segment", "").strip()
        segment = None
        if new_name:
            segment = Segment.objects.filter(name__iexact=new_name).first()  # resolved for real on confirm
            params["new_segment"] = new_name[:150]
        elif request.POST.get("segment_id", "").isdigit():
            segment = Segment.objects.filter(pk=int(request.POST["segment_id"])).first()
        if not segment and not new_name:
            problems.append("Choose a segment, or name a new one to create.")
        params["segment_id"] = segment.pk if segment else None
        params["segment_name"] = segment.name if segment else new_name

    qs = jobs.owned_only(model, jobs.resolve_queryset(model_name, user, scope), user)
    count = qs.count()
    if count == 0:
        problems.append("No matching records — nothing to do.")
    if scope["type"] == "filter" and request.POST.get("expected_count", "").isdigit() and int(request.POST["expected_count"]) != count:
        problems.append("The list changed since you selected it. Please reload and try again.")
    if action in ("assign", "reassign", "transfer") and not problems:
        active_ids = set(User.objects.filter(pk__in=params["staff_ids"], staff_profile__status="active").values_list("pk", flat=True))
        if len(active_ids) != len(set(params["staff_ids"])):
            problems.append("One of the selected staff members is inactive or doesn't exist.")
        try:  # dry-run so impossible plans fail BEFORE anything is changed
            staff = [User.objects.get(pk=i) for i in params["staff_ids"] if i in active_ids]
            services.build_plan(params["method"], list(range(count)), staff, params)
        except services.AssignmentError as exc:
            problems.append(str(exc))

    back = "all_leads" if model_name == "lead" else "contacts"
    if problems:
        for p in problems:
            messages.error(request, p)
        return redirect(back)

    if request.POST.get("confirmed") != "1":
        from_names = {}
        if action in ("assign", "reassign", "transfer"):
            field = "assigned_to" if model is Lead else "current_assigned_to"
            for row in qs.values(field).annotate(c=Count("id")):
                who = user_label(User.objects.filter(pk=row[field]).first()) if row[field] else "Unassigned"
                from_names[who] = row["c"]
        to_names = [user_label(u) for u in User.objects.filter(pk__in=params.get("staff_ids", []))]
        return render(request, "bulk_confirm.html", {
            "model_name": model_name, "action": action, "count": count, "params": params,
            "verb": ACTION_LABELS[action], "from_names": from_names, "to_names": to_names,
            "destructive": action == "delete", "post": request.POST, "scope": scope,
            "ids": scope.get("ids", []), "back": back, "active_page": "contacts" if model_name == "contact" else "all_leads",
        })

    if action == "add_to_segment":
        # Segment is only actually created here, on confirmed submission — never
        # on the earlier "show me the confirmation page" POST.
        if params.get("segment_id"):
            segment = Segment.objects.filter(pk=params["segment_id"]).first()
        else:
            try:
                segment = services.get_or_create_segment(params.get("new_segment", ""), user)
            except services.AssignmentError as exc:
                messages.error(request, str(exc))
                return redirect(back)
        params["segment_id"] = segment.pk
        title = f"Add {count:,} contacts to '{segment.name}'"
    else:
        title = f"{action.title()} {count:,} {model_name}s"
    job = services.new_job(action, title, count, params, user)
    services.start_job(job)
    return redirect("job_detail", job_id=job.pk)


@active_account_required
def job_detail(request, job_id):
    job = get_object_or_404(BackgroundJob, pk=job_id)
    if job.created_by_id != request.user.id and not access.is_admin(request.user):
        raise Http404
    if request.GET.get("json"):
        return JsonResponse({"status": job.status, "processed": job.processed, "total": job.total,
                             "percent": job.progress_percent, "error": job.error, "result": job.result})
    return render(request, "job_detail.html", {"job": job, "active_page": "contacts"})


# ------------------------------------------------------------------ dedicated bulk-assign page (admin)
@admin_required
def bulk_assign_page(request):
    """Distribution planner: one / equal / custom / percent / random across staff."""
    model_name = request.GET.get("model", "contact")
    if model_name not in ("lead", "contact"):
        raise Http404
    model = jobs.MODELS[model_name]
    scope = {"type": "filter", "query": request.GET.get("query", "")}
    batch = None
    if request.GET.get("batch_id", "").isdigit():
        batch = get_object_or_404(ImportBatch, pk=request.GET["batch_id"])
        scope = {"type": "batch", "batch_id": batch.pk, "only_unassigned": request.GET.get("only_unassigned") == "1"}
    qs = jobs.resolve_queryset(model_name, request.user, scope)
    segment = None
    if scope["type"] == "filter" and "segment=" in scope["query"] and model_name == "contact":
        seg_id = dict(pair.split("=") for pair in scope["query"].split("&") if "=" in pair).get("segment")
        segment = Segment.objects.filter(pk=seg_id).first() if seg_id and seg_id.isdigit() else None
    ctx = {
        "model_name": model_name, "scope": scope, "batch": batch, "count": qs.count(),
        "staff_members": services.active_staff(), "query": request.GET.get("query", ""),
        "only_unassigned": request.GET.get("only_unassigned") == "1",
        "active_page": "segments" if segment else "contacts", "segment": segment,
    }
    if segment:
        ctx["assigned_count"] = qs.exclude(current_assigned_to=None).count()
        ctx["unassigned_count"] = ctx["count"] - ctx["assigned_count"]
    return render(request, "assign_page.html", ctx)
