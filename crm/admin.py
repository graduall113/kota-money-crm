from django.contrib import admin

from .models import Attendance, AttendanceCorrection, AttendanceEvent, CallDevice, CallRecord, Lead, LeadSyncEvent, PrivilegedNumber, StaffProfile, TrustedDevice


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "status", "phone")
    list_filter = ("role", "status")
    search_fields = ("user__username", "user__email", "user__first_name", "user__last_name")


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("display_id", "customer_name", "contact_number", "status", "reference_by", "assigned_to", "created_at")
    list_filter = ("status",)
    search_fields = ("customer_name", "contact_number")
    # Lead ID is derived from the immutable primary key; show it, never edit it.
    readonly_fields = ("display_id",)


@admin.register(LeadSyncEvent)
class LeadSyncEventAdmin(admin.ModelAdmin):
    """Read-only view of the Lead -> n8n -> Google Sheets sync queue (status, attempts, last error)."""
    list_display = ("lead_reference_id", "status", "reasons", "attempts", "next_attempt_at", "http_status", "created_at", "finished_at")
    list_filter = ("status",)
    search_fields = ("lead_reference_id", "idempotency_key", "last_error")
    readonly_fields = [f.name for f in LeadSyncEvent._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(CallDevice)
class CallDeviceAdmin(admin.ModelAdmin):
    list_display = ("label", "staff", "is_paired", "is_active", "last_seen", "created_at")
    list_filter = ("is_active",)
    search_fields = ("label", "staff__username", "staff__first_name", "staff__last_name", "android_device_id")
    readonly_fields = ("token_hash", "pairing_code", "pairing_code_expires", "paired_at", "last_seen", "created_at")


@admin.register(CallRecord)
class CallRecordAdmin(admin.ModelAdmin):
    list_display = ("phone_number", "staff", "direction", "status", "duration_seconds", "started_at", "lead")
    list_filter = ("direction", "status", "source")
    search_fields = ("phone_number", "phone_normalized", "external_call_id", "staff__username")
    readonly_fields = ("external_call_id", "created_at")
    date_hierarchy = "started_at"


@admin.register(PrivilegedNumber)
class PrivilegedNumberAdmin(admin.ModelAdmin):
    list_display = ("phone_number", "label", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("phone_number", "phone_normalized", "label")


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    """Read-only audit view: attendance is only ever written by crm/attendance.py."""

    list_display = ("user", "work_date", "start_time", "end_time", "worked_duration", "attendance_status", "auto_ended",
                    "start_verification", "start_distance_from_office", "start_ip")
    list_filter = ("attendance_status", "auto_ended", "start_verification", "work_date")
    search_fields = ("user__username", "user__first_name", "user__last_name")
    date_hierarchy = "work_date"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AttendanceEvent)
class AttendanceEventAdmin(admin.ModelAdmin):
    """Read-only: events are written by the server. Review them in the CRM's Attendance Review screen."""

    list_display = ("created_at", "user_name", "action", "event_type", "outcome", "review_status", "ip")
    list_filter = ("event_type", "outcome", "review_status", "action")
    search_fields = ("user_name", "message", "ip")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TrustedDevice)
class TrustedDeviceAdmin(admin.ModelAdmin):
    list_display = ("label", "user", "is_active", "enrolled_at", "last_seen_at", "last_ip")
    list_filter = ("is_active",)
    search_fields = ("label", "user__username")
    def has_add_permission(self, request):
        return False  # devices only come from the enrolment flow

    readonly_fields = ("enrollment_code_hash", "token_hash", "enrolled_at", "last_seen_at", "last_ip", "user_agent",
                       "revoked_at", "revoked_by", "created_at", "created_by")


@admin.register(AttendanceCorrection)
class AttendanceCorrectionAdmin(admin.ModelAdmin):
    """Read-only: corrections are written only by crm.attendance_admin.apply_correction()."""

    list_display = ("created_at", "admin_name", "staff_name", "work_date", "field", "old_value", "new_value", "reason")
    list_filter = ("field",)
    search_fields = ("staff_name", "admin_name", "reason")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
