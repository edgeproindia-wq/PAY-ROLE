"""Company self-registration without admin approval (the default), and the switch that brings approval back."""
import re

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Company, Notification, User

PWD = 'StrongPass123'


def payload(**over):
    data = {'company_name': 'OpenCo', 'owner_full_name': 'Open Owner', 'contact_email': 'owner@openco.com',
            'contact_phone': '9876543210', 'address': 'Hosur', 'username': 'openowner',
            'password1': PWD, 'password2': PWD}
    data.update(over)
    return data


@override_settings(REQUIRE_EMAIL_VERIFICATION=False)
class NoApprovalNeededTests(TestCase):
    def test_a_new_company_is_approved_at_once_and_the_owner_can_sign_in(self):
        resp = self.client.post(reverse('company_register'), payload(), follow=True)
        self.assertContains(resp, 'You can sign in now')
        owner = User.objects.get(username='openowner')
        self.assertTrue(owner.is_active)
        self.assertEqual(owner.company.status, 'APPROVED')
        self.client.logout()
        resp = self.client.post(reverse('login'), {'username': 'openowner', 'password': PWD})
        self.assertRedirects(resp, reverse('post_login_redirect'), fetch_redirect_response=False)

    def test_no_waiting_for_approval_email_is_sent(self):
        self.client.post(reverse('company_register'), payload())
        self.assertEqual(len(mail.outbox), 0)

    def test_the_platform_admin_still_gets_an_information_notice(self):
        User.objects.create_superuser('boss', 'boss@ex.com', PWD)
        self.client.post(reverse('company_register'), payload())
        texts = list(Notification.objects.values_list('message', flat=True))
        self.assertTrue(any('New company registered: OpenCo' in t for t in texts), texts)
        self.assertFalse(any('pending approval' in t for t in texts), texts)

    def test_the_register_page_no_longer_promises_an_approval_step(self):
        page = self.client.get(reverse('company_register')).content.decode()
        self.assertNotIn('approves', page)

    def test_a_company_that_was_rejected_or_suspended_is_still_blocked(self):
        self.client.post(reverse('company_register'), payload())
        company = Company.objects.get(name='OpenCo')
        for status in ('REJECTED', 'SUSPENDED'):
            company.status = status
            company.save()
            self.client.logout()
            resp = self.client.post(reverse('login'), {'username': 'openowner', 'password': PWD})
            self.assertEqual(resp.status_code, 200, status)          # the login form again, not a redirect


@override_settings(REQUIRE_EMAIL_VERIFICATION=False, REQUIRE_ADMIN_APPROVAL=True)
class ApprovalSwitchedBackOnTests(TestCase):
    def test_with_the_switch_on_a_new_company_waits_for_the_admin(self):
        self.client.post(reverse('company_register'), payload())
        owner = User.objects.get(username='openowner')
        self.assertFalse(owner.is_active)
        self.assertEqual(owner.company.status, 'PENDING_APPROVAL')


@override_settings(REQUIRE_EMAIL_VERIFICATION=True)
class EmailCodeWithoutApprovalTests(TestCase):
    def test_register_then_verify_the_email_then_sign_in_with_no_admin_step(self):
        resp = self.client.post(reverse('company_register'), payload())
        self.assertRedirects(resp, reverse('verify_email'))
        owner = User.objects.get(username='openowner')
        self.assertTrue(owner.is_active)
        self.assertFalse(owner.email_verified)
        refused = self.client.post(reverse('login'), {'username': 'openowner', 'password': PWD})
        self.assertContains(refused, 'not verified')                  # the email check still applies
        code = re.search(r'\b(\d{6})\b', mail.outbox[-1].body).group(1)
        resp = self.client.post(reverse('verify_email'), {'code': code}, follow=True)
        self.assertContains(resp, 'You can sign in now')
        resp = self.client.post(reverse('login'), {'username': 'openowner', 'password': PWD})
        self.assertRedirects(resp, reverse('post_login_redirect'), fetch_redirect_response=False)
