"""
Outbound Django CRM -> n8n webhook integration.

This is the *opposite* direction from crm/api.py (which accepts inbound
POSTs from n8n). This module is called right after the CRM's own native
Lead form (crm/views.py:lead_create) saves a Lead to the database, and
pushes that same lead to the n8n Production webhook so the existing
Gmail / Google Sheets / WhatsApp automation keeps working.

The Django database is always the source of truth: this function is
called *after* the Lead row is already committed, and it never raises —
any failure (timeout, connection error, non-2xx response) is caught,
logged, and recorded on the Lead itself via the n8n_sync_status /
n8n_last_sync / n8n_error fields. The lead is never rolled back or
deleted because the webhook failed.
"""

import hashlib
import json
import logging

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("crm.n8n")

# Kept generous but bounded so a slow/unreachable n8n instance can never
# hang the CRM's request/response cycle indefinitely.
DEFAULT_TIMEOUT_SECONDS = 15


def build_lead_payload(lead):
    """
    Builds the exact JSON payload the existing n8n workflow (Gmail +
    Google Sheets nodes) expects. Every value is derived from the actual
    saved Lead row — nothing here is hardcoded or a placeholder.

    reference_by / assigned_to send the free-text values collected on the
    n8n-matching form (Lead.reference_by_name / assigned_to_name) — NOT
    the CRM's internal staff-ownership FKs (Lead.reference_by / assigned_to),
    which are a separate, unrelated concept. See models.py / forms.py.
    """
    return {
        # "lead_id" is the numeric database id and MUST stay numeric — the
        # existing n8n workflow already consumes it. The human-readable ID is a
        # separate, additive field: "lead_reference_id" (e.g. "KM-1058"). It is
        # what the Google Sheet's "Lead ID" column is filled from.
        "lead_id": lead.pk,
        "lead_reference_id": lead.display_id,
        "date": lead.form_date.strftime("%d/%m/%Y") if lead.form_date else "",
        "name": lead.customer_name,
        "contact_no": str(lead.contact_number or ""),
        "work_profile": lead.work_profile or "",
        "income": lead.income or "",
        "requirement": lead.requirement or "",
        # Decimal -> str so it's JSON-serializable and Sheets/Gmail get a
        # plain value like "2500000" rather than a Python Decimal repr.
        "loan_amount": str(lead.loan_amount) if lead.loan_amount is not None else "",
        "bank_calling": lead.bank_calling or "",
        "status": lead.get_status_display(),
        "reference_by": lead.reference_by_name or "",
        "assigned_to": lead.assigned_to_name or "",
    }


# Keys that describe the DELIVERY, not the lead. They are left out of the content
# hash so a Lead save that changes nothing synced never triggers a re-send.
_NON_CONTENT_KEYS = {"event", "idempotency_key", "sync_reason", "is_new_lead", "updated_at"}

CREATION_REASONS = ("created", "converted")


def build_sync_payload(lead, reasons=""):
    """
    The lead_upsert payload sent to n8n for EVERY sync (new lead, edit, status /
    assignment / reference change, contact edit ...).

    It is build_lead_payload() (all legacy keys, unchanged) plus ADDITIVE keys, so the
    existing workflow keeps working and extra Sheet columns can be mapped whenever wanted:

      event / idempotency_key   "lead_upsert" / "lead-KM-1050" (also sent as the
                                Idempotency-Key header). The Sheet key is lead_reference_id.
      sync_reason / is_new_lead why this was sent; is_new_lead lets the n8n workflow send
                                Gmail / WhatsApp ONLY for a brand-new lead, not on every edit.
      assigned_to_staff /       the CRM staff-ownership users (My Leads owner / creator).
      reference_by_staff        The legacy assigned_to / reference_by keys still carry the
                                free-text values from the form and are NOT redefined here.
      email, city, source, interest, next_followup_date, contact_id, updated_at
    """
    from .models import user_label

    reason_list = [r for r in str(reasons or "").split(",") if r]
    payload = build_lead_payload(lead)
    payload.update({
        "event": "lead_upsert",
        "idempotency_key": f"lead-{lead.display_id}",
        "sync_reason": ",".join(reason_list),
        "is_new_lead": any(r in CREATION_REASONS for r in reason_list),
        "email": lead.email or "",
        "city": lead.city or "",
        "source": lead.source or "",
        "interest": lead.get_interest_display() if lead.interest else "",
        "next_followup_date": lead.next_followup_date.strftime("%d/%m/%Y") if lead.next_followup_date else "",
        "assigned_to_staff": user_label(lead.assigned_to) if lead.assigned_to_id else "",
        "reference_by_staff": user_label(lead.reference_by) if lead.reference_by_id else "",
        "contact_id": lead.contact_id or "",
        "updated_at": timezone.localtime(lead.updated_at).strftime("%d/%m/%Y %H:%M") if lead.updated_at else "",
    })
    return payload


def payload_content_hash(payload):
    """Stable hash of only the lead's synced CONTENT (see _NON_CONTENT_KEYS)."""
    content = {k: v for k, v in payload.items() if k not in _NON_CONTENT_KEYS}
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def post_payload(webhook_url, payload):
    """
    One HTTP POST to n8n. Raises requests.exceptions.RequestException on a network
    error OR a non-2xx answer (so n8n's own 5xx, e.g. Google Sheets failing, counts as
    a failed delivery and gets retried). Returns the response on success.

    The Idempotency-Key is stable per Lead and identical on every attempt. n8n Cloud
    does not de-duplicate on it by itself: the guard against a second Sheet row is the
    Google Sheets node "Append or Update Row" matching on the Lead ID column.
    """
    response = requests.post(
        webhook_url,
        json=payload,
        headers={"Content-Type": "application/json", "Idempotency-Key": payload["idempotency_key"]},
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response


def send_lead_to_n8n(lead):
    """
    Synchronous "sync this lead to n8n right now" (Add New Lead / Convert Contact).

    Kept with its original contract: returns (success: bool, error_message: str), never
    raises, and updates Lead.n8n_sync_status / n8n_last_sync / n8n_error. Internally it
    now goes through the sync queue (crm/lead_sync.py), so a failed delivery is retried
    automatically instead of being lost, and it can never race a second event for the
    same Lead into a second Sheet row.
    """
    from . import lead_sync

    return lead_sync.sync_lead_now(lead, reason="converted" if lead.contact_id else "created")
