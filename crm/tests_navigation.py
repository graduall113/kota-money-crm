"""
Sidebar / navigation restructure.

Two things are verified:

1. COSMETIC  - admins see the grouped, collapsible Staff + Calling navigation; staff see
               exactly one "Calling" link and none of the admin-only items.
2. SECURITY  - hiding a link is not protection. Every admin-only URL must answer a
               normal staff member with 403 (GET *and* POST) when typed in by hand, and
               send an anonymous visitor to the login page.

The clock is frozen at 27/09/2026 12:00 IST (same convention as the other attendance suites);
the staff user has an open attendance row so the attendance gate lets them through - that way a
403 can only come from the permission check, not from the "Start your day" redirect.
"""
import re

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from .models import Attendance, Setting
from .settings_store import get_bool
from .tests_attendance import Clock, at

User = get_user_model()
TODAY = at(12).date()

# (url name, kwargs) - every one of these is admin-only on the server.
# Object ids are fake on purpose: the permission check must fire before any lookup.
ADMIN_ONLY_GET = [
    # Staff group
    ("staff_list", {}), ("staff_add", {}), ("staff_detail", {"user_id": 1}), ("staff_edit", {"user_id": 1}),
    ("staff_remove", {"user_id": 1}), ("staff_delete", {"user_id": 1}),
    ("staff_attendance", {}), ("staff_attendance_export", {}), ("staff_attendance_detail", {"pk": 1}),
    ("attendance_events", {}), ("attendance_devices", {}), ("attendance_device_new", {}),
    ("holiday_list", {}), ("holiday_create", {}), ("holiday_edit", {"pk": 1}), ("holiday_delete", {"pk": 1}),
    # Calling group (admin part)
    ("calling_devices", {}), ("calling_device_pair", {}), ("calling_privileged_numbers", {}),
    # Everything else that was already admin-only and must stay that way
    ("import_list", {}), ("import_new", {}), ("import_detail", {"batch_id": 1}),
    ("audit_log", {}), ("performance", {}), ("document_analytics", {}), ("document_settings", {}), ("reports", {}),
]
ADMIN_ONLY_POST = [
    ("staff_toggle_status", {"user_id": 1}), ("staff_remove", {"user_id": 1}), ("staff_delete", {"user_id": 1}),
    ("staff_attendance_correct", {"pk": 1}),
    ("attendance_event_review", {"event_id": 1}), ("attendance_override", {}),
    ("attendance_device_new", {}), ("attendance_device_revoke", {"device_id": 1}),
    ("holiday_create", {}), ("holiday_edit", {"pk": 1}), ("holiday_delete", {"pk": 1}), ("holiday_toggle", {"pk": 1}),
    ("calling_device_pair", {}), ("calling_device_toggle", {"device_id": 1}), ("calling_device_delete", {"device_id": 1}),
    ("calling_privileged_number_toggle", {"number_id": 1}),
]
# Links that exist ONLY in the admin sidebar.
ADMIN_LINK_NAMES = [
    "staff_list", "staff_attendance", "attendance_events", "attendance_devices", "holiday_list",
    "calling_devices", "calling_privileged_numbers", "import_list", "performance", "audit_log", "reports",
]


def sidebar_html(response):
    html = response.content.decode()
    return html[html.index('<aside class="sidebar"'):html.index("</aside>")]


class NavBase(TestCase):
    def setUp(self):
        clock = Clock(at(12))
        clock.__enter__()
        self.addCleanup(clock.__exit__, None, None, None)
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.rahul = User.objects.create_user("rahul", password="pw", first_name="Rahul")
        Attendance.objects.create(user=self.rahul, work_date=TODAY, start_time=at(10, 0))
        self.admin_c, self.staff_c = Client(), Client()
        self.admin_c.force_login(self.admin)
        self.staff_c.force_login(self.rahul)


