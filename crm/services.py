"""
Business logic shared by views, background jobs and the importer:
audit log, timeline, assignment / transfer, distribution plans, document checklist.
"""
import logging
import random
import threading

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.db.models import Count, F, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from . import lead_sync
from .models import (
    Activity,
    AssignmentHistory,
    AuditLog,
    BackgroundJob,
    Contact,
    ContactSegment,
    DocumentHistory,
    DocumentType,
    Lead,
    LeadDocument,
    Segment,
    user_label,
)

logger = logging.getLogger("crm")
CHUNK = 2000


# ---------------------------------------------------------------- audit / timeline
def log_audit(user, action, summary, details=None, target_type="", target_id=""):
    return AuditLog.objects.create(
        actor=user if getattr(user, "pk", None) else None,
        actor_name=user_label(user) if getattr(user, "pk", None) else "System",
        action=action,
        summary=summary[:500],
        details=details or {},
        target_type=target_type,
        target_id=str(target_id or ""),
    )


def log_activity(actor, kind, message, lead=None, contact=None):
    return Activity.objects.create(
        lead=lead, contact=contact, kind=kind, message=message[:500],
        actor=actor if getattr(actor, "pk", None) else None,
        actor_name=user_label(actor) if getattr(actor, "pk", None) else "System",
    )


def active_staff():
    return User.objects.filter(staff_profile__status="active").select_related("staff_profile").order_by("first_name", "username")


# ---------------------------------------------------------------- single assign / transfer
class AssignmentError(Exception):
    pass


def _check_target(to_user):
    if to_user is None:
        return
    profile = getattr(to_user, "staff_profile", None)
    if not profile or not profile.is_account_active:
        raise AssignmentError("The selected staff member is inactive.")


@transaction.atomic
def assign_lead(lead, to_user, by, action=AssignmentHistory.ACTION_ASSIGN, reason=""):
    """
    Changes ONLY assigned_to. reference_by is never touched here — that is the
    whole point of keeping the two concepts separate.
    """
    _check_target(to_user)
    old = lead.assigned_to
    if old == to_user:
        raise AssignmentError("The lead is already assigned to that person.")
    lead.assigned_to = to_user
    if lead.original_assigned_to_id is None and to_user is not None:
        lead.original_assigned_to = to_user
    lead._sync_reason = "assignment_changed"  # -> Lead post_save queues the Sheet sync
    lead.save(update_fields=["assigned_to", "original_assigned_to", "updated_at"])
    AssignmentHistory.objects.create(
        lead=lead, action=action, from_user=old, to_user=to_user,
        from_name=user_label(old), to_name=user_label(to_user),
        changed_by=by, changed_by_name=user_label(by), reason=reason[:300],
    )
    verb = "Lead transferred" if action == AssignmentHistory.ACTION_TRANSFER else "Lead assigned"
    msg = f"{verb}: {user_label(old) or 'Unassigned'} → {user_label(to_user) or 'Unassigned'}"
    if reason:
        msg += f" ({reason})"
    log_activity(by, "transfer" if action == AssignmentHistory.ACTION_TRANSFER else "assign", msg, lead=lead)
    log_audit(
        by, "lead_transferred" if action == AssignmentHistory.ACTION_TRANSFER else "lead_assigned",
        f"{lead.display_id} {user_label(old) or 'Unassigned'} → {user_label(to_user) or 'Unassigned'}",
        {"from": user_label(old), "to": user_label(to_user), "reason": reason}, "lead", lead.pk,
    )
    return lead


def transfer_lead(lead, to_user, by, reason=""):
    if to_user is None:
        raise AssignmentError("Choose who to transfer the lead to.")
    return assign_lead(lead, to_user, by, action=AssignmentHistory.ACTION_TRANSFER, reason=reason)


