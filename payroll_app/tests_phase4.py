"""Phase-4 tests: shifts / working hours and pay components in payroll."""
import datetime
import io
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from .models import Attendance, PayrollRun, SalaryStructure, User
from .models_phase4 import PayComponent, Shift, ShiftAssignment
from .phase4 import day_summary, month_summary
from .tests_audit import PWD, company, employee, user


class Base(TestCase):
    def setUp(self):
        self.c1, self.c2 = company('Acme'), company('Beta')
        self.owner = user('own', 'COMPANY_OWNER', self.c1)
        self.owner2 = user('own2', 'COMPANY_OWNER', self.c2)
        self.e1 = employee(self.c1, 'A1', 'a1@ex.com', login='emp1')
        self.b1 = employee(self.c2, 'B1', 'b1@ex.com')
        for e in (self.e1, self.b1):
            SalaryStructure.objects.create(employee=e, basic=20000, hra=8000, conveyance=1600, special_allowance=10400)
        self.day = Shift.objects.create(company=self.c1, name='Day', start_time=datetime.time(9), end_time=datetime.time(18),
                                        break_minutes=60, grace_minutes=10)

    def login(self, u):
        self.client.cookies.clear()
        self.client.force_login(u)

    def run_payroll(self, month, owner=None):
        self.login(owner or self.owner)
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': month})
        return PayrollRun.objects.get(company=(owner or self.owner).company, month__iexact=month)


class ShiftTests(Base):
    def test_hours_late_overtime_and_overnight(self):
        self.assertEqual(self.day.scheduled_minutes, 480)
        rec = Attendance(employee=self.e1, date=datetime.date(2026, 9, 1), check_in=datetime.time(9, 25), check_out=datetime.time(19, 25))
        self.assertEqual(day_summary(rec, self.day), {'worked': 540, 'late': True, 'overtime': 60})
        night = Shift.objects.create(company=self.c1, name='Night', start_time=datetime.time(22), end_time=datetime.time(6), break_minutes=30)
        self.assertEqual(night.scheduled_minutes, 450)
        rec2 = Attendance(employee=self.e1, date=datetime.date(2026, 9, 2), check_in=datetime.time(22, 5), check_out=datetime.time(6, 0))
        self.assertEqual(day_summary(rec2, night), {'worked': 445, 'late': False, 'overtime': 0})

    def test_assignment_and_month_report(self):
        self.login(self.owner)
        self.client.post(reverse('hr_shifts'), {'form': 'assign', 'employee': self.e1.pk, 'shift': self.day.pk, 'effective_from': '2026-09-01'})
        self.assertEqual(ShiftAssignment.objects.get().shift, self.day)
        Attendance.objects.create(employee=self.e1, date=datetime.date(2026, 9, 3), status='PRESENT',
                                  check_in=datetime.time(9, 0), check_out=datetime.time(19, 0))
        s = month_summary(self.e1, 2026, 9)
        self.assertEqual((s['days'], s['hours'], s['overtime_hours'], s['late_days']), (1, 9.0, 1.0, 0))
        page = self.client.get(reverse('hr_work_hours'), {'month': '2026-09'})
        self.assertContains(page, 'A1')
        self.assertNotContains(page, 'B1')

    def test_isolation_and_roles(self):
        self.login(self.owner2)
        self.client.post(reverse('hr_shifts'), {'form': 'assign', 'employee': self.e1.pk, 'shift': self.day.pk, 'effective_from': '2026-09-01'})
        self.client.post(reverse('hr_shifts'), {'deactivate': self.day.pk})
        self.day.refresh_from_db()
        self.assertTrue(self.day.active)
        self.assertFalse(ShiftAssignment.objects.exists())
        self.assertNotContains(self.client.get(reverse('hr_shifts')), 'Day (')
        self.login(self.e1.user)
        for n in ('hr_shifts', 'hr_work_hours', 'hr_pay_components'):
            self.assertEqual(self.client.get(reverse(n)).status_code, 403, n)

    def test_employee_sees_own_shift_on_dashboard(self):
        ShiftAssignment.objects.create(company=self.c1, employee=self.e1, shift=self.day, effective_from=datetime.date(2020, 1, 1))
        self.login(self.e1.user)
        self.assertContains(self.client.get(reverse('employee_home')), 'Day (09:00-18:00)')


