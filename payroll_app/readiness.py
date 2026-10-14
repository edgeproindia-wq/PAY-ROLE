"""Platform admin only: a live checklist of what is still missing before real salaries are processed, plus a test-email button.
It reads the settings the running site is actually using. It never shows a password, key or token."""
import os

from django.conf import settings
from django.contrib import messages
from django.core.mail import send_mail
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import path
from django.utils import timezone

from .models import Company, Employee
from .payroll_engine import TAX_RULES
from .permissions import admin_required

PASS, ACTION, WARN = 'PASS', 'ACTION NEEDED', 'WARNING'


def _fy(today):
    start = today.year if today.month >= 4 else today.year - 1
    return f'{start}-{(start + 1) % 100:02d}'


def run_checks():
    checks = []

    def add(name, status, detail, fix=''):
        checks.append({'name': name, 'status': status, 'detail': detail, 'fix': fix})

    add('Debug mode is off', PASS if not settings.DEBUG else ACTION,
        'Debug is off.' if not settings.DEBUG else 'Debug is ON. Error pages would show internal details.',
        '' if not settings.DEBUG else 'On Render, set DJANGO_DEBUG to false.')
    key = settings.SECRET_KEY or ''
    strong = len(key) >= 50 and not key.startswith('django-insecure')
    add('Secret key is long and random', PASS if strong else ACTION,
        'The key is long and random (its value is never shown here).' if strong else 'The secret key is short or a default one.',
        '' if strong else 'Set DJANGO_SECRET_KEY on Render to a new 60+ character random value.')
    hosts_ok = '*' not in settings.ALLOWED_HOSTS
    add('Only your own addresses are allowed', PASS if hosts_ok else ACTION,
        'ALLOWED_HOSTS does not contain "*".' if hosts_ok else 'ALLOWED_HOSTS allows every address.', '' if hosts_ok else 'Set DJANGO_ALLOWED_HOSTS to your real domain.')
    https = bool(settings.SECURE_SSL_REDIRECT and settings.SESSION_COOKIE_SECURE and settings.CSRF_COOKIE_SECURE)
    add('HTTPS-only cookies and redirect', PASS if https else ACTION,
        'The site redirects to HTTPS and cookies are secure-only.' if https else 'HTTPS redirect or secure cookies are not all on.',
        '' if https else 'Turn on DJANGO_SECURE_SSL_REDIRECT, DJANGO_SESSION_COOKIE_SECURE and DJANGO_CSRF_COOKIE_SECURE.')
    engine = settings.DATABASES['default']['ENGINE']
    pg = 'postgresql' in engine
    add('Database is PostgreSQL', PASS if pg else ACTION, 'PostgreSQL is in use.' if pg else f'Engine is {engine}. SQLite on a host is lost on redeploy.',
        '' if pg else 'Set DATABASE_URL to your Aiven PostgreSQL address.')
    try:
        pending = len(MigrationExecutor(connection).migration_plan(MigrationExecutor(connection).loader.graph.leaf_nodes()))
    except Exception as exc:                                                    # pragma: no cover
        pending = -1
        add('All database updates are applied', WARN, f'Could not check: {type(exc).__name__}', '')
    if pending == 0:
        add('All database updates are applied', PASS, 'No pending migrations.')
    elif pending > 0:
        add('All database updates are applied', ACTION, f'{pending} migration(s) are waiting.', 'Redeploy so build.sh runs "migrate".')
    smtp = settings.EMAIL_BACKEND.endswith('smtp.EmailBackend')
    missing = [n for n, ok in (('EMAIL_HOST', bool(settings.EMAIL_HOST)), ('EMAIL_HOST_USER', bool(settings.EMAIL_HOST_USER)),
                               ('EMAIL_HOST_PASSWORD', bool(settings.EMAIL_HOST_PASSWORD)),
                               ('DEFAULT_FROM_EMAIL', not settings.DEFAULT_FROM_EMAIL.endswith('.local'))) if not ok]
    if smtp and not missing:
        add('Email is configured', PASS, f'Mail goes through {settings.EMAIL_HOST}. Press "Send test email" below to prove delivery.')
    else:
        add('Email is configured', ACTION, 'Emails are NOT being sent.' + (f' Missing: {", ".join(missing)}.' if missing else ''),
            'On Render set EMAIL_HOST=smtp.gmail.com, EMAIL_PORT=587, EMAIL_USE_TLS=true, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD and DEFAULT_FROM_EMAIL.')
    fy = _fy(timezone.localdate())
    if fy in TAX_RULES:
        add(f'Tax rules for FY {fy}', PASS, f'Rules for FY {fy} are configured.')
    else:
        add(f'Tax rules for FY {fy}', ACTION, f'No rules for FY {fy}. Payroll uses the latest earlier year.', 'Get the slabs and standard deduction from your CA and send them to be added.')
    on_render = getattr(settings, 'ON_RENDER', False)
    if on_render and 'MEDIA_ROOT' not in os.environ:
        add('Uploaded files are stored safely', ACTION, 'Files are written to the server disk, which is erased on every redeploy.',
            'Attach a Render Disk and set MEDIA_ROOT to its mount path, or use cloud storage.')
    elif on_render:
        add('Uploaded files are stored safely', WARN, f'MEDIA_ROOT is set to {settings.MEDIA_ROOT}.', 'Confirm that path is a Render Disk mount.')
    else:
        add('Uploaded files are stored safely', WARN, 'Not running on Render, so this cannot be judged here.', '')
    counts = dict(Employee.objects.order_by().values_list('company_id').annotate(n=Count('id')))
    empty = [c.name for c in Company.objects.all() if not counts.get(c.pk)]
    add('Every company has employees', PASS if not empty else WARN,
        'Every company has at least one employee.' if not empty else f'{len(empty)} company(ies) have no employees: {", ".join(empty[:6])}.',
        '' if not empty else 'If employees were imported into another company, use "Move employees" in the admin panel.')
    return checks


