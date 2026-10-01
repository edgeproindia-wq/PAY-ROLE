"""Phase-3 regression tests: leave cancel/comments, grievances, announcements,
investment-proof downloads, demo-request CRM + email, PT / loans / insurance in
payroll and payslip, removed mock-up route, and XSS in the new pages."""
import datetime
import io
from decimal import Decimal

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import DemoRequest, InvestmentDeclaration, LeaveRequest, Notification, PayrollRun, SalaryStructure, User
from .models_features import (Announcement, DemoRequestActivity, Grievance, InsurancePolicy, Loan, LoanRepayment,
                              ProfessionalTaxSlab)
from .tests_audit import PWD, company, employee, user

XSS = '<script>alert(1)</script>'


def pdf_file(name='proof.pdf'):
    return SimpleUploadedFile(name, b'%PDF-1.4 test', content_type='application/pdf')


class Base(TestCase):
    def setUp(self):
        self.c1, self.c2 = company('Acme'), company('Beta')
        self.admin = User.objects.create_superuser('root', 'root@ex.com', PWD)
        self.owner = user('own', 'COMPANY_OWNER', self.c1)
        self.owner2 = user('own2', 'COMPANY_OWNER', self.c2)
        self.e1 = employee(self.c1, 'A1', 'a1@ex.com', login='emp1')
        self.e2 = employee(self.c1, 'A2', 'a2@ex.com', login='emp2')
        self.b1 = employee(self.c2, 'B1', 'b1@ex.com', login='empb')
        for e in (self.e1, self.e2, self.b1):
            SalaryStructure.objects.create(employee=e, basic=20000, hra=8000, conveyance=1600, special_allowance=10400)

    def login(self, u):
        self.client.cookies.clear()      # like a separate browser: no flash messages carried over
        self.client.force_login(u)

    def run_and_release(self, month='September 2026', owner=None):
        self.login(owner or self.owner)
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': month})
        run = PayrollRun.objects.get(company=(owner or self.owner).company, month__iexact=month)
        for a in ('validate', 'approve', 'release'):
            self.client.post(reverse('payroll_combined'), {'action': a, 'run_id': run.pk})
        run.refresh_from_db()
        return run


class LeaveTests(Base):
    def _leave(self, status='PENDING', start=None):
        start = start or timezone.localdate() + datetime.timedelta(days=10)
        return LeaveRequest.objects.create(employee=self.e1, leave_type='CASUAL', from_date=start,
                                           to_date=start + datetime.timedelta(days=1), status=status)

    def test_employee_cancels_pending_and_future_approved_leave_and_owner_is_notified(self):
        for status in ('PENDING', 'APPROVED'):
            lv = self._leave(status)
            self.login(self.e1.user)
            self.client.post(reverse('leave_cancel', args=[lv.pk]), {'reason': 'plans changed'})
            lv.refresh_from_db()
            self.assertEqual(lv.status, 'CANCELLED')
            self.assertEqual(lv.cancellation_reason, 'plans changed')
            self.assertIsNotNone(lv.cancelled_at)
        self.assertTrue(Notification.objects.filter(recipient=self.owner, message__icontains='cancelled').exists())

    def test_started_approved_leave_and_other_employees_leave_cannot_be_cancelled(self):
        started = self._leave('APPROVED', start=timezone.localdate() - datetime.timedelta(days=1))
        self.login(self.e1.user)
        self.client.post(reverse('leave_cancel', args=[started.pk]))
        started.refresh_from_db()
        self.assertEqual(started.status, 'APPROVED')
        mine = self._leave()
        self.login(self.e2.user)
        self.assertEqual(self.client.post(reverse('leave_cancel', args=[mine.pk])).status_code, 404)
        self.login(self.owner)
        self.assertEqual(self.client.post(reverse('leave_cancel', args=[mine.pk])).status_code, 403)

    def test_cancelled_leave_frees_balance_for_reapplication(self):
        lv = self._leave()
        self.login(self.e1.user)
        self.client.post(reverse('leave_cancel', args=[lv.pk]))
        resp = self.client.post(reverse('leave_management'), {'leave_type': 'CASUAL', 'from_date': lv.from_date,
                                                             'to_date': lv.to_date, 'reason': 'again'})
        self.assertEqual(LeaveRequest.objects.filter(employee=self.e1, status='PENDING').count(), 1, resp.status_code)

    def test_approver_comment_is_saved_and_sent_to_employee(self):
        lv = self._leave()
        self.login(self.owner)
        self.client.post(reverse('leave_decision', args=[lv.pk]), {'decision': 'REJECTED', 'comment': 'Month-end close'})
        lv.refresh_from_db()
        self.assertEqual((lv.status, lv.approver_comment), ('REJECTED', 'Month-end close'))
        self.assertTrue(Notification.objects.filter(recipient=self.e1.user, message__icontains='Month-end close').exists())


