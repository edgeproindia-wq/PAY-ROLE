"""End-to-end test of the registration approval flow.

register -> "pending admin approval" message -> admin is told (bell + email)
-> login blocked -> admin approves -> registrant is emailed -> login works.
Also proves that existing logins are not affected.
"""
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Company, Notification, User

ADMIN_PW = 'Adm1n-Pass#2026'
OLD_PW = 'Old0wner-Pass#2026'
NEW_PW = 'Br4nd-New-Pass#2026'
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'


def _logged_in(client):
    return '_auth_user_id' in client.session


@override_settings(EMAIL_BACKEND=EMAIL_BACKEND, DEMO_NOTIFY_EMAIL='boss@example.com',
                   REQUIRE_EMAIL_VERIFICATION=False)
class RegistrationApprovalFlowTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('flowadmin', 'flowadmin@example.com', ADMIN_PW)
        self.old_company = Company.objects.create(name='Old Client Ltd', contact_email='old@example.com',
                                                  status='APPROVED')
        self.old_owner = User.objects.create_user('oldowner', 'oldowner@example.com', OLD_PW,
                                                  role='COMPANY_OWNER', company=self.old_company)
        self.old_employee = User.objects.create_user('oldemp', 'oldemp@example.com', OLD_PW,
                                                     role='EMPLOYEE', company=self.old_company)
        self.form = {
            'company_name': 'Brand New Traders', 'owner_full_name': 'Ravi Kumar',
            'contact_email': 'ravi@newtraders.example', 'contact_phone': '9876543210',
            'address': 'Hosur', 'username': 'ravinew',
            'password1': NEW_PW, 'password2': NEW_PW,
        }

    def _register(self):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('company_register'), self.form, follow=True)

    def _login(self, client, username, password):
        return client.post(reverse('login'), {'username': username, 'password': password}, follow=True)

    # ------------------------------------------------------------------ 1
    def test_full_flow_from_register_to_login(self):
        # 1. register -> success message says pending approval
        resp = self._register()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'pending admin approval')
        company = Company.objects.get(name='Brand New Traders')
        owner = User.objects.get(username='ravinew')
        self.assertEqual(company.status, 'PENDING_APPROVAL')
        self.assertFalse(owner.is_active)

        # 2. admin is notified: bell + email
        self.assertTrue(Notification.objects.filter(recipient=self.admin,
                                                    message__contains='Brand New Traders').exists())
        admin_mails = [m for m in mail.outbox if 'waiting for approval' in m.subject]
        self.assertEqual(len(admin_mails), 1)
        self.assertIn('boss@example.com', admin_mails[0].to)
        self.assertIn('Brand New Traders', admin_mails[0].body)
        self.assertNotIn(NEW_PW, admin_mails[0].body)   # never email a password

        # 3. login is blocked while pending
        fresh = self.client_class()
        resp = self._login(fresh, 'ravinew', NEW_PW)
        self.assertFalse(_logged_in(fresh))

        # 4. admin approves -> registrant is emailed
        mail.outbox.clear()
        boss = self.client_class()
        self.assertTrue(_logged_in(boss) or self._login(boss, 'flowadmin', ADMIN_PW) is not None)
        self.assertTrue(_logged_in(boss))
        boss.post(reverse('admin_company_decide', args=[company.pk]), {'decision': 'APPROVED'}, follow=True)
        company.refresh_from_db(); owner.refresh_from_db()
        self.assertEqual(company.status, 'APPROVED')
        self.assertTrue(owner.is_active)
        approval = [m for m in mail.outbox if 'approved' in m.subject.lower()]
        self.assertEqual(len(approval), 1)
        self.assertEqual(approval[0].to, ['ravi@newtraders.example'])

        # 5. now the registrant can sign in
        after = self.client_class()
        self._login(after, 'ravinew', NEW_PW)
        self.assertTrue(_logged_in(after))

    # ------------------------------------------------------------------ 2
    def test_login_page_explains_why_login_is_refused(self):
        self._register()
        fresh = self.client_class()
        resp = self._login(fresh, 'ravinew', NEW_PW)
        self.assertFalse(_logged_in(fresh))
        self.assertContains(resp, 'pending')

    # ------------------------------------------------------------------ 3
    def test_existing_logins_still_work(self):
        for username, password in (('oldowner', OLD_PW), ('oldemp', OLD_PW), ('flowadmin', ADMIN_PW)):
            c = self.client_class()
            self._login(c, username, password)
            self.assertTrue(_logged_in(c), f'{username} could not sign in')
        self.assertEqual(User.objects.filter(username='oldowner', is_active=True).count(), 1)

    # ------------------------------------------------------------------ 4
    def test_rejected_company_stays_blocked(self):
        self._register()
        company = Company.objects.get(name='Brand New Traders')
        boss = self.client_class()
        self._login(boss, 'flowadmin', ADMIN_PW)
        boss.post(reverse('admin_company_decide', args=[company.pk]),
                  {'decision': 'REJECTED', 'reason': 'test'}, follow=True)
        fresh = self.client_class()
        self._login(fresh, 'ravinew', NEW_PW)
        self.assertFalse(_logged_in(fresh))
        self.assertFalse(User.objects.get(username='ravinew').is_active)

    # ------------------------------------------------------------------ 5
    def test_double_click_approve_sends_one_email(self):
        self._register()
        company = Company.objects.get(name='Brand New Traders')
        boss = self.client_class()
        self._login(boss, 'flowadmin', ADMIN_PW)
        mail.outbox.clear()
        for _ in range(2):
            boss.post(reverse('admin_company_decide', args=[company.pk]), {'decision': 'APPROVED'}, follow=True)
        self.assertEqual(len([m for m in mail.outbox if 'approved' in m.subject.lower()]), 1)

    # ------------------------------------------------------------------ 6
    def test_mail_problems_never_break_registration_or_approval(self):
        with mock.patch('payroll_app.views.send_email_safe', return_value=(False, 'smtp down')):
            resp = self._register()
            self.assertContains(resp, 'pending admin approval')
            company = Company.objects.get(name='Brand New Traders')
            self.assertEqual(company.status, 'PENDING_APPROVAL')
            boss = self.client_class()
            self._login(boss, 'flowadmin', ADMIN_PW)
            resp = boss.post(reverse('admin_company_decide', args=[company.pk]),
                             {'decision': 'APPROVED'}, follow=True)
        company.refresh_from_db()
        self.assertEqual(company.status, 'APPROVED')
        self.assertTrue(User.objects.get(username='ravinew').is_active)
        self.assertContains(resp, 'could not be sent')

    # ------------------------------------------------------------------ 7
    def test_no_admin_email_address_does_not_crash(self):
        User.objects.filter(pk=self.admin.pk).update(email='')
        with override_settings(DEMO_NOTIFY_EMAIL=''):
            resp = self._register()
        self.assertContains(resp, 'pending admin approval')
        self.assertTrue(Company.objects.filter(name='Brand New Traders').exists())