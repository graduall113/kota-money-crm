"""Admin analytics: per-telecaller performance and document-checklist reporting."""
from django.contrib.auth.models import User
from django.db.models import Count, F, Q
from django.utils import timezone

from . import filters, services
from .models import AssignmentHistory, Contact, DocumentType, Lead, LeadDocument, user_label


def _dated(qs, params, field="created_at"):
    qs = filters._range(qs, field, params.get("from"), params.get("to"))
    return qs


def staff_performance(params):
    today = timezone.localdate()
    contacts = _dated(Contact.objects.exclude(current_assigned_to=None).filter(is_deleted=False), params)
    c_rows = {r["current_assigned_to"]: r for r in contacts.values("current_assigned_to").annotate(
        assigned=Count("id"),
        contacted=Count("id", filter=~Q(status=Contact.STATUS_NEW)),
        interested=Count("id", filter=Q(status=Contact.STATUS_INTERESTED)),
        not_interested=Count("id", filter=Q(status=Contact.STATUS_NOT_INTERESTED)),
        converted=Count("id", filter=Q(status=Contact.STATUS_CONVERTED)),
        followups=Count("id", filter=Q(followup_status="pending", next_followup_date__lte=today)),
    )}
    leads = services.annotate_doc_progress(_dated(Lead.objects.exclude(assigned_to=None), params))
    l_rows = {r["assigned_to"]: r for r in leads.values("assigned_to").annotate(
        assigned=Count("id"),
        contacted=Count("id", filter=Q(last_contacted__isnull=False)),
        interested=Count("id", filter=Q(interest=Lead.INTEREST_INTERESTED)),
        not_interested=Count("id", filter=Q(interest=Lead.INTEREST_NOT_INTERESTED)),
        docs_complete=Count("id", filter=Q(docs_total__gt=0, docs_received__gte=F("docs_total"))),
        docs_partial=Count("id", filter=Q(docs_received__gt=0, docs_received__lt=F("docs_total"))),
        docs_not_started=Count("id", filter=Q(docs_received=0)),
        meeting=Count("id", filter=Q(status=Lead.STATUS_MEETING_REQUESTED)),
        processing=Count("id", filter=Q(status=Lead.STATUS_PROCESSING)),
        approved=Count("id", filter=Q(status=Lead.STATUS_APPROVED)),
        rejected=Count("id", filter=Q(status=Lead.STATUS_REJECTED)),
        followups=Count("id", filter=Q(followup_status="pending", next_followup_date__lte=today)),
    )}
    transfers = _dated(AssignmentHistory.objects.filter(action=AssignmentHistory.ACTION_TRANSFER), params)
    t_rows = {r["from_user"]: r["n"] for r in transfers.exclude(from_user=None).values("from_user").annotate(n=Count("id"))}

    out = []
    for u in User.objects.select_related("staff_profile").filter(staff_profile__role="staff").order_by("first_name", "username"):
        c, l = c_rows.get(u.pk, {}), l_rows.get(u.pk, {})
        g = lambda d, k: d.get(k, 0) or 0  # noqa: E731
        row = {
            "user": u, "name": user_label(u), "active": u.staff_profile.is_account_active,
            "contacts": g(c, "assigned"), "leads": g(l, "assigned"),
            "assigned": g(c, "assigned") + g(l, "assigned"),
            "contacted": g(c, "contacted") + g(l, "contacted"),
            "interested": g(c, "interested") + g(l, "interested"),
            "not_interested": g(c, "not_interested") + g(l, "not_interested"),
            "docs_pending": g(l, "docs_not_started") + g(l, "docs_partial"),
            "docs_partial": g(l, "docs_partial"), "docs_complete": g(l, "docs_complete"),
            "meeting": g(l, "meeting"), "processing": g(l, "processing"),
            "approved": g(l, "approved"), "rejected": g(l, "rejected"),
            "converted": g(c, "converted"), "transferred": t_rows.get(u.pk, 0),
            "followups": g(c, "followups") + g(l, "followups"),
        }
        out.append(row)
    return out


def document_report(params):
    """Checklist reporting, filterable by staff / doc status / document / date."""
    leads = _dated(Lead.objects.all(), params)
    staff_id = params.get("staff")
    if staff_id and str(staff_id).isdigit():
        leads = leads.filter(assigned_to_id=int(staff_id))
    status = params.get("doc_status")
    annotated = services.annotate_doc_progress(leads)
    total = leads.count()
    complete = annotated.filter(docs_total__gt=0, docs_received__gte=F("docs_total")).count()
    partial = annotated.filter(docs_received__gt=0, docs_received__lt=F("docs_total")).count()
    not_started = annotated.filter(docs_received=0).count()
    if status in ("not_started", "partial", "complete"):
        leads = services.filter_doc_status(leads, status)

    lead_ids = leads.values("pk")
    doc_rows = []
    types = services.active_doc_types()
    if str(params.get("document") or "").isdigit():
        types = types.filter(pk=int(params["document"]))
    n_leads = leads.count()
    for dt in types:
        received = LeadDocument.objects.filter(document_type=dt, received=True, lead__in=lead_ids).count()
        applicable = n_leads if dt.is_default else LeadDocument.objects.filter(document_type=dt, lead__in=lead_ids).count()
        doc_rows.append({"doc": dt, "received": received, "applicable": applicable, "pending": max(applicable - received, 0)})
    doc_rows.sort(key=lambda r: -r["pending"])

    staff_rows = []
    per_staff = services.annotate_doc_progress(leads.exclude(assigned_to=None)).values("assigned_to").annotate(
        n=Count("id"),
        complete=Count("id", filter=Q(docs_total__gt=0, docs_received__gte=F("docs_total"))),
        partial=Count("id", filter=Q(docs_received__gt=0, docs_received__lt=F("docs_total"))),
        not_started=Count("id", filter=Q(docs_received=0)),
    )
    names = {u.pk: user_label(u) for u in User.objects.filter(pk__in=[r["assigned_to"] for r in per_staff])}
    for r in per_staff:
        staff_rows.append({**r, "name": names.get(r["assigned_to"], "—"),
                           "percent": round(r["complete"] * 100 / r["n"]) if r["n"] else 0})
    staff_rows.sort(key=lambda r: -r["percent"])
    return {"total": total, "complete": complete, "partial": partial, "not_started": not_started,
            "pending": total - complete, "doc_rows": doc_rows, "staff_rows": staff_rows}
