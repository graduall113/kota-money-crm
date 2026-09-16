from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect


def _has_admin_profile(user):
    profile = getattr(user, "staff_profile", None)
    return bool(profile and profile.is_admin)


def admin_required(view_func):
    """
    Requires an authenticated user with StaffProfile.role == 'admin'.
    Staff who try to reach an admin-only URL directly get a 403 —
    this is a real permission check, not a hidden sidebar link.
    """

    @wraps(view_func)
    @login_required
    def _wrapped(request, *args, **kwargs):
        if not _has_admin_profile(request.user):
            raise PermissionDenied("This section is only available to admins.")
        return view_func(request, *args, **kwargs)

    return _wrapped


def active_account_required(view_func):
    """
    Blocks login-but-deactivated accounts from using the CRM even though
    Django's session is technically still valid.
    """

    @wraps(view_func)
    @login_required
    def _wrapped(request, *args, **kwargs):
        profile = getattr(request.user, "staff_profile", None)
        if profile and not profile.is_account_active:
            messages.error(request, "Your account has been deactivated. Contact an admin.")
            from django.contrib.auth import logout

            logout(request)
            return redirect("login")
        return view_func(request, *args, **kwargs)

    return _wrapped
