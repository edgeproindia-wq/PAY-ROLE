"""Email backend that sends through Brevo's HTTPS API (port 443).

Render's Free plan blocks outbound SMTP (ports 25/465/587), so Gmail SMTP cannot
work there. This backend needs no extra packages and no SMTP.

Env vars:
    DJANGO_EMAIL_BACKEND = config.brevo_backend.BrevoEmailBackend
    BREVO_API_KEY        = your Brevo API key
    DEFAULT_FROM_EMAIL   = a sender address verified in Brevo
"""
import json
import logging
import os
import urllib.error
import urllib.request
from email.utils import parseaddr

from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)
API_URL = 'https://api.brevo.com/v3/smtp/email'


class BrevoEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        api_key = os.environ.get('BREVO_API_KEY', '')
        if not api_key:
            if not self.fail_silently:
                raise RuntimeError('BREVO_API_KEY is not set')
            return 0
        sent = 0
        for message in email_messages:
            try:
                self._send_one(api_key, message)
                sent += 1
            except Exception:
                logger.exception('Brevo email to %s failed', message.to)
                if not self.fail_silently:
                    raise
        return sent

    def _send_one(self, api_key, message):
        name, address = parseaddr(message.from_email or os.environ.get('DEFAULT_FROM_EMAIL', ''))
        payload = {
            'sender': {'email': address, **({'name': name} if name else {})},
            'to': [{'email': r} for r in message.to],
            'subject': message.subject,
            'textContent': message.body,
        }
        if message.cc:
            payload['cc'] = [{'email': r} for r in message.cc]
        request = urllib.request.Request(
            API_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={'api-key': api_key, 'content-type': 'application/json', 'accept': 'application/json'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')[:300]
            raise RuntimeError(f'Brevo API error {exc.code}: {detail}') from exc