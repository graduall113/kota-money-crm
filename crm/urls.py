from django.contrib.auth import views as auth_views
from django.urls import path

from . import api, views

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

    # CRM
    path("", views.dashboard, name="dashboard"),
    path("leads/add/", views.lead_create, name="lead_create"),
    path("leads/<int:lead_id>/edit/", views.lead_edit, name="lead_edit"),
    path("leads/<int:lead_id>/delete/", views.lead_delete, name="lead_delete"),
    path("leads/all/", views.all_leads, name="all_leads"),
    path("leads/mine/", views.my_leads, name="my_leads"),
    path("leads/follow-ups/", views.followups, name="followups"),
    path("reports/", views.reports, name="reports"),
    path("settings/", views.settings_page, name="settings_page"),

    # Admin — staff management
    path("staff/", views.staff_list, name="staff_list"),
    path("staff/add/", views.staff_add, name="staff_add"),
    path("staff/<int:user_id>/edit/", views.staff_edit, name="staff_edit"),
    path("staff/<int:user_id>/toggle-status/", views.staff_toggle_status, name="staff_toggle_status"),
]
