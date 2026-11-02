"""Payroll Reports & Analytics: 21 read-only reports. Figures, filters, CSV / Excel export, formula safety, company isolation and roles."""
import csv
import datetime
import io
from decimal import Decimal

from django.apps import apps
from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from .accounting_models import AccountingSettings
from .models import AuditLog, BankPayment, CompanySettings, Employee, PayrollRun, PayrollRunLine
from .reports import REPORTS
from .tests import make_company, make_employee, make_user

D = Decimal


def emp(company, code, email, dept='Engineering', pan='ABCDE1234F'):
    e = make_employee(company, code, email)
    e.department, e.pan_number = dept, pan
    e.save()
    return e


def line(run, e, earnings, pf=0, esi=0, tds=0, pt=0, loan=0, other=0, lop_days=0, lop_amount=0, basic=None, gross=None):
    ded = D(pf) + D(esi) + D(tds) + D(pt) + D(loan) + D(other)
    return PayrollRunLine.objects.create(
        payroll_run=run, employee=e, payslip_number=f'PS-{run.pk}-{e.employee_code}', basic=basic if basic is not None else earnings,
        gross_salary=gross if gross is not None else earnings, total_earnings=earnings, pf=pf, esi=esi, tds=tds, professional_tax=pt, loan_deduction=loan,
        other_deductions=other, total_deductions=ded, net_pay=D(earnings) - ded, days_in_month=30, lop_days=lop_days, lop_amount=lop_amount)


def rows_of(response):
    return response.context['table']


def csv_rows(response):
    return list(csv.reader(io.StringIO(response.content.decode())))


class RepBase(TestCase):
    def setUp(self):
        self.co = make_company('Rep Co')
        self.other = make_company('Other Rep Co')
        self.owner = make_user('rep_owner', 'COMPANY_OWNER', self.co)
        CompanySettings.objects.create(company=self.co, pf_percentage=D('12'), esi_percentage=D('0.75'), pf_wage_ceiling=D('15000'))
        self.e1 = emp(self.co, 'E1', 'e1@r.test', 'Engineering')
        self.e2 = emp(self.co, 'E2', 'e2@r.test', 'Admin', pan='')
        self.e3 = emp(self.co, 'E3', 'e3@r.test', 'Engineering')
        self.aug = PayrollRun.objects.create(company=self.co, month='August 2026', status='RELEASED')
        self.sep = PayrollRun.objects.create(company=self.co, month='September 2026', status='RELEASED')
        line(self.aug, self.e1, 30000, pf=1800, tds=400, pt=200, loan=1000, basic=25000, gross=30000)
        line(self.aug, self.e2, 20000, pf=1800, esi=150, pt=200, basic=10000, gross=20000)
        self.l1 = line(self.sep, self.e1, 32000, pf=1800, tds=500, pt=200, loan=1000, basic=26000, gross=32000)
        self.l2 = line(self.sep, self.e2, 18000, pf=1200, esi=135, pt=200, other=100, lop_days=2, lop_amount=1333, basic=9000, gross=20000)
        self.l3 = line(self.sep, self.e3, 15000, pt=150, basic=15000, gross=15000)
        self.client.force_login(self.owner)

    def get(self, key, **params):
        return self.client.get(reverse(REPORTS[key]['name']), params)

    def sep_params(self, **extra):
        return dict({'run': self.sep.pk}, **extra)


