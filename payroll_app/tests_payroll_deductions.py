"""Loans, insurance, professional tax, unpaid leave and pay components in a real payroll run.

September 2026 (30 days), employee gross 40,000 (basic 20,000):
  unpaid leave 14-16 Sep (3 days) -> LOP 3 days = 40,000 / 30 * 3          = 4,000.00
  pay factor 27/30 = 0.9 -> earned basic 18,000 -> PF 12%                   = 2,160.00
  earned gross = 40,000 - 4,000                                              = 36,000.00
  allowance 1,000 (taxable, pro-rated by 0.9)                                =   900.00  earning
  canteen deduction (fixed)                                                  =   300.00
  professional tax slab 25,000+ -> 200 ; insurance premium 500 ; loan EMI 5,000
  total earnings   = 36,000 + 900                                            = 36,900.00
  total deductions = 2,160 + 0 ESI + 0 TDS + 200 + 5,000 + 500 + 300         =  8,160.00
  net pay                                                                    = 28,740.00
The loan is 10,000 in total: September and October take 5,000 each, November takes nothing and closes it.
"""
import datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from .models import Employee, LeaveRequest, PayrollRun, PayrollRunLine, SalaryStructure
from .models_features import InsurancePolicy, Loan, ProfessionalTaxSlab
from .models_phase4 import PayComponent
from .tests import make_company, make_employee, make_user

D = Decimal


class DeductionsInPayrollTests(TestCase):
    def setUp(self):
        self.co = make_company('Ded Co')
        self.owner = make_user('dedowner', 'COMPANY_OWNER', self.co)
        self.e = make_employee(self.co, 'DD1', 'dd1@example.com')
        SalaryStructure.objects.create(employee=self.e, basic=20000, hra=8000, conveyance=1600, special_allowance=10400)
        self.client.force_login(self.owner)

    def make_run(self, month):
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': month}, follow=True)
        return PayrollRunLine.objects.get(payroll_run__company=self.co, payroll_run__month=month, employee=self.e)

    def configure_everything(self):
        LeaveRequest.objects.create(employee=self.e, leave_type='UNPAID', status='APPROVED',
                                    from_date=datetime.date(2026, 9, 14), to_date=datetime.date(2026, 9, 16))
        PayComponent.objects.create(company=self.co, employee=self.e, name='Allowance', kind='EARNING', amount=1000,
                                    taxable=True, effective_from=datetime.date(2026, 1, 1))
        PayComponent.objects.create(company=self.co, employee=self.e, name='Canteen', kind='DEDUCTION', amount=300,
                                    effective_from=datetime.date(2026, 1, 1))
        ProfessionalTaxSlab.objects.create(company=self.co, min_monthly_gross=25000, monthly_amount=200)
        InsurancePolicy.objects.create(company=self.co, employee=self.e, provider='Acme Health', policy_number='P1',
                                       employee_premium_monthly=500, effective_from=datetime.date(2026, 1, 1))
        self.loan = Loan.objects.create(company=self.co, employee=self.e, reference_no='L1', loan_amount=10000,
                                        tenure_months=2, emi_amount=5000, start_date=datetime.date(2026, 9, 1))

    def test_everything_together_matches_the_hand_calculation(self):
        self.configure_everything()
        ln = self.make_run('September 2026')
        self.assertEqual((ln.lop_days, ln.lop_amount), (D('3.0'), D('4000.00')))
        self.assertEqual((ln.pf, ln.esi, ln.tds), (D('2160.00'), D('0.00'), D('0.00')))
        self.assertEqual((ln.professional_tax, ln.insurance_deduction, ln.loan_deduction), (D('200.00'), D('500.00'), D('5000.00')))
        self.assertEqual((ln.other_earnings, ln.other_deductions), (D('900.00'), D('300.00')))
        self.assertEqual((ln.total_earnings, ln.total_deductions, ln.net_pay), (D('36900.00'), D('8160.00'), D('28740.00')))

    def test_loan_is_recovered_in_full_then_closes(self):
        self.configure_everything()
        self.assertEqual(self.make_run('September 2026').loan_deduction, D('5000.00'))
        self.assertEqual(self.make_run('October 2026').loan_deduction, D('5000.00'))
        self.assertEqual(self.make_run('November 2026').loan_deduction, D('0.00'))
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'CLOSED')
        self.assertEqual(self.loan.recovered(), D('10000.00'))

    def test_loan_that_has_not_started_yet_is_not_deducted(self):
        self.configure_everything()
        Loan.objects.filter(pk=self.loan.pk).update(start_date=datetime.date(2026, 10, 1))
        self.assertEqual(self.make_run('September 2026').loan_deduction, D('0.00'))

    def test_things_outside_their_dates_are_ignored(self):
        InsurancePolicy.objects.create(company=self.co, employee=self.e, provider='Old', policy_number='P0',
                                       employee_premium_monthly=700, effective_from=datetime.date(2025, 1, 1),
                                       effective_to=datetime.date(2026, 8, 31))
        PayComponent.objects.create(company=self.co, employee=self.e, name='Past bonus', kind='EARNING', amount=5000,
                                    effective_from=datetime.date(2026, 1, 1), effective_to=datetime.date(2026, 8, 31))
        LeaveRequest.objects.create(employee=self.e, leave_type='UNPAID', status='REJECTED',
                                    from_date=datetime.date(2026, 9, 14), to_date=datetime.date(2026, 9, 16))
        LeaveRequest.objects.create(employee=self.e, leave_type='CASUAL', status='APPROVED',
                                    from_date=datetime.date(2026, 9, 21), to_date=datetime.date(2026, 9, 22))
        ln = self.make_run('September 2026')
        self.assertEqual((ln.insurance_deduction, ln.other_earnings, ln.lop_days), (D('0.00'), D('0.00'), D('0.0')))
        self.assertEqual(ln.net_pay, D('37600.00'))      # 40,000 - PF 2,400, nothing else