class GrievanceTests(Base):
    def raise_one(self, u=None):
        self.login(u or self.e1.user)
        self.client.post(reverse('grievances'), {'category': 'PAYROLL', 'subject': 'PF missing ' + XSS,
                                                 'department': 'Finance', 'description': 'Details', 'attachment': pdf_file()})
        return Grievance.objects.latest('pk')

    def test_raise_track_and_owner_respond(self):
        g = self.raise_one()
        self.assertRegex(g.tracking_id, r'^GRV-\d{4}-\d{5}$')
        self.assertEqual((g.company, g.status), (self.c1, 'OPEN'))
        self.assertTrue(Notification.objects.filter(recipient=self.owner, message__icontains=g.tracking_id).exists())
        self.login(self.owner)
        self.client.post(reverse('grievance_detail', args=[g.pk]), {'status': 'RESOLVED', 'message': 'Fixed in Sept',
                                                                    'assigned_to': self.owner.pk})
        g.refresh_from_db()
        self.assertEqual((g.status, g.assigned_to), ('RESOLVED', self.owner))
        self.assertEqual(g.updates.count(), 2)
        self.assertTrue(Notification.objects.filter(recipient=self.e1.user, message__icontains=g.tracking_id).exists())
        self.login(self.e1.user)
        page = self.client.get(reverse('grievance_detail', args=[g.pk]))
        self.assertContains(page, 'Fixed in Sept')
        self.assertNotContains(page, XSS)                          # escaped
        self.assertEqual(self.client.get(reverse('grievance_attachment', args=[g.pk])).status_code, 200)

    def test_isolation(self):
        g = self.raise_one()
        for u in (self.e2.user, self.owner2):
            self.login(u)
            self.assertEqual(self.client.get(reverse('grievance_detail', args=[g.pk])).status_code, 404)
            self.assertEqual(self.client.get(reverse('grievance_attachment', args=[g.pk])).status_code, 404)
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse('grievance_detail', args=[g.pk])).status_code, 403)
        self.login(self.owner2)
        self.assertNotContains(self.client.get(reverse('grievances')), g.tracking_id)

    def test_employee_cannot_change_status(self):
        g = self.raise_one()
        self.client.post(reverse('grievance_detail', args=[g.pk]), {'status': 'CLOSED', 'message': 'hi'})
        g.refresh_from_db()
        self.assertEqual(g.status, 'OPEN')

    def test_bad_attachment_rejected(self):
        self.login(self.e1.user)
        self.client.post(reverse('grievances'), {'category': 'OTHER', 'subject': 's', 'description': 'd',
                                                 'attachment': SimpleUploadedFile('x.exe', b'MZ')})
        self.assertFalse(Grievance.objects.exists())


class AnnouncementTests(Base):
    def test_publish_notifies_and_employees_see_only_active_own_company(self):
        self.login(self.owner)
        today = timezone.localdate()
        self.client.post(reverse('announcements'), {'title': 'Diwali ' + XSS, 'message': 'Office closed', 'audience': 'ALL',
                                                    'publish_date': today})
        Announcement.objects.create(company=self.c1, title='Old news', message='x', publish_date=today - datetime.timedelta(days=9),
                                    expiry_date=today - datetime.timedelta(days=1))
        Announcement.objects.create(company=self.c2, title='Beta only', message='x')
        self.assertTrue(Notification.objects.filter(recipient=self.e1.user, message__icontains='Diwali').exists())
        self.login(self.e1.user)
        page = self.client.get(reverse('announcements'))
        self.assertContains(page, 'Diwali')
        self.assertNotContains(page, XSS)
        self.assertNotContains(page, 'Old news')
        self.assertNotContains(page, 'Beta only')
        self.assertContains(self.client.get(reverse('employee_home')), 'Diwali')

    def test_employee_cannot_publish(self):
        self.login(self.e1.user)
        self.client.post(reverse('announcements'), {'title': 't', 'message': 'm', 'audience': 'ALL', 'publish_date': '2026-01-01'})
        self.assertFalse(Announcement.objects.exists())


