"""Launch readiness page: admin only, honest about live settings, never leaks secrets, and the test email really goes through."""
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import User
from .payroll_engine import TAX_RULES
from .readiness import _fy, run_checks
from .tests import make_company, make_employee, make_user

SECRET = 'SUPER-SECRET-VALUE-1234567890-abcdefghijklmnopqrstuvwxyz'


def by_name(prefix):
    return next(c for c in run_checks() if c['name'].startswith(prefix))


class ReadinessTests(TestCase):
    def setUp(self):
        self.admin = make_user('readyadmin', 'ADMIN', None)
        User.objects.filter(pk=self.admin.pk).update(email='boss@ready.test')
        self.url = reverse('admin_readiness')

    def test_only_the_platform_admin_can_open_it(self):
        co = make_company('Ready Co')
        for role in ('COMPANY_OWNER', 'EMPLOYEE'):
            self.client.force_login(make_user(f'ready_{role}', role, co))
            self.assertEqual(self.client.get(self.url).status_code, 403, role)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.admin)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Launch readiness')

    @override_settings(SECRET_KEY=SECRET, EMAIL_HOST_PASSWORD='mailpass-9876', EMAIL_HOST_USER='me@x.test')
    def test_no_secret_value_is_ever_shown(self):
        self.client.force_login(self.admin)
        resp = self.client.get(self.url)
        self.assertNotContains(resp, SECRET)
        self.assertNotContains(resp, 'mailpass-9876')

    @override_settings(DEBUG=True)
    def test_debug_on_needs_action(self):
        self.assertEqual(by_name('Debug')['status'], 'ACTION NEEDED')

    @override_settings(SECRET_KEY='short')
    def test_a_short_secret_key_needs_action(self):
        self.assertEqual(by_name('Secret key')['status'], 'ACTION NEEDED')

    @override_settings(SECRET_KEY=SECRET)
    def test_a_long_secret_key_passes(self):
        self.assertEqual(by_name('Secret key')['status'], 'PASS')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend', EMAIL_HOST='')
    def test_console_email_needs_action(self):
        c = by_name('Email')
        self.assertEqual(c['status'], 'ACTION NEEDED')
        self.assertIn('NOT being sent', c['detail'])

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend', EMAIL_HOST='smtp.example.test',
                       EMAIL_HOST_USER='me@x.test', EMAIL_HOST_PASSWORD='p', DEFAULT_FROM_EMAIL='payroll@real.test')
    def test_a_complete_email_setup_passes(self):
        self.assertEqual(by_name('Email')['status'], 'PASS')

    def test_tax_rules_for_the_current_year(self):
        fy = _fy(timezone.localdate())
        with mock.patch.dict(TAX_RULES, {k: v for k, v in TAX_RULES.items() if k != fy}, clear=True):
            self.assertEqual(by_name('Tax rules')['status'], 'ACTION NEEDED')
        with mock.patch.dict(TAX_RULES, {fy: dict(next(iter(TAX_RULES.values())))}):
            self.assertEqual(by_name('Tax rules')['status'], 'PASS')

    def test_a_company_without_employees_is_flagged(self):
        make_company('Empty Co')
        full = make_company('Full Co')
        make_employee(full, 'RD1', 'rd1@ready.test')
        c = by_name('Every company')
        self.assertEqual(c['status'], 'WARNING')
        self.assertIn('Empty Co', c['detail'])
        self.assertNotIn('Full Co', c['detail'])

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_the_test_email_is_really_sent_to_the_admin(self):
        self.client.force_login(self.admin)
        self.client.post(self.url, {'action': 'test_email'})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['boss@ready.test'])

    def test_a_failing_mail_server_shows_the_reason_and_does_not_crash(self):
        self.client.force_login(self.admin)
        with mock.patch('payroll_app.readiness.send_mail', side_effect=ConnectionRefusedError('smtp down')):
            resp = self.client.post(self.url, {'action': 'test_email'}, follow=True)
        self.assertContains(resp, 'could not be sent')
        self.assertContains(resp, 'ConnectionRefusedError')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_test_emails_are_rate_limited(self):
        self.client.force_login(self.admin)
        self.client.post(self.url, {'action': 'test_email'})
        self.client.post(self.url, {'action': 'test_email'})
        self.assertEqual(len(mail.outbox), 1)