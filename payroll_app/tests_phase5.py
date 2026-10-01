"""Phase-5 regression tests: login throttling, credential rotation command, joiner
edge dates, unpaid leave in payroll, new leave types, loan auto-close, percentage
pay components, document upload/verify/publish, Form 16 Part B drafts, announcement
read status, reimbursement comments, double-submit guard and export scoping."""
import datetime
import io
from decimal import Decimal

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from .models import (Announcement, LeaveRequest, Notification, PayrollRun, Reimbursement, SalaryStructure, User)
from .models_documents import EmployeeDocument
from .models_features import Loan
from .models_phase4 import PayComponent
from .tests_audit import PWD, company, employee, user


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.c1, self.c2 = company('Acme'), company('Beta')
        self.owner = user('own', 'COMPANY_OWNER', self.c1)
        self.owner2 = user('own2', 'COMPANY_OWNER', self.c2)
        self.e1 = employee(self.c1, 'A1', 'a1@ex.com', login='emp1')
        self.e2 = employee(self.c1, 'A2', 'a2@ex.com', login='emp2')
        for e in (self.e1, self.e2):
            SalaryStructure.objects.create(employee=e, basic=20000, hra=8000, conveyance=1600, special_allowance=10400)

    def login(self, u):
        self.client.cookies.clear()
        self.client.force_login(u)

    def make_run(self, month, release=False):
        self.login(self.owner)
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': month})
        run = PayrollRun.objects.get(company=self.c1, month__iexact=month)
        if release:
            for a in ('validate', 'approve', 'release'):
                self.client.post(reverse('payroll_combined'), {'action': a, 'run_id': run.pk})
            run.refresh_from_db()
        return run

    def joiner(self, code, doj):
        e = employee(self.c1, code, f'{code.lower()}@ex.com')
        e.date_of_joining = doj
        e.save()
        SalaryStructure.objects.create(employee=e, basic=20000, hra=8000, conveyance=1600, special_allowance=10400)
        return e


class SecurityTests(Base):
    def test_login_locked_after_five_failures(self):
        for _ in range(5):
            self.client.post(reverse('login'), {'username': 'emp1', 'password': 'wrong'})
        resp = self.client.post(reverse('login'), {'username': 'emp1', 'password': PWD})
        self.assertContains(resp, 'Too many failed')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_lock_user_command_disables_password(self):
        out = io.StringIO()
        call_command('lock_user', 'own', stdout=out)
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.has_usable_password())
        self.assertFalse(self.client.login(username='own', password=PWD))
        self.assertNotIn(PWD, out.getvalue())

    def test_double_submit_guard_on_every_page(self):
        self.login(self.owner)
        self.assertContains(self.client.get(reverse('dashboard')), 'data-submit-guard')

    def test_exports_are_company_scoped(self):
        employee(self.c2, 'B7', 'b7@ex.com')
        self.login(self.owner)
        body = self.client.get(reverse('employee_master_export_csv')).content.decode()
        self.assertIn('a1@ex.com', body)
        self.assertNotIn('b7@ex.com', body)


class JoinerEdgeTests(Base):
    def test_first_last_and_boundary_joiners(self):
        first = self.joiner('J1', datetime.date(2026, 9, 1))
        last = self.joiner('J2', datetime.date(2026, 9, 30))
        before = self.joiner('J3', datetime.date(2026, 8, 31))
        after = self.joiner('J4', datetime.date(2026, 10, 1))
        run = self.make_run('September 2026')
        lines = {l.employee_id: l for l in run.lines.all()}
        self.assertEqual(lines[first.pk].total_earnings, Decimal('40000.00'))
        self.assertEqual(lines[before.pk].total_earnings, Decimal('40000.00'))
        self.assertEqual(lines[last.pk].total_earnings, Decimal('1333.33'))       # 1 of 30 days
        self.assertNotIn(after.pk, lines)

    def test_february_joiner(self):
        j = self.joiner('J5', datetime.date(2027, 2, 15))
        run = self.make_run('February 2027')
        self.assertEqual(run.lines.get(employee=j).total_earnings, Decimal('20000.00'))   # 14 of 28 days


class LeavePayrollTests(Base):
    def test_unpaid_leave_is_loss_of_pay_without_double_count(self):
        from .models import Attendance
        LeaveRequest.objects.create(employee=self.e1, leave_type='UNPAID', from_date=datetime.date(2026, 9, 10),
                                    to_date=datetime.date(2026, 9, 11), status='APPROVED')
        Attendance.objects.create(employee=self.e1, date=datetime.date(2026, 9, 10), status='ABSENT')
        line = self.make_run('September 2026').lines.get(employee=self.e1)
        self.assertEqual(line.lop_days, Decimal('2.0'))
        self.assertEqual(line.lop_amount, Decimal('2666.67'))

    def test_new_leave_types_can_be_applied(self):
        self.login(self.e1.user)
        self.client.post(reverse('leave_management'), {'leave_type': 'MATERNITY', 'from_date': '2026-11-02',
                                                       'to_date': '2026-11-30', 'reason': 'x'})
        self.assertTrue(LeaveRequest.objects.filter(employee=self.e1, leave_type='MATERNITY').exists())


