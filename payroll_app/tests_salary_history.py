"""Salary effective-date engine: payroll uses the salary in force for the month, history is kept, released payslips never change."""
import datetime
import re
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Employee, PayComponent, PayrollRun, PayrollRunLine, SalaryRevision, SalaryStructure
from .payroll_engine import build_run_lines
from .phase4 import component_totals
from .tests import make_company, make_employee, make_user

D = datetime.date


def add_revision(emp, eff, basic, hra=0, conv=0, special=0):
    return SalaryRevision.objects.create(employee=emp, effective_from=eff, basic=basic, hra=hra, conveyance=conv, special_allowance=special)


class EngineTests(TestCase):
    def setUp(self):
        self.co = make_company('Salary Co')
        self.emp = make_employee(self.co, 'SH1', 'sh1@salary.test')
        Employee.objects.filter(pk=self.emp.pk).update(date_of_joining=D(2025, 1, 1))
        self.emp.refresh_from_db()
        SalaryStructure.objects.create(employee=self.emp, basic=20000, hra=8000, conveyance=1600, special_allowance=10400)   # gross 40,000
        add_revision(self.emp, D(2026, 10, 1), 30000, 12000, 1600, 6400)                                                         # gross 50,000

    def run_month(self, month, status='DRAFT'):
        run = PayrollRun.objects.create(company=self.co, month=month, status=status)
        build_run_lines(run, Employee.objects.filter(company=self.co))
        return run.lines.get(employee=self.emp)

    def test_an_old_month_uses_the_old_salary_and_a_new_month_the_new_one(self):
        self.assertEqual(self.run_month('September 2026').gross_salary, Decimal('40000.00'))
        self.assertEqual(self.run_month('October 2026').gross_salary, Decimal('50000.00'))

    def test_a_structure_saved_the_old_way_creates_the_opening_revision(self):
        base = SalaryRevision.objects.get(employee=self.emp, effective_from=D(2000, 1, 1))
        self.assertEqual(base.gross_salary, Decimal('40000.00'))

    def test_percent_of_basic_components_use_the_salary_of_that_month(self):
        PayComponent.objects.create(company=self.co, employee=self.emp, name='Incentive', kind='EARNING', calc_type='PERCENT_BASIC',
                                    percent=10, amount=0, effective_from=D(2026, 1, 1))
        sept = component_totals(self.emp, (D(2026, 9, 1), D(2026, 9, 30)), Decimal('1'))[0]
        octo = component_totals(self.emp, (D(2026, 10, 1), D(2026, 10, 31)), Decimal('1'))[0]
        self.assertEqual((sept, octo), (Decimal('2000.00'), Decimal('3000.00')))

    def test_released_payslips_never_change_when_a_back_dated_salary_is_added(self):
        line = self.run_month('September 2026', status='RELEASED')
        before = line.gross_salary
        owner = make_user('salowner', 'COMPANY_OWNER', self.co)
        self.client.force_login(owner)
        first = D(2026, 9, 1)
        self.client.post(reverse('salary_history'), {'employee': self.emp.pk, 'effective_from': first.isoformat(), 'basic': '45000', 'hra': '0',
                                                     'conveyance': '0', 'special_allowance': '0'})
        self.assertTrue(SalaryRevision.objects.filter(employee=self.emp, effective_from=first).exists())
        self.assertEqual(PayrollRunLine.objects.get(pk=line.pk).gross_salary, before)

    def test_an_employee_without_any_salary_is_still_skipped(self):
        other = make_employee(self.co, 'SH2', 'sh2@salary.test')
        run = PayrollRun.objects.create(company=self.co, month='October 2026')
        created, skipped = build_run_lines(run, Employee.objects.filter(pk=other.pk))
        self.assertEqual((created, [e.pk for e in skipped]), (0, [other.pk]))


