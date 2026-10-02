"""
Android-facing API for Staff Activity ONLY (separate from the calling-sync API in crm/calling_api.py,
which is not modified and not called from here).

  POST /api/staff-activity/live-call/   live call event: CALL_STARTED | CALL_ACTIVE | CALL_ENDED
  POST /api/staff-activity/status/      heartbeat-style poll: is the 15-minute warning due? (+ ack)

Auth: the existing device bearer token (CallDevice.authenticate - read-only reuse; revoked / unknown /
inactive-staff devices get 401). The server alone decides whether a call qualifies; Android never learns
who the customer is. Phone numbers are never logged or stored.
"""
import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import live_call, staff_activity as sa
from .calling_api import _bearer_token
from .models import CallDevice

logger = logging.getLogger("crm.staff_activity_api")

WARNING_TITLE = "Kota Money Activity Warning"
WARNING_BODY = "You have been inactive in Kota Money CRM for 15 minutes. Please resume CRM work."


def _error(message, status=400):
    return JsonResponse({"success": False, "message": message}, status=status)


def _auth(request):
    device = CallDevice.authenticate(_bearer_token(request))
    if device is None:
        return None, _error("Invalid, revoked, or expired device token.", 401)
    return device, None


def _body(request):
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


@csrf_exempt
@require_POST
def live_call_event(request):
    device, err = _auth(request)
    if err:
        return err
    data = _body(request)
    if data is None:
        return _error("Request body must be a JSON object.")
    session_id = str(data.get("session_id") or "").strip()
    state = str(data.get("state") or "").strip().upper()
    if not live_call.valid_session_id(session_id):
        return _error("A stable session_id (8-64 chars: letters, digits, . _ : -) is required.")
    if state not in live_call.EVENT_STATES:
        return _error("state must be CALL_STARTED, CALL_ACTIVE or CALL_ENDED.")

    now = sa._now()
    qualifying = live_call.ingest(device, session_id, state, str(data.get("phone_number") or "")[:30],
                                  data.get("timestamp"), now)
    sa.reconcile_live_call(device.staff)
    logger.info("[LIVE CALL] device=%s staff=%s state=%s qualifying=%s", device.pk, device.staff_id, state, qualifying)
    return JsonResponse({
        "success": True,
        "qualifying_assigned_call": qualifying,
        "heartbeat_seconds": settings.ACTIVITY_LIVE_CALL_HEARTBEAT_SECONDS,
    })


@csrf_exempt
@require_POST
def device_status(request):
    device, err = _auth(request)
    if err:
        return err
    data = _body(request)
    if data is None:
        return _error("Request body must be a JSON object.")
    if not sa.is_monitored(device.staff):
        return JsonResponse({"success": True, "monitored": False, "warning": None,
                             "next_poll_seconds": settings.ACTIVITY_DEVICE_POLL_SECONDS})
    ack = data.get("ack_warning_id")
    ack = ack if isinstance(ack, int) and not isinstance(ack, bool) else None
    st = sa.device_status(device.staff, ack)
    poll = settings.ACTIVITY_DEVICE_POLL_SECONDS
    left = st.get("seconds_to_inactive")
    if left is not None and st["state"] != sa.LUNCH:
        poll = max(5, min(poll, left + 1))  # wake right when the 15-minute mark is reached
    warning = ({"id": st["warning_id"], "title": WARNING_TITLE, "body": WARNING_BODY}
               if st["warning_id"] else None)
    return JsonResponse({"success": True, "monitored": True, "state": st["state"], "reason": st["reason"],
                         "warning": warning, "next_poll_seconds": poll})
