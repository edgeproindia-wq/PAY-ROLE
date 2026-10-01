"""Employee dashboard, payslip PDF and tax documents: access rules and happy paths."""
import datetime
import itertools
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import models
from django.test import TestCase
from django.urls import reverse

from .models import Company, Employee, Notification, PayrollRunLine, User
from .models_documents import EmployeeDocument

_seq = itertools.count(1)


def make(model, **kw):
    """Create a row, filling any other required field with a harmless value."""
    if model is User:
        if 'username' not in kw:
            kw['username'] = f'autouser{next(_seq)}'
        return User.objects.create_user(kw.pop('username'), password='StrongPass123!x', **kw)
    for f in model._meta.concrete_fields:
        if f.primary_key or f.name in kw or f.attname in kw or f.null or f.has_default() or getattr(f, 'auto_now_add', False) or getattr(f, 'auto_now', False):
            continue
        if f.blank and not isinstance(f, (models.ForeignKey, models.DateField, models.DecimalField, models.IntegerField)):
            continue
        n = next(_seq)
        if f.choices:
            kw[f.name] = f.choices[0][0]
        elif isinstance(f, models.ForeignKey):
            kw[f.name] = make(f.related_model)
        elif isinstance(f, models.EmailField):
            kw[f.name] = f'auto{n}@ex.com'
        elif isinstance(f, models.DateTimeField):
            kw[f.name] = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        elif isinstance(f, models.DateField):
            kw[f.name] = datetime.date(2026, 1, 1)
        elif isinstance(f, models.DecimalField):
            kw[f.name] = Decimal('0')
        elif isinstance(f, (models.IntegerField, models.FloatField)):
            kw[f.name] = 1
        elif isinstance(f, models.BooleanField):
            kw[f.name] = False
        else:
            kw[f.name] = f'auto{n}'[: f.max_length or 20]
    return model.objects.create(**kw)


def released_status():
    field = PayrollRunLine._meta.get_field('payroll_run').related_model._meta.get_field('status')
    codes = [c for c, _ in field.choices]
    return next((c for c in codes if any(k in c.upper() for k in ('RELEAS', 'LOCK', 'PAID'))), codes[-1])


class EmployeePortalTests(TestCase):
    def setUp(self):
        self.c1 = make(Company, name='Acme', status='APPROVED')
        self.c2 = make(Company, name='Beta', status='APPROVED')
        extra = {'email_verified': True} if 'email_verified' in {f.name for f in User._meta.get_fields()} else {}
        self.u1 = make(User, username='emp1', role='EMPLOYEE', company=self.c1, email='e1@acme.com', **extra)
        self.u2 = make(User, username='emp2', role='EMPLOYEE', company=self.c1, email='e2@acme.com', **extra)
        self.owner = make(User, username='own1', role='COMPANY_OWNER', company=self.c1, email='o@acme.com', **extra)
        self.owner2 = make(User, username='own2', role='COMPANY_OWNER', company=self.c2, email='o@beta.com', **extra)
        self.e1 = make(Employee, company=self.c1, user=self.u1, first_name='Anu', email='e1@acme.com')
        self.e2 = make(Employee, company=self.c1, user=self.u2, first_name='Bala', email='e2@acme.com')
        run_model = PayrollRunLine._meta.get_field('payroll_run').related_model
        run = make(run_model, status=released_status())
        money = {f.name: Decimal('1000') for f in PayrollRunLine._meta.concrete_fields
                 if isinstance(f, models.DecimalField) and not f.null}
        self.line1 = make(PayrollRunLine, payroll_run=run, employee=self.e1, **money)
        self.line2 = make(PayrollRunLine, payroll_run=run, employee=self.e2, **money)

    def _upload(self, client_user, employee, doc_type='FORM16'):
        self.client.force_login(client_user)
        pdf = SimpleUploadedFile('form16.pdf', b'%PDF-1.4 test', content_type='application/pdf')
        return self.client.post(reverse('hr_documents'), {'employee': employee.pk, 'doc_type': doc_type,
                                                          'financial_year': '2025-26', 'file': pdf})

    def test_employee_dashboard_and_own_payslip_pdf(self):
        self.client.force_login(self.u1)
        resp = self.client.get(reverse('employee_home'))
        self.assertContains(resp, 'Welcome, Anu')
        self.assertContains(resp, 'Form 16')
        self.assertContains(resp, 'Not issued yet')
        pdf = self.client.get(reverse('payslip_pdf', args=[self.line1.pk]))
        self.assertEqual(pdf.status_code, 200)
        self.assertIn(pdf['Content-Type'], ('application/pdf', 'text/html; charset=utf-8'))

    def test_employee_cannot_open_colleagues_payslip(self):
        self.client.force_login(self.u1)
        self.assertEqual(self.client.get(reverse('payslip_pdf', args=[self.line2.pk])).status_code, 404)

    def test_owner_uploads_form16_employee_downloads_and_is_notified(self):
        resp = self._upload(self.owner, self.e1)
        self.assertRedirects(resp, reverse('hr_documents'), fetch_redirect_response=False)
        doc = EmployeeDocument.objects.get()
        self.assertTrue(Notification.objects.filter(recipient=self.u1, message__icontains='Form 16').exists())
        self.client.force_login(self.u1)
        dl = self.client.get(reverse('employee_document_download', args=[doc.pk]))
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl.content, b'%PDF-1.4 test')
        self.assertContains(self.client.get(reverse('employee_home')), 'Available')

    def test_documents_are_isolated(self):
        self._upload(self.owner, self.e1)
        doc = EmployeeDocument.objects.get()
        self.client.force_login(self.u2)                                  # colleague
        self.assertEqual(self.client.get(reverse('employee_document_download', args=[doc.pk])).status_code, 404)
        self.client.force_login(self.owner2)                              # other company's owner
        self.assertEqual(self.client.get(reverse('hr_document_download', args=[doc.pk])).status_code, 404)

    def test_owner_cannot_upload_for_other_company_employee(self):
        self._upload(self.owner2, self.e1)
        self.assertFalse(EmployeeDocument.objects.exists())

    def test_role_checks(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('employee_home')).status_code, 403)
        self.client.force_login(self.u1)
        self.assertEqual(self.client.get(reverse('hr_documents')).status_code, 403)

    def test_bad_file_rejected(self):
        self.client.force_login(self.owner)
        exe = SimpleUploadedFile('x.exe', b'MZ', content_type='application/octet-stream')
        resp = self.client.post(reverse('hr_documents'), {'employee': self.e1.pk, 'doc_type': 'FORM16',
                                                          'financial_year': '2025-26', 'file': exe})
        self.assertContains(resp, 'Only PDF, JPG or PNG')
        self.assertFalse(EmployeeDocument.objects.exists())

    def test_employee_lands_on_own_dashboard_after_login(self):
        self.client.force_login(self.u1)
        self.assertRedirects(self.client.get(reverse('post_login_redirect')), reverse('employee_home'),
                             fetch_redirect_response=False)