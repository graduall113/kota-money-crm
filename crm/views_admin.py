"""Admin-only screens: Settings, Document Checklist manager, Audit Log, Analytics, Staff detail."""
from django.contrib import messages
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, Max
from django.shortcuts import get_object_or_404, redirect, render

from . import analytics, filters, services
from .decorators import active_account_required, admin_required
from .forms import DocumentTypeForm
from .models import AuditLog, Contact, DocumentHistory, DocumentType, ImportBatch, Lead, LeadDocument, Setting, user_label
from .settings_store import (
    BOOL_KEYS, DEFAULTS, get_bool, get_int, get_n8n_webhook_url, get_setting, set_setting, validate_webhook_url,
)

SETTINGS_SECTIONS = [
    ("account", "Account"), ("leads", "Leads & Assignment"), ("imports", "Import"), ("n8n", "n8n / Automation"),
    ("documents", "Document Checklist"), ("security", "Security"), ("data", "Data Management"),
]


@active_account_required
def settings_page(request):
    admin = request.user.staff_profile.is_admin
    section = request.GET.get("section") or request.POST.get("section") or "account"
    if not admin:
        section = "account"
    if request.method == "POST" and admin:
        handler = {"leads": _save_leads, "imports": _save_imports, "n8n": _save_n8n, "security": _save_security}.get(section)
        if handler:
            handler(request)
        return redirect(f"{request.path}?section={section}")
    ctx = {
        "sections": SETTINGS_SECTIONS if admin else [("account", "Account")], "section": section,
        "active_page": "settings_page", "is_admin_view": admin,
        "s": {k: (get_bool(k) if k in BOOL_KEYS else get_setting(k)) for k in DEFAULTS},
        "n8n_effective": get_n8n_webhook_url(),
    }
    if section == "data" and admin:
        ctx.update({"n_leads": Lead.objects.count(), "n_contacts": Contact.objects.count(),
                    "n_batches": ImportBatch.objects.count(), "n_audit": AuditLog.objects.count()})
    return render(request, "settings.html", ctx)


def _changed(request, before, after, label):
    diff = {k: {"from": before.get(k), "to": after[k]} for k in after if str(before.get(k)) != str(after[k])}
    if diff:
        services.log_audit(request.user, "settings_changed", f"{label} settings changed", {"changes": diff}, "settings")
    return diff


def _save_leads(request):
    keys = ["referrer_keeps_access", "staff_can_transfer", "staff_can_export"]
    before = {k: get_bool(k) for k in keys}
    after = {k: request.POST.get(k) == "on" for k in keys}
    for k, v in after.items():
        set_setting(k, v, request.user)
    _changed(request, before, after, "Lead & assignment")
    messages.success(request, "Lead settings saved.")


def _save_imports(request):
    try:
        chunk = int(request.POST.get("import_chunk_size", ""))
        mb = int(request.POST.get("import_max_file_mb", ""))
    except ValueError:
        messages.error(request, "Enter whole numbers.")
        return
    policy = request.POST.get("import_default_duplicate_policy")
    if not (200 <= chunk <= 10000) or not (1 <= mb <= 500) or policy not in dict(ImportBatch.DUP_CHOICES):
        messages.error(request, "Chunk size must be 200–10,000, max file size 1–500 MB, and a valid duplicate policy.")
        return
    before = {k: get_setting(k) for k in ("import_chunk_size", "import_max_file_mb", "import_default_duplicate_policy")}
    after = {"import_chunk_size": chunk, "import_max_file_mb": mb, "import_default_duplicate_policy": policy}
    for k, v in after.items():
        set_setting(k, v, request.user)
    _changed(request, before, after, "Import")
    messages.success(request, "Import settings saved.")


def _save_n8n(request):
    url = request.POST.get("n8n_webhook_url", "").strip()
    error = validate_webhook_url(url)
    if error:
        messages.error(request, error)
        return
    before = {"n8n_webhook_url": get_setting("n8n_webhook_url"), "n8n_enabled": get_bool("n8n_enabled")}
    after = {"n8n_webhook_url": url, "n8n_enabled": request.POST.get("n8n_enabled") == "on"}
    set_setting("n8n_webhook_url", url, request.user)
    set_setting("n8n_enabled", after["n8n_enabled"], request.user)
    _changed(request, before, after, "n8n")
    messages.success(request, "n8n settings saved." if url else "Saved — using the built-in default webhook URL.")


def _save_security(request):
    before = {"allow_public_registration": get_bool("allow_public_registration")}
    after = {"allow_public_registration": request.POST.get("allow_public_registration") == "on"}
    set_setting("allow_public_registration", after["allow_public_registration"], request.user)
    _changed(request, before, after, "Security")
    messages.success(request, "Security settings saved.")


# ------------------------------------------------------------------ document checklist manager
def _renumber():
    for i, dt in enumerate(DocumentType.objects.filter(is_archived=False).order_by("sort_order", "id"), start=1):
        if dt.sort_order != i * 10:
            DocumentType.objects.filter(pk=dt.pk).update(sort_order=i * 10)


