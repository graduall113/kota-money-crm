"""
API surface for the n8n → Django integration (see master prompt sections 5-8, 27-28).

n8n's HTTP Request node POSTs the submitted lead here after its existing
Google Sheets + Gmail steps run. This view never talks back to n8n's form
or workflow — it only accepts the payload, validates it, resolves the
reference/assignment staff members, and writes a Lead row.
"""

import datetime
import json

from django.contrib.auth.models import User
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Lead


def _error(message, status=400, **extra):
    payload = {"success": False, "message": message}
    payload.update(extra)
    return JsonResponse(payload, status=status)


def _check_api_key(request):
    """
    n8n sends the shared secret in the X-N8N-API-KEY header.
    settings.N8N_API_KEY is read from the N8N_API_KEY environment
    variable — it is never hardcoded in source.
    """
    expected = getattr(settings, "N8N_API_KEY", "")
    provided = request.headers.get("X-N8N-API-KEY", "")
    return bool(expected) and provided == expected


def _resolve_staff_user(raw_name):
    """
    Looks up a staff member from a free-text name/username/email as it
    might arrive from the n8n form (e.g. "Prakhar"). Tries, in order:
    username, email, and a case-insensitive match on full name.
    Returns (user_or_None, was_provided).
    """
    if raw_name is None:
        return None, False

    name = str(raw_name).strip()
    if not name:
        return None, False

    user = (
        User.objects.filter(username__iexact=name).first()
        or User.objects.filter(email__iexact=name).first()
    )
    if user:
        return user, True

    for candidate in User.objects.select_related("staff_profile").all():
        full_name = candidate.get_full_name().strip()
        if full_name and full_name.lower() == name.lower():
            return candidate, True

    return None, True  # a name was given, but nobody matched it


def _resolve_status(raw_status):
    if not raw_status:
        return Lead.STATUS_NEW, True
    key = str(raw_status).strip().lower()
    if key in Lead.STATUS_ALIASES:
        return Lead.STATUS_ALIASES[key], True
    return None, False


def _parse_date(raw_date):
    """Accepts DD/MM/YYYY (the n8n form's format) or ISO YYYY-MM-DD."""
    if not raw_date:
        return None
    raw_date = str(raw_date).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(raw_date, fmt).date()
        except ValueError:
            continue
    return None


def _parse_decimal(raw_amount):
    if raw_amount in (None, ""):
        return None
    cleaned = str(raw_amount).replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


@csrf_exempt
@require_POST
def create_lead(request):
    # 1. Authenticate the request.
    if not _check_api_key(request):
        return _error("Invalid or missing API key.", status=401)

    # 2. Validate incoming data.
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return _error("Request body must be valid JSON.", status=400)

    customer_name = str(data.get("customer_name") or "").strip()
    contact_number = str(data.get("contact_number") or "").strip()

    if not customer_name:
        return _error("Missing customer name.", status=400)

    digits = "".join(ch for ch in contact_number if ch.isdigit())
    if len(digits) < 7:
        return _error("Invalid phone number.", status=400)

    status_value, status_ok = _resolve_status(data.get("status"))
    if not status_ok:
        return _error(f"Invalid status '{data.get('status')}'.", status=400)

    # 3/4. Convert n8n field names to Django fields + find the correct
    # Reference By / Assigned To users. Never guess — an unmatched name
    # is a clear, explicit error back to n8n.
    reference_by_user, reference_provided = _resolve_staff_user(data.get("reference_by"))
    if reference_provided and reference_by_user is None:
        return _error(
            f"Reference by staff member '{data.get('reference_by')}' was not found.",
            status=400,
        )

    assigned_to_user, assigned_provided = _resolve_staff_user(data.get("assigned_to"))
    if assigned_provided and assigned_to_user is None:
        return _error(
            f"Assigned To staff member '{data.get('assigned_to')}' was not found.",
            status=400,
        )

    # Duplicate prevention: n8n may retry the same webhook delivery.
    # submitted_at (the form's submittedAt) is treated as the external
    # submission id; if we've already created a Lead for it, hand back
    # that existing lead instead of creating a second one.
    submitted_at = str(data.get("submitted_at") or "").strip() or None
    if submitted_at:
        existing = Lead.objects.filter(external_submission_id=submitted_at).first()
        if existing:
            return JsonResponse(
                {
                    "success": True,
                    "message": "Duplicate submission — existing lead returned.",
                    "lead_id": existing.pk,
                    "duplicate": True,
                }
            )

    # 6. Create the Lead.
    try:
        lead = Lead.objects.create(
            customer_name=customer_name,
            contact_number=contact_number,
            work_profile=str(data.get("work_profile") or "").strip(),
            income=str(data.get("income") or "").strip(),
            requirement=str(data.get("requirement") or "").strip(),
            loan_amount=_parse_decimal(data.get("loan_amount")),
            bank_calling=str(data.get("bank_calling") or "").strip(),
            status=status_value,
            form_date=_parse_date(data.get("form_date")),
            external_submission_id=submitted_at,
            reference_by=reference_by_user,
            assigned_to=assigned_to_user,
        )
    except Exception:
        return _error("Database error while creating the lead.", status=500)

    # Timeline + assignment history for API-created leads (best effort).
    try:
        from .models import AssignmentHistory, user_label
        from .services import log_activity, log_audit

        if assigned_to_user:
            lead.original_assigned_to = assigned_to_user
            lead.save(update_fields=["original_assigned_to"])
            AssignmentHistory.objects.create(
                lead=lead, action=AssignmentHistory.ACTION_ASSIGN, to_user=assigned_to_user,
                to_name=user_label(assigned_to_user), changed_by_name="n8n", reason="Lead created via n8n")
        log_activity(None, "created", "Lead created via n8n form", lead=lead)
        log_audit(None, "lead_created", f"{lead.display_id} created via n8n API", {}, "lead", lead.pk)
    except Exception:  # never fail the webhook because of bookkeeping
        pass

    # 7. Return JSON success response.
    return JsonResponse(
        {"success": True, "message": "Lead created successfully", "lead_id": lead.pk},
        status=201,
    )
