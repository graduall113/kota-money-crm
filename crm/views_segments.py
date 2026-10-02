"""
Segments: plain named, manually-curated contact groups. No rule/condition
engine — membership only ever changes because a person (Admin) added or
removed a contact.

Visibility: every active user can view the segment list and a segment's
detail page, but only ever sees the contacts within it they're already
authorized to see (access.visible_contacts). Creating, renaming, deleting a
segment, and changing who's in it (including bulk "Add to Segment" and
segment-wise assignment) is Admin-only — access.can_manage_segments().
"""
from django.contrib import messages
from django.db import transaction
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render

from . import access, filters, services
from .decorators import active_account_required, admin_required
from .forms import SegmentForm
from .models import Contact, ContactSegment, Segment


@active_account_required
def segment_list(request):
    admin = access.can_manage_segments(request.user)
    if admin:
        segments = list(Segment.objects.annotate(n=Count("contact_links", distinct=True))
                        .select_related("created_by").order_by("name"))
    else:
        # Staff see every segment (they're just named folders), but the count
        # shown is scoped to contacts that staff member is authorized to see.
        segments = list(Segment.objects.order_by("name"))
        visible_ids = set(access.visible_contacts(request.user).values_list("pk", flat=True))
        for seg in segments:
            seg.n = ContactSegment.objects.filter(segment=seg, contact_id__in=visible_ids).count()
    return render(request, "segments/segment_list.html", {
        "segments": segments, "can_manage": admin, "active_page": "segments",
    })


@admin_required
def segment_create(request):
    if request.method == "POST":
        form = SegmentForm(request.POST)
        if form.is_valid():
            segment = form.save(commit=False)
            segment.created_by = request.user
            segment.save()
            services.log_audit(request.user, "segment_created", f"Segment '{segment.name}' created", {}, "segment", segment.pk)
            messages.success(request, f"Segment '{segment.name}' created.")
            return redirect("segment_detail", segment_id=segment.pk)
    else:
        form = SegmentForm()
    return render(request, "segments/segment_form.html", {"form": form, "mode": "create", "active_page": "segments"})


@admin_required
def segment_edit(request, segment_id):
    segment = get_object_or_404(Segment, pk=segment_id)
    if request.method == "POST":
        form = SegmentForm(request.POST, instance=segment)
        if form.is_valid():
            form.save()
            services.log_audit(request.user, "segment_edited", f"Segment '{segment.name}' updated", {}, "segment", segment.pk)
            messages.success(request, "Segment updated.")
            return redirect("segment_detail", segment_id=segment.pk)
    else:
        form = SegmentForm(instance=segment)
    return render(request, "segments/segment_form.html", {"form": form, "mode": "edit", "segment": segment, "active_page": "segments"})


@admin_required
def segment_delete(request, segment_id):
    segment = get_object_or_404(Segment, pk=segment_id)
    count = segment.contact_links.count()
    if request.method == "POST":
        name, seg_pk = segment.name, segment.pk
        with transaction.atomic():
            segment.delete()  # CASCADE only removes ContactSegment rows — contacts themselves are untouched
            services.log_audit(request.user, "segment_deleted", f"Segment '{name}' permanently deleted ({count} membership(s) removed)",
                               {"segment": name, "segment_id": seg_pk, "members": count}, "segment", seg_pk)
        messages.success(request, f"Segment '{name}' was deleted. Its {count} contact(s) were not affected.")
        return redirect("segment_list")
    return render(request, "segments/segment_confirm_delete.html", {"segment": segment, "count": count, "active_page": "segments"})


@active_account_required
def segment_detail(request, segment_id):
    segment = get_object_or_404(Segment, pk=segment_id)
    admin = access.can_manage_segments(request.user)
    base = access.visible_contacts(request.user).filter(segments=segment)
    qs = filters.filter_contacts(base.select_related("reference_by", "current_assigned_to", "import_batch"), request.GET)
    page = filters.paginate(request, qs, per_page=25)
    total_all = segment.contact_links.count()  # true segment size, admin context
    page_query = filters.querystring_without(request, "page")
    # The bulk-action bar's "select all N matching records" resolves this same
    # query again from scratch server-side, so it must carry `segment=` too —
    # this page's own filters never include it (it's the URL path, not a GET
    # param), and without it a "select all" here would wrongly reach every
    # segment's contacts, not just this one.
    bulk_query = f"segment={segment.pk}" + (f"&{page_query}" if page_query else "")
    return render(request, "segments/segment_detail.html", {
        "segment": segment, "page": page, "contacts": page.object_list,
        "total_count": page.paginator.count, "total_all": total_all,
        "can_manage": admin, "staff_members": services.active_staff() if admin else [],
        "bulk_staff": services.active_staff(),
        "all_segments": Segment.objects.filter(is_active=True).order_by("name"),
        "status_choices": Contact.STATUS_CHOICES,
        "querystring": page_query, "bulk_query": bulk_query,
        "is_admin_view": admin, "active_page": "segments", "GET": request.GET,
        "has_filters": any(k for k in request.GET if k not in ("page", "per_page", "sort")),
    })


@admin_required
def segment_remove_contact(request, segment_id, contact_id):
    segment = get_object_or_404(Segment, pk=segment_id)
    contact = get_object_or_404(Contact, pk=contact_id)
    if request.method == "POST":
        services.remove_contact_from_segment(segment, contact, request.user)
        messages.success(request, f"{contact.name} removed from '{segment.name}'.")
    return redirect("segment_detail", segment_id=segment.pk)


@admin_required
def segment_add_contact_form(request, contact_id):
    """Contact-detail 'Add to Segment' — pick an existing segment, or create one inline."""
    contact = get_object_or_404(Contact, pk=contact_id)
    if request.method == "POST":
        new_name = request.POST.get("new_segment", "").strip()
        if new_name:
            try:
                segment = services.get_or_create_segment(new_name, request.user)
            except services.AssignmentError as exc:
                messages.error(request, str(exc))
                return redirect("contact_detail", contact_id=contact.pk)
        else:
            segment = get_object_or_404(Segment, pk=request.POST.get("segment_id") or 0)
        services.add_contacts_to_segment(segment, [contact.pk], request.user)
        messages.success(request, f"Added to '{segment.name}'.")
    return redirect("contact_detail", contact_id=contact.pk)