@transaction.atomic
def assign_contact(contact, to_user, by, action=AssignmentHistory.ACTION_ASSIGN, reason=""):
    _check_target(to_user)
    old = contact.current_assigned_to
    if old == to_user:
        raise AssignmentError("The contact is already assigned to that person.")
    contact.current_assigned_to = to_user
    if contact.original_assigned_to_id is None and to_user is not None:
        contact.original_assigned_to = to_user
    contact.save(update_fields=["current_assigned_to", "original_assigned_to", "updated_at"])
    AssignmentHistory.objects.create(
        contact=contact, action=action, from_user=old, to_user=to_user,
        from_name=user_label(old), to_name=user_label(to_user),
        changed_by=by, changed_by_name=user_label(by), reason=reason[:300],
    )
    log_audit(
        by, "contact_reassigned" if old else "contact_assigned",
        f"Contact {contact.pk}: {user_label(old) or 'Unassigned'} → {user_label(to_user) or 'Unassigned'}",
        {"from": user_label(old), "to": user_label(to_user), "reason": reason}, "contact", contact.pk,
    )
    return contact


# ---------------------------------------------------------------- bulk assignment
def _bulk_assign_chunk(model, pks, to_user, by, action, reason):
    """Assign one chunk of pks in a few queries. Returns {from_user_id: count}."""
    field = "assigned_to_id" if model is Lead else "current_assigned_to_id"
    rows = list(model.objects.filter(pk__in=pks).values_list("pk", field))
    moved = [(pk, prev) for pk, prev in rows if prev != (to_user.pk if to_user else None)]
    if not moved:
        return {}
    names = {u.pk: user_label(u) for u in User.objects.filter(pk__in={p for _, p in moved if p})}
    to_name, by_name = user_label(to_user), user_label(by)
    history, activities, from_counts = [], [], {}
    for pk, prev in moved:
        from_counts[prev] = from_counts.get(prev, 0) + 1
        kwargs = {"lead_id": pk} if model is Lead else {"contact_id": pk}
        row_action = action
        if action != AssignmentHistory.ACTION_TRANSFER:
            row_action = AssignmentHistory.ACTION_ASSIGN if prev is None else AssignmentHistory.ACTION_REASSIGN
        history.append(AssignmentHistory(
            action=row_action, from_user_id=prev, to_user=to_user,
            from_name=names.get(prev, ""), to_name=to_name,
            changed_by=by, changed_by_name=by_name, reason=reason[:300], **kwargs,
        ))
        if model is Lead:
            activities.append(Activity(
                lead_id=pk, kind="transfer" if action == AssignmentHistory.ACTION_TRANSFER else "assign",
                message=f"Lead {'transferred' if action == AssignmentHistory.ACTION_TRANSFER else 'assigned'}: "
                        f"{names.get(prev) or 'Unassigned'} → {to_name or 'Unassigned'}",
                actor=by, actor_name=by_name,
            ))
    ids = [pk for pk, _ in moved]
    with transaction.atomic():
        model.objects.filter(pk__in=ids).update(**{field: to_user.pk if to_user else None, "updated_at": timezone.now()})
        # first-ever assignee is remembered once and never overwritten
        if to_user:
            model.objects.filter(pk__in=ids, original_assigned_to__isnull=True).update(original_assigned_to=to_user)
        if model is Lead:  # queryset.update() fires no signal: queue the Sheet sync explicitly
            lead_sync.enqueue_many(ids, "assignment_changed")
        AssignmentHistory.objects.bulk_create(history, batch_size=1000)
        if activities:
            Activity.objects.bulk_create(activities, batch_size=1000)
    return from_counts


def build_plan(method, pks, staff, params):
    """
    Turns an assignment method into [(user, [pks...])].
    method: one | equal | custom | percent | random
    params: {'counts': {user_id: n}, 'percents': {user_id: pct}}
    'random' shuffles the records, then splits equally.
    Raises AssignmentError on impossible plans (never silently drops records).
    """
    pks = list(pks)
    n = len(pks)
    if not staff:
        raise AssignmentError("Select at least one staff member.")
    if method == "one":
        return [(staff[0], pks)]
    if method == "random":
        random.shuffle(pks)
        method = "equal"
    if method == "equal":
        base, extra = divmod(n, len(staff))
        counts = [base + (1 if i < extra else 0) for i in range(len(staff))]
    elif method == "custom":
        counts = [int(params.get("counts", {}).get(str(u.pk), 0) or 0) for u in staff]
        if any(c < 0 for c in counts):
            raise AssignmentError("Counts cannot be negative.")
        if sum(counts) > n:
            raise AssignmentError(f"Custom counts total {sum(counts):,} but only {n:,} records are selected.")
    elif method == "percent":
        pcts = [float(params.get("percents", {}).get(str(u.pk), 0) or 0) for u in staff]
        if any(p < 0 for p in pcts) or sum(pcts) > 100.0001:
            raise AssignmentError("Percentages must be positive and add up to at most 100%.")
        counts = [int(n * p / 100) for p in pcts]
        # hand leftover rounding records to the largest shares so 100% distributes everything
        if abs(sum(pcts) - 100) < 0.0001:
            left = n - sum(counts)
            order = sorted(range(len(staff)), key=lambda i: -pcts[i])
            for i in range(left):
                counts[order[i % len(order)]] += 1
    else:
        raise AssignmentError("Unknown assignment method.")
    plan, cursor = [], 0
    for user, c in zip(staff, counts):
        if c:
            plan.append((user, pks[cursor:cursor + c]))
            cursor += c
    return plan