@admin_required
def document_settings(request):
    """Add / rename / enable-disable / reorder / require / archive checklist documents."""
    edit_id = request.GET.get("edit")
    instance = DocumentType.objects.filter(pk=edit_id, is_archived=False).first() if edit_id else None

    if request.method == "POST":
        op = request.POST.get("op")
        dt = DocumentType.objects.filter(pk=request.POST.get("id") or 0, is_archived=False).first()
        if op == "save":
            form = DocumentTypeForm(request.POST, instance=dt)
            if form.is_valid():
                was_new = dt is None
                old_name = dt.name if dt else ""
                obj = form.save(commit=False)
                if was_new:
                    obj.sort_order = (DocumentType.objects.aggregate(m=Max("sort_order"))["m"] or 0) + 10
                obj.save()
                services.log_audit(request.user, "document_type_added" if was_new else "document_type_edited",
                                   f"Document '{obj.name}' " + ("added" if was_new else f"updated (was '{old_name}')"),
                                   {"required": obj.is_required, "default": obj.is_default, "active": obj.is_active},
                                   "document_type", obj.pk)
                messages.success(request, "Document saved.")
                return redirect("document_settings")
            return render(request, "settings_documents.html", _doc_ctx(form, instance=dt))
        if dt:
            if op == "toggle":
                dt.is_active = not dt.is_active
                dt.save(update_fields=["is_active"])
                services.log_audit(request.user, "document_type_disabled",
                                   f"Document '{dt.name}' {'enabled' if dt.is_active else 'disabled'}", {}, "document_type", dt.pk)
            elif op in ("up", "down"):
                with transaction.atomic():
                    _renumber()
                    dt.refresh_from_db()
                    ordered = list(DocumentType.objects.filter(is_archived=False).order_by("sort_order", "id"))
                    i = next(n for n, d in enumerate(ordered) if d.pk == dt.pk)
                    j = i - 1 if op == "up" else i + 1
                    if 0 <= j < len(ordered):
                        a, b = ordered[i], ordered[j]
                        DocumentType.objects.filter(pk=a.pk).update(sort_order=b.sort_order)
                        DocumentType.objects.filter(pk=b.pk).update(sort_order=a.sort_order)
            elif op == "delete":
                used = LeadDocument.objects.filter(document_type=dt).exists() or dt.lead_documents.exists()
                used = used or DocumentHistory.objects.filter(document_type=dt).exists()
                name = dt.name
                if used:  # never destroy history: archive instead (hidden everywhere, history intact)
                    dt.is_archived, dt.is_active = True, False
                    dt.save(update_fields=["is_archived", "is_active"])
                    msg = f"'{name}' has history, so it was archived instead of erased."
                else:
                    dt.delete()
                    msg = f"'{name}' was deleted."
                services.log_audit(request.user, "document_type_deleted", f"Document '{name}' removed", {"archived": used}, "document_type", "")
                messages.success(request, msg)
        return redirect("document_settings")
    return render(request, "settings_documents.html", _doc_ctx(DocumentTypeForm(instance=instance), instance=instance))


def _doc_ctx(form, instance=None):
    docs = list(DocumentType.objects.filter(is_archived=False))
    counts = dict(LeadDocument.objects.filter(received=True).values_list("document_type").annotate(n=Count("id")).values_list("document_type", "n"))
    for d in docs:
        d.received_count = counts.get(d.pk, 0)
    return {"docs": docs, "form": form, "editing": instance, "active_page": "settings_page",
            "sections": SETTINGS_SECTIONS, "section": "documents", "is_admin_view": True}


# ------------------------------------------------------------------ audit log
@admin_required
def audit_log(request):
    qs = AuditLog.objects.all()
    p = request.GET
    if p.get("action"):
        qs = qs.filter(action=p["action"])
    if p.get("actor", "").isdigit():
        qs = qs.filter(actor_id=int(p["actor"]))
    if p.get("q"):
        qs = qs.filter(summary__icontains=p["q"].strip())
    qs = filters._range(qs, "created_at", p.get("from"), p.get("to"))
    page = filters.paginate(request, qs, per_page=50)
    return render(request, "audit_log.html", {
        "page": page, "logs": page.object_list, "querystring": filters.querystring_without(request, "page"),
        "actions": AuditLog.objects.order_by().values_list("action", flat=True).distinct(),
        "admins": User.objects.filter(staff_profile__role="admin"), "GET": p, "active_page": "audit",
    })


# ------------------------------------------------------------------ analytics
@admin_required
def performance(request):
    rows = analytics.staff_performance(request.GET)
    return render(request, "analytics/performance.html", {"rows": rows, "GET": request.GET, "active_page": "analytics"})


@admin_required
def document_analytics(request):
    report = analytics.document_report(request.GET)
    return render(request, "analytics/documents.html", {
        "r": report, "GET": request.GET, "staff_members": services.active_staff(),
        "doc_types": services.active_doc_types(), "active_page": "analytics",
    })


# ------------------------------------------------------------------ staff detail
@admin_required
def staff_detail(request, user_id):
    target = get_object_or_404(User.objects.select_related("staff_profile"), pk=user_id)
    perf = next((r for r in analytics.staff_performance({}) if r["user"].pk == target.pk), None)
    return render(request, "staff/staff_detail.html", {
        "target": target, "perf": perf,
        "leads_count": Lead.objects.filter(assigned_to=target).count(),
        "contacts_count": Contact.objects.filter(current_assigned_to=target).count(),
        "referred_count": Lead.objects.filter(reference_by=target).count(),
        "active_page": "staff",
    })
