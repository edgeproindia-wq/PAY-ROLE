from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta

from payroll_app.models import AuditLog


class Command(BaseCommand):
    help = 'Delete AuditLog entries older than N days (default 365). Run periodically (e.g. a monthly cron/Render Cron Job).'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=365, help='Delete entries older than this many days.')
        parser.add_argument('--dry-run', action='store_true', help='Show how many would be deleted without deleting.')

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=options['days'])
        qs = AuditLog.objects.filter(timestamp__lt=cutoff)
        count = qs.count()
        if options['dry_run']:
            self.stdout.write(f'Would delete {count} audit log entries older than {cutoff.date()}.')
            return
        qs.delete()
        self.stdout.write(self.style.SUCCESS(f'Deleted {count} audit log entries older than {cutoff.date()}.'))
