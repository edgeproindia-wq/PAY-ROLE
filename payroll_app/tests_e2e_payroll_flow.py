"""End-to-end: salary + attendance -> payroll run -> approve -> release -> payslip -> bank payment.

Uses the REAL payroll engine. Figures are worked out by hand (September 2026 = 30 days):
  employee A  gross 40,000, 2 absent days + 1 half day = 2.5 LOP days
     LOP amount   = 40,000 / 30 * 2.5            = 3,333.33
     earned basic = 20,000 * (30 - 2.5) / 30      = 18,333.33  -> PF 12% = 2,200.00
     net          = 40,000 - 3,333.33 - 2,200.00  = 34,466.67   (ESI 0: gross above 21,000; TDS 0: under the rebate)
  employee B  joined 16 Sep: 15 days not paid
     net          = 20,000 - PF 1,200.00          = 18,800.00
  employee C  joins 5 Oct: not part of the September run
"""
import datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from .models import Attendance, BankPayment, Employee, PayrollRun, PayrollRunLine, SalaryStructure
from .tests import make_company, make_employee, make_user

D = Decimal


class PayrollEndToEndTests(TestCase):
    def setUp(self):
        self.co, self.other = make_company('Flow Co'), make_company('Other Co')
        self.owner = make_user('flowowner', 'COMPANY_OWNER', self.co)
        self.owner2 = make_user('otherowner', 'COMPANY_OWNER', self.other)
        self.a = make_employee(self.co, 'FA1', 'fa1@example.com')
        self.b = make_employee(self.co, 'FB1', 'fb1@example.com')
        self.c = make_employee(self.co, 'FC1', 'fc1@example.com')
        self.x = make_employee(self.other, 'XX1', 'xx1@example.com')
        Employee.objects.filter(pk=self.b.pk).update(date_of_joining=datetime.date(2026, 9, 16))
        Employee.objects.filter(pk=self.c.pk).update(date_of_joining=datetime.date(2026, 10, 5))
        for e in (self.a, self.b, self.c, self.x):
            SalaryStructure.objects.create(employee=e, basic=20000, hra=8000, conveyance=1600, special_allowance=10400)
        self.a_user = make_user('flowa', 'EMPLOYEE', self.co)
        self.b_user = make_user('flowb', 'EMPLOYEE', self.co)
        Employee.objects.filter(pk=self.a.pk).update(user=self.a_user)
        Employee.objects.filter(pk=self.b.pk).update(user=self.b_user)
        Employee.objects.filter(pk=self.a.pk).update(bank_name='HDFC', bank_account_no='123456789012', ifsc_code='HDFC0001234')
        for day, status in ((3, 'ABSENT'), (4, 'ABSENT'), (10, 'HALF_DAY'), (7, 'PRESENT')):
            Attendance.objects.create(employee=self.a, date=datetime.date(2026, 9, day), status=status)
        Attendance.objects.create(employee=self.a, date=datetime.date(2026, 8, 12), status='ABSENT')    # other month: ignored
        Attendance.objects.create(employee=self.a, date=datetime.date(2026, 10, 2), status='ABSENT')   # other month: ignored

    def owner_post(self, **data):
        self.client.force_login(self.owner)
        return self.client.post(reverse('payroll_combined'), data, follow=True)

    def line(self, emp):
        return PayrollRunLine.objects.get(payroll_run__company=self.co, employee=emp)

    def run_to_release(self):
        self.owner_post(action='create', month='September 2026')
        run = PayrollRun.objects.get(company=self.co)
        for action in ('validate', 'approve', 'release'):
            self.owner_post(action=action, run_id=run.pk)
        run.refresh_from_db()
        return run

    def test_the_whole_chain(self):
        # 1. run created: draft, eligible employees only, hand-calculated figures
        self.owner_post(action='create', month='September 2026')
        run = PayrollRun.objects.get(company=self.co)
        self.assertEqual(run.status, 'DRAFT')
        self.assertEqual(set(run.lines.values_list('employee__employee_code', flat=True)), {'FA1', 'FB1'})
        la, lb = self.line(self.a), self.line(self.b)
        self.assertEqual((la.days_in_month, la.lop_days, la.lop_amount), (30, D('2.5'), D('3333.33')))
        self.assertEqual((la.pf, la.esi, la.tds, la.net_pay), (D('2200.00'), D('0.00'), D('0.00'), D('34466.67')))
        self.assertEqual((lb.pf, lb.net_pay), (D('1200.00'), D('18800.00')))
        for ln in (la, lb):
            self.assertEqual(ln.total_earnings - ln.total_deductions, ln.net_pay)

        # 2. a draft payslip is invisible to the employee
        self.client.force_login(self.a_user)
        self.assertEqual(len(self.client.get(reverse('payslips')).context['lines']), 0)

        # 3. approve + release -> now visible, and only their own
        for action in ('validate', 'approve', 'release'):
            self.owner_post(action=action, run_id=run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, 'RELEASED')
        self.client.force_login(self.a_user)
        shown = list(self.client.get(reverse('payslips')).context['lines'])
        self.assertEqual([ln.pk for ln in shown], [la.pk])
        self.assertEqual(shown[0].net_pay, D('34466.67'))
        self.client.force_login(self.b_user)
        self.assertEqual([ln.pk for ln in self.client.get(reverse('payslips')).context['lines']], [lb.pk])

        # 4. bank payment copies the finalized net pay; people without bank details are skipped
        self.client.force_login(self.owner)
        self.client.post(reverse('bank_transfer'), {'action': 'prepare'}, follow=True)
        pay = BankPayment.objects.get(payroll_line=la)
        self.assertEqual(pay.amount, la.net_pay)
        self.assertEqual((pay.account_no, pay.ifsc_code), ('123456789012', 'HDFC0001234'))
        self.assertFalse(BankPayment.objects.filter(payroll_line=lb).exists())

        # 5. a later salary revision cannot change a released payslip, and a released run cannot be reprocessed
        SalaryStructure.objects.filter(employee=self.a).update(basic=99999)
        self.owner_post(action='reprocess', run_id=run.pk)
        la.refresh_from_db()
        self.assertEqual((la.basic, la.net_pay), (D('20000.00'), D('34466.67')))
        pay.refresh_from_db()
        self.assertEqual(pay.amount, D('34466.67'))

    def test_another_company_sees_nothing_of_this_payroll(self):
        run = self.run_to_release()
        self.client.force_login(self.owner2)
        self.assertEqual(len(self.client.get(reverse('payroll_combined')).context['payroll_runs']), 0)
        resp = self.client.post(reverse('payroll_combined'), {'action': 'reprocess', 'run_id': run.pk})
        self.assertEqual(resp.status_code, 403)
        self.client.force_login(self.owner)
        self.client.post(reverse('bank_transfer'), {'action': 'prepare'})
        pay = BankPayment.objects.get(payroll_line=self.line(self.a))
        self.client.force_login(self.owner2)
        resp = self.client.post(reverse('bank_payment_update', args=[pay.pk]), {'action': 'INITIATE'})
        self.assertEqual(resp.status_code, 403)
        pay.refresh_from_db()
        self.assertEqual(pay.status, 'PENDING')

    def test_duplicate_month_is_refused(self):
        self.owner_post(action='create', month='September 2026')
        self.owner_post(action='create', month='september 2026')
        self.assertEqual(PayrollRun.objects.filter(company=self.co).count(), 1)

    def test_other_months_attendance_does_not_affect_this_month(self):
        self.owner_post(action='create', month='September 2026')
        self.assertEqual(self.line(self.a).lop_days, D('2.5'))     # August and October absences ignored