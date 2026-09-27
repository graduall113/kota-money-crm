from django.contrib.auth import views as auth_views
from django.urls import path

from . import api, calling_api, views, views_admin, views_calling, views_contacts, views_import, views_segments

urlpatterns = [
    # Auth
    path("login/", views.login_view, name="login"),
    path("register/", views.register_view, name="register"),
    path("logout/", views.logout_view, name="logout"),
    path("profile/", views.profile_view, name="profile"),

    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="auth/password_reset_form.html",
            email_template_name="auth/password_reset_email.txt",
            subject_template_name="auth/password_reset_subject.txt",
        ),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(template_name="auth/password_reset_done.html"),
        name="password_reset_done",
    ),
    path(
        "password-reset/confirm/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(template_name="auth/password_reset_confirm.html"),
        name="password_reset_confirm",
    ),
    path(
        "password-reset/complete/",
        auth_views.PasswordResetCompleteView.as_view(template_name="auth/password_reset_complete.html"),
        name="password_reset_complete",
    ),

    # API — n8n → Django (see crm/api.py)
    path("api/leads/create/", api.create_lead, name="api_lead_create"),

    # API — Android companion app + n8n calling report (see crm/calling_api.py)
    path("api/calling/pair/", calling_api.pair_device, name="api_calling_pair"),
    path("api/calling/sync/", calling_api.sync_calls, name="api_calling_sync"),
    path("api/calling/daily/", calling_api.daily_report, name="api_calling_daily"),

    # CRM — dashboard + leads
    path("", views.dashboard, name="dashboard"),
    path("leads/add/", views.lead_create, name="lead_create"),
    path("leads/all/", views.all_leads, name="all_leads"),
    path("leads/mine/", views.my_leads, name="my_leads"),
    path("leads/follow-ups/", views.followups, name="followups"),
    path("leads/export/", views_contacts.leads_export, name="leads_export"),
    path("leads/<int:lead_id>/", views.lead_detail, name="lead_detail"),
    path("leads/<int:lead_id>/action/", views.lead_action, name="lead_action"),
    path("leads/<int:lead_id>/edit/", views.lead_edit, name="lead_edit"),
    path("leads/<int:lead_id>/delete/", views.lead_delete, name="lead_delete"),
    path("reports/", views.reports, name="reports"),
    path("settings/", views_admin.settings_page, name="settings_page"),

    # Calling Analytics (Android companion app)
    path("calling/", views_calling.calling_dashboard, name="calling_dashboard"),
    path("calling/devices/", views_calling.device_list, name="calling_devices"),
    path("calling/devices/pair/", views_calling.device_pair_new, name="calling_device_pair"),
    path("calling/devices/<int:device_id>/toggle/", views_calling.device_toggle, name="calling_device_toggle"),
    path("calling/privileged-numbers/", views_calling.privileged_numbers, name="calling_privileged_numbers"),
    path("calling/privileged-numbers/<int:number_id>/toggle/", views_calling.privileged_number_toggle, name="calling_privileged_number_toggle"),

    # Segments
    path("segments/", views_segments.segment_list, name="segment_list"),
    path("segments/new/", views_segments.segment_create, name="segment_create"),
    path("segments/<int:segment_id>/", views_segments.segment_detail, name="segment_detail"),
    path("segments/<int:segment_id>/edit/", views_segments.segment_edit, name="segment_edit"),
    path("segments/<int:segment_id>/delete/", views_segments.segment_delete, name="segment_delete"),
    path("segments/<int:segment_id>/remove/<int:contact_id>/", views_segments.segment_remove_contact, name="segment_remove_contact"),
    path("contacts/<int:contact_id>/add-to-segment/", views_segments.segment_add_contact_form, name="segment_add_contact"),

    # Contacts
    path("contacts/", views_contacts.contacts_list, name="contacts"),
    path("contacts/add/", views_contacts.contact_create, name="contact_create"),
    path("contacts/export/", views_contacts.contacts_export, name="contacts_export"),
    path("contacts/<int:contact_id>/", views_contacts.contact_detail, name="contact_detail"),
    path("contacts/<int:contact_id>/edit/", views_contacts.contact_edit, name="contact_edit"),
    path("contacts/<int:contact_id>/action/", views_contacts.contact_action, name="contact_action"),
    path("contacts/<int:contact_id>/delete/", views_contacts.contact_delete, name="contact_delete"),

    # Bulk actions + jobs
    path("bulk/<str:model_name>/", views_contacts.bulk_action, name="bulk_action"),
    path("assign/", views_contacts.bulk_assign_page, name="bulk_assign"),
    path("jobs/<int:job_id>/", views_contacts.job_detail, name="job_detail"),

    # Admin — import data
    path("imports/", views_import.import_list, name="import_list"),
    path("imports/new/", views_import.import_new, name="import_new"),
    path("imports/<int:batch_id>/", views_import.import_detail, name="import_detail"),
    path("imports/<int:batch_id>/map/", views_import.import_map, name="import_map"),
    path("imports/<int:batch_id>/progress/", views_import.import_progress, name="import_progress"),
    path("imports/<int:batch_id>/start/", views_import.import_start, name="import_start"),
    path("imports/<int:batch_id>/revalidate/", views_import.import_revalidate, name="import_revalidate"),
    path("imports/<int:batch_id>/undo/", views_import.import_undo, name="import_undo"),
    path("imports/<int:batch_id>/restore/", views_import.import_restore, name="import_restore"),
    path("imports/<int:batch_id>/review/", views_import.import_review, name="import_review"),
    path("imports/<int:batch_id>/errors/", views_import.import_errors, name="import_errors"),

    # Admin — settings, checklist, audit, analytics
    path("settings/documents/", views_admin.document_settings, name="document_settings"),
    path("audit-log/", views_admin.audit_log, name="audit_log"),
    path("analytics/", views_admin.performance, name="performance"),
    path("analytics/documents/", views_admin.document_analytics, name="document_analytics"),

    # Admin — staff management
    path("staff/", views.staff_list, name="staff_list"),
    path("staff/add/", views.staff_add, name="staff_add"),
    path("staff/<int:user_id>/", views_admin.staff_detail, name="staff_detail"),
    path("staff/<int:user_id>/edit/", views.staff_edit, name="staff_edit"),
    path("staff/<int:user_id>/remove/", views.staff_remove, name="staff_remove"),
    path("staff/<int:user_id>/delete/", views.staff_delete, name="staff_delete"),
    path("staff/<int:user_id>/toggle-status/", views.staff_toggle_status, name="staff_toggle_status"),
]