def apply_plan(model, plan, by, action, reason="", progress=None):
    """Executes a plan chunk by chunk. Returns {'moved': n, 'from': {name: n}, 'to': {name: n}}."""
    moved, from_totals, to_totals = 0, {}, {}
    for user, pks in plan:
        for i in range(0, len(pks), CHUNK):
            chunk = pks[i:i + CHUNK]
            counts = _bulk_assign_chunk(model, chunk, user, by, action, reason)
            got = sum(counts.values())
            moved += got
            to_totals[user_label(user)] = to_totals.get(user_label(user), 0) + got
            for prev, c in counts.items():
                key = user_label(User.objects.filter(pk=prev).first()) if prev else "Unassigned"
                from_totals[key] = from_totals.get(key, 0) + c
            if progress:
                progress(len(chunk))
    return {"moved": moved, "from": from_totals, "to": to_totals}


# ---------------------------------------------------------------- other bulk operations
def bulk_set_status(model, pks, status, by, progress=None):
    changed = 0
    for i in range(0, len(pks), CHUNK):
        chunk = pks[i:i + CHUNK]
        rows = list(model.objects.filter(pk__in=chunk).exclude(status=status).values_list("pk", "status"))
        if rows and model is Lead:
            labels = dict(Lead.STATUS_CHOICES)
            Activity.objects.bulk_create([
                Activity(lead_id=pk, kind="status", actor=by, actor_name=user_label(by),
                         message=f"Status changed: {labels.get(old, old)} → {labels.get(status, status)}")
                for pk, old in rows
            ], batch_size=1000)
        model.objects.filter(pk__in=[pk for pk, _ in rows]).update(status=status, updated_at=timezone.now())
        if model is Lead:
            lead_sync.enqueue_many([pk for pk, _ in rows], "status_changed")
        changed += len(rows)
        if progress:
            progress(len(chunk))
    return changed


def bulk_set_followup(model, pks, date, time, notes, by, progress=None):
    for i in range(0, len(pks), CHUNK):
        chunk = pks[i:i + CHUNK]
        model.objects.filter(pk__in=chunk).update(
            next_followup_date=date, next_followup_time=time, followup_notes=notes,
            followup_status="pending", updated_at=timezone.now(),
        )
        if model is Lead:
            lead_sync.enqueue_many(chunk, "followup_changed")
        if progress:
            progress(len(chunk))
    return len(pks)


# ---------------------------------------------------------------- permanent deletion
class ImportUndoError(Exception):
    """Undo Import refused up-front (busy / already undone). Nothing was changed."""


def _delete_chunk(model, chunk):
    """
    Physically deletes one chunk. Returns (deleted_ids, protected) where protected is
    {pk: reason}. Relationships are never forced: Django's collector applies each FK's own
    on_delete (Contact -> Lead.contact is SET_NULL, so a Lead can never be cascade-deleted
    from its Contact). If a PROTECT/RESTRICT FK blocks the chunk, it is retried record by
    record inside savepoints so only the genuinely blocked records are skipped and named.
    """
    from django.db.models import ProtectedError, RestrictedError

    try:
        with transaction.atomic():  # savepoint when already inside a transaction
            model.objects.filter(pk__in=chunk).delete()
        return list(chunk), {}
    except (ProtectedError, RestrictedError):
        deleted, protected = [], {}
        for pk in chunk:
            try:
                with transaction.atomic():
                    model.objects.filter(pk=pk).delete()
                deleted.append(pk)
            except (ProtectedError, RestrictedError) as exc:
                protected[pk] = f"Referenced by {len(exc.protected_objects if hasattr(exc, 'protected_objects') else exc.restricted_objects)} protected record(s)"
        return deleted, protected


def delete_records(model, pks, progress=None):
    """
    Permanently deletes `pks` of `model` (Lead or Contact) in chunks. Call it inside
    transaction.atomic() for all-or-nothing behaviour (bulk_delete / undo_import do).
    Returns {"selected", "deleted", "protected": {pk: reason}, "leads_detached"}.
    leads_detached = Leads that survive because their Contact was deleted (Lead.contact -> NULL).
    """
    pks = list(pks)
    result = {"selected": len(pks), "deleted": 0, "protected": {}, "leads_detached": 0}
    for i in range(0, len(pks), CHUNK):
        chunk = pks[i:i + CHUNK]
        if model is Contact:
            result["leads_detached"] += Lead.objects.filter(contact_id__in=chunk).count()
        ok, protected = _delete_chunk(model, chunk)
        result["deleted"] += len(ok)
        result["protected"].update(protected)
        if model is Contact and protected:  # blocked contacts keep their Leads attached
            result["leads_detached"] -= Lead.objects.filter(contact_id__in=list(protected)).count()
        if progress:
            progress(len(chunk))
    return result


def bulk_delete(model, pks, by, progress=None):
    """
    Bulk "Delete": a real database DELETE of every eligible selected record, all inside ONE
    transaction — an unexpected failure rolls the whole thing back (nothing half-deleted).
    Records blocked by a protected relationship are skipped, named in the result, and never
    reported as deleted. Writes the audit entry in the same transaction.
    """
    name = "lead" if model is Lead else "contact"
    with transaction.atomic():
        result = delete_records(model, pks, progress)
        skipped = len(result["protected"])
        summary = f"Bulk deleted {result['deleted']:,} of {result['selected']:,} selected {name}s permanently"
        if skipped:
            summary += f" — {skipped:,} skipped (protected)"
        details = {
            "selected": result["selected"], "deleted": result["deleted"], "protected": skipped,
            "deleted_ids": [pk for pk in pks if pk not in result["protected"]][:200],
            "protected_ids": list(result["protected"])[:200], "protected_reasons": dict(list(result["protected"].items())[:50]),
            "ids_truncated": len(pks) > 200,
        }
        if model is Contact:
            details["leads_kept_detached"] = result["leads_detached"]
        log_audit(by, "bulk_delete", summary, details, name, "")
    return result


def undo_import(batch, by):
    """
    Undo Import = PERMANENT deletion of the contacts this batch CREATED, and nothing else.

    Source of truth is Contact.import_batch, which the importer sets only on contacts it
    creates; pre-existing contacts that were matched, skipped or merely updated keep their
    own import_batch (or none) and are never touched. Contacts already soft-deleted by an
    old-style undo (is_deleted=True) are legacy rows and are left alone.

    Converted contacts: the Lead survives with Lead.contact -> NULL (SET_NULL), keeping its own
    copied customer data and Lead.import_batch. The ImportBatch row stays as history.
    Everything — deletes, batch bookkeeping, audit — commits or rolls back together.
    """
    from .models import ImportBatch, ImportReviewRow

    with transaction.atomic():
        locked = ImportBatch.objects.select_for_update().get(pk=batch.pk)
        if locked.is_busy:
            raise ImportUndoError("Wait for this import to finish before undoing it.")
        if locked.undone_at:
            raise ImportUndoError("This import was already undone.")
        ids = list(Contact.objects.filter(import_batch=locked, is_deleted=False).order_by("pk").values_list("pk", flat=True))
        if not ids:
            raise ImportUndoError("There are no imported contacts left to remove.")
        result = delete_records(Contact, ids)
        # Duplicate-review rows this batch had parked can no longer be applied meaningfully.
        closed = locked.review_rows.filter(applied=False).update(applied=True, decision=ImportReviewRow.DECISION_SKIP)
        now = timezone.now()
        locked.undone_at, locked.undone_by = now, by
        locked.undo_selected_count, locked.undo_deleted_count = result["selected"], result["deleted"]
        locked.undo_protected_count, locked.undo_leads_detached = len(result["protected"]), result["leads_detached"]
        fields = ["undone_at", "undone_by", "undo_selected_count", "undo_deleted_count",
                  "undo_protected_count", "undo_leads_detached"]
        if locked.status == ImportBatch.STATUS_REVIEW:
            locked.status, locked.completed_at = ImportBatch.STATUS_COMPLETED, now
            fields += ["status", "completed_at"]
        locked.save(update_fields=fields)
        summary = f"Import {locked.code} undone — {result['deleted']:,} of {result['selected']:,} contacts permanently deleted"
        if result["protected"]:
            summary += f", {len(result['protected']):,} kept (protected)"
        if result["leads_detached"]:
            summary += f", {result['leads_detached']:,} converted lead(s) kept"
        log_audit(by, "import_undone", summary, {
            "batch": locked.code, "batch_id": locked.pk, "originally_created": locked.imported_count,
            "selected": result["selected"], "deleted": result["deleted"],
            "protected": len(result["protected"]), "protected_ids": list(result["protected"])[:200],
            "converted_leads_kept": result["leads_detached"], "review_rows_closed": closed,
            "undone_at": now.isoformat(),
        }, "import", locked.pk)
    return result