class PageTests(TestCase):
    def setUp(self):
        self.co, self.other = make_company('Hist Alpha'), make_company('Hist Beta')
        self.owner = make_user('histowner', 'COMPANY_OWNER', self.co)
        self.emp = make_employee(self.co, 'HA1', 'ha1@hist.test')
        self.stranger = make_employee(self.other, 'HB1', 'hb1@hist.test')
        SalaryStructure.objects.create(employee=self.emp, basic=20000, hra=8000, conveyance=1600, special_allowance=10400)
        today = timezone.localdate()
        self.this_month = today.replace(day=1)
        self.next_month = (self.this_month + datetime.timedelta(days=32)).replace(day=1)
        self.client.force_login(self.owner)

    def post(self, eff, basic='30000', emp=None, **extra):
        data = {'employee': (emp or self.emp).pk, 'effective_from': eff.isoformat() if hasattr(eff, 'isoformat') else eff, 'basic': basic,
                'hra': '0', 'conveyance': '0', 'special_allowance': '0'}
        data.update(extra)
        return self.client.post(reverse('salary_history'), data)

    def test_the_dropdown_lists_the_companys_employees_only(self):
        html = self.client.get(reverse('salary_history')).content.decode()
        self.assertIn('HA1 - ', html)
        self.assertNotIn('HB1', html)

    def test_a_current_month_revision_updates_the_current_salary_but_a_future_one_does_not(self):
        self.post(self.next_month, basic='55000')
        self.assertEqual(SalaryStructure.objects.get(employee=self.emp).basic, Decimal('20000.00'))
        self.post(self.this_month, basic='33000')
        self.assertEqual(SalaryStructure.objects.get(employee=self.emp).basic, Decimal('33000.00'))

    def test_the_timeline_shows_when_each_salary_starts_and_ends(self):
        self.post(self.this_month, basic='33000')
        html = self.client.get(reverse('salary_history') + f'?employee={self.emp.pk}').content.decode()
        self.assertIn('In force now', html)
        self.assertIn('Opening salary', html)
        self.assertIn(f'{self.this_month - datetime.timedelta(days=1):%d %b %Y}', html)      # the opening salary ends the day before

    def test_mid_month_duplicates_and_bad_amounts_are_refused(self):
        self.post(self.this_month.replace(day=15))
        self.post(self.this_month, basic='0')
        self.assertEqual(SalaryRevision.objects.filter(employee=self.emp).count(), 1)         # only the opening salary
        self.post(self.this_month, basic='31000')
        resp = self.post(self.this_month, basic='32000')
        self.assertIn('already has a salary starting', resp.content.decode())
        self.assertEqual(SalaryRevision.objects.filter(employee=self.emp).count(), 2)

    def test_another_companys_employee_cannot_be_changed_and_employees_cannot_open_the_page(self):
        self.post(self.this_month, emp=self.stranger)
        self.assertFalse(SalaryRevision.objects.filter(employee=self.stranger, effective_from=self.this_month).exists())
        emp_user = make_user('histemp', 'EMPLOYEE', self.co)
        Employee.objects.filter(pk=self.emp.pk).update(user=emp_user)
        self.client.force_login(emp_user)
        self.assertEqual(self.client.get(reverse('salary_history')).status_code, 403)


class PayrollMonthDropdownTests(TestCase):
    def setUp(self):
        self.co = make_company('Month Co')
        self.owner = make_user('monthowner', 'COMPANY_OWNER', self.co)
        self.client.force_login(self.owner)
        today = timezone.localdate()
        self.label = today.strftime('%B %Y')

    def test_month_is_a_dropdown_and_shows_months_that_already_exist(self):
        PayrollRun.objects.create(company=self.co, month=self.label, status='DRAFT')
        html = self.client.get(reverse('payroll_combined')).content.decode()
        m = re.search(r'<select[^>]*name="month".*?</select>', html, re.S)
        self.assertIsNotNone(m)
        self.assertIn(f'{self.label} - already created', m.group(0))
        self.assertIn('- Select payroll month -', m.group(0))

    def test_a_month_picked_from_the_dropdown_creates_one_run_and_free_text_is_refused(self):
        other = (timezone.localdate().replace(day=1) - datetime.timedelta(days=40)).strftime('%B %Y')
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': other})
        self.assertEqual(PayrollRun.objects.filter(company=self.co, month=other).count(), 1)
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': other})
        self.assertEqual(PayrollRun.objects.filter(company=self.co, month=other).count(), 1)
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': 'next tuesday'})
        self.assertEqual(PayrollRun.objects.filter(company=self.co).count(), 1)


class SafetyFixTests(TestCase):
    def setUp(self):
        self.co = make_company('Safety Co')
        self.owner = make_user('safetyowner', 'COMPANY_OWNER', self.co)
        self.client.force_login(self.owner)
        self.last = (timezone.localdate().replace(day=1) - datetime.timedelta(days=1))

    def test_reprocessing_a_run_still_leaves_out_people_who_join_later(self):
        early = make_employee(self.co, 'SF1', 'sf1@safety.test')
        late = make_employee(self.co, 'SF2', 'sf2@safety.test')
        Employee.objects.filter(pk=early.pk).update(date_of_joining=D(2025, 1, 1))
        Employee.objects.filter(pk=late.pk).update(date_of_joining=timezone.localdate() + datetime.timedelta(days=60))
        for e in (early, late):
            SalaryStructure.objects.create(employee=e, basic=20000, hra=0, conveyance=0, special_allowance=0)
        label = self.last.strftime('%B %Y')
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': label})
        run = PayrollRun.objects.get(company=self.co, month=label)
        self.client.post(reverse('payroll_combined'), {'action': 'reprocess', 'run_id': run.pk})
        self.assertEqual(sorted(run.lines.values_list('employee__employee_code', flat=True)), ['SF1'])

    def test_an_employee_with_payroll_history_cannot_be_deleted_but_one_without_can(self):
        paid = make_employee(self.co, 'SF3', 'sf3@safety.test')
        free = make_employee(self.co, 'SF4', 'sf4@safety.test')
        run = PayrollRun.objects.create(company=self.co, month='August 2026', status='RELEASED')
        PayrollRunLine.objects.create(payroll_run=run, employee=paid, basic=1, gross_salary=1, net_pay=1)
        self.client.post(reverse('employee_delete', args=[paid.pk]))
        self.assertTrue(Employee.objects.filter(pk=paid.pk).exists())
        self.assertTrue(PayrollRunLine.objects.filter(employee=paid).exists())
        self.client.post(reverse('employee_delete', args=[free.pk]))
        self.assertFalse(Employee.objects.filter(pk=free.pk).exists())
