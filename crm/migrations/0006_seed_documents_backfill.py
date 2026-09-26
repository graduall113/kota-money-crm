from django.db import migrations

DEFAULT_DOCUMENTS = [
    "Aadhaar Card",
    "PAN Card",
    "Photograph",
    "Salary Slip",
    "Bank Statement",
    "ITR",
    "Address Proof",
]


def seed(apps, schema_editor):
    DocumentType = apps.get_model("crm", "DocumentType")
    Lead = apps.get_model("crm", "Lead")
    if not DocumentType.objects.exists():
        for i, name in enumerate(DEFAULT_DOCUMENTS, start=1):
            DocumentType.objects.create(name=name, sort_order=i * 10)
    import re
    batch = []
    for lead in Lead.objects.all().only("id", "contact_number").iterator(chunk_size=2000):
        digits = re.sub(r"\D", "", lead.contact_number or "")
        if digits.startswith("00"):
            digits = digits[2:]
        lead.phone_normalized = digits[-10:] if len(digits) > 10 else digits
        batch.append(lead)
        if len(batch) >= 1000:
            Lead.objects.bulk_update(batch, ["phone_normalized"])
            batch = []
    if batch:
        Lead.objects.bulk_update(batch, ["phone_normalized"])
    # Preserve the first-known assignee for leads that were already assigned.
    Lead.objects.filter(original_assigned_to__isnull=True, assigned_to__isnull=False).update(
        original_assigned_to=models_F("assigned_to")
    )


def models_F(name):
    from django.db.models import F
    return F(name)


class Migration(migrations.Migration):
    dependencies = [("crm", "0005_upgrade_contacts_imports_documents")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
