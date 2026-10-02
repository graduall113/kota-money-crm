from django.conf import settings

from . import attendance, staff_activity


def attendance_widget(request):
    """Sidebar attendance control — only for staff who are subject to attendance."""
    user = getattr(request, "user", None)
    if user is None or not attendance.requires_attendance(user):
        return {}
    state, record = attendance.request_state(request)
    info = attendance.describe(state, record)
    ctx = {"attendance": info}
    if state == attendance.STATE_ACTIVE and settings.ACTIVITY_MONITORING_ENABLED:
        from .models import LunchBreak
        lunch = LunchBreak.objects.filter(user=user, work_date=record.work_date).first()
        ctx["activity_cfg"] = {
            "heartbeat": settings.ACTIVITY_HEARTBEAT_SECONDS, "report_min": settings.ACTIVITY_REPORT_MIN_SECONDS,
            "warn_minutes": settings.ACTIVITY_WARNING_MINUTES, "inactive_minutes": settings.ACTIVITY_INACTIVITY_MINUTES,
        }
        ctx["lunch"] = {
            "on_lunch": bool(lunch and lunch.ended_at is None),
            "taken": lunch is not None, "started_label": attendance.fmt_clock(lunch.started_at) if lunch else "",
        }
    return ctx
