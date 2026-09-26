from django.contrib import admin

from .models import CallDevice, CallRecord, Lead, PrivilegedNumber, StaffProfile


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "status", "phone")
    list_filter = ("role", "status")
    search_fields = ("user__username", "user__email", "user__first_name", "user__last_name")


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("customer_name", "contact_number", "status", "reference_by", "assigned_to", "created_at")
    list_filter = ("status",)
    search_fields = ("customer_name", "contact_number")


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
