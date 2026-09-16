from django.conf import settings


def site_settings(request):
    """
    Exposes select settings.py values to every template.
    This is how {{ N8N_FORM_URL }} becomes available globally,
    so the n8n URL only ever has to be defined in one place.
    """
    return {
        "N8N_FORM_URL": settings.N8N_FORM_URL,
        "N8N_LEAD_WEBHOOK_URL": settings.N8N_LEAD_WEBHOOK_URL,
    }
