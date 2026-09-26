"""Executes BackgroundJob rows (bulk assign / reassign / transfer / status / follow-up / delete)."""
import datetime
import logging

from django.contrib.auth.models import User
from django.http import QueryDict
from django.utils import timezone

from . import filters, services
from .access import is_admin, visible_contacts, visible_leads
from .models import AssignmentHistory, Contact, Lead, Segment, user_label

logger = logging.getLogger("crm")

MODELS = {"lead": Lead, "contact": Contact}


def resolve_queryset(model_name, user, scope):
    """Rebuilds the authorized queryset for a bulk operation from the ACTING user."""
    if model_name == "lead":
        base, flt = visible_leads(user), filters.filter_leads
    else:
        base, flt = visible_contacts(user), filters.filter_contacts
    if scope.get("type") == "ids":
        ids = [int(i) for i in scope.get("ids", []) if str(i).isdigit()]
        return base.filter(pk__in=ids)
    if scope.get("type") == "batch":
        return base.filter(import_batch_id=int(scope["batch_id"]), **({"current_assigned_to__isnull": True} if scope.get("only_unassigned") else {}))
    return flt(base, QueryDict(scope.get("query", "")))


def owned_only(model, qs, user):
    """Non-admins may only move records they currently own."""
    if is_admin(user):
        return qs
    return qs.filter(**({"assigned_to": user} if model is Lead else {"current_assigned_to": user}))


def execute_job(job_id):
    from .models import BackgroundJob

    job = BackgroundJob.objects.get(pk=job_id)
    job.status, job.heartbeat = BackgroundJob.STATUS_RUNNING, timezone.now()
    job.save(update_fields=["status", "heartbeat"])
    try:
        user = job.created_by
        p = job.params
        model = MODELS[p["model"]]
        qs = resolve_queryset(p["model"], user, p["scope"])
        qs = owned_only(model, qs, user)  # staff can only ever act on records they currently own
        pks = list(qs.order_by("pk").values_list("pk", flat=True))
        job.total = len(pks)
        job.save(update_fields=["total"])

        def progress(n):
            job.processed += n
            job.heartbeat = timezone.now()
            job.save(update_fields=["processed", "heartbeat"])

        result = {}
        if job.kind in ("assign", "reassign", "transfer"):
            staff = list(User.objects.filter(pk__in=p["staff_ids"], staff_profile__status="active"))
            order = {int(i): n for n, i in enumerate(p["staff_ids"])}
            staff.sort(key=lambda u: order[u.pk])
            action = {"assign": AssignmentHistory.ACTION_ASSIGN, "reassign": AssignmentHistory.ACTION_REASSIGN,
                      "transfer": AssignmentHistory.ACTION_TRANSFER}[job.kind]
            plan = services.build_plan(p["method"], pks, staff, p)
            result = services.apply_plan(model, plan, user, action, p.get("reason", ""), progress)
            frm = ", ".join(f"{k} ({v:,})" for k, v in result["from"].items()) or "—"
            to = ", ".join(f"{k} ({v:,})" for k, v in result["to"].items()) or "—"
            audit_action = {
                ("assign", "lead"): "bulk_assignment", ("assign", "contact"): "bulk_assignment",
                ("reassign", "lead"): "bulk_reassignment", ("reassign", "contact"): "bulk_reassignment",
                ("transfer", "lead"): "bulk_transfer", ("transfer", "contact"): "bulk_transfer",
            }[(job.kind, p["model"])]
            services.log_audit(
                user, audit_action, f"{result['moved']:,} {p['model']}s {job.kind}ed",
                {"from": frm, "to": to, "method": p["method"], "moved": result["moved"], "reason": p.get("reason", "")},
                p["model"], "",
            )
        elif job.kind == "status":
            changed = services.bulk_set_status(model, pks, p["status"], user, progress)
            result = {"changed": changed}
            services.log_audit(user, "bulk_status_update", f"{changed:,} {p['model']}s set to {p['status']}",
                               {"status": p["status"], "changed": changed}, p["model"])
        elif job.kind == "followup":
            d = datetime.date.fromisoformat(p["date"])
            t = datetime.time.fromisoformat(p["time"]) if p.get("time") else None
            services.bulk_set_followup(model, pks, d, t, p.get("notes", ""), user, progress)
            result = {"updated": len(pks)}
            services.log_audit(user, "bulk_followup", f"Follow-up {d} set on {len(pks):,} {p['model']}s", {"date": p["date"]}, p["model"])
        elif job.kind == "add_to_segment":
            segment = Segment.objects.get(pk=p["segment_id"])
            added = services.bulk_add_to_segment(pks, segment, user, progress)
            result = {"added": added, "segment": segment.name}
        elif job.kind == "delete":
            if not is_admin(user):
                raise PermissionError("Only admins can bulk delete.")
            deleted = services.bulk_delete(model, pks, user, progress)
            result = {"deleted": deleted}
            services.log_audit(user, "bulk_delete", f"{deleted:,} {p['model']}s deleted", {"deleted": deleted}, p["model"])
        job.result, job.status = result, BackgroundJob.STATUS_DONE
    except Exception as exc:  # noqa: BLE001 — recorded on the job, never crashes the server
        logger.exception("Background job %s failed", job_id)
        job.status, job.error = BackgroundJob.STATUS_FAILED, str(exc)[:1000]
    job.finished_at = timezone.now()
    job.save()