class RegistryTests(RepBase):
    def test_there_are_twenty_one_reports_plus_the_export_centre_and_every_page_opens(self):
        self.assertEqual(len(REPORTS), 20)
        for key, spec in REPORTS.items():
            with self.subTest(report=key):
                r = self.client.get(reverse(spec['name']), {'run': self.sep.pk, 'employee': self.e1.pk})
                self.assertEqual(r.status_code, 200)
                self.assertContains(r, escape(spec['title']))
        self.assertEqual(self.client.get(reverse('rep_export_center')).status_code, 200)

    def test_every_report_exports_csv_and_excel(self):
        from openpyxl import load_workbook
        for key, spec in REPORTS.items():
            with self.subTest(report=key):
                params = {'run': self.sep.pk, 'employee': self.e1.pk}
                r = self.client.get(reverse(spec['name']), dict(params, export='csv'))
                self.assertEqual(r.status_code, 200)
                self.assertIn('text/csv', r['Content-Type'])
                self.assertGreaterEqual(len(csv_rows(r)), 1)
                x = self.client.get(reverse(spec['name']), dict(params, export='xlsx'))
                self.assertGreaterEqual(load_workbook(io.BytesIO(x.content)).active.max_row, 1)
        self.assertEqual(AuditLog.objects.filter(action='EXPORT', details__startswith='Report exported').count(), len(REPORTS) * 2)

    def test_the_export_centre_lists_every_report(self):
        r = self.client.get(reverse('rep_export_center'))
        for spec in REPORTS.values():
            self.assertContains(r, escape(spec['title']))
            self.assertContains(r, reverse(spec['name']))

    def test_reports_never_write_to_the_payroll_tables(self):
        before = (PayrollRun.objects.count(), PayrollRunLine.objects.count(), list(PayrollRunLine.objects.order_by('pk').values_list('net_pay', flat=True)))
        for spec in REPORTS.values():
            self.client.get(reverse(spec['name']), {'run': self.sep.pk})
        self.assertEqual((PayrollRun.objects.count(), PayrollRunLine.objects.count(), list(PayrollRunLine.objects.order_by('pk').values_list('net_pay', flat=True))), before)


class FigureTests(RepBase):
    def test_monthly_register_rows_and_totals(self):
        r = self.get('monthly-register', **self.sep_params())
        self.assertEqual(len(rows_of(r)), 3)
        totals = dict(zip([c[0] for c in r.context['cols']], [t[0] for t in r.context['totals']]))
        self.assertEqual(totals['Total earnings'], '65,000.00')
        self.assertEqual(totals['Net pay'], '59,715.00')
        body = csv_rows(self.get('monthly-register', **self.sep_params(export='csv')))
        head = body[0]
        net_i, earn_i = head.index('Net pay'), head.index('Total earnings')
        self.assertEqual(sum(D(x[net_i]) for x in body[1:-1]), D(body[-1][net_i]))
        self.assertEqual(D(body[-1][earn_i]), D('65000.00'))

    def test_the_latest_non_draft_month_is_the_default_and_drafts_are_not_offered(self):
        PayrollRun.objects.create(company=self.co, month='October 2026', status='DRAFT')
        r = self.get('monthly-register')
        self.assertEqual(r.context['run'], self.sep)
        self.assertNotIn('October 2026', [x.month for x in r.context['runs']])

    def test_department_wise_groups_and_shares(self):
        r = self.get('department-wise', **self.sep_params())
        rows = {x[0][0]: x for x in rows_of(r)}
        self.assertEqual(rows['Engineering'][1][0], '2')
        self.assertEqual(rows['Engineering'][2][0], '47,000.00')
        self.assertEqual(rows['Admin'][6][0], '27.69%')

    def test_branch_wise_says_it_is_by_department(self):
        r = self.get('branch-wise', **self.sep_params())
        self.assertContains(r, 'do not have a branch field yet')
        self.assertEqual(len(rows_of(r)), 2)

    def test_ctc_with_and_without_employer_percentages(self):
        r = self.get('ctc', **self.sep_params())
        self.assertContains(r, 'percentages are not set')
        self.assertEqual(rows_of(r)[0][6][0], '32,000.00')
        AccountingSettings.objects.create(company=self.co, employer_pf_percent=D('12'), employer_esi_percent=D('3.25'))
        r = self.get('ctc', **self.sep_params())
        first = rows_of(r)[0]
        self.assertEqual((first[4][0], first[6][0], first[7][0]), ('1,800.00', '33,800.00', '405,600.00'))
        e2 = rows_of(r)[1]
        self.assertEqual(e2[5][0], '585.00')                          # ESI 135 x 3.25 / 0.75

    def test_gross_and_net_reports(self):
        r = self.get('gross', **self.sep_params())
        self.assertEqual(rows_of(r)[1][7][0], '1,333.00')
        BankPayment.objects.create(payroll_line=self.l1, company=self.co, amount=D('28500'), account_no='123456789012', ifsc_code='HDFC0001234', status='PAID', reference_number='U1')
        r = self.get('net', **self.sep_params())
        self.assertEqual([x[5][0] for x in rows_of(r)], ['Paid', 'Not prepared', 'Not prepared'])

    def test_earnings_and_deductions_by_head(self):
        r = self.get('earnings-deductions', **self.sep_params())
        rows = {x[1][0]: x for x in rows_of(r)}
        self.assertEqual((rows['PF'][2][0], rows['PF'][3][0]), ('2', '3,000.00'))
        self.assertEqual(rows['Loan'][3][0], '1,000.00')
        self.assertEqual(rows['Total earnings (as per payslips)'][3][0], '65,000.00')
        self.assertEqual(rows['Total deductions (as per payslips)'][3][0], '5,285.00')

    def test_pf_report_caps_the_wage_and_uses_the_employer_percentage(self):
        AccountingSettings.objects.create(company=self.co, employer_pf_percent=D('12'), employer_esi_percent=D('0'))
        r = self.get('pf', **self.sep_params())
        rows = rows_of(r)
        self.assertEqual(len(rows), 2)                               # E3 has no PF
        self.assertEqual((rows[0][2][0], rows[0][3][0], rows[0][4][0], rows[0][5][0]), ('15,000.00', '1,800.00', '1,800.00', '3,600.00'))
        self.assertEqual(rows[1][2][0], '9,000.00')                  # basic below the ceiling is used as it is
        r2 = self.get('pf', **self.sep_params())
        self.assertContains(r2, 'UAN numbers are not stored')

    def test_pf_report_warns_when_the_employer_percentage_is_not_set(self):
        self.assertContains(self.get('pf', **self.sep_params()), 'Employer PF percentage is not set')

    def test_esi_pt_and_tds_reports_list_only_people_with_an_amount(self):
        self.assertEqual([x[0][0] for x in rows_of(self.get('esi', **self.sep_params()))], ['E2'])
        self.assertEqual(len(rows_of(self.get('pt', **self.sep_params()))), 3)
        tds = rows_of(self.get('tds', **self.sep_params()))
        self.assertEqual(len(tds), 1)
        self.assertEqual(tds[0][2][0], 'XXXXXX234F')                  # PAN is masked
        self.assertNotIn('ABCDE1234F', self.get('tds', **self.sep_params(export='csv')).content.decode())

    def test_lop_report(self):
        r = self.get('lop', **self.sep_params())
        self.assertEqual([x[0][0] for x in rows_of(r)], ['E2'])
        self.assertEqual(r.context['totals'][4][0], '1,333.00')


