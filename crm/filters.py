"""
Filter / search engines for the Leads and Contacts lists.

They ALWAYS receive an already-authorized queryset (access.visible_*), so a
tampered query string can only ever narrow what the user is allowed to see.
Bad values (non-numeric ids, malformed dates) are ignored, never raised.
"""
import datetime
import re

from django.core.paginator import EmptyPage, Paginator
from django.db.models import Q
from django.utils import timezone

from . import services
from .models import Contact, Lead, LeadDocument


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _date(value):
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(str(value).strip(), fmt).date()
        except (TypeError, ValueError):
            continue
    return None


def _dec(value):
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _user_filter(qs, field, raw):
    if raw in (None, ""):
        return qs
    if raw == "none":
        return qs.filter(**{f"{field}__isnull": True})
    uid = _int(raw)
    return qs.filter(**{f"{field}_id": uid}) if uid is not None else qs


def _range(qs, field, start, end, is_datetime=True):
    start, end = _date(start), _date(end)
    if start:
        qs = qs.filter(**{f"{field}__date__gte" if is_datetime else f"{field}__gte": start})
    if end:
        qs = qs.filter(**{f"{field}__date__lte" if is_datetime else f"{field}__lte": end})
    return qs


def _followup_bucket(qs, bucket):
    today = timezone.localdate()
    base = qs.filter(followup_status="pending", next_followup_date__isnull=False)
    if bucket == "today":
        return base.filter(next_followup_date=today)
    if bucket == "overdue":
        return base.filter(next_followup_date__lt=today)
    if bucket == "upcoming":
        return base.filter(next_followup_date__gt=today)
    return qs


def _search_q(q, phone_field, extra):
    q = (q or "").strip()
    if not q:
        return None
    cond = Q()
    for f in extra:
        cond |= Q(**{f"{f}__icontains": q})
    digits = re.sub(r"\D", "", q)
    if len(digits) >= 3:
        cond |= Q(**{f"{phone_field}__contains": digits})
    return cond


LEAD_SORTS = {
    "created": "created_at", "-created": "-created_at", "updated": "updated_at", "-updated": "-updated_at",
    "name": "customer_name", "-name": "-customer_name", "loan": "loan_amount", "-loan": "-loan_amount",
    "followup": "next_followup_date", "-followup": "-next_followup_date",
}
CONTACT_SORTS = {
    "created": "id", "-created": "-id", "name": "name", "-name": "-name",
    "updated": "updated_at", "-updated": "-updated_at",
}


def filter_leads(qs, p):
    q = (p.get("q") or "").strip()
    if q:
        cond = _search_q(q, "phone_normalized", ["customer_name", "email"]) or Q()
        m = re.fullmatch(r"(?i)km-?(\d+)|(\d{1,9})", q)
        if m:
            cond |= Q(pk=int(m.group(1) or m.group(2)))
        qs = qs.filter(cond)
    if p.get("status"):
        qs = qs.filter(status=p["status"])
    qs = _user_filter(qs, "assigned_to", p.get("assigned_to"))
    qs = _user_filter(qs, "reference_by", p.get("reference_by"))
    qs = _range(qs, "form_date", p.get("date_from"), p.get("date_to"), is_datetime=False)
    qs = _range(qs, "created_at", p.get("created_from"), p.get("created_to"))
    qs = _range(qs, "updated_at", p.get("updated_from"), p.get("updated_to"))
    qs = _range(qs, "next_followup_date", p.get("followup_from"), p.get("followup_to"), is_datetime=False)
    if p.get("source"):
        qs = qs.filter(source__iexact=p["source"])
    if p.get("work_profile"):
        qs = qs.filter(work_profile__icontains=p["work_profile"].strip())
    if p.get("requirement"):
        qs = qs.filter(requirement__icontains=p["requirement"].strip())
    if p.get("income"):
        qs = qs.filter(income__icontains=p["income"].strip())
    lo, hi = _dec(p.get("loan_min")), _dec(p.get("loan_max"))
    if lo is not None:
        qs = qs.filter(loan_amount__gte=lo)
    if hi is not None:
        qs = qs.filter(loan_amount__lte=hi)
    if p.get("interest") in ("interested", "not_interested"):
        qs = qs.filter(interest=p["interest"])
    if _int(p.get("import_batch")) is not None:
        qs = qs.filter(import_batch_id=_int(p["import_batch"]))
    if p.get("followup"):
        qs = _followup_bucket(qs, p["followup"])
    if p.get("doc_status") in ("not_started", "partial", "complete", "pending"):
        qs = services.filter_doc_status(qs, p["doc_status"])
    doc_id = _int(p.get("doc"))
    if doc_id is not None and p.get("doc_state") in ("received", "pending"):
        received_ids = LeadDocument.objects.filter(document_type_id=doc_id, received=True).values("lead_id")
        qs = qs.filter(pk__in=received_ids) if p["doc_state"] == "received" else qs.exclude(pk__in=received_ids)
    return qs.order_by(LEAD_SORTS.get(p.get("sort"), "-created_at"), "-id")


def filter_contacts(qs, p):
    q = (p.get("q") or "").strip()
    if q:
        cond = _search_q(q, "phone_normalized", ["name", "email"])
        if cond is not None:
            qs = qs.filter(cond)
    if p.get("status"):
        qs = qs.filter(status=p["status"])
    qs = _user_filter(qs, "current_assigned_to", p.get("assigned_to"))
    qs = _user_filter(qs, "reference_by", p.get("reference_by"))
    if _int(p.get("import_batch")) is not None:
        qs = qs.filter(import_batch_id=_int(p["import_batch"]))
    if _int(p.get("segment")) is not None:
        qs = qs.filter(segments__id=_int(p["segment"]))
    if p.get("city"):
        qs = qs.filter(city__icontains=p["city"].strip())
    if p.get("source"):
        qs = qs.filter(source__iexact=p["source"])
    if p.get("work_profile"):
        qs = qs.filter(work_profile__icontains=p["work_profile"].strip())
    qs = _range(qs, "created_at", p.get("created_from"), p.get("created_to"))
    qs = _range(qs, "updated_at", p.get("updated_from"), p.get("updated_to"))
    if p.get("has_lead") == "yes":
        qs = qs.filter(leads__isnull=False).distinct()
    elif p.get("has_lead") == "no":
        qs = qs.filter(leads__isnull=True)
    if p.get("followup"):
        qs = _followup_bucket(qs, p["followup"])
    return qs.order_by(CONTACT_SORTS.get(p.get("sort"), "-id"))


def paginate(request, qs, per_page=50):
    """Server-side pagination. Never renders more than `per_page` rows."""
    try:
        per_page = max(10, min(int(request.GET.get("per_page", per_page)), 200))
    except ValueError:
        pass
    paginator = Paginator(qs, per_page)
    try:
        page = paginator.page(request.GET.get("page") or 1)
    except (EmptyPage, Exception):
        page = paginator.page(1)
    return page


def querystring_without(request, *drop):
    q = request.GET.copy()
    for key in drop:
        q.pop(key, None)
    return q.urlencode()
