from django.apps import AppConfig


class CrmConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm"

    def ready(self):
        from . import settings_store, signals  # noqa: F401  (registers the post_save signals + per-request settings snapshot)
