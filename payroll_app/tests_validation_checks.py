"""Checks before a payroll run is validated, the net salary in words, and the new dashboard figures."""
import datetime
from decimal import Decimal as D

from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from .amount_words import rupees_in_words
from .models import Attendance, BankPayment, PayrollRun, PayrollRunLine, SalaryStructure
from .pay_cycles import move_cycle
from .pay_cycle_models import PayCycle
from .payroll_checks import ERROR, WARNING, run_checks
from .pdf import build_payslip_pdf
from .tests import make_company, make_employee, make_user


class WordsTests(SimpleTestCase):
    def test_indian_style_amounts(self):
        cases = {'0': 'Rupees Zero Only', '1': 'Rupees One Only', '19': 'Rupees Nineteen Only', '99': 'Rupees Ninety Nine Only',
                 '105': 'Rupees One Hundred Five Only', '46400': 'Rupees Forty Six Thousand Four Hundred Only',
                 '100000': 'Rupees One Lakh Only', '10000000': 'Rupees One Crore Only',
                 '1234567.89': 'Rupees Twelve Lakh Thirty Four Thousand Five Hundred Sixty Seven and Eighty Nine Paise Only',
                 '123456789': 'Rupees Twelve Crore Thirty Four Lakh Fifty Six Thousand Seven Hundred Eighty Nine Only',
                 '25000.50': 'Rupees Twenty Five Thousand and Fifty Paise Only', '0.05': 'Rupees Zero and Five Paise Only',
                 '-1500': 'Minus Rupees One Thousand Five Hundred Only'}
        for amount, words in cases.items():
            self.assertEqual(rupees_in_words(D(amount)), words, amount)

    def test_rounding_to_paise(self):
        self.assertEqual(rupees_in_words(D('10.005')), 'Rupees Ten and One Paise Only')


class Base(TestCase):
    def setUp(self):
        self.co = make_company('Check Co')
        self.owner = make_user('chk_owner', 'COMPANY_OWNER', self.co)
        self.client.force_login(self.owner)

    def employee(self, code, bank='1234567890', ifsc='HDFC0000001', pan='ABCDE1234F', basic=30000, structure=True):
        e = make_employee(self.co, code, code.lower() + '@example.com')
        e.date_of_joining = datetime.date(2024, 1, 1)
        e.bank_account_no, e.ifsc_code, e.pan_number = bank, ifsc, pan
        e.save()
        if structure:
            SalaryStructure.objects.create(employee=e, basic=basic, hra=basic * D('0.4'), conveyance=1600, special_allowance=6400)
        return e

    def make_run(self, month='November 2026'):
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': month})
        return PayrollRun.objects.filter(company=self.co, month=month).latest('pk')

    def validate(self, run):
        r = self.client.post(reverse('payroll_combined'), {'action': 'validate', 'run_id': run.pk}, follow=True)
        run.refresh_from_db()
        return [(m.level_tag, str(m)) for m in r.context['messages']]

    def codes(self, run):
        return {i.code: i for i in run_checks(run)}


class CheckTests(Base):
    def test_a_clean_run_has_no_issues_and_validates(self):
        self.employee('K1')
        run = self.make_run()
        self.assertEqual(run_checks(run), [])
        self.validate(run)
        self.assertEqual(run.status, 'VALIDATED')

    def test_negative_net_is_an_error_and_stops_validation(self):
        self.employee('K1')
        run = self.make_run()
        PayrollRunLine.objects.filter(payroll_run=run).update(total_deductions=90000)
        self.assertEqual(self.codes(run)['NEGATIVE_NET'].level, ERROR)
        msgs = self.validate(run)
        self.assertEqual(run.status, 'DRAFT')
        self.assertTrue(any(level == 'error' and 'was not validated' in text for level, text in msgs), msgs)

    def test_warnings_are_shown_but_do_not_stop_validation(self):
        self.employee('K1', bank='', ifsc='', pan='')
        run = self.make_run()
        found = self.codes(run)
        self.assertEqual((found['NO_BANK'].level, found['NO_PAN'].level), (WARNING, WARNING))
        msgs = self.validate(run)
        self.assertEqual(run.status, 'VALIDATED')
        self.assertTrue(any(level == 'warning' and 'no complete bank details' in text and 'K1' in text for level, text in msgs), msgs)

    def test_two_employees_on_one_bank_account(self):
        self.employee('K1', bank='5555666677')
        self.employee('K2', bank='5555666677')
        self.assertIn('SHARED_ACCOUNT', self.codes(self.make_run()))

    def test_an_active_employee_with_no_salary_structure_is_named(self):
        self.employee('K1')
        self.employee('K2', structure=False)
        issue = self.codes(self.make_run())['NOT_IN_RUN']
        self.assertIn('K2', issue.message)

    def test_attendance_gaps_are_only_checked_when_the_company_records_attendance(self):
        a, b = self.employee('K1'), self.employee('K2')
        run = self.make_run()
        self.assertNotIn('NO_ATTENDANCE', self.codes(run))                    # nobody's attendance is recorded: not checked
        Attendance.objects.create(employee=a, date=datetime.date(2026, 11, 2), status='PRESENT')
        issue = self.codes(run)['NO_ATTENDANCE']
        self.assertIn('K2', issue.message)
        self.assertNotIn('K1', issue.message)

    def test_a_big_swing_from_the_previous_run_is_flagged(self):
        e = self.employee('K1', basic=30000)
        first = self.make_run('October 2026')
        SalaryStructure.objects.filter(employee=e).update(basic=60000, hra=24000)
        # same company, a later run with a much higher net
        PayrollRun.objects.filter(pk=first.pk).update(status='RELEASED')
        second = self.make_run('November 2026')
        PayrollRunLine.objects.filter(payroll_run=second).update(net_pay=D('90000.00'))
        issue = self.codes(second)['VARIANCE']
        self.assertIn('higher than October 2026', issue.message)

    def test_the_pay_cycle_validate_runs_the_same_checks(self):
        self.employee('K1')
        run = self.make_run()
        PayrollRunLine.objects.filter(payroll_run=run).update(total_deductions=90000)
        cycle = PayCycle.objects.create(company=self.co, name='Nov cycle', cycle_type='MONTHLY', payroll_month='November 2026',
                                        start_date=datetime.date(2026, 11, 1), end_date=datetime.date(2026, 11, 30), payroll_run=run, created_by=self.owner)
        request = RequestFactory().post('/')
        request.user, request.session = self.owner, {}
        request._messages = FallbackStorage(request)
        ok, text = move_cycle(cycle, 'validate', request)
        self.assertFalse(ok)
        self.assertIn('deductions', text)
        run.refresh_from_db()
        self.assertEqual(run.status, 'DRAFT')


class PayslipWordsAndDashboardTests(Base):
    def test_the_payslip_page_and_pdf_show_the_net_pay_in_words(self):
        self.employee('K1')
        run = self.make_run()
        PayrollRun.objects.filter(pk=run.pk).update(status='RELEASED')
        line = run.lines.get()
        page = self.client.get(reverse('payslip_detail', args=[line.pk]))
        self.assertContains(page, 'In words:')
        self.assertContains(page, rupees_in_words(line.net_pay))
        self.assertTrue(build_payslip_pdf(line).startswith(b'%PDF'))

    def test_dashboard_shows_deductions_lop_failed_payments_joiners_and_change(self):
        a, b = self.employee('K1'), self.employee('K2')
        a.employment_status = 'RESIGNED'
        a.save()
        first = self.make_run('October 2026')
        second = self.make_run('November 2026')
        PayrollRunLine.objects.filter(payroll_run=first).update(net_pay=D('50000.00'))
        PayrollRunLine.objects.filter(payroll_run=second, employee=b).update(net_pay=D('55000.00'), lop_days=D('2'), total_deductions=D('4000.00'))
        BankPayment.objects.create(payroll_line=PayrollRunLine.objects.filter(payroll_run=second).first(), company=self.co, amount=D('100'),
                                   bank_name='X', account_holder_name='Y', account_no='1', ifsc_code='Z', status='FAILED')
        page = self.client.get(reverse('dashboard'))
        self.assertContains(page, 'Total Deductions')
        self.assertContains(page, 'Employees with LOP')
        self.assertContains(page, 'Failed Salary Payments')
        ctx = page.context
        self.assertEqual(ctx['lop_people'], 1)
        self.assertEqual(ctx['failed_payments'], 1)
        self.assertEqual(ctx['exited_employees'], 1)
        self.assertEqual(ctx['previous_run'], first)
        self.assertGreater(ctx['latest_deductions'], D('4000.00') - D('0.01'))