# ---------------------------------------------------------------- background jobs
def _run_job(job_id, close=True):
    from .jobs import execute_job  # local import: jobs imports services

    try:
        execute_job(job_id)
    finally:
        if close:
            connection.close()  # worker threads own their DB connection


def start_job(job):
    """Small jobs run inline (instant feedback); big ones in a worker thread."""
    inline = getattr(settings, "CRM_JOBS_INLINE", False) or job.total <= 1500
    if inline:
        _run_job(job.pk, close=False)
    else:
        threading.Thread(target=_run_job, args=(job.pk,), daemon=True).start()
    return job


def new_job(kind, title, total, params, user):
    return BackgroundJob.objects.create(kind=kind, title=title, total=total, params=params, created_by=user)


# ---------------------------------------------------------------- document checklist
def active_doc_types():
    return DocumentType.objects.filter(is_active=True, is_archived=False)


def required_active_q(prefix=""):
    return Q(**{f"{prefix}document_type__is_active": True, f"{prefix}document_type__is_archived": False,
                f"{prefix}document_type__is_required": True})


def annotate_doc_progress(qs):
    """
    docs_received / docs_total per lead using correlated subqueries, so it can be
    filtered on AND aggregated over. total = default required docs + extra
    (non-default, required) docs attached to that lead.
    """
    default_required = active_doc_types().filter(is_required=True, is_default=True).count()
    base = LeadDocument.objects.filter(lead=OuterRef("pk")).filter(required_active_q())
    received = base.filter(received=True).order_by().values("lead").annotate(c=Count("id")).values("c")
    extra = base.filter(document_type__is_default=False).order_by().values("lead").annotate(c=Count("id")).values("c")
    return qs.annotate(
        docs_received=Coalesce(Subquery(received, output_field=IntegerField()), Value(0)),
        docs_extra=Coalesce(Subquery(extra, output_field=IntegerField()), Value(0)),
    ).annotate(docs_total=F("docs_extra") + Value(default_required))


def doc_status_from(received, total):
    if total and received >= total:
        return "complete"
    if received > 0:
        return "partial"
    return "not_started"


DOC_STATUS_LABELS = {"not_started": "Not Started", "partial": "Partially Received", "complete": "Complete"}


def filter_doc_status(qs, status):
    qs = annotate_doc_progress(qs)
    if status == "complete":
        return qs.filter(docs_total__gt=0, docs_received__gte=F("docs_total"))
    if status == "partial":
        return qs.filter(docs_received__gt=0, docs_received__lt=F("docs_total"))
    if status == "not_started":
        return qs.filter(docs_received=0)
    if status == "pending":  # anything not complete
        return qs.filter(Q(docs_total=0) | Q(docs_received__lt=F("docs_total")))
    return qs


def doc_progress_map(lead_ids):
    """{lead_id: (received, total, status)} for one page of leads (2 small queries)."""
    default_required = active_doc_types().filter(is_required=True, is_default=True).count()
    rows = (LeadDocument.objects.filter(lead_id__in=lead_ids).filter(required_active_q())
            .values("lead_id").annotate(rec=Count("id", filter=Q(received=True)),
                                        extra=Count("id", filter=Q(document_type__is_default=False))))
    got = {r["lead_id"]: r for r in rows}
    out = {}
    for pk in lead_ids:
        r = got.get(pk)
        rec, total = (r["rec"], default_required + r["extra"]) if r else (0, default_required)
        out[pk] = (rec, total, doc_status_from(rec, total))
    return out


