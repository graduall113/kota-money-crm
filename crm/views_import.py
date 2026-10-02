"""Admin: Import Data (upload -> map -> validate -> configure -> import -> report)."""
import csv
import logging
import os

from django.contrib import messages
from django.db.models import Count
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import filters, importer, services
from .decorators import admin_required
from .exports import _Echo, _safe
from .forms import ImportUploadForm
from .models import Contact, ImportBatch, ImportReviewRow, ImportRowError, Segment
from .settings_store import get_int, get_setting

logger = logging.getLogger("crm")


@admin_required
def import_list(request):
    batches = ImportBatch.objects.select_related("uploaded_by")
    page = filters.paginate(request, batches, per_page=25)
    for b in page.object_list:
        b.assignment_label = b.assignment_status
    return render(request, "imports/import_list.html", {"page": page, "batches": page.object_list, "active_page": "imports"})


@admin_required
def import_new(request):
    max_mb = get_int("import_max_file_mb", 100)
    if request.method == "POST":
        form = ImportUploadForm(request.POST, request.FILES)
        if form.is_valid():
            f = form.cleaned_data["file"]
            ext = os.path.splitext(f.name)[1].lower()
            if ext not in importer.ALLOWED_EXT:
                form.add_error("file", "Upload a .csv, .xlsx or .xls file.")
            elif f.size > max_mb * 1024 * 1024:
                form.add_error("file", f"File is larger than the {max_mb} MB limit (Admin → Settings → Import).")
            else:
                code = importer.new_batch_code()
                path = importer.upload_dir() / f"{code}{ext}"
                with open(path, "wb") as out:
                    for chunk in f.chunks():
                        out.write(chunk)
                try:
                    headers, rows = importer.read_preview(str(path), ext[1:])
                except importer.ImportProblem as exc:
                    path.unlink(missing_ok=True)
                    form.add_error("file", str(exc))
                except Exception:  # corrupt / password-protected / wrong format
                    path.unlink(missing_ok=True)
                    form.add_error("file", "This file couldn't be read. Check that it's a valid CSV / Excel file.")
                else:
                    batch = ImportBatch.objects.create(
                        code=code, file_name=f.name[:255], stored_path=str(path), file_type=ext[1:],
                        uploaded_by=request.user, headers=headers, source_label=form.cleaned_data["source_label"],
                        mapping=importer.suggest_mapping(headers),
                        duplicate_policy=get_setting("import_default_duplicate_policy"),
                    )
                    services.log_audit(request.user, "import_started", f"Import {code} started ({f.name})",
                                       {"batch": code, "file": f.name}, "import", batch.pk)
                    return redirect("import_map", batch_id=batch.pk)
    else:
        form = ImportUploadForm()
    return render(request, "imports/import_new.html", {"form": form, "max_mb": max_mb, "active_page": "imports"})


def _batch(batch_id):
    return get_object_or_404(ImportBatch, pk=batch_id)


@admin_required
def import_map(request, batch_id):
    batch = _batch(batch_id)
    if batch.is_busy or batch.status in (ImportBatch.STATUS_COMPLETED, ImportBatch.STATUS_REVIEW):
        return redirect("import_detail", batch_id=batch.pk)
    headers = batch.headers
    if request.method == "POST":
        mapping, used, errors = {}, set(), []
        for key, label, required, _ in importer.TARGET_FIELDS:
            raw = request.POST.get(f"map_{key}", "")
            if raw.isdigit() and int(raw) < len(headers):
                idx = int(raw)
                if idx in used:
                    errors.append(f"Column '{headers[idx]}' is mapped to more than one field.")
                used.add(idx)
                mapping[key] = idx
            elif required:
                errors.append(f"{label} must be mapped to a column.")
        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            batch.mapping, batch.status = mapping, ImportBatch.STATUS_MAPPED
            batch.save(update_fields=["mapping", "status"])
            importer.start(batch.pk, "analyze")
            return redirect("import_detail", batch_id=batch.pk)
    try:
        _, preview = importer.read_preview(batch.stored_path, batch.file_type, limit=5)
    except Exception:
        preview = []
    fields = [{"key": k, "label": l, "required": r, "selected": batch.mapping.get(k)} for k, l, r, _ in importer.TARGET_FIELDS]
    return render(request, "imports/import_map.html", {
        "batch": batch, "fields": fields, "headers": list(enumerate(headers)),
        "preview_rows": [cells for _, cells in preview],
        "classification": importer.classify_headers(headers, batch.mapping),
        "active_page": "imports",
    })


