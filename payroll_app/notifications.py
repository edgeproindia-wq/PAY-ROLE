"""Outbound notifications (email + SMS).

Design rules:
- Credentials come only from environment variables (see settings / .env.example).
- A missing or failing provider NEVER breaks the user's request: every failure
  is logged with logger.warning/exception and reported back to the caller so it
  can be stored (e.g. DemoRequest.notification_error) and shown to admins.
"""
import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger('payroll_app.notifications')

SMS_TIMEOUT_SECONDS = 10


def send_email_safe(subject, body, recipients):
    """Send a plain-text email. Returns (ok: bool, error: str)."""
    recipients = [r for r in recipients if r]
    if not recipients:
        return False, 'No email recipient configured'
    try:
        sent = send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients,
            fail_silently=False,
        )
        if sent:
            return True, ''
        return False, 'Mail backend reported 0 messages sent'
    except Exception as exc:  # SMTP down, bad credentials, DNS, timeout...
        logger.exception('Email to %s failed', recipients)
        return False, f'Email failed: {exc.__class__.__name__}'


def _normalise_indian_mobile(number):
    digits = ''.join(ch for ch in (number or '') if ch.isdigit())
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[2:]
    return digits


def send_sms_safe(number, text):
    """Send an SMS through the provider configured in SMS_PROVIDER.

    Supported: 'fast2sms' (India) and 'twilio'. Returns (ok, error)."""
    provider = (getattr(settings, 'SMS_PROVIDER', '') or '').strip().lower()
    mobile = _normalise_indian_mobile(number)
    if not mobile:
        return False, 'No SMS recipient configured'
    if not provider:
        logger.warning('SMS not sent to %s: SMS_PROVIDER is not configured', mobile)
        return False, 'SMS provider not configured'

    try:
        if provider == 'fast2sms':
            api_key = getattr(settings, 'FAST2SMS_API_KEY', '')
            if not api_key:
                return False, 'FAST2SMS_API_KEY not set'
            payload = json.dumps({'route': 'q', 'message': text[:600], 'numbers': mobile}).encode()
            req = urllib.request.Request(
                'https://www.fast2sms.com/dev/bulkV2', data=payload, method='POST',
                headers={'authorization': api_key, 'Content-Type': 'application/json'},
            )
            with urllib.request.urlopen(req, timeout=SMS_TIMEOUT_SECONDS) as resp:
                body = json.loads(resp.read().decode() or '{}')
            if body.get('return') is True:
                return True, ''
            logger.warning('Fast2SMS rejected message: %s', body)
            return False, f"Fast2SMS error: {str(body.get('message', 'unknown'))[:150]}"

        if provider == 'twilio':
            sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '')
            token = getattr(settings, 'TWILIO_AUTH_TOKEN', '')
            from_no = getattr(settings, 'TWILIO_FROM_NUMBER', '')
            if not (sid and token and from_no):
                return False, 'Twilio credentials not set'
            data = urllib.parse.urlencode({'To': f'+91{mobile}', 'From': from_no, 'Body': text[:1500]}).encode()
            auth = base64.b64encode(f'{sid}:{token}'.encode()).decode()
            req = urllib.request.Request(
                f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json', data=data, method='POST',
                headers={'Authorization': f'Basic {auth}'},
            )
            with urllib.request.urlopen(req, timeout=SMS_TIMEOUT_SECONDS) as resp:
                if 200 <= resp.status < 300:
                    return True, ''
                return False, f'Twilio HTTP {resp.status}'

        return False, f'Unknown SMS_PROVIDER "{provider}"'
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        logger.exception('SMS to %s via %s failed', mobile, provider)
        return False, f'SMS failed: {exc.__class__.__name__}'


def _demo_recipients():
    """The configured admin/host address (ADMIN_NOTIFICATION_EMAIL); if none is set,
    every active platform admin's email, so alerts are never silently dropped."""
    if settings.DEMO_NOTIFY_EMAIL:
        return [settings.DEMO_NOTIFY_EMAIL]
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    users = get_user_model().objects.filter(Q(is_superuser=True) | Q(role='ADMIN'), is_active=True)
    return [e for e in users.exclude(email='').values_list('email', flat=True)]


def notify_demo_request(demo):
    """Email + SMS the configured recipients about a new demo request and
    record the outcome on the DemoRequest row itself."""
    when = timezone.localtime(demo.preferred_datetime).strftime('%d %b %Y %I:%M %p') if demo.preferred_datetime else 'Not specified'
    submitted = timezone.localtime(demo.created_at).strftime('%d %b %Y %I:%M %p')
    body = (
        'New demo request received\n\n'
        f'Request ID    : {demo.request_code}\n'
        f'Full name     : {demo.full_name}\n'
        f'Email         : {demo.email}\n'
        f'Mobile        : {demo.phone or "-"}\n'
        f'Company       : {demo.company_name}\n'
        f'Employees     : {demo.team_size or "-"}\n'
        f'Preferred demo: {when}\n'
        f'Industry      : {demo.industry or "-"}\n'
        f'Modules       : {demo.modules or "-"}\n'
        f'Message       : {demo.message or "-"}\n'
        f'Submitted at  : {submitted}\n'
    )
    email_ok, email_err = send_email_safe(
        f'New Request Demo Received - {demo.company_name} ({demo.request_code})', body, _demo_recipients()
    )
    sms_text = (
        f'New demo request: {demo.full_name}, {demo.company_name}, {demo.phone or demo.email}, '
        f'staff {demo.team_size or "-"}, demo {when}.'
    )
    sms_ok, sms_err = send_sms_safe(settings.DEMO_NOTIFY_MOBILE, sms_text)

    errors = '; '.join(e for e in (email_err, sms_err) if e)
    type(demo).objects.filter(pk=demo.pk).update(
        email_notified=email_ok, sms_notified=sms_ok, notification_error=errors[:500]
    )
    demo.email_notified, demo.sms_notified, demo.notification_error = email_ok, sms_ok, errors[:500]
    return email_ok, sms_ok
