"""Checks the live settings that a payroll launch depends on, and proves they work.

    python manage.py launch_check                      # settings, database, file storage
    python manage.py launch_check --email you@x.com    # also sends one real test email

Safe to run on production: the storage test writes one tiny file and deletes it again, and nothing in the payroll data is touched."""
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = 'Check database, file storage and (optionally) email before going live.'

    def add_arguments(self, parser):
        parser.add_argument('--email', help='Send a real test email to this address.')

    def line(self, ok, text):
        self.stdout.write(('  [OK]   ' if ok else '  [FAIL] ') + text)
        return ok

    def handle(self, *args, **options):
        results = []
        self.stdout.write('Launch check')

        results.append(self.line(not settings.DEBUG, 'DEBUG is off' if not settings.DEBUG else 'DEBUG is ON - turn it off in production'))
        results.append(self.line(bool(settings.SECRET_KEY) and 'insecure' not in settings.SECRET_KEY, 'Secret key is set'))
        hosts = [h for h in settings.ALLOWED_HOSTS if h not in ('localhost', '127.0.0.1')]
        results.append(self.line(bool(hosts), 'Allowed hosts: ' + (', '.join(hosts) or 'only localhost - set DJANGO_ALLOWED_HOSTS')))

        try:
            with connection.cursor() as cur:
                cur.execute('SELECT 1')
            results.append(self.line(True, f'Database reachable ({connection.vendor})'))
        except Exception as exc:
            results.append(self.line(False, f'Database error: {exc}'))

        if not settings.STORAGE_BUCKET:
            results.append(self.line(False, 'File storage is the server disk: uploads are lost on every deploy. Set STORAGE_BUCKET, '
                                            'STORAGE_ACCESS_KEY_ID, STORAGE_SECRET_ACCESS_KEY (and STORAGE_ENDPOINT_URL for R2).'))
        else:
            name = f'launch-check/{uuid.uuid4().hex}.txt'
            try:
                saved = default_storage.save(name, ContentFile(b'launch check'))
                ok = default_storage.exists(saved) and default_storage.open(saved).read() == b'launch check'
                default_storage.delete(saved)
                results.append(self.line(ok, f'File storage bucket "{settings.STORAGE_BUCKET}": wrote, read and deleted a test file'))
            except Exception as exc:
                results.append(self.line(False, f'File storage error: {exc}'))

        smtp = settings.EMAIL_BACKEND.endswith('smtp.EmailBackend')
        if smtp and not settings.EMAIL_HOST:
            results.append(self.line(False, 'Email: EMAIL_HOST is empty, so no email can be sent'))
        else:
            results.append(self.line(True, f'Email backend: {settings.EMAIL_BACKEND.rsplit(".", 1)[-1]}'
                                           + (f' via {settings.EMAIL_HOST}:{settings.EMAIL_PORT}' if smtp else '')))
        if options.get('email'):
            try:
                sent = send_mail('Payroll launch check', 'If you can read this, email works.', settings.DEFAULT_FROM_EMAIL,
                                 [options['email']], fail_silently=False)
                results.append(self.line(bool(sent), f'Test email sent to {options["email"]} - check the inbox (and spam)'))
            except Exception as exc:
                results.append(self.line(False, f'Test email failed: {exc}'))
        else:
            self.stdout.write('  [skip] Test email not sent. Run again with --email you@example.com')

        self.stdout.write('')
        failed = results.count(False)
        self.stdout.write('All checks passed.' if not failed else f'{failed} problem(s) to fix before launch.')