class SidebarVisibilityTests(NavBase):
    def test_admin_sees_staff_group_with_all_five_items(self):
        html = sidebar_html(self.admin_c.get(reverse("dashboard")))
        self.assertIn('data-nav-group="staff"', html)
        for name, label in [("staff_list", "Staff"), ("staff_attendance", "Staff Attendance"),
                            ("attendance_events", "Attendance Review"), ("attendance_devices", "Attendance Devices"),
                            ("holiday_list", "Holidays")]:
            self.assertRegex(html, rf'<a href="{re.escape(reverse(name))}" class="nav-sublink[^"]*">{label}</a>')

    def test_admin_sees_calling_group_with_three_items(self):
        html = sidebar_html(self.admin_c.get(reverse("dashboard")))
        self.assertIn('data-nav-group="calling"', html)
        for name, label in [("calling_dashboard", "Calling"), ("calling_devices", "Calling Devices"),
                            ("calling_privileged_numbers", "Privileged Numbers")]:
            self.assertRegex(html, rf'<a href="{re.escape(reverse(name))}" class="nav-sublink[^"]*">{label}</a>')

    def test_old_flat_admin_links_are_gone(self):
        """The grouped items must not also appear as top-level .nav-link entries."""
        html = sidebar_html(self.admin_c.get(reverse("dashboard")))
        for name in ["staff_list", "staff_attendance", "attendance_events", "attendance_devices", "holiday_list",
                     "calling_devices", "calling_privileged_numbers"]:
            self.assertNotIn(f'<a href="{reverse(name)}" class="nav-link', html, name)

    def test_admin_keeps_the_other_sidebar_items(self):
        html = sidebar_html(self.admin_c.get(reverse("dashboard")))
        for name in ["dashboard", "all_leads", "my_leads", "contacts", "segment_list", "followups",
                     "import_list", "performance", "audit_log", "reports", "settings_page"]:
            self.assertIn(f'href="{reverse(name)}"', html, name)

    def test_staff_sees_exactly_one_calling_link_and_no_groups(self):
        html = sidebar_html(self.staff_c.get(reverse("dashboard")))
        self.assertNotIn("data-nav-group", html)
        self.assertNotIn("<details", html)
        self.assertEqual(html.count(f'href="{reverse("calling_dashboard")}"'), 1)
        self.assertRegex(html, rf'<a href="{re.escape(reverse("calling_dashboard"))}" class="nav-link[^"]*">\s*<svg.*?</svg>\s*Calling\s*</a>')

    def test_staff_does_not_see_any_admin_only_link(self):
        html = sidebar_html(self.staff_c.get(reverse("dashboard")))
        for name in ADMIN_LINK_NAMES:
            self.assertNotIn(f'href="{reverse(name)}"', html, name)
        for label in ("Calling Devices", "Privileged Numbers", "Staff Attendance", "Attendance Review",
                      "Attendance Devices", "Holidays"):
            self.assertNotIn(label, html, label)

    def test_staff_keeps_their_own_items(self):
        html = sidebar_html(self.staff_c.get(reverse("dashboard")))
        for name in ["dashboard", "my_leads", "contacts", "segment_list", "followups", "calling_dashboard", "settings_page"]:
            self.assertIn(f'href="{reverse(name)}"', html, name)

    def test_group_containing_current_page_is_open_others_collapsed(self):
        def groups(resp):
            return {m.group(1): " open" in m.group(0) or m.group(0).rstrip(">").endswith("open")
                    for m in re.finditer(r'<details class="nav-group" data-nav-group="(\w+)"[^>]*>', sidebar_html(resp))}

        self.assertEqual(groups(self.admin_c.get(reverse("dashboard"))), {"calling": False, "staff": False})
        self.assertEqual(groups(self.admin_c.get(reverse("holiday_list"))), {"calling": False, "staff": True})
        self.assertEqual(groups(self.admin_c.get(reverse("attendance_devices"))), {"calling": False, "staff": True})
        self.assertEqual(groups(self.admin_c.get(reverse("staff_list"))), {"calling": False, "staff": True})
        self.assertEqual(groups(self.admin_c.get(reverse("calling_privileged_numbers"))), {"calling": True, "staff": False})
        self.assertEqual(groups(self.admin_c.get(reverse("calling_dashboard"))), {"calling": True, "staff": False})

    def test_active_sublink_is_highlighted(self):
        html = sidebar_html(self.admin_c.get(reverse("holiday_list")))
        self.assertRegex(html, rf'<a href="{re.escape(reverse("holiday_list"))}" class="nav-sublink is-active">')
        self.assertNotRegex(html, rf'<a href="{re.escape(reverse("staff_list"))}" class="nav-sublink is-active">')


class DirectUrlAccessTests(NavBase):
    """A hidden link is not a lock: staff typing the URL must get a real 403."""

    def test_staff_gets_403_on_every_admin_only_url_get(self):
        for name, kw in ADMIN_ONLY_GET:
            resp = self.staff_c.get(reverse(name, kwargs=kw))
            self.assertEqual(resp.status_code, 403, f"GET {name} -> {resp.status_code}")

    def test_staff_gets_403_on_every_admin_only_url_post(self):
        for name, kw in ADMIN_ONLY_POST:
            resp = self.staff_c.post(reverse(name, kwargs=kw), {})
            self.assertEqual(resp.status_code, 403, f"POST {name} -> {resp.status_code}")

    def test_anonymous_is_sent_to_login_for_every_admin_only_url(self):
        anon = Client()
        for name, kw in ADMIN_ONLY_GET:
            resp = anon.get(reverse(name, kwargs=kw))
            self.assertEqual(resp.status_code, 302, f"{name} -> {resp.status_code}")
            self.assertIn(reverse("login"), resp["Location"], name)

    def test_staff_can_still_open_their_calling_dashboard(self):
        resp = self.staff_c.get(reverse("calling_dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["is_admin_view"])

    def test_admin_can_open_every_listing_page(self):
        for name in ["staff_list", "staff_attendance", "attendance_events", "attendance_devices", "holiday_list",
                     "calling_dashboard", "calling_devices", "calling_privileged_numbers"]:
            self.assertEqual(self.admin_c.get(reverse(name)).status_code, 200, name)

    def test_urls_unchanged(self):
        """Restructuring is navigation only - no URL moved."""
        expected = {
            "staff_list": "/staff/", "staff_attendance": "/staff-attendance/", "attendance_events": "/attendance/events/",
            "attendance_devices": "/attendance/devices/", "holiday_list": "/holidays/",
            "calling_dashboard": "/calling/", "calling_devices": "/calling/devices/",
            "calling_privileged_numbers": "/calling/privileged-numbers/", "settings_page": "/settings/",
            "document_settings": "/settings/documents/",
        }
        for name, path in expected.items():
            self.assertEqual(reverse(name), path)


class SettingsSectionTests(NavBase):
    EXPECTED = ["Account", "Leads & Assignment", "Import", "n8n / Automation", "Document Checklist",
                "Attendance", "Holidays", "Security", "Data Management"]

    def test_admin_sections_in_requested_order(self):
        resp = self.admin_c.get(reverse("settings_page"))
        self.assertEqual([label for _, label in resp.context["sections"]], self.EXPECTED)

    def test_document_checklist_is_not_listed_twice(self):
        html = self.admin_c.get(reverse("settings_page")).content.decode()
        nav = html[html.index('<nav class="settings-nav">'):html.index("</nav>", html.index('<nav class="settings-nav">'))]
        self.assertEqual(nav.count("Document Checklist"), 1)

    def test_every_section_renders_for_admin(self):
        for key, label in self.admin_c.get(reverse("settings_page")).context["sections"]:
            resp = self.admin_c.get(reverse("settings_page"), {"section": key})
            self.assertEqual(resp.status_code, 200, key)
            self.assertEqual(resp.context["section"], key)
            self.assertContains(resp, f'<a href="?section={key}" class="is-active">')

    def test_holidays_section_points_at_the_existing_screen(self):
        resp = self.admin_c.get(reverse("settings_page"), {"section": "holidays"})
        self.assertContains(resp, f'href="{reverse("holiday_list")}"')

    def test_unknown_section_falls_back_to_account(self):
        resp = self.admin_c.get(reverse("settings_page"), {"section": "does-not-exist"})
        self.assertEqual(resp.context["section"], "account")

    def test_staff_only_get_account_section(self):
        for key in ["security", "attendance", "data", "holidays", "leads"]:
            resp = self.staff_c.get(reverse("settings_page"), {"section": key})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.context["section"], "account", key)
            self.assertEqual([k for k, _ in resp.context["sections"]], ["account"])

    def test_staff_cannot_change_settings_by_post(self):
        before = get_bool("allow_public_registration")
        self.staff_c.post(reverse("settings_page"), {"section": "security", "allow_public_registration": "on" if not before else ""})
        self.assertEqual(get_bool("allow_public_registration"), before)

    def test_admin_can_still_save_a_section(self):
        self.admin_c.post(reverse("settings_page"), {"section": "security"})   # checkbox absent -> off
        self.assertFalse(get_bool("allow_public_registration"))
        self.admin_c.post(reverse("settings_page"), {"section": "security", "allow_public_registration": "on"})
        self.assertTrue(get_bool("allow_public_registration"))
