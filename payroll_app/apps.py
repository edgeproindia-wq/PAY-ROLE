from django.apps import AppConfig


class PayrollAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'payroll_app'
    verbose_name = 'Payroll'

    def ready(self):
        # Register signal handlers (notifications / audit log helpers)
        from . import signals  # noqa: F401
