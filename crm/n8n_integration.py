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
        "lead_id": lead.pk,
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


def send_lead_to_n8n(lead):
    """
    POSTs the given (already-saved) Lead to settings.N8N_LEAD_WEBHOOK_URL
    and updates its n8n_sync_status / n8n_last_sync / n8n_error fields
    to reflect the outcome.

    Returns (success: bool, error_message: str). On success error_message
    is "". This function never raises — every failure mode is caught so
    the calling view can always show the user a clean message instead of
    a Django error page.
    """
    # Single source of truth: the URL an admin saved in Settings → n8n, falling
    # back to settings.N8N_LEAD_WEBHOOK_URL (env var) when none is saved.
    from .settings_store import get_bool, get_n8n_webhook_url

    if not get_bool("n8n_enabled"):
        return False, "n8n integration is switched off in Settings."
    webhook_url = get_n8n_webhook_url()

    if not webhook_url:
        error_message = "N8N_LEAD_WEBHOOK_URL is not configured."
        logger.error("n8n sync skipped for lead %s: %s", lead.pk, error_message)
        lead.n8n_sync_status = lead.N8N_SYNC_FAILED
        lead.n8n_error = error_message
        lead.n8n_last_sync = timezone.now()
        lead.save(update_fields=["n8n_sync_status", "n8n_error", "n8n_last_sync"])
        return False, error_message

    payload = build_lead_payload(lead)
    logger.info("Sending lead %s to n8n webhook %s", lead.pk, webhook_url)

    try:
        response = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        error_message = str(exc)[:2000]
        logger.error("n8n webhook request failed for lead %s: %s", lead.pk, error_message)
        lead.n8n_sync_status = lead.N8N_SYNC_FAILED
        lead.n8n_error = error_message
        lead.n8n_last_sync = timezone.now()
        lead.save(update_fields=["n8n_sync_status", "n8n_error", "n8n_last_sync"])
        return False, error_message

    logger.info(
        "n8n webhook accepted lead %s (HTTP %s)", lead.pk, response.status_code
    )
    lead.n8n_sync_status = lead.N8N_SYNC_SUCCESS
    lead.n8n_error = ""
    lead.n8n_last_sync = timezone.now()
    lead.save(update_fields=["n8n_sync_status", "n8n_error", "n8n_last_sync"])
    return True, ""