def lead_checklist(lead):
    """[{type, received}] for every document applicable to this lead, in admin order."""
    rows = {r.document_type_id: r for r in lead.documents.select_related("document_type")}
    result = []
    for dt in active_doc_types():
        row = rows.get(dt.pk)
        if dt.is_default or row is not None:
            result.append({"type": dt, "received": bool(row and row.received), "attached": row is not None})
    return result


def checklist_summary(items):
    required = [i for i in items if i["type"].is_required]
    received = sum(1 for i in required if i["received"])
    total = len(required)
    return {
        "received": received, "total": total, "pending": total - received,
        "percent": round(received * 100 / total) if total else 0,
        "status": doc_status_from(received, total),
        "status_label": DOC_STATUS_LABELS[doc_status_from(received, total)],
    }


@transaction.atomic
def save_lead_checklist(lead, received_ids, by):
    """Diffs ticks against the DB and writes history only for real changes."""
    changes = []
    items = lead_checklist(lead)
    existing = {r.document_type_id: r for r in lead.documents.all()}
    now = timezone.now()
    for item in items:
        dt = item["type"]
        want = dt.pk in received_ids
        if want == item["received"]:
            continue
        row = existing.get(dt.pk)
        if row:
            row.received, row.received_at, row.updated_by = want, (now if want else None), by
            row.save(update_fields=["received", "received_at", "updated_by", "updated_at"])
        else:
            LeadDocument.objects.create(lead=lead, document_type=dt, received=want,
                                        received_at=now if want else None, updated_by=by)
        DocumentHistory.objects.create(
            lead=lead, document_type=dt, document_name=dt.name,
            old_received=item["received"], new_received=want, changed_by=by, changed_by_name=user_label(by),
        )
        log_activity(by, "document", f"{dt.name} marked {'received' if want else 'not received'}", lead=lead)
        changes.append(f"{dt.name} → {'Received' if want else 'Pending'}")
    if changes:
        Lead.objects.filter(pk=lead.pk).update(updated_at=now)
        log_audit(by, "document_checklist_updated", f"{lead.display_id}: " + "; ".join(changes),
                  {"changes": changes}, "lead", lead.pk)
    return changes


def attach_extra_document(lead, doc_type, by):
    row, created = LeadDocument.objects.get_or_create(lead=lead, document_type=doc_type)
    if created:
        log_activity(by, "document", f"{doc_type.name} added to checklist", lead=lead)
    return created


# ---------------------------------------------------------------- segments
def _add_membership(segment, contact_ids, by):
    """Core, unaudited insert — never disturbs existing memberships; the DB
    unique constraint (not a pre-check) is what actually blocks duplicates."""
    contact_ids = list(contact_ids)
    if not contact_ids:
        return 0
    before = set(ContactSegment.objects.filter(segment=segment, contact_id__in=contact_ids).values_list("contact_id", flat=True))
    new_ids = [pk for pk in contact_ids if pk not in before]
    ContactSegment.objects.bulk_create(
        [ContactSegment(segment=segment, contact_id=pk, added_by=by) for pk in new_ids],
        batch_size=1000, ignore_conflicts=True,
    )
    return len(new_ids)


def add_contacts_to_segment(segment, contact_ids, by):
    """Single-call add (contact detail, segment detail) — logs one audit entry."""
    added = _add_membership(segment, contact_ids, by)
    if added:
        log_audit(by, "segment_contacts_added", f"{added:,} contact(s) added to segment '{segment.name}'",
                  {"segment": segment.name, "added": added}, "segment", segment.pk)
    return added


def bulk_add_to_segment(pks, segment, by, progress=None):
    """Chunked add for large selections (jobs.py) — one audit entry for the whole run."""
    added = 0
    for i in range(0, len(pks), CHUNK):
        chunk = pks[i:i + CHUNK]
        added += _add_membership(segment, chunk, by)
        if progress:
            progress(len(chunk))
    if added:
        log_audit(by, "segment_contacts_added", f"{added:,} contact(s) added to segment '{segment.name}'",
                  {"segment": segment.name, "added": added}, "segment", segment.pk)
    return added


def bulk_remove_from_segment(pks, segment, by, progress=None):
    """
    Chunked removal for large selections (jobs.py) — one audit entry for the
    whole run. Only removes the segment *membership* (ContactSegment rows);
    the contacts themselves, their leads, and all their other data are
    completely untouched.
    """
    removed = 0
    for i in range(0, len(pks), CHUNK):
        chunk = pks[i:i + CHUNK]
        n, _ = ContactSegment.objects.filter(segment=segment, contact_id__in=chunk).delete()
        removed += n
        if progress:
            progress(len(chunk))
    if removed:
        log_audit(by, "segment_contacts_removed", f"{removed:,} contact(s) removed from segment '{segment.name}'",
                  {"segment": segment.name, "removed": removed}, "segment", segment.pk)
    return removed


def remove_contact_from_segment(segment, contact, by):
    deleted, _ = ContactSegment.objects.filter(segment=segment, contact=contact).delete()
    if deleted:
        log_audit(by, "segment_contacts_removed", f"{contact.name} removed from segment '{segment.name}'",
                  {"segment": segment.name, "contact": contact.name}, "segment", segment.pk)
        log_activity(by, "segment", f"Removed from segment '{segment.name}'", contact=contact)
    return bool(deleted)


def get_or_create_segment(name, by):
    """Used by the "+ Create New Segment" inline shortcuts (contact detail, bulk action, import)."""
    name = (name or "").strip()
    if not name:
        raise AssignmentError("Segment name is required.")
    segment = Segment.objects.filter(name__iexact=name).first()
    if segment is None:
        segment = Segment.objects.create(name=name, created_by=by)
        log_audit(by, "segment_created", f"Segment '{segment.name}' created", {}, "segment", segment.pk)
    return segment


# ---------------------------------------------------------------- Contact edit -> its converted Lead
# Contact field -> the Lead field that mirrors it. Only fields the Contact edit form actually
# CHANGED are pushed, so an old Contact value can never overwrite a newer Lead edit.
CONTACT_TO_LEAD_FIELDS = {
    "name": "customer_name", "phone": "contact_number", "work_profile": "work_profile", "income": "income",
    "requirement": "requirement", "loan_amount": "loan_amount", "email": "email", "city": "city", "source": "source",
}


def linked_lead_for_contact(contact):
    """The Lead this Contact was converted into (Lead.contact), or None. Same pick as views._converted_lead_for."""
    return Lead.objects.filter(contact_id=contact.pk).order_by("-created_at", "-id").first()


def propagate_contact_to_lead(contact, changed_fields, by=None):
    """
    Called after a Contact edit is saved. If the Contact was already converted, the EXISTING Lead
    (same Lead ID, e.g. KM-1050) receives the changed shared fields and is re-synced to the SAME
    Google Sheet row. Never creates a Lead, never allocates a new Lead ID, never appends a row.
    Returns the Lead, or None when the Contact was never converted.
    """
    lead = linked_lead_for_contact(contact)
    if lead is None:
        return None
    updates = {}
    for cfield in changed_fields:
        lfield = CONTACT_TO_LEAD_FIELDS.get(cfield)
        if not lfield:
            continue
        value = getattr(contact, cfield)
        if lfield in ("customer_name", "contact_number") and not value:
            continue  # required on a Lead
        if lfield == "loan_amount" and value is None:
            continue  # a cleared Contact amount must not blank the Lead's
        if lfield == "contact_number" and len(str(value)) > Lead._meta.get_field("contact_number").max_length:
            logger.warning("Contact %s phone too long for Lead %s; not copied", contact.pk, lead.pk)
            continue
        if getattr(lead, lfield) != value:
            updates[lfield] = value
    lead._sync_reason = "contact_edited"
    if updates:
        for field, value in updates.items():
            setattr(lead, field, value)
        lead.save(update_fields=[*updates, "updated_at"])  # post_save queues the sync
        log_activity(by, "edited", "Updated from contact edit: " + ", ".join(sorted(updates)), lead=lead)
    # Saving a converted Contact always re-checks the Sheet row (a no-op send is skipped if
    # nothing synced differs). Coalesces with the event the save above just queued.
    lead_sync.enqueue_lead_sync(lead, "contact_edited")
    transaction.on_commit(lead_sync.schedule_drain)
    return lead