class ProofTests(Base):
    def test_proof_download_is_scoped(self):
        d = InvestmentDeclaration.objects.create(employee=self.e1, section='80C', investment_type='PPF', declared_amount=1000, financial_year='2026-27',
                                                 proof_document=pdf_file())
        for u, allowed in ((self.e1.user, True), (self.owner, True), (self.e2.user, False), (self.owner2, False), (self.admin, False)):
            self.login(u)
            code = self.client.get(reverse('declaration_proof', args=[d.pk])).status_code
            if allowed:
                self.assertEqual(code, 200, u.username)
            else:
                self.assertIn(code, (403, 404), u.username)
        self.login(self.owner)
        self.assertContains(self.client.get(reverse('hr_declarations')), '80C')


@override_settings(SMS_PROVIDER='', DEMO_NOTIFY_EMAIL='admin-alerts@example.com')
class DemoCrmTests(Base):
    def submit(self):
        self.client.post(reverse('request_demo'), {'full_name': 'Ravi', 'company_name': 'DemoCo', 'email': 'ravi@democo.com',
                                                   'phone': '9876543210', 'team_size': '50', 'industry': 'MANUFACTURING',
                                                   'modules_choice': ['PAYROLL', 'ATTENDANCE'], 'message': 'Need it'})
        return DemoRequest.objects.get()

    def test_request_id_fields_and_email(self):
        d = self.submit()
        self.assertRegex(d.request_code, r'^DR-\d{4}-\d{5}$')
        self.assertEqual(d.industry, 'MANUFACTURING')
        self.assertIn('Payroll', d.modules)
        msg = mail.outbox[-1]
        self.assertEqual(msg.to, ['admin-alerts@example.com'])
        self.assertIn(d.request_code, msg.subject)
        self.assertIn(d.request_code, msg.body)
        self.assertIn('Payroll', msg.body)

    def test_admin_search_assign_status_followup_history(self):
        d = self.submit()
        self.login(self.admin)
        self.assertContains(self.client.get(reverse('admin_demo_requests'), {'q': d.request_code}), 'DemoCo')
        self.assertNotContains(self.client.get(reverse('admin_demo_requests'), {'q': 'nomatch'}), 'DemoCo')
        self.client.post(reverse('admin_demo_detail', args=[d.pk]), {'status': 'DEMO_SCHEDULED', 'assigned_to': self.admin.pk,
                                                                     'follow_up_date': '2026-10-15', 'note': 'Call Friday'})
        d.refresh_from_db()
        self.assertEqual((d.status, d.assigned_to, str(d.follow_up_date)), ('DEMO_SCHEDULED', self.admin, '2026-10-15'))
        a = DemoRequestActivity.objects.get(demo=d)
        self.assertIn('Demo scheduled', a.action)
        self.assertEqual(a.note, 'Call Friday')
        self.assertContains(self.client.get(reverse('admin_demo_requests'), {'status': 'DEMO_SCHEDULED'}), d.request_code)

    def test_non_admin_blocked(self):
        d = self.submit()
        for u in (self.owner, self.e1.user):
            self.login(u)
            self.assertEqual(self.client.get(reverse('admin_demo_detail', args=[d.pk])).status_code, 403)
            self.assertEqual(self.client.get(reverse('admin_demo_requests')).status_code, 403)

    def test_email_failure_keeps_request(self):
        with override_settings(EMAIL_BACKEND='payroll_app.tests_audit.BrokenEmailBackend'):
            try:
                self.submit()
            except Exception:
                pass
        d = DemoRequest.objects.get()
        self.assertFalse(d.email_notified)
        self.assertTrue(d.request_code)