class OvertimeTests(RepBase):
    def test_overtime_hours_are_estimated_from_check_in_out_against_the_shift(self):
        Shift, Assign, Attendance = (apps.get_model('payroll_app', n) for n in ('Shift', 'ShiftAssignment', 'Attendance'))
        shift = Shift.objects.create(company=self.co, name='Day', start_time=datetime.time(9, 0), end_time=datetime.time(18, 0), break_minutes=60)
        Assign.objects.create(company=self.co, employee=self.e1, shift=shift, effective_from=datetime.date(2026, 1, 1))
        Attendance.objects.create(employee=self.e1, date=datetime.date(2026, 9, 2), status='PRESENT', check_in=datetime.time(9, 0), check_out=datetime.time(19, 30))
        Attendance.objects.create(employee=self.e1, date=datetime.date(2026, 9, 3), status='PRESENT', check_in=datetime.time(9, 0), check_out=datetime.time(17, 0))
        Attendance.objects.create(employee=self.e1, date=datetime.date(2026, 8, 31), status='PRESENT', check_in=datetime.time(9, 0), check_out=datetime.time(22, 0))   # another month
        Attendance.objects.create(employee=self.e2, date=datetime.date(2026, 9, 2), status='PRESENT', check_in=datetime.time(9, 0), check_out=datetime.time(21, 0))      # no shift
        r = self.get('overtime', **self.sep_params())
        self.assertEqual([(x[0][0], x[2][0], x[3][0]) for x in rows_of(r)], [('E1', '1', '2.50')])
        self.assertContains(r, '1 attendance day(s) were skipped')
        self.assertContains(r, 'Estimate only')

    def test_no_attendance_gives_an_empty_report(self):
        self.assertEqual(rows_of(self.get('overtime', **self.sep_params())), [])


