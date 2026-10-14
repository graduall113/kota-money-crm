"""
API surface for the n8n → Django integration (see master prompt sections 5-8, 27-28).

n8n's HTTP Request node POSTs the submitted lead here after its existing
Google Sheets + Gmail steps run. This view never talks back to n8n's form
or workflow — it only accepts the payload, validates it, and writes a Lead row.

OWNERSHIP POLICY (inbound leads)
--------------------------------
Three different things are kept strictly apart:

* ``assigned_to`` / ``reference_by`` in the payload  -> FREE TEXT ("Assigned To" = whoever will nurture the lead,
  "Reference by" = whoever referred it). Stored as typed in ``Lead.assigned_to_name`` / ``Lead.reference_by_name``.
  They may be CRM staff, an external partner or anyone else, and they are NEVER looked up, matched or used to decide
  who owns the lead in the CRM. A name that happens to equal a username / e-mail / full name grants nothing.
* ``Lead.assigned_to``  (CRM owner FK) -> decides who sees / edits the lead. The payload carries no logged-in user, so
  an inbound lead is created UNOWNED (visible to admins only) and an admin assigns it, exactly like any other
  unassigned lead. The only way to set an owner at creation is the separate, explicit ``crm_owner`` key (username or
  e-mail of exactly one ACTIVE staff member). If it is absent or cannot be resolved safely, the lead stays unowned and
  the response says so; the lead is never rejected or lost because of it.
* ``Lead.reference_by`` (CRM referrer FK) and ``Lead.created_by`` -> stay empty for inbound leads (no CRM user
  created them). A free-text reference name can therefore never grant referrer view / edit / delete rights.
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


OWNER_OK = "ok"
OWNER_NOT_GIVEN = "not_given"
OWNER_NOT_FOUND = "not_found"
OWNER_AMBIGUOUS = "ambiguous"
OWNER_INELIGIBLE = "ineligible"


def _resolve_crm_owner(raw_owner):
    """
    Resolves the EXPLICIT ``crm_owner`` key (never the free-text Assigned To / Reference by) to one CRM user.

    Accepts a username or an e-mail address only (no full-name or fuzzy matching: names are not unique and
    guessing is how a lead ends up with the wrong owner). The match must be unambiguous and the account must
    be allowed to own leads - the same rule services.assign_lead() enforces (active StaffProfile) plus an
    active Django account.

    Returns (user_or_None, status) where status is one of the OWNER_* constants.
    """
    if raw_owner is None or isinstance(raw_owner, (dict, list, bool)):
        return None, (OWNER_NOT_GIVEN if raw_owner is None else OWNER_NOT_FOUND)
    value = str(raw_owner).strip()
    if not value:
        return None, OWNER_NOT_GIVEN

    user = User.objects.filter(username=value).first()          # usernames are unique: an exact hit is unambiguous
    if user is None:
        matches = list(User.objects.filter(username__iexact=value)[:2])
        if not matches and "@" in value:
            matches = list(User.objects.filter(email__iexact=value)[:2])
        if len(matches) > 1:
            return None, OWNER_AMBIGUOUS
        user = matches[0] if matches else None
    if user is None:
        return None, OWNER_NOT_FOUND

    profile = getattr(user, "staff_profile", None)
    if not user.is_active or profile is None or not profile.is_account_active:
        return None, OWNER_INELIGIBLE
    return user, OWNER_OK


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

    # 3/4. "Reference by" and "Assigned To" are FREE-TEXT names on the n8n form: whoever referred the lead and
    # whoever will nurture it - CRM staff, an external partner or anyone else. They are stored exactly as typed
    # (whitespace tidied) in Lead.reference_by_name / assigned_to_name and are NEVER matched against CRM users, so
    # they can neither be rejected for "not being staff" nor grant anybody ownership, visibility or edit/delete
    # rights. CRM ownership comes only from the separate, explicit `crm_owner` key (see the module docstring);
    # without a valid one the lead is created unowned (admins see it and assign it). Nothing is ever guessed and
    # no user account is ever created.
    reference_name = " ".join(str(data.get("reference_by") or "").split())
    assigned_name = " ".join(str(data.get("assigned_to") or "").split())
    for label, value in (("Reference by", reference_name), ("Assigned To", assigned_name)):
        if len(value) > 200:
            return _error(f"{label} is too long (maximum 200 characters).", status=400)
    owner_user, owner_status = _resolve_crm_owner(data.get("crm_owner"))
    warnings = []
    if owner_status not in (OWNER_OK, OWNER_NOT_GIVEN):
        reason = {
            OWNER_NOT_FOUND: "no CRM user has that username or e-mail",
            OWNER_AMBIGUOUS: "more than one CRM user matches",
            OWNER_INELIGIBLE: "that account is inactive or cannot own leads",
        }[owner_status]
        warnings.append(f"crm_owner was not applied ({reason}); the lead was created unassigned for an admin to assign.")

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
                    "lead_reference_id": existing.display_id,
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
            reference_by=None,          # free-text names never create CRM referrer rights
            assigned_to=owner_user,     # only an explicit, valid `crm_owner`; otherwise unowned
            reference_by_name=reference_name,
            assigned_to_name=assigned_name,
        )
    except Exception:
        return _error("Database error while creating the lead.", status=500)

    # Timeline + assignment history for API-created leads (best effort).
    try:
        from .models import AssignmentHistory, user_label
        from .services import log_activity, log_audit

        if owner_user:
            lead.original_assigned_to = owner_user
            lead.save(update_fields=["original_assigned_to"])
            AssignmentHistory.objects.create(
                lead=lead, action=AssignmentHistory.ACTION_ASSIGN, to_user=owner_user,
                to_name=user_label(owner_user), changed_by_name="n8n", reason="Lead created via n8n (crm_owner)")
        log_activity(None, "created", "Lead created via n8n form", lead=lead)
        log_audit(None, "lead_created", f"{lead.display_id} created via n8n API",
                  {"crm_owner": user_label(owner_user) if owner_user else "", "owner_status": owner_status,
                   "assigned_to_name": assigned_name, "reference_by_name": reference_name}, "lead", lead.pk)
    except Exception:  # never fail the webhook because of bookkeeping
        pass

    # 7. Return JSON success response.
    body = {"success": True, "message": "Lead created successfully", "lead_id": lead.pk,
            "lead_reference_id": lead.display_id, "crm_owner_assigned": bool(owner_user)}
    if warnings:
        body["warnings"] = warnings
    return JsonResponse(body, status=201)