MANUAL = [
    'The secrets that were pasted in the chat (Aiven password, Django secret key, Gmail app password, superuser password) are replaced with new ones.',
    'A recent Aiven backup exists, and a test restore has been done once.',
    'An accountant has signed off the PF, ESI and professional tax rules.',
    'One month of payroll was run side by side with the Excel salary and every net pay matched.',
]


@admin_required
def readiness(request):
    if request.method == 'POST' and request.POST.get('action') == 'test_email':
        to = (request.user.email or '').strip()
        last = request.session.get('readiness_mail_at', 0)
        now = timezone.now().timestamp()
        if not to:
            messages.error(request, 'Your login has no email address, so there is nowhere to send the test. Add an email to your user first.')
        elif now - last < 30:
            messages.error(request, 'Please wait 30 seconds between test emails.')
        else:
            request.session['readiness_mail_at'] = now
            try:
                send_mail('Namma Payroll test email', 'If you can read this, email delivery from the live site works.', settings.DEFAULT_FROM_EMAIL, [to], fail_silently=False)
            except Exception as exc:
                messages.error(request, f'The email could not be sent: {type(exc).__name__}: {str(exc)[:200]}')
            else:
                if settings.EMAIL_BACKEND.endswith('console.EmailBackend'):
                    messages.warning(request, 'Email is not configured, so nothing was really sent (it only went to the server log).')
                else:
                    messages.success(request, f'The mail server accepted the test email for {to}. Check that inbox, and the spam folder.')
        return redirect(request.path)
    checks = run_checks()
    summary = {'action': sum(c['status'] == ACTION for c in checks), 'warn': sum(c['status'] == WARN for c in checks), 'ok': sum(c['status'] == PASS for c in checks)}
    return render(request, 'readiness.html', {'checks': checks, 'summary': summary, 'manual': MANUAL, 'admin_email': request.user.email})


urlpatterns = [path('admin-panel/readiness/', readiness, name='admin_readiness')]