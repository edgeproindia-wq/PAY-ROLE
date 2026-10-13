"""Stage 3 hardening: tax year follows the payroll month, loans close only on released payroll, uploads must be real files,
Full & Final suggests the pending salary."""
import datetime
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .employee_portal import DocumentUploadForm
from .forms import ReimbursementForm
from .models import Employee, FullFinalSettlement, PayrollRunLine, SalaryStructure, validate_proof_document
from .models_features import Loan, validate_attachment
from .payroll_engine import TAX_RULES, monthly_tds, parse_month, q, tax_year_for_period
from .phase5 import PersonalDocForm
from .settlement import suggest_pending_salary
from .tests import make_company, make_employee, make_user
from .upload_checks import check_file_matches_extension

D = Decimal
PDF = b'%PDF-1.4\n1 0 obj\n<<>>\nendobj\n'
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 20
JPG = b'\xff\xd8\xff\xe0' + b'\x00' * 20
HTML = b'<html><script>alert(1)</script></html>'


class TaxYearTests(TestCase):
    def test_a_month_in_a_configured_year_uses_that_year(self):
        self.assertEqual(tax_year_for_period(parse_month('September 2025')), '2025-26')
        self.assertEqual(tax_year_for_period(parse_month('March 2026')), '2025-26')

    def test_a_year_without_rules_uses_the_latest_earlier_year_and_warns(self):
        with self.assertLogs('payroll_app.payroll_engine', 'WARNING'):
            self.assertEqual(tax_year_for_period(parse_month('April 2030')), max(TAX_RULES))

    def test_the_financial_year_starts_in_april(self):
        with mock.patch.dict(TAX_RULES, {'2026-27': dict(TAX_RULES['2025-26'])}):
            self.assertEqual(tax_year_for_period(parse_month('April 2026')), '2026-27')
            self.assertEqual(tax_year_for_period(parse_month('March 2027')), '2026-27')
            self.assertEqual(tax_year_for_period(parse_month('March 2026')), '2025-26')

    def test_payroll_uses_the_rules_of_the_months_financial_year(self):
        co = make_company('Tax Co')
        owner = make_user('taxowner', 'COMPANY_OWNER', co)
        e = make_employee(co, 'TX1', 'tx1@example.com')
        SalaryStructure.objects.create(employee=e, basic=100000, hra=40000, conveyance=0, special_allowance=60000)
        self.client.force_login(owner)
        changed = dict(TAX_RULES['2025-26'], standard_deduction=D('0'))          # pretend 2026-27 had no standard deduction
        with mock.patch.dict(TAX_RULES, {'2026-27': changed}):
            expected_sept = monthly_tds(D('2400000'), '2026-27')
            for month in ('March 2026', 'September 2026'):
                self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': month}, follow=True)
        march = PayrollRunLine.objects.get(payroll_run__month='March 2026', employee=e)
        sept = PayrollRunLine.objects.get(payroll_run__month='September 2026', employee=e)
        self.assertEqual(march.tds, monthly_tds(D('2400000'), '2025-26'))
        self.assertEqual(sept.tds, expected_sept)
        self.assertGreater(sept.tds, march.tds)


class LoanClosingTests(TestCase):
    def setUp(self):
        self.co = make_company('Loan Co')
        self.owner = make_user('loanowner', 'COMPANY_OWNER', self.co)
        self.e = make_employee(self.co, 'LN1', 'ln1@example.com')
        SalaryStructure.objects.create(employee=self.e, basic=20000, hra=8000, conveyance=1600, special_allowance=10400)
        self.loan = Loan.objects.create(company=self.co, employee=self.e, reference_no='L9', loan_amount=10000,
                                        tenure_months=2, emi_amount=5000, start_date=datetime.date(2026, 9, 1))
        self.client.force_login(self.owner)

    def make_run(self, month):
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': month}, follow=True)
        return PayrollRunLine.objects.get(payroll_run__month=month, employee=self.e)

    def test_loan_covered_only_by_drafts_stays_open_and_deducts_again_if_a_draft_loses_its_EMI(self):
        self.make_run('September 2026')
        self.make_run('October 2026')
        nov = self.make_run('November 2026')
        self.assertEqual(nov.loan_deduction, D('0.00'))
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'ACTIVE')                      # not closed on the strength of drafts
        PayrollRunLine.objects.filter(payroll_run__month='October 2026', employee=self.e).delete()
        self.client.post(reverse('payroll_combined'), {'action': 'reprocess', 'run_id': nov.payroll_run_id}, follow=True)
        nov = PayrollRunLine.objects.get(payroll_run__month='November 2026', employee=self.e)
        self.assertEqual(nov.loan_deduction, D('5000.00'))                # the missing EMI is taken after all


def upload(name, content):
    return SimpleUploadedFile(name, content)


class UploadSignatureTests(TestCase):
    def test_real_files_pass(self):
        for name, data in (('a.pdf', PDF), ('a.png', PNG), ('a.jpg', JPG), ('a.jpeg', JPG)):
            check_file_matches_extension(upload(name, data))

    def test_pdf_with_a_few_leading_bytes_passes(self):
        check_file_matches_extension(upload('a.pdf', b'\r\n  ' + PDF))

    def test_disguised_files_are_refused(self):
        for name, data in (('a.pdf', HTML), ('a.png', HTML), ('a.jpg', PNG), ('a.pdf', PNG), ('a.pdf', b'')):
            with self.assertRaises(ValidationError, msg=name):
                check_file_matches_extension(upload(name, data))

    def test_every_upload_form_uses_the_check(self):
        for validator in (validate_attachment, validate_proof_document):
            validator(upload('ok.pdf', PDF))
            with self.assertRaises(ValidationError):
                validator(upload('bad.pdf', HTML))
        form = PersonalDocForm({'doc_type': 'ID_PROOF'}, {'file': upload('bad.pdf', HTML)})
        self.assertIn('file', form.errors)
        self.assertTrue(PersonalDocForm({'doc_type': 'ID_PROOF'}, {'file': upload('ok.pdf', PDF)}).is_valid())
        co = make_company('Up Co')
        e = make_employee(co, 'UP1', 'up1@example.com')
        data = {'employee': e.pk, 'doc_type': 'FORM16', 'financial_year': '2025-26'}
        self.assertIn('file', DocumentUploadForm(co, data, {'file': upload('bad.pdf', HTML)}).errors)
        self.assertNotIn('file', DocumentUploadForm(co, data, {'file': upload('ok.pdf', PDF)}).errors)
        rform = ReimbursementForm({'employee': e.pk}, {'receipt': upload('bad.pdf', HTML)}, employee_queryset=Employee.objects.all())
        self.assertIn('receipt', rform.errors)


class FullFinalTests(TestCase):
    def setUp(self):
        self.co = make_company('Settle Co')
        self.owner = make_user('settleowner', 'COMPANY_OWNER', self.co)
        self.e = make_employee(self.co, 'FF1', 'ff1@example.com')
        Employee.objects.filter(pk=self.e.pk).update(date_of_joining=datetime.date(2025, 1, 1))
        self.e.refresh_from_db()
        SalaryStructure.objects.create(employee=self.e, basic=20000, hra=8000, conveyance=1600, special_allowance=1400)       # gross 31,000
        self.client.force_login(self.owner)
        self.last_day = datetime.date(2026, 9, 10)

    def post(self, **extra):
        data = {'employee': self.e.pk, 'last_working_day': self.last_day.isoformat()}
        data.update(extra)
        return self.client.post(reverse('full_final_settlement'), data, follow=True)

    def test_pending_salary_is_calculated_when_left_empty(self):
        self.post()
        rec = FullFinalSettlement.objects.get(employee=self.e)
        self.assertEqual(rec.pending_salary, q(D('31000') / 30 * 10))            # 10 of September's 30 days
        self.assertEqual((rec.leave_encashment, rec.deductions), (0, 0))

    def test_an_amount_typed_by_hand_is_kept(self):
        self.post(pending_salary='5000', leave_encashment='1000', deductions='200')
        rec = FullFinalSettlement.objects.get(employee=self.e)
        self.assertEqual((rec.pending_salary, rec.leave_encashment, rec.deductions), (D('5000.00'), D('1000.00'), D('200.00')))

    def test_absent_days_reduce_the_suggestion(self):
        from .models import Attendance
        Attendance.objects.create(employee=self.e, date=datetime.date(2026, 9, 3), status='ABSENT')
        amount, note = suggest_pending_salary(self.e, self.last_day)
        self.assertEqual(amount, q(D('31000') / 30 * 9))
        self.assertIn('loss-of-pay', note)

    def test_a_month_already_paid_adds_no_pending_salary(self):
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': 'September 2026'}, follow=True)
        amount, note = suggest_pending_salary(self.e, self.last_day)
        self.assertEqual(amount, D('0.00'))
        self.assertIn('already in a payroll run', note)

    def test_duplicates_future_dates_and_dates_before_joining_are_refused(self):
        self.post()
        self.post()
        self.assertEqual(FullFinalSettlement.objects.filter(employee=self.e).count(), 1)
        other = make_employee(self.co, 'FF2', 'ff2@example.com')
        SalaryStructure.objects.create(employee=other, basic=20000, hra=0, conveyance=0, special_allowance=0)
        future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        self.client.post(reverse('full_final_settlement'), {'employee': other.pk, 'last_working_day': future})
        Employee.objects.filter(pk=other.pk).update(date_of_joining=datetime.date(2026, 9, 20))
        self.client.post(reverse('full_final_settlement'), {'employee': other.pk, 'last_working_day': '2026-09-10'})
        self.assertFalse(FullFinalSettlement.objects.filter(employee=other).exists())