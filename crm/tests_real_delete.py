"""
Real database deletion: single delete, bulk delete, Undo Import, Contact -> Lead safety, audit,
permissions, POST/CSRF, rollback. Everything runs in Django's isolated TestCase database; no
production data is ever touched.
"""
import datetime
import os
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from io import StringIO

from . import access, importer, services
from .models import (
    Activity, AssignmentHistory, Attendance, AuditLog, CallDevice, CallRecord, Contact, ContactSegment,
    Holiday, ImportBatch, ImportReviewRow, Lead, Segment, StaffProfile,
)

User = get_user_model()


def mk_batch(code, user=None, status=ImportBatch.STATUS_COMPLETED, **kw):
    return ImportBatch.objects.create(code=code, file_name=f"{code}.csv", uploaded_by=user, status=status, **kw)


def mk_contacts(n, batch=None, prefix="C", start=0, **kw):
    objs = [Contact(name=f"{prefix}{i}", phone=f"9{(start + i):09d}", import_batch=batch, **kw) for i in range(n)]
    Contact.objects.bulk_create(objs, batch_size=1000)
    return list(Contact.objects.filter(name__startswith=prefix, import_batch=batch).order_by("pk"))[:n]


@override_settings(ATTENDANCE_ENFORCED=False)
class RealDeleteBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True)
        self.staff = User.objects.create_user("rahul", password="pw", first_name="Rahul")
        self.other = User.objects.create_user("amit", password="pw")
        self.c = Client()
        self.c.force_login(self.admin)
        self.sc = Client()
        self.sc.force_login(self.staff)

    def mk_lead(self, contact=None, **kw):
        return Lead.objects.create(customer_name="Priya", contact_number="9876543210", contact=contact, **kw)

    def audit(self, action):
        return AuditLog.objects.filter(action=action)


