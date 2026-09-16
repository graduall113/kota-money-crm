from django.contrib import admin

from .models import Lead, StaffProfile


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