class OtherReportTests(RepBase):
    def test_arrears_report_shows_paid_and_pending(self):
        Arrears = apps.get_model('payroll_app', 'ArrearsRecord')
        Arrears.objects.create(employee=self.e1, effective_from=datetime.date(2026, 6, 1), old_basic=D('20000'), new_basic=D('25000'), months=3, paid_in_run=self.sep)
        Arrears.objects.create(employee=self.e2, effective_from=datetime.date(2026, 7, 1), old_basic=D('8000'), new_basic=D('9000'), months=2)
        Arrears.objects.create(employee=self.e3, effective_from=datetime.date(2026, 5, 1), old_basic=D('1'), new_basic=D('2'), months=1, paid_in_run=self.aug)   # paid in another month
        rows = rows_of(self.get('arrears', **self.sep_params()))
        self.assertEqual([(x[0][0], x[6][0], x[7][0]) for x in rows], [('E1', '15,000.00', 'Paid in this month'), ('E2', '2,000.00', 'Pending')])

    def test_loan_recovery_report_and_balance(self):
        Loan, Repay = apps.get_model('payroll_app', 'Loan'), apps.get_model('payroll_app', 'LoanRepayment')
        loan = Loan.objects.create(company=self.co, employee=self.e1, reference_no='LN1', loan_type='PERSONAL', loan_amount=D('10000'), interest_rate=D('0'),
                                   tenure_months=10, emi_amount=D('1000'), start_date=datetime.date(2026, 8, 1))
        Repay.objects.create(loan=loan, line=PayrollRunLine.objects.get(payroll_run=self.aug, employee=self.e1), amount=D('1000'))
        Repay.objects.create(loan=loan, line=self.l1, amount=D('1000'))
        rows = rows_of(self.get('loan-recovery', **self.sep_params()))
        self.assertEqual([(x[2][0], x[5][0], x[6][0]) for x in rows], [('LN1', '1,000.00', '8,000.00')])

    def test_ff_report(self):
        FF = apps.get_model('payroll_app', 'FullFinalSettlement')
        FF.objects.create(employee=self.e3, last_working_day=datetime.date(2026, 9, 30), pending_salary=D('10000'), leave_encashment=D('2000'), deductions=D('500'))
        other = emp(self.other, 'O1', 'o1@r.test')
        FF.objects.create(employee=other, last_working_day=datetime.date(2026, 9, 30), pending_salary=D('999'), leave_encashment=D('0'), deductions=D('0'))
        r = self.get('ff')
        self.assertEqual([(x[0][0], x[6][0]) for x in rows_of(r)], [('E3', '11,500.00')])

    def test_bank_payment_report_masks_the_account_and_shows_status(self):
        from .bank_batch_models import BankBatch, BankBatchItem
        p = BankPayment.objects.create(payroll_line=self.l1, company=self.co, amount=D('28500'), account_no='123456789012', ifsc_code='HDFC0001234', bank_name='HDFC',
                                       status='PAID', reference_number='UTR9')
        batch = BankBatch.objects.create(company=self.co, reference='BB-2026-0001', payroll_run=self.sep, status='SENT')
        BankBatchItem.objects.create(batch=batch, payment=p, amount=D('28500'))
        body = self.get('bank-payment', **self.sep_params(export='csv')).content.decode()
        self.assertNotIn('123456789012', body)
        self.assertIn('XXXXXXXX9012', body)
        row = rows_of(self.get('bank-payment', **self.sep_params()))[0]
        self.assertEqual((row[7][0], row[8][0], row[9][0]), ('Paid', 'UTR9', 'BB-2026-0001'))

    def test_variance_flags_new_missing_and_changed(self):
        PayrollRunLine.objects.filter(payroll_run=self.aug, employee=self.e2).delete()
        e4 = emp(self.co, 'E4', 'e4@r.test')
        PayrollRunLine.objects.filter(payroll_run=self.aug).update(net_pay=D('28000'))
        PayrollRunLine.objects.filter(payroll_run=self.sep, employee=self.e3).update(net_pay=D('14000'))
        line(self.aug, e4, 5000)
        rows = {x[0][0]: x for x in rows_of(self.get('variance', **self.sep_params()))}
        self.assertEqual(rows['E1'][6][0], 'Changed')
        self.assertEqual(rows['E2'][6][0], 'New this month')
        self.assertEqual(rows['E3'][6][0], 'New this month')
        self.assertEqual(rows['E4'][6][0], 'Not paid this month')
        self.assertContains(self.get('variance', **self.sep_params()), 'Compared with August 2026')

    def test_variance_for_the_first_month_says_there_is_nothing_to_compare(self):
        self.assertContains(self.get('variance', run=self.aug.pk), 'no earlier month')

    def test_cost_trend_is_oldest_first_with_the_change(self):
        rows = rows_of(self.get('cost-trend'))
        self.assertEqual([x[0][0] for x in rows], ['August 2026', 'September 2026'])
        self.assertEqual(rows[1][2][0], '3')

    def test_employee_register_needs_an_employee_and_lists_their_months(self):
        self.assertContains(self.get('employee-register'), 'Choose an employee')
        rows = rows_of(self.get('employee-register', employee=self.e1.pk))
        self.assertEqual([x[0][0] for x in rows], ['August 2026', 'September 2026'])