class PayrollExtrasTests(Base):
    def test_pt_loan_insurance_deducted_and_on_payslip(self):
        ProfessionalTaxSlab.objects.create(company=self.c1, min_monthly_gross=25000, monthly_amount=200, february_amount=300)
        loan = Loan.objects.create(company=self.c1, employee=self.e1, reference_no='L-1', loan_amount=5000, tenure_months=2,
                                   emi_amount=3000, start_date=datetime.date(2026, 9, 1))
        InsurancePolicy.objects.create(company=self.c1, employee=self.e1, provider='Star', policy_number='P1',
                                       employee_premium_monthly=500, effective_from=datetime.date(2026, 1, 1))
        run = self.run_and_release('September 2026')
        l1, l2 = run.lines.get(employee=self.e1), run.lines.get(employee=self.e2)
        self.assertEqual((l1.professional_tax, l1.loan_deduction, l1.insurance_deduction), (Decimal('200.00'), Decimal('3000.00'), Decimal('500.00')))
        self.assertEqual(l1.total_deductions, l1.pf + l1.esi + l1.tds + Decimal('3700.00'))
        self.assertEqual(l1.net_pay, l1.total_earnings - l1.total_deductions)
        self.assertEqual((l2.loan_deduction, l2.insurance_deduction), (Decimal('0.00'), Decimal('0.00')))
        self.assertEqual(loan.balance, Decimal('2000.00'))
        run2 = self.run_and_release('October 2026')
        self.assertEqual(run2.lines.get(employee=self.e1).loan_deduction, Decimal('2000.00'))   # capped at remaining
        self.assertEqual(Loan.objects.get().balance, Decimal('0.00'))
        run3 = self.run_and_release('November 2026')
        self.assertEqual(run3.lines.get(employee=self.e1).loan_deduction, Decimal('0.00'))
        self.login(self.e1.user)
        pdf = self.client.get(reverse('payslip_pdf', args=[l1.pk]))
        from pypdf import PdfReader
        text = ' '.join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
        for label in ('Professional Tax', 'Loan EMI', 'Insurance'):
            self.assertIn(label, text)

    def test_february_pt_and_reprocessing_draft_does_not_double_count_loan(self):
        ProfessionalTaxSlab.objects.create(company=self.c1, min_monthly_gross=25000, monthly_amount=200, february_amount=300)
        Loan.objects.create(company=self.c1, employee=self.e1, reference_no='L-2', loan_amount=10000, tenure_months=5,
                            emi_amount=2000, start_date=datetime.date(2027, 1, 1))
        self.login(self.owner)
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': 'February 2027'})
        run = PayrollRun.objects.get(company=self.c1, month__iexact='February 2027')
        self.client.post(reverse('payroll_combined'), {'action': 'reprocess', 'run_id': run.pk})
        line = run.lines.get(employee=self.e1)
        self.assertEqual(line.professional_tax, Decimal('300.00'))
        self.assertEqual(LoanRepayment.objects.filter(line__payroll_run=run).count(), 1)

    def test_no_configuration_means_no_change(self):
        run = self.run_and_release('September 2026')
        l = run.lines.get(employee=self.e1)
        self.assertEqual((l.professional_tax, l.loan_deduction, l.insurance_deduction), (0, 0, 0))

    def test_hr_pages_scoped_and_owner_only(self):
        loan = Loan.objects.create(company=self.c1, employee=self.e1, reference_no='L-3', loan_amount=1000, tenure_months=1,
                                   emi_amount=1000, start_date=datetime.date(2026, 9, 1))
        self.login(self.owner2)
        self.assertNotContains(self.client.get(reverse('hr_loans')), 'L-3')
        self.client.post(reverse('hr_loans'), {'close': loan.pk})
        loan.refresh_from_db()
        self.assertEqual(loan.status, 'ACTIVE')
        resp = self.client.post(reverse('hr_loans'), {'employee': self.e1.pk, 'reference_no': 'X', 'loan_amount': 1,
                                                      'tenure_months': 1, 'emi_amount': 1, 'start_date': '2026-09-01'})
        self.assertFalse(Loan.objects.filter(reference_no='X').exists())
        for u in (self.e1.user, self.admin):
            self.login(u)
            for name in ('hr_loans', 'hr_insurance', 'hr_professional_tax', 'hr_declarations'):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)
        self.login(self.e1.user)
        self.assertContains(self.client.get(reverse('employee_home')), 'L-3')


class RouteTests(Base):
    def test_removed_mockup_page_is_404(self):
        self.assertEqual(self.client.get('/page/Staffing module mobile ui mockup/').status_code, 404)

    def test_new_pages_load_for_right_roles(self):
        pages = {self.owner: ['grievances', 'announcements', 'hr_loans', 'hr_insurance', 'hr_professional_tax',
                              'hr_declarations', 'hr_documents'],
                 self.e1.user: ['employee_home', 'grievances', 'announcements']}
        for u, names in pages.items():
            self.login(u)
            for n in names:
                self.assertEqual(self.client.get(reverse(n)).status_code, 200, n)