"""Companies that were waiting for admin approval are approved by migration 0015, so nobody is stuck on the pending message."""
import importlib

from django.apps import apps
from django.test import TestCase
from django.urls import reverse

from .models import Company, User

PWD = 'StrongPass123'
migration = importlib.import_module('payroll_app.migrations.0015_approve_pending_companies')


def make(name, status, username, active=False, verified=True):
    company = Company.objects.create(name=name, contact_email=f'{username}@ex.com', status=status)
    owner = User.objects.create_user(username=username, password=PWD, role='COMPANY_OWNER', company=company,
                                     email=f'{username}@ex.com', is_active=active, email_verified=verified)
    return company, owner


class ApprovePendingMigrationTests(TestCase):
    def test_waiting_companies_are_approved_and_their_owners_can_sign_in(self):
        company, owner = make('WaitCo', 'PENDING_APPROVAL', 'waitowner')
        migration.approve_waiting(apps, None)
        company.refresh_from_db()
        owner.refresh_from_db()
        self.assertEqual(company.status, 'APPROVED')
        self.assertIsNotNone(company.approved_at)
        self.assertTrue(owner.is_active)
        resp = self.client.post(reverse('login'), {'username': 'waitowner', 'password': PWD})
        self.assertRedirects(resp, reverse('post_login_redirect'), fetch_redirect_response=False)

    def test_rejected_and_suspended_companies_are_left_alone(self):
        rejected, r_owner = make('NoCo', 'REJECTED', 'rejowner')
        suspended, s_owner = make('StopCo', 'SUSPENDED', 'susowner')
        migration.approve_waiting(apps, None)
        for obj in (rejected, suspended, r_owner, s_owner):
            obj.refresh_from_db()
        self.assertEqual((rejected.status, suspended.status), ('REJECTED', 'SUSPENDED'))
        self.assertFalse(r_owner.is_active or s_owner.is_active)

    def test_an_unverified_email_stays_unverified(self):
        company, owner = make('MailCo', 'PENDING_APPROVAL', 'mailowner', verified=False)
        migration.approve_waiting(apps, None)
        owner.refresh_from_db()
        self.assertTrue(owner.is_active)
        self.assertFalse(owner.email_verified)

    def test_running_it_twice_changes_nothing_more(self):
        company, owner = make('TwiceCo', 'PENDING_APPROVAL', 'twiceowner')
        migration.approve_waiting(apps, None)
        company.refresh_from_db()
        first = company.approved_at
        migration.approve_waiting(apps, None)
        company.refresh_from_db()
        self.assertEqual(company.approved_at, first)