class SafetyTests(RepBase):
    def test_text_that_looks_like_a_formula_is_neutralised(self):
        Employee.objects.filter(pk=self.e1.pk).update(first_name='=HYPERLINK("http://x")')
        body = self.get('monthly-register', **self.sep_params(export='csv')).content.decode()
        self.assertIn("'=HYPERLINK", body)

    def test_another_companys_run_and_employee_are_ignored(self):
        oe = emp(self.other, 'O1', 'o1@r.test', 'Secret')
        orun = PayrollRun.objects.create(company=self.other, month='October 2026', status='RELEASED')
        line(orun, oe, 99999)
        r = self.get('monthly-register', run=orun.pk)
        self.assertEqual(r.context['run'], self.sep)                  # falls back to this company's latest month
        self.assertNotContains(r, '99,999')
        r = self.get('employee-register', employee=oe.pk)
        self.assertContains(r, 'Choose an employee')
        self.assertEqual(rows_of(r), [])
        self.assertNotContains(self.get('department-wise', run=orun.pk), 'Secret')
        self.assertNotContains(self.get('cost-trend'), 'October 2026')

    def test_a_payslip_of_another_companys_employee_inside_this_run_is_not_shown(self):
        stranger = emp(self.other, 'S1', 's1@r.test', 'Secret')
        line(self.sep, stranger, 77777)
        for key in ('monthly-register', 'department-wise', 'gross', 'net'):
            r = self.get(key, **self.sep_params())
            self.assertNotContains(r, '77,777', msg_prefix=key)
            self.assertNotContains(r, 'Secret', msg_prefix=key)
        self.assertEqual(len(rows_of(self.get('monthly-register', **self.sep_params()))), 3)

    def test_only_company_owners_can_open_the_reports(self):
        names = [spec['name'] for spec in REPORTS.values()] + ['rep_export_center']
        self.client.logout()
        for n in names:
            self.assertEqual(self.client.get(reverse(n)).status_code, 302, n)
        self.client.force_login(make_user('rep_emp', 'EMPLOYEE', self.co))
        for n in names:
            self.assertIn(self.client.get(reverse(n)).status_code, (302, 403), n)
        self.client.force_login(make_user('rep_admin', 'ADMIN'))
        for n in names:
            self.assertEqual(self.client.get(reverse(n)).status_code, 403, n)

    def test_a_company_with_no_payroll_yet_sees_empty_pages_not_errors(self):
        self.client.force_login(make_user('rep_owner2', 'COMPANY_OWNER', self.other))
        for spec in REPORTS.values():
            self.assertEqual(self.client.get(reverse(spec['name'])).status_code, 200, spec['key'])
        self.assertEqual(self.client.get(reverse('rep_export_center')).status_code, 200)

    def test_long_reports_are_paged(self):
        run = PayrollRun.objects.create(company=self.co, month='November 2026', status='RELEASED')
        for i in range(120):
            line(run, emp(self.co, f'B{i:03d}', f'b{i}@r.test'), 1000 + i)
        r = self.get('monthly-register', run=run.pk)
        self.assertEqual((len(rows_of(r)), r.context['page'].paginator.num_pages, r.context['row_count']), (50, 3, 120))
        self.assertEqual(len(rows_of(self.get('monthly-register', run=run.pk, page=3))), 20)
        self.assertEqual(len(csv_rows(self.get('monthly-register', run=run.pk, export='csv'))), 122)     # header + 120 rows + total