@admin_required
def import_detail(request, batch_id):
    batch = _batch(batch_id)
    return render(request, "imports/import_detail.html", {
        "batch": batch, "dup_choices": ImportBatch.DUP_CHOICES,
        "staff_members": services.active_staff(),
        "errors": batch.row_errors.all()[:15], "error_total": batch.row_errors.count(),
        "review_pending": batch.review_rows.filter(applied=False, decision="").count(),
        "review_total": batch.review_rows.filter(applied=False).count(),
        "mapped": [(importer.FIELD_LABELS[k], batch.headers[i] if i < len(batch.headers) else "?") for k, i in batch.mapping.items()],
        "assigned_count": batch.contacts.filter(is_deleted=False).exclude(current_assigned_to=None).count(),
        "contact_count": batch.contacts.filter(is_deleted=False).count(),
        "per_staff": batch.contacts.filter(is_deleted=False).exclude(current_assigned_to=None).values("current_assigned_to__first_name", "current_assigned_to__username").annotate(n=Count("id")).order_by("-n"),
        "segments": Segment.objects.filter(is_active=True).order_by("name"),
        "removable_count": batch.contacts.filter(is_deleted=False).count(),
        # contacts that would be deleted but were converted to a Lead (the Lead is kept)
        "removable_with_leads": batch.contacts.filter(is_deleted=False, leads__isnull=False).distinct().count(),
        # legacy: contacts hidden (not deleted) by an old-style undo, before real deletion existed
        "legacy_hidden_count": batch.contacts.filter(is_deleted=True).count(),
        "active_page": "imports",
    })


@admin_required
def import_progress(request, batch_id):
    b = _batch(batch_id)
    return JsonResponse({
        "status": b.status, "status_label": b.get_status_display(), "busy": b.is_busy, "stalled": b.is_stalled,
        "processed": b.processed_rows, "total": b.total_rows, "percent": b.progress_percent,
        "imported": b.imported_count, "updated": b.updated_count, "duplicates": b.duplicate_count,
        "skipped": b.skipped_count, "errors": b.error_count, "error_message": b.error_message,
    })


@admin_required
def import_start(request, batch_id):
    """Configure + start (or resume) the import."""
    batch = _batch(batch_id)
    if request.method != "POST":
        return redirect("import_detail", batch_id=batch.pk)
    resumable = batch.processed_rows > 0 and (batch.analysis_new + batch.analysis_duplicates) > 0 and (
        (batch.status == ImportBatch.STATUS_IMPORTING and batch.is_stalled) or batch.status == ImportBatch.STATUS_FAILED)
    if resumable:
        batch.status = ImportBatch.STATUS_IMPORTING
        batch.save(update_fields=["status"])
        importer.start(batch.pk, "import")  # resume where it stopped
        messages.success(request, "Import resumed.")
        return redirect("import_detail", batch_id=batch.pk)
    if batch.status != ImportBatch.STATUS_ANALYZED:
        messages.error(request, "This batch isn't ready to import.")
        return redirect("import_detail", batch_id=batch.pk)
    policy = request.POST.get("duplicate_policy")
    if policy not in dict(ImportBatch.DUP_CHOICES):
        messages.error(request, "Choose how duplicates should be handled.")
        return redirect("import_detail", batch_id=batch.pk)
    assignee = services.active_staff().filter(pk=request.POST.get("assign_to") or 0).first()
    new_segment_name = request.POST.get("new_segment", "").strip()
    if new_segment_name:
        segment = services.get_or_create_segment(new_segment_name, request.user)
    elif request.POST.get("add_to_segment", "").isdigit():
        segment = Segment.objects.filter(pk=int(request.POST["add_to_segment"])).first()
    else:
        segment = None
    batch.duplicate_policy, batch.assign_to_on_import, batch.add_to_segment = policy, assignee, segment
    batch.source_label = request.POST.get("source_label", batch.source_label)[:100]
    batch.status, batch.processed_rows = ImportBatch.STATUS_ANALYZED, 0
    batch.save(update_fields=["duplicate_policy", "assign_to_on_import", "add_to_segment", "source_label", "status", "processed_rows"])
    importer.start(batch.pk, "import")
    return redirect("import_detail", batch_id=batch.pk)


@admin_required
def import_undo(request, batch_id):
    """
    Undo Import = permanently DELETE the contacts this batch created (see services.undo_import).
    POST + CSRF only; the confirmation modal on the import page states the exact count, and the
    server re-derives the records itself. Pre-existing / matched / updated contacts are never
    touched, and Leads survive (Lead.contact -> NULL). The batch row stays as history.
    """
    batch = _batch(batch_id)
    if request.method != "POST":
        return redirect("import_detail", batch_id=batch.pk)
    try:
        result = services.undo_import(batch, request.user)
    except services.ImportUndoError as exc:
        messages.error(request, str(exc))
        return redirect("import_detail", batch_id=batch.pk)
    except Exception:  # noqa: BLE001 — the transaction already rolled back; say so honestly
        logger.exception("Undo import failed for batch %s", batch.pk)
        services.log_audit(request.user, "import_undo_failed", f"Undo of import {batch.code} failed — nothing was deleted",
                           {"batch": batch.code, "batch_id": batch.pk}, "import", batch.pk)
        messages.error(request, "Undo failed and was rolled back — no contacts were deleted. Please try again.")
        return redirect("import_detail", batch_id=batch.pk)
    n = result["deleted"]
    msg = f"{n:,} contact{'s' if n != 1 else ''} from this import were permanently deleted."
    if result["leads_detached"]:
        msg += f" {result['leads_detached']:,} converted lead(s) were kept."
    if result["protected"]:
        messages.warning(request, msg + f" {len(result['protected']):,} could not be deleted because other records depend on them.")
    else:
        messages.success(request, msg)
    return redirect("import_detail", batch_id=batch.pk)


@admin_required
def import_revalidate(request, batch_id):
    batch = _batch(batch_id)
    if request.method == "POST" and not batch.is_busy:
        importer.start(batch.pk, "analyze")
    return redirect("import_detail", batch_id=batch.pk)


@admin_required
def import_review(request, batch_id):
    batch = _batch(batch_id)
    if request.method == "POST":
        op = request.POST.get("op")
        rows = batch.review_rows.filter(applied=False)
        if op == "bulk":  # apply one decision to every remaining undecided row
            decision = request.POST.get("decision")
            if decision in ("skip", "update", "new"):
                rows.filter(decision="").update(decision=decision)
        elif op == "page":
            for key, val in request.POST.items():
                if key.startswith("d_") and key[2:].isdigit() and val in ("", "skip", "update", "new"):
                    rows.filter(pk=int(key[2:])).update(decision=val)
        elif op in ("apply", "finish"):
            importer.start(batch.pk, "review", finish=(op == "finish"))
            return redirect("import_detail", batch_id=batch.pk)
        return redirect(request.get_full_path())
    qs = batch.review_rows.filter(applied=False).select_related("existing_contact").order_by("row_number")
    page = filters.paginate(request, qs, per_page=25)
    return render(request, "imports/import_review.html", {
        "batch": batch, "page": page, "rows": page.object_list,
        "undecided": batch.review_rows.filter(applied=False, decision="").count(),
        "querystring": filters.querystring_without(request, "page"), "active_page": "imports",
    })


@admin_required
def import_errors(request, batch_id):
    """Full error report; ?download=1 streams a CSV of the failed rows with their original values."""
    batch = _batch(batch_id)
    qs = batch.row_errors.all()
    if request.GET.get("download"):
        writer = csv.writer(_Echo())

        def rows():
            yield "\ufeff" + writer.writerow(["Row Number", "Field", "Problem", "Reason", *batch.headers])
            for e in qs.iterator(chunk_size=2000):
                yield writer.writerow([e.row_number, e.field, e.problem, e.reason, *[_safe(c) for c in e.raw]])

        resp = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{batch.code}-errors.csv"'
        return resp
    page = filters.paginate(request, qs, per_page=50)
    return render(request, "imports/import_errors.html", {"batch": batch, "page": page, "errors": page.object_list, "active_page": "imports"})