# ------------------------------------------------------------------ single deletes
class SingleDeleteTests(RealDeleteBase):
    def test_contact_delete_removes_row_and_keeps_audit(self):
        ct = Contact.objects.create(name="A", phone="9000000001")
        pk = ct.pk
        self.assertTrue(Contact.objects.filter(pk=pk).exists())
        r = self.c.post(reverse("contact_delete", args=[pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Contact.objects.filter(pk=pk).exists())          # physically gone
        log = self.audit("contact_deleted").get()
        self.assertEqual(log.target_id, str(pk))                           # audit survives the row

    def test_contact_delete_keeps_converted_lead_and_detaches_it(self):
        ct = Contact.objects.create(name="A", phone="9000000001")
        lead = self.mk_lead(contact=ct, customer_name="Keep Me")
        self.c.post(reverse("contact_delete", args=[ct.pk]))
        self.assertFalse(Contact.objects.filter(pk=ct.pk).exists())
        lead.refresh_from_db()                                             # Lead NOT deleted
        self.assertIsNone(lead.contact_id)
        self.assertEqual(lead.customer_name, "Keep Me")
        self.assertEqual(self.audit("contact_deleted").get().details["leads_kept_detached"], [lead.pk])

    def test_contact_delete_removes_own_history_and_segment_links_only(self):
        ct = Contact.objects.create(name="A", phone="9000000001")
        seg = Segment.objects.create(name="VIP")
        ContactSegment.objects.create(contact=ct, segment=seg)
        Activity.objects.create(contact=ct, kind="note", message="x")
        self.c.post(reverse("contact_delete", args=[ct.pk]))
        self.assertFalse(ContactSegment.objects.filter(segment=seg).exists())
        self.assertFalse(Activity.objects.filter(message="x").exists())
        self.assertTrue(Segment.objects.filter(pk=seg.pk).exists())        # the segment itself stays

    def test_lead_delete_removes_row(self):
        lead = self.mk_lead()
        pk = lead.pk
        r = self.c.post(reverse("lead_delete", args=[pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Lead.objects.filter(pk=pk).exists())
        self.assertEqual(self.audit("lead_deleted").get().target_id, str(pk))

    def test_lead_delete_does_not_delete_its_contact(self):
        ct = Contact.objects.create(name="A", phone="9000000001")
        lead = self.mk_lead(contact=ct)
        self.c.post(reverse("lead_delete", args=[lead.pk]))
        self.assertTrue(Contact.objects.filter(pk=ct.pk).exists())

    def test_segment_delete_keeps_contacts(self):
        ct = Contact.objects.create(name="A", phone="9000000001")
        seg = Segment.objects.create(name="VIP")
        ContactSegment.objects.create(contact=ct, segment=seg)
        self.c.post(reverse("segment_delete", args=[seg.pk]))
        self.assertFalse(Segment.objects.filter(pk=seg.pk).exists())
        self.assertFalse(ContactSegment.objects.filter(contact=ct).exists())
        self.assertTrue(Contact.objects.filter(pk=ct.pk).exists())
        self.assertEqual(self.audit("segment_deleted").get().target_id, str(seg.pk))

    def test_holiday_delete_removes_row(self):
        h = Holiday.objects.create(name="Diwali", start_date=datetime.date(2026, 11, 8), end_date=datetime.date(2026, 11, 8))
        self.c.post(reverse("holiday_delete", args=[h.pk]))
        self.assertFalse(Holiday.objects.filter(pk=h.pk).exists())
        self.assertTrue(self.audit("holiday_deleted").exists())

    def test_calling_device_delete_removes_row_keeps_call_records(self):
        d = CallDevice.objects.create(staff=self.staff, label="phone", created_by=self.admin)
        self.c.post(reverse("calling_device_delete", args=[d.pk]))
        self.assertFalse(CallDevice.objects.filter(pk=d.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.staff.pk).exists())
        self.assertTrue(self.audit("call_device_deleted").exists())

    def test_privileged_numbers_have_no_delete_route(self):
        with self.assertRaises(NoReverseMatch):
            reverse("calling_privileged_number_delete", args=[1])


# ------------------------------------------------------------------ staff
class StaffDeleteTests(RealDeleteBase):
    def deactivate(self, user):
        StaffProfile.objects.filter(user=user).update(status="inactive")

    def test_staff_without_history_is_physically_deleted(self):
        self.deactivate(self.other)
        self.c.post(reverse("staff_delete", args=[self.other.pk]))
        self.assertFalse(User.objects.filter(pk=self.other.pk).exists())
        self.assertTrue(self.audit("staff_deleted").exists())

    def test_staff_with_call_device_is_kept(self):
        self.deactivate(self.other)
        d = CallDevice.objects.create(staff=self.other, label="p")
        self.c.post(reverse("staff_delete", args=[self.other.pk]))
        self.assertTrue(User.objects.filter(pk=self.other.pk).exists())
        self.assertTrue(CallDevice.objects.filter(pk=d.pk).exists())

    def test_staff_with_attendance_is_kept(self):
        self.deactivate(self.other)
        Attendance.objects.create(user=self.other, work_date=datetime.date(2026, 9, 27), start_time=timezone.now(),
                                  start_verification=Attendance.VERIFY_VERIFIED)
        self.c.post(reverse("staff_delete", args=[self.other.pk]))
        self.assertTrue(User.objects.filter(pk=self.other.pk).exists())
        self.assertEqual(self.other.attendance_records.count(), 1)

    def test_staff_with_assigned_records_is_not_deleted_and_records_survive(self):
        self.deactivate(self.other)
        ct = Contact.objects.create(name="A", phone="9000000001", current_assigned_to=self.other)
        self.c.post(reverse("staff_delete", args=[self.other.pk]))
        self.assertTrue(User.objects.filter(pk=self.other.pk).exists())
        self.assertTrue(Contact.objects.filter(pk=ct.pk).exists())

    def test_reassign_delete_path_also_refuses_when_call_history_exists(self):
        self.deactivate(self.other)
        CallDevice.objects.create(staff=self.other, label="p")
        r = self.c.post(reverse("staff_remove", args=[self.other.pk]), {"choice": "reassign_delete", "reassign_to": self.staff.pk})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(User.objects.filter(pk=self.other.pk).exists())
        self.assertTrue(CallDevice.objects.filter(staff=self.other).exists())

    def test_deleting_staff_never_deletes_leads_or_contacts(self):
        # Even if every guard were bypassed, the FKs are SET_NULL: records survive the user.
        ct = Contact.objects.create(name="A", phone="9000000001", current_assigned_to=self.other, reference_by=self.other)
        lead = self.mk_lead(assigned_to=self.other)
        User.objects.filter(pk=self.other.pk).delete()
        ct.refresh_from_db(); lead.refresh_from_db()
        self.assertIsNone(ct.current_assigned_to_id)
        self.assertIsNone(lead.assigned_to_id)


# ------------------------------------------------------------------ bulk delete
class BulkDeleteTests(RealDeleteBase):
    def bulk(self, model, ids, client=None, confirmed=True, extra=None):
        data = {"action": "delete", "scope_type": "ids", "ids": [str(i) for i in ids]}
        if confirmed:
            data["confirmed"] = "1"
        data.update(extra or {})
        return (client or self.c).post(reverse("bulk_action", args=[model]), data)

    def test_confirmation_step_deletes_nothing_and_shows_count_and_lead_warning(self):
        cts = mk_contacts(3)
        self.mk_lead(contact=cts[0])
        r = self.bulk("contact", [c.pk for c in cts], confirmed=False)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "permanently")
        self.assertContains(r, "Delete Permanently")
        self.assertEqual(r.context["leads_linked"], 1)
        self.assertEqual(Contact.objects.count(), 3)

    def test_bulk_delete_contacts_physically_removes_only_selected(self):
        cts = mk_contacts(5)
        sel, keep = cts[:3], cts[3:]
        self.bulk("contact", [c.pk for c in sel])
        self.assertFalse(Contact.objects.filter(pk__in=[c.pk for c in sel]).exists())
        self.assertEqual(Contact.objects.filter(pk__in=[c.pk for c in keep]).count(), 2)

    def test_bulk_delete_contacts_keeps_leads(self):
        cts = mk_contacts(2)
        lead = self.mk_lead(contact=cts[0])
        self.bulk("contact", [c.pk for c in cts])
        self.assertTrue(Lead.objects.filter(pk=lead.pk).exists())
        self.assertIsNone(Lead.objects.get(pk=lead.pk).contact_id)

    def test_bulk_delete_leads_physically_removes_them(self):
        leads = [self.mk_lead() for _ in range(4)]
        keep = self.mk_lead()
        self.bulk("lead", [l.pk for l in leads])
        self.assertFalse(Lead.objects.filter(pk__in=[l.pk for l in leads]).exists())
        self.assertTrue(Lead.objects.filter(pk=keep.pk).exists())

    def test_bulk_delete_audit_has_counts_and_ids(self):
        cts = mk_contacts(3)
        self.bulk("contact", [c.pk for c in cts])
        log = self.audit("bulk_delete").get()
        self.assertEqual(log.actor_id, self.admin.pk)
        self.assertEqual(log.details["selected"], 3)
        self.assertEqual(log.details["deleted"], 3)
        self.assertEqual(log.details["protected"], 0)
        self.assertEqual(sorted(log.details["deleted_ids"]), sorted(c.pk for c in cts))

    def test_bulk_delete_is_admin_only(self):
        cts = mk_contacts(2, reference_by=self.staff, current_assigned_to=self.staff)
        r = self.bulk("contact", [c.pk for c in cts], client=self.sc)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(Contact.objects.count(), 2)

    def test_bulk_delete_get_is_not_allowed(self):
        r = self.c.get(reverse("bulk_action", args=["contact"]))
        self.assertEqual(r.status_code, 404)

    def test_protected_records_are_skipped_named_and_not_counted_deleted(self):
        cts = mk_contacts(4)
        blocked = cts[1].pk
        real = services._delete_chunk

        def fake(model, chunk):
            if blocked in chunk:
                ok, prot = real(model, [p for p in chunk if p != blocked])
                prot[blocked] = "Referenced by 1 protected record(s)"
                return ok, prot
            return real(model, chunk)

        with mock.patch.object(services, "_delete_chunk", side_effect=fake):
            res = services.bulk_delete(Contact, [c.pk for c in cts], self.admin)
        self.assertEqual(res["deleted"], 3)
        self.assertEqual(list(res["protected"]), [blocked])
        self.assertTrue(Contact.objects.filter(pk=blocked).exists())
        log = self.audit("bulk_delete").get()
        self.assertEqual(log.details["protected_ids"], [blocked])
        self.assertIn("skipped", log.summary)

    def test_failure_rolls_back_everything_and_writes_no_success_audit(self):
        cts = mk_contacts(6)
        real = services._delete_chunk
        calls = {"n": 0}

        def boom(model, chunk):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk on fire")
            return real(model, chunk)

        with mock.patch.object(services, "CHUNK", 2), mock.patch.object(services, "_delete_chunk", side_effect=boom):
            with self.assertRaises(RuntimeError):
                services.bulk_delete(Contact, [c.pk for c in cts], self.admin)
        self.assertEqual(Contact.objects.count(), 6)                       # first chunk rolled back too
        self.assertFalse(self.audit("bulk_delete").exists())


# ------------------------------------------------------------------ import tracking + undo
class ImportTrackingTests(RealDeleteBase):
    def run_import(self, rows, policy=ImportBatch.DUP_UPDATE, code="B-RUN"):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w") as fh:
            fh.write("name,phone\n" + "\n".join(f"{n},{p}" for n, p in rows) + "\n")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        batch = mk_batch(code, self.admin, status=ImportBatch.STATUS_ANALYZED, stored_path=path, file_type="csv",
                         headers=["name", "phone"], mapping={"name": 0, "phone": 1}, duplicate_policy=policy,
                         total_rows=len(rows))
        importer.import_batch(batch.pk)
        batch.refresh_from_db()
        return batch

    def test_only_created_contacts_carry_the_batch(self):
        old = Contact.objects.create(name="Old", phone="9111111111")
        batch = self.run_import([("Old Renamed", "9111111111"), ("Brand New", "9222222222")])
        old.refresh_from_db()
        self.assertEqual(old.name, "Old Renamed")                          # updated by the import...
        self.assertIsNone(old.import_batch_id)                             # ...but NOT owned by it
        self.assertEqual(list(batch.contacts.values_list("name", flat=True)), ["Brand New"])

    def test_undo_after_real_import_keeps_updated_preexisting_contact(self):
        old = Contact.objects.create(name="Old", phone="9111111111")
        batch = self.run_import([("Old Renamed", "9111111111"), ("Brand New", "9222222222")])
        new_pk = batch.contacts.get().pk
        services.undo_import(batch, self.admin)
        self.assertFalse(Contact.objects.filter(pk=new_pk).exists())
        self.assertTrue(Contact.objects.filter(pk=old.pk).exists())

    def test_matched_and_skipped_contact_is_never_deleted(self):
        old = Contact.objects.create(name="Old", phone="9111111111")
        batch = self.run_import([("Dup", "9111111111"), ("Brand New", "9222222222")], policy=ImportBatch.DUP_SKIP)
        services.undo_import(batch, self.admin)
        self.assertTrue(Contact.objects.filter(pk=old.pk).exists())
        self.assertFalse(Contact.objects.filter(name="Brand New").exists())


class UndoImportTests(RealDeleteBase):
    def setUp(self):
        super().setUp()
        self.b24 = mk_batch("B-24", self.admin)
        self.b25 = mk_batch("B-25", self.admin, imported_count=3)
        self.b26 = mk_batch("B-26", self.admin)
        self.c24 = mk_contacts(2, self.b24, prefix="X", start=100)
        self.c25 = mk_contacts(3, self.b25, prefix="Y", start=200)
        self.c26 = mk_contacts(2, self.b26, prefix="Z", start=300)
        self.pre = Contact.objects.create(name="Pre", phone="9444444444")
        self.undo_url = reverse("import_undo", args=[self.b25.pk])

    def ids(self, cts):
        return [c.pk for c in cts]

    def test_undo_physically_deletes_only_this_batchs_contacts(self):
        self.c.post(self.undo_url)
        self.assertFalse(Contact.objects.filter(pk__in=self.ids(self.c25)).exists())   # gone from DB
        self.assertEqual(Contact.objects.filter(pk__in=self.ids(self.c24) + self.ids(self.c26) + [self.pre.pk]).count(), 5)

    def test_deleted_contacts_not_flagged_soft_deleted_just_gone(self):
        self.c.post(self.undo_url)
        self.assertFalse(Contact.objects.filter(is_deleted=True).exists())

    def test_contacts_leave_search_lists_segments_and_assignments(self):
        seg = Segment.objects.create(name="S")
        for c in self.c25:
            ContactSegment.objects.create(contact=c, segment=seg)
        Contact.objects.filter(pk=self.c25[0].pk).update(current_assigned_to=self.staff)
        self.c.post(self.undo_url)
        self.assertFalse(access.visible_contacts(self.admin).filter(name__startswith="Y").exists())
        self.assertFalse(access.visible_contacts(self.staff).filter(name__startswith="Y").exists())
        self.assertFalse(ContactSegment.objects.filter(segment=seg).exists())
        self.assertEqual(Contact.objects.filter(current_assigned_to=self.staff).count(), 0)
        r = self.c.get(reverse("contacts") + "?q=Y0")
        self.assertNotContains(r, ">Y0<")

    def test_import_history_survives_with_outcome(self):
        self.c.post(self.undo_url)
        b = ImportBatch.objects.get(pk=self.b25.pk)                        # batch row still exists
        self.assertIsNotNone(b.undone_at)
        self.assertEqual(b.undone_by_id, self.admin.pk)
        self.assertEqual((b.undo_selected_count, b.undo_deleted_count, b.undo_protected_count), (3, 3, 0))
        self.assertEqual(b.imported_count, 3)                              # original counters untouched

    def test_undo_audit_record(self):
        self.c.post(self.undo_url)
        log = self.audit("import_undone").get()
        self.assertEqual(log.actor_id, self.admin.pk)
        self.assertEqual(log.target_id, str(self.b25.pk))
        d = log.details
        self.assertEqual((d["selected"], d["deleted"], d["protected"], d["converted_leads_kept"]), (3, 3, 0, 0))
        self.assertEqual(d["batch"], "B-25")
        self.assertIn("3 of 3", log.summary)

    def test_converted_lead_survives_undo_with_contact_nulled(self):
        lead = self.mk_lead(contact=self.c25[0], customer_name="Important", import_batch=self.b25)
        self.c.post(self.undo_url)
        self.assertFalse(Contact.objects.filter(pk=self.c25[0].pk).exists())
        lead = Lead.objects.get(pk=lead.pk)                                # still there
        self.assertIsNone(lead.contact_id)
        self.assertEqual(lead.customer_name, "Important")                  # own data intact
        self.assertEqual(lead.import_batch_id, self.b25.pk)                # import_batch preserved
        self.assertEqual(self.audit("import_undone").get().details["converted_leads_kept"], 1)
        self.assertEqual(ImportBatch.objects.get(pk=self.b25.pk).undo_leads_detached, 1)

    def test_lead_of_other_batch_or_manual_lead_untouched(self):
        a = self.mk_lead(contact=self.c24[0])
        b = self.mk_lead()
        self.c.post(self.undo_url)
        self.assertEqual(Lead.objects.count(), 2)
        self.assertEqual(Lead.objects.get(pk=a.pk).contact_id, self.c24[0].pk)

    def test_call_records_survive_with_contact_nulled(self):
        rec = CallRecord.objects.create(staff=self.staff, contact=self.c25[0], **self._call_kwargs())
        self.c.post(self.undo_url)
        self.assertIsNone(CallRecord.objects.get(pk=rec.pk).contact_id)

    def _call_kwargs(self):
        # fill any required CallRecord fields that have no default
        kw = {}
        for f in CallRecord._meta.get_fields():
            if getattr(f, "concrete", False) and not f.null and not f.has_default() and not f.primary_key \
                    and not getattr(f, "auto_now_add", False) and f.name not in ("staff", "contact") and not f.is_relation:
                kw[f.name] = {"CharField": "9876543210", "DateTimeField": timezone.now(), "DateField": datetime.date.today(),
                              "IntegerField": 0, "PositiveIntegerField": 0, "BooleanField": False}.get(f.get_internal_type(), "x")
        return kw

    def test_pending_review_row_of_other_batch_survives_deletion_of_its_contact(self):
        other = mk_batch("B-27", self.admin, status=ImportBatch.STATUS_REVIEW)
        row = ImportReviewRow.objects.create(batch=other, row_number=2, existing_contact=self.c25[0], data={"name": "Dup"})
        self.c.post(self.undo_url)
        row = ImportReviewRow.objects.get(pk=row.pk)                       # NOT cascade-deleted
        self.assertIsNone(row.existing_contact_id)
        self.assertEqual(row.data, {"name": "Dup"})
        self.assertFalse(row.applied)

    def test_apply_review_skips_update_decision_whose_contact_was_deleted(self):
        other = mk_batch("B-27", self.admin, status=ImportBatch.STATUS_REVIEW)
        ImportReviewRow.objects.create(batch=other, row_number=2, existing_contact=None, data={"name": "Dup"},
                                       decision=ImportReviewRow.DECISION_UPDATE)
        importer.apply_review(other.pk, finish=True)
        other.refresh_from_db()
        self.assertEqual(other.skipped_count, 1)
        self.assertEqual(Contact.objects.filter(name="Dup").count(), 0)
        self.assertEqual(other.status, ImportBatch.STATUS_COMPLETED)

    def test_undo_closes_own_pending_review_rows_and_review_status(self):
        b = mk_batch("B-28", self.admin, status=ImportBatch.STATUS_REVIEW)
        mk_contacts(1, b, prefix="Q", start=900)
        row = ImportReviewRow.objects.create(batch=b, row_number=2, existing_contact=self.pre, data={})
        services.undo_import(b, self.admin)
        row.refresh_from_db(); b.refresh_from_db()
        self.assertTrue(row.applied)
        self.assertEqual(b.status, ImportBatch.STATUS_COMPLETED)
        self.assertTrue(Contact.objects.filter(pk=self.pre.pk).exists())

    def test_cannot_undo_twice(self):
        self.c.post(self.undo_url)
        self.c.post(self.undo_url)
        self.assertEqual(self.audit("import_undone").count(), 1)

    def test_busy_batch_cannot_be_undone(self):
        ImportBatch.objects.filter(pk=self.b25.pk).update(status=ImportBatch.STATUS_IMPORTING)
        self.c.post(self.undo_url)
        self.assertEqual(Contact.objects.filter(pk__in=self.ids(self.c25)).count(), 3)

    def test_legacy_soft_deleted_contacts_left_alone(self):
        legacy = Contact.objects.create(name="Legacy", phone="9555555555", import_batch=self.b25,
                                        is_deleted=True, deleted_at=timezone.now())
        self.c.post(self.undo_url)
        self.assertTrue(Contact.objects.filter(pk=legacy.pk, is_deleted=True).exists())

    def test_failure_rolls_back_contacts_batch_and_audit(self):
        real = services._delete_chunk
        calls = {"n": 0}

        def boom(model, chunk):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return real(model, chunk)

        with mock.patch.object(services, "CHUNK", 2), mock.patch.object(services, "_delete_chunk", side_effect=boom):
            r = self.c.post(self.undo_url)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Contact.objects.filter(pk__in=self.ids(self.c25)).count(), 3)   # nothing deleted
        b = ImportBatch.objects.get(pk=self.b25.pk)
        self.assertIsNone(b.undone_at)
        self.assertFalse(self.audit("import_undone").exists())
        self.assertTrue(self.audit("import_undo_failed").exists())

    def test_undo_5000_contacts(self):
        big = mk_batch("B-BIG", self.admin)
        mk_contacts(5000, big, prefix="BIG", start=10_000)
        keep = Contact.objects.create(name="Keep", phone="9666666666")
        lead = self.mk_lead(contact=Contact.objects.filter(import_batch=big).first())
        res = services.undo_import(big, self.admin)
        self.assertEqual((res["selected"], res["deleted"]), (5000, 5000))
        self.assertFalse(Contact.objects.filter(import_batch=big).exists())
        self.assertTrue(Contact.objects.filter(pk=keep.pk).exists())
        self.assertTrue(Lead.objects.filter(pk=lead.pk).exists())
        self.assertEqual(Contact.objects.filter(pk__in=self.ids(self.c24)).count(), 2)

    def test_referential_integrity_after_undo(self):
        self.mk_lead(contact=self.c25[0]); self.mk_lead(contact=self.c24[0])
        Activity.objects.create(contact=self.c25[1], kind="note", message="n")
        self.c.post(self.undo_url)
        existing = set(Contact.objects.values_list("pk", flat=True))
        self.assertTrue(set(Lead.objects.exclude(contact=None).values_list("contact_id", flat=True)) <= existing)
        self.assertTrue(set(Activity.objects.exclude(contact=None).values_list("contact_id", flat=True)) <= existing)
        self.assertTrue(set(AssignmentHistory.objects.exclude(contact=None).values_list("contact_id", flat=True)) <= existing)
        self.assertTrue(set(ContactSegment.objects.values_list("contact_id", flat=True)) <= existing)

    def test_restore_route_is_gone(self):
        with self.assertRaises(NoReverseMatch):
            reverse("import_restore", args=[self.b25.pk])

    def test_import_detail_page_renders_before_and_after_undo(self):
        self.mk_lead(contact=self.c25[0])
        r = self.c.get(reverse("import_detail", args=[self.b25.pk]))
        self.assertContains(r, "permanently delete 3 contacts")
        self.c.post(self.undo_url)
        r = self.c.get(reverse("import_detail", args=[self.b25.pk]))
        self.assertContains(r, "permanently deleted")
        self.assertNotContains(r, "Restore")


# ------------------------------------------------------------------ security
class DestructiveSecurityTests(RealDeleteBase):
    def setUp(self):
        super().setUp()
        self.batch = mk_batch("B-1", self.admin)
        self.contact = mk_contacts(1, self.batch)[0]
        self.lead = self.mk_lead()
        self.seg = Segment.objects.create(name="S")
        self.hol = Holiday.objects.create(name="H", start_date=datetime.date(2026, 11, 8), end_date=datetime.date(2026, 11, 8))
        self.dev = CallDevice.objects.create(staff=self.staff, label="p")

    def test_get_never_deletes(self):
        for name, obj in [("contact_delete", self.contact), ("lead_delete", self.lead), ("segment_delete", self.seg),
                          ("holiday_delete", self.hol), ("import_undo", self.batch), ("calling_device_delete", self.dev)]:
            self.c.get(reverse(name, args=[obj.pk]))
        self.assertTrue(Contact.objects.filter(pk=self.contact.pk).exists())
        self.assertTrue(Lead.objects.filter(pk=self.lead.pk).exists())
        self.assertTrue(Segment.objects.filter(pk=self.seg.pk).exists())
        self.assertTrue(Holiday.objects.filter(pk=self.hol.pk).exists())
        self.assertTrue(CallDevice.objects.filter(pk=self.dev.pk).exists())
        self.assertIsNone(ImportBatch.objects.get(pk=self.batch.pk).undone_at)

    def test_post_without_csrf_token_is_rejected(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.admin)
        for name, obj in [("contact_delete", self.contact), ("lead_delete", self.lead), ("segment_delete", self.seg),
                          ("holiday_delete", self.hol), ("import_undo", self.batch), ("calling_device_delete", self.dev)]:
            self.assertEqual(strict.post(reverse(name, args=[obj.pk])).status_code, 403, name)
        strict2 = Client(enforce_csrf_checks=True)
        strict2.force_login(self.admin)
        r = strict2.post(reverse("bulk_action", args=["contact"]),
                         {"action": "delete", "scope_type": "ids", "ids": [str(self.contact.pk)], "confirmed": "1"})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(Contact.objects.filter(pk=self.contact.pk).exists())
        self.assertTrue(Lead.objects.filter(pk=self.lead.pk).exists())
        self.assertIsNone(ImportBatch.objects.get(pk=self.batch.pk).undone_at)

    def test_staff_cannot_run_admin_only_destructive_actions(self):
        self.assertEqual(self.sc.post(reverse("import_undo", args=[self.batch.pk])).status_code, 403)
        self.assertEqual(self.sc.post(reverse("segment_delete", args=[self.seg.pk])).status_code, 403)
        self.assertEqual(self.sc.post(reverse("holiday_delete", args=[self.hol.pk])).status_code, 403)
        self.assertEqual(self.sc.post(reverse("calling_device_delete", args=[self.dev.pk])).status_code, 403)
        self.assertTrue(Contact.objects.filter(pk=self.contact.pk).exists())
        self.assertTrue(Segment.objects.filter(pk=self.seg.pk).exists())
        self.assertIsNone(ImportBatch.objects.get(pk=self.batch.pk).undone_at)

    def test_staff_cannot_delete_a_contact_or_lead_they_do_not_own(self):
        r = self.sc.post(reverse("contact_delete", args=[self.contact.pk]))
        self.assertEqual(r.status_code, 404)                               # not visible to them at all
        r = self.sc.post(reverse("lead_delete", args=[self.lead.pk]))
        self.assertEqual(r.status_code, 404)
        self.assertTrue(Contact.objects.filter(pk=self.contact.pk).exists())
        self.assertTrue(Lead.objects.filter(pk=self.lead.pk).exists())

    def test_staff_can_delete_own_referred_contact_and_audit_records_them(self):
        mine = Contact.objects.create(name="Mine", phone="9777777777", reference_by=self.staff, current_assigned_to=self.staff)
        self.sc.post(reverse("contact_delete", args=[mine.pk]))
        self.assertFalse(Contact.objects.filter(pk=mine.pk).exists())
        self.assertEqual(self.audit("contact_deleted").get().actor_id, self.staff.pk)


# ------------------------------------------------------------------ legacy purge command
class PurgeCommandTests(RealDeleteBase):
    def setUp(self):
        super().setUp()
        self.legacy = Contact.objects.create(name="Old", phone="9888888888", is_deleted=True, deleted_at=timezone.now())
        self.live = Contact.objects.create(name="Live", phone="9999999999")

    def test_default_is_dry_run(self):
        out = StringIO()
        call_command("purge_soft_deleted_contacts", stdout=out)
        self.assertIn("DRY RUN", out.getvalue())
        self.assertTrue(Contact.objects.filter(pk=self.legacy.pk).exists())

    def test_confirm_deletes_only_legacy_rows(self):
        lead = self.mk_lead(contact=self.legacy)
        call_command("purge_soft_deleted_contacts", "--confirm", stdout=StringIO())
        self.assertFalse(Contact.objects.filter(pk=self.legacy.pk).exists())
        self.assertTrue(Contact.objects.filter(pk=self.live.pk).exists())
        self.assertTrue(Lead.objects.filter(pk=lead.pk).exists())
        self.assertTrue(self.audit("purge_soft_deleted").exists())

    def test_legacy_rows_stay_hidden_until_purged(self):
        self.assertFalse(access.visible_contacts(self.admin).filter(pk=self.legacy.pk).exists())
