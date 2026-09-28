"""
Attendance gate: deny-by-default for non-admin staff.

Every URL a staff member requests is blocked unless they have an ACTIVE
attendance record today OR the URL is on the small allow-list below. Because
it is a middleware (not per-view decorators), direct URL access, POSTs and
XHR/JSON calls are all blocked, and any view added in future is protected
automatically.
"""
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import Resolver404, resolve, reverse

from . import attendance

# Reachable without an active day: sign-in/out, password reset, the attendance
# page + its two actions, and the account/profile pages.
ALLOWED_URL_NAMES = {
    "login", "register", "logout",
    "password_reset", "password_reset_done", "password_reset_confirm", "password_reset_complete",
    "attendance", "attendance_start", "attendance_end", "attendance_device_enroll",
    "profile", "settings_page",
}
# Not session/CRM pages: token- or key-authenticated APIs (n8n, Android app),
# and Django's own admin site (governed by is_staff/admin, never locked here).
EXEMPT_PATH_PREFIXES = ("/api/", "/static/")


class AttendanceRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and attendance.requires_attendance(user) and not self._exempt(request):
            state, _ = attendance.request_state(request)
            if state != attendance.STATE_ACTIVE:
                return self._deny(request, state)
        return self.get_response(request)

    @staticmethod
    def _exempt(request):
        path = request.path_info
        if path.startswith(EXEMPT_PATH_PREFIXES) or path.startswith(reverse("admin:index")):
            return True
        try:
            match = resolve(path)
        except Resolver404:
            return True  # let Django render its normal 404
        return match.url_name in ALLOWED_URL_NAMES and not match.namespaces

    @staticmethod
    def _deny(request, state):
        if state == attendance.STATE_ENDED:
            msg = "Your day has ended. CRM access is locked until you start your day tomorrow."
        else:
            msg = "Start your day to access the CRM."
        if request.headers.get("x-requested-with") == "XMLHttpRequest" or "application/json" in request.headers.get("accept", ""):
            return JsonResponse({"error": "attendance_required", "state": state, "detail": msg}, status=403)
        messages.warning(request, msg)
        return redirect("attendance")
