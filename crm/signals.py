from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import StaffProfile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_staff_profile(sender, instance, created, **kwargs):
    """
    Every User gets a StaffProfile automatically — including superusers
    created with `createsuperuser`, who default to the admin role so
    there's always a working admin account after first setup.
    """
    if created and not hasattr(instance, "staff_profile"):
        StaffProfile.objects.create(
            user=instance,
            role=StaffProfile.ROLE_ADMIN if instance.is_superuser else StaffProfile.ROLE_STAFF,
        )