class LoanAndComponentTests(Base):
    def test_loan_auto_closes_when_recovered(self):
        loan = Loan.objects.create(company=self.c1, employee=self.e1, reference_no='L1', loan_type='SALARY_ADVANCE',
                                   loan_amount=3000, tenure_months=1, emi_amount=3000, start_date=datetime.date(2026, 9, 1))
        self.make_run('September 2026', release=True)
        self.make_run('October 2026', release=True)
        loan.refresh_from_db()
        self.assertEqual((loan.status, loan.balance), ('CLOSED', 0))

    def test_percentage_component(self):
        PayComponent.objects.create(company=self.c1, employee=self.e1, name='Night allowance', kind='EARNING',
                                    calc_type='PERCENT_BASIC', percent=10, amount=0, effective_from=datetime.date(2026, 1, 1))
        line = self.make_run('September 2026').lines.get(employee=self.e1)
        self.assertEqual(line.other_earnings, Decimal('2000.00'))


class DocumentTests(Base):
    def test_employee_upload_owner_verify_and_isolation(self):
        self.login(self.e1.user)
        self.client.post(reverse('my_document_upload'), {'doc_type': 'ID_PROOF', 'title': 'Aadhaar',
                                                         'file': SimpleUploadedFile('id.pdf', b'%PDF-1.4 x', content_type='application/pdf')})
        doc = EmployeeDocument.objects.get(doc_type='ID_PROOF')
        self.assertEqual(doc.status, 'PENDING')
        self.assertTrue(Notification.objects.filter(recipient=self.owner).exists())
        self.login(self.owner)
        self.client.post(reverse('hr_document_action', args=[doc.pk]), {'action': 'verify'})
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'VERIFIED')
        for u in (self.e2.user, self.owner2):
            self.login(u)
            codes = [self.client.get(reverse('employee_document_download', args=[doc.pk])).status_code,
                     self.client.post(reverse('hr_document_action', args=[doc.pk]), {'action': 'delete'}).status_code]
            self.assertTrue(all(c in (403, 404) for c in codes), codes)
        self.assertTrue(EmployeeDocument.objects.filter(pk=doc.pk).exists())

    def test_bad_upload_rejected(self):
        self.login(self.e1.user)
        self.client.post(reverse('my_document_upload'), {'doc_type': 'OTHER', 'file': SimpleUploadedFile('x.exe', b'MZ')})
        self.assertFalse(EmployeeDocument.objects.exists())


class Form16Tests(Base):
    def test_draft_generated_hidden_until_published(self):
        self.make_run('September 2026', release=True)
        self.make_run('October 2026', release=True)
        self.login(self.owner)
        self.client.post(reverse('hr_form16'), {'financial_year': '2026-27', 'employee': self.e1.pk, 'tan': 'BLRA12345B'})
        doc = EmployeeDocument.objects.get(employee=self.e1, doc_type='FORM16')
        self.assertEqual(doc.status, 'DRAFT')
        from pypdf import PdfReader
        text = ' '.join(p.extract_text() for p in PdfReader(io.BytesIO(bytes(doc.data))).pages)
        for needle in ('DRAFT', 'BLRA12345B', '80,000.00', 'Months included'):
            self.assertIn(needle, text)
        self.login(self.e1.user)
        self.assertEqual(self.client.get(reverse('employee_document_download', args=[doc.pk])).status_code, 404)
        self.login(self.owner)
        self.client.post(reverse('hr_document_action', args=[doc.pk]), {'action': 'publish'})
        self.login(self.e1.user)
        self.assertEqual(self.client.get(reverse('employee_document_download', args=[doc.pk])).status_code, 200)

    def test_employee_cannot_generate(self):
        self.login(self.e1.user)
        self.assertEqual(self.client.get(reverse('hr_form16')).status_code, 403)


class AnnouncementReadAndReimbursementTests(Base):
    def test_unread_count_clears_after_viewing(self):
        Announcement.objects.create(company=self.c1, title='Holiday', message='x')
        self.login(self.e1.user)
        self.assertContains(self.client.get(reverse('employee_home')), '1 new')
        self.client.get(reverse('announcements'))
        self.assertNotContains(self.client.get(reverse('employee_home')), '1 new')

    def test_reimbursement_comment(self):
        r = Reimbursement.objects.create(employee=self.e1, category='TRAVEL', amount=500, date=datetime.date(2026, 9, 1))
        self.login(self.owner)
        self.client.post(reverse('reimbursement_decision', args=[r.pk]), {'decision': 'REJECTED', 'comment': 'No bill'})
        r.refresh_from_db()
        self.assertEqual((r.status, r.approver_comment), ('REJECTED', 'No bill'))
        self.assertTrue(Notification.objects.filter(recipient=self.e1.user, message__icontains='No bill').exists())