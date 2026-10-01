"""Phase-6 hardening tests: missing private files never cause a 500, login throttling
fails open if the cache is unavailable, and public contact details come from settings."""
import datetime
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import InvestmentDeclaration, Reimbursement
from .tests_audit import company, employee, user


class MissingFileTests(TestCase):
    def setUp(self):
        self.c = company('Acme')
        self.owner = user('own', 'COMPANY_OWNER', self.c)
        self.e = employee(self.c, 'A1', 'a1@ex.com', login='emp1')

    def test_lost_receipt_and_proof_give_404_not_500(self):
        r = Reimbursement.objects.create(employee=self.e, category='TRAVEL', amount=10, date=datetime.date(2026, 9, 1),
                                         receipt=SimpleUploadedFile('bill.pdf', b'%PDF-1.4', content_type='application/pdf'))
        d = InvestmentDeclaration.objects.create(employee=self.e, section='80C', investment_type='PPF', declared_amount=1,
                                                 financial_year='2026-27',
                                                 proof_document=SimpleUploadedFile('p.pdf', b'%PDF-1.4', content_type='application/pdf'))
        r.receipt.storage.delete(r.receipt.name)          # simulate a file lost from Render's disk
        d.proof_document.storage.delete(d.proof_document.name)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('reimbursement_receipt', args=[r.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('declaration_proof', args=[d.pk])).status_code, 404)


class ThrottleResilienceTests(TestCase):
    def test_login_still_works_if_cache_is_down(self):
        c = company('Acme')
        u = user('own', 'COMPANY_OWNER', c)
        from .tests_audit import PWD
        with mock.patch('payroll_app.login_throttle.cache.get', side_effect=Exception('cache down')), \
             mock.patch('payroll_app.login_throttle.cache.incr', side_effect=Exception('cache down')):
            self.client.post(reverse('login'), {'username': 'own', 'password': 'wrong'})
            self.client.post(reverse('login'), {'username': 'own', 'password': PWD})
        self.assertEqual(str(self.client.session.get('_auth_user_id')), str(u.pk))


class PublicContactTests(TestCase):
    def test_no_personal_contact_hardcoded(self):
        body = self.client.get(reverse('landing')).content.decode()
        self.assertNotIn('jaganbharath46', body)
        self.assertNotIn('6383538565', body)

    @override_settings(PUBLIC_CONTACT_EMAIL='hello@company.example', PUBLIC_CONTACT_PHONE='+91 80000 00000')
    def test_contact_from_settings(self):
        self.assertContains(self.client.get(reverse('landing')), 'hello@company.example')