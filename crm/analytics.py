"""Admin analytics: per-telecaller performance and document-checklist reporting."""
from django.contrib.auth.models import User
from django.db.models import Count, F, Q
from django.utils import timezone

from . import filters, services
from .models import AssignmentHistory, Contact, DocumentType, Lead, LeadDocument, user_label


def _dated(qs, params, field="created_at"):
    qs = filters._range(qs, field, params.get("from"), params.get("to"))
    return qs


def staff_performance(params, user_ids=None):
    """
    Per-telecaller rows. `user_ids` (optional) limits the work to those staff members - the Staff detail
    page needs one person, and used to compute every telecaller's numbers just to pick one row. The numbers
    for a given person are identical either way (every figure is grouped per user).
    """
    today = timezone.localdate()
    contact_base = Contact.objects.exclude(current_assigned_to=None).filter(is_deleted=False)
    lead_base = Lead.objects.exclude(assigned_to=None)
    transfer_base = AssignmentHistory.objects.filter(action=AssignmentHistory.ACTION_TRANSFER)
    if user_ids is not None:
        user_ids = list(user_ids)
        contact_base = contact_base.filter(current_assigned_to_id__in=user_ids)
        lead_base = lead_base.filter(assigned_to_id__in=user_ids)
        transfer_base = transfer_base.filter(from_user_id__in=user_ids)
    contacts = _dated(contact_base, params)
    c_rows = {r["current_assigned_to"]: r for r in contacts.values("current_assigned_to").annotate(
        assigned=Count("id"),
        contacted=Count("id", filter=~Q(status=Contact.STATUS_NEW)),
        interested=Count("id", filter=Q(status=Contact.STATUS_INTERESTED)),
        not_interested=Count("id", filter=Q(status=Contact.STATUS_NOT_INTERESTED)),
        converted=Count("id", filter=Q(status=Contact.STATUS_CONVERTED)),
        followups=Count("id", filter=Q(followup_status="pending", next_followup_date__lte=today)),
    )}
    leads = services.annotate_doc_progress(_dated(lead_base, params))
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
    transfers = _dated(transfer_base, params)
    t_rows = {r["from_user"]: r["n"] for r in transfers.exclude(from_user=None).values("from_user").annotate(n=Count("id"))}

    out = []
    users = User.objects.select_related("staff_profile").filter(staff_profile__role="staff").order_by("first_name", "username")
    if user_ids is not None:
        users = users.filter(pk__in=user_ids)
    for u in users:
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
    status_applied = status in ("not_started", "partial", "complete")
    if status_applied:
        leads = services.filter_doc_status(leads, status)  # already carries docs_received / docs_total
    # The summary cards describe the SAME filtered lead set as the two tables below (date range + staff + document
    # status), computed in one aggregate query. (They used to be taken before the status filter, so choosing
    # "Complete" left "Total leads" showing every lead while the tables listed only the complete ones.)
    # The "Document" dropdown only narrows the per-document table: it has no meaning at lead level.
    agg = (leads if status_applied else services.annotate_doc_progress(leads)).order_by().aggregate(
        total=Count("id"),
        complete=Count("id", filter=Q(docs_total__gt=0, docs_received__gte=F("docs_total"))),
        partial=Count("id", filter=Q(docs_received__gt=0, docs_received__lt=F("docs_total"))),
        not_started=Count("id", filter=Q(docs_received=0)),
    )
    total, complete, partial, not_started = agg["total"], agg["complete"], agg["partial"], agg["not_started"]

    lead_ids = leads.values("pk")
    doc_rows = []
    types = services.active_doc_types()
    if str(params.get("document") or "").isdigit():
        types = types.filter(pk=int(params["document"]))
    n_leads = total  # same set as the cards
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