class ComponentTests(Base):
    def test_earning_prorated_deduction_fixed_and_taxable_in_tds(self):
        mid = employee(self.c1, 'A3', 'a3@ex.com')
        mid.date_of_joining = datetime.date(2026, 9, 16)
        mid.save()
        SalaryStructure.objects.create(employee=mid, basic=20000, hra=8000, conveyance=1600, special_allowance=10400)
        for e in (self.e1, mid):
            PayComponent.objects.create(company=self.c1, employee=e, name='Shift allowance', kind='EARNING', amount=3000,
                                        effective_from=datetime.date(2026, 1, 1))
            PayComponent.objects.create(company=self.c1, employee=e, name='Canteen', kind='DEDUCTION', amount=500,
                                        effective_from=datetime.date(2026, 1, 1))
        run = self.run_payroll('September 2026')
        full, half = run.lines.get(employee=self.e1), run.lines.get(employee=mid)
        self.assertEqual((full.other_earnings, full.other_deductions), (Decimal('3000.00'), Decimal('500.00')))
        self.assertEqual((half.other_earnings, half.other_deductions), (Decimal('1500.00'), Decimal('500.00')))
        self.assertEqual(full.total_earnings, Decimal('43000.00'))
        self.assertEqual(full.net_pay, full.total_earnings - full.total_deductions)
        self.assertEqual(full.tds, Decimal('0.00'))                  # 43000*12 = 5.16L -> below rebate
        PayComponent.objects.filter(employee=self.e1, kind='EARNING').update(amount=90000)
        run2 = self.run_payroll('October 2026')
        self.assertGreater(run2.lines.get(employee=self.e1).tds, 0)  # taxable earning raises TDS

    def test_no_components_no_change_and_out_of_range_ignored(self):
        PayComponent.objects.create(company=self.c1, employee=self.e1, name='Old', kind='EARNING', amount=1000,
                                    effective_from=datetime.date(2025, 1, 1), effective_to=datetime.date(2025, 12, 31))
        run = self.run_payroll('September 2026')
        line = run.lines.get(employee=self.e1)
        self.assertEqual((line.other_earnings, line.other_deductions, line.total_earnings), (0, 0, Decimal('40000.00')))

    def test_payslip_shows_components(self):
        PayComponent.objects.create(company=self.c1, employee=self.e1, name='Shift allowance', kind='EARNING', amount=3000,
                                    effective_from=datetime.date(2026, 1, 1))
        PayComponent.objects.create(company=self.c1, employee=self.e1, name='Canteen', kind='DEDUCTION', amount=500,
                                    effective_from=datetime.date(2026, 1, 1))
        run = self.run_payroll('September 2026')
        for a in ('validate', 'approve', 'release'):
            self.client.post(reverse('payroll_combined'), {'action': a, 'run_id': run.pk})
        line = run.lines.get(employee=self.e1)
        self.login(self.e1.user)
        from pypdf import PdfReader
        text = ' '.join(p.extract_text() for p in PdfReader(io.BytesIO(self.client.get(reverse('payslip_pdf', args=[line.pk])).content)).pages)
        self.assertIn('Other Allowances', text)
        self.assertIn('Other Deductions', text)

    def test_owner_add_and_scoping(self):
        self.login(self.owner)
        self.client.post(reverse('hr_pay_components'), {'employee': self.e1.pk, 'name': 'Bonus', 'kind': 'EARNING', 'amount': '100',
                                                        'taxable': 'on', 'effective_from': '2026-09-01'})
        self.assertTrue(PayComponent.objects.filter(name='Bonus', company=self.c1).exists())
        self.login(self.owner2)
        self.client.post(reverse('hr_pay_components'), {'employee': self.e1.pk, 'name': 'Hack', 'kind': 'DEDUCTION', 'amount': '100',
                                                        'effective_from': '2026-09-01'})
        self.assertFalse(PayComponent.objects.filter(name='Hack').exists())
        self.assertNotContains(self.client.get(reverse('hr_pay_components')), 'Bonus')


class JoinerPayslipTests(Base):
    def test_payslip_shows_days_before_joining_and_adds_up(self):
        mid = employee(self.c1, 'A9', 'a9@ex.com', login='emp9')
        mid.date_of_joining = datetime.date(2026, 9, 16)
        mid.save()
        SalaryStructure.objects.create(employee=mid, basic=20000, hra=8000, conveyance=1600, special_allowance=10400)
        run = self.run_payroll('September 2026')
        for a in ('validate', 'approve', 'release'):
            self.client.post(reverse('payroll_combined'), {'action': a, 'run_id': run.pk})
        line = run.lines.get(employee=mid)
        self.login(mid.user)
        from pypdf import PdfReader
        text = ' '.join(p.extract_text() for p in PdfReader(io.BytesIO(self.client.get(reverse('payslip_pdf', args=[line.pk])).content)).pages)
        self.assertIn('Days before joining', text)
        self.assertIn('20,000.00', text)