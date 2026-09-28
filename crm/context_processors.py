from . import attendance


def attendance_widget(request):
    """Sidebar attendance control — only for staff who are subject to attendance."""
    user = getattr(request, "user", None)
    if user is None or not attendance.requires_attendance(user):
        return {}
    state, record = attendance.request_state(request)
    return {"attendance": attendance.describe(state, record)}
