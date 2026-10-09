"""Round 2: dashboard attendance numbers are real and company-scoped, payroll tracker shows the right step."""
import datetime
import json
from decimal import Decimal

from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Attendance, Employee, LeaveRequest, PayrollRun, PayrollRunLine
from .tests import make_company, make_employee, make_user


class DashboardNumbersTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.co, self.other = make_company('Dash Co'), make_company('Dash Other')
        self.owner = make_user('dashowner', 'COMPANY_OWNER', self.co)
        self.emps = [make_employee(self.co, f'DA{i}', f'da{i}@example.com') for i in range(1, 6)]
        self.stranger = make_employee(self.other, 'DX1', 'dx1@example.com')
        self.client.force_login(self.owner)

    def mark(self, emp, status, day=None):
        return Attendance.objects.create(employee=emp, date=day or self.today, status=status)

    def ctx(self):
        return self.client.get(reverse('dashboard')).context

    def test_todays_numbers_come_from_the_database(self):
        self.mark(self.emps[0], 'PRESENT'); self.mark(self.emps[1], 'PRESENT'); self.mark(self.emps[2], 'ABSENT')
        LeaveRequest.objects.create(employee=self.emps[3], leave_type='CASUAL', status='APPROVED',
                                    from_date=self.today, to_date=self.today)
        c = self.ctx()
        self.assertEqual((c['present_today'], c['absent_today'], c['on_leave_today'], c['not_marked_today']), (2, 1, 1, 1))

    def test_other_companies_and_inactive_people_are_not_counted(self):
        self.mark(self.stranger, 'PRESENT')
        Employee.objects.filter(pk=self.emps[4].pk).update(employment_status='RESIGNED')
        c = self.ctx()
        self.assertEqual(c['present_today'], 0)
        self.assertEqual(c['not_marked_today'], 4)                      # five employees, one resigned

    def test_seven_day_trend_and_empty_states(self):
        c = self.ctx()
        self.assertFalse(c['attendance_has_data'])
        self.assertFalse(c['cost_has_data'])
        self.mark(self.emps[0], 'PRESENT', self.today - datetime.timedelta(days=1))
        self.mark(self.emps[1], 'PRESENT', self.today - datetime.timedelta(days=1))
        self.mark(self.emps[0], 'PRESENT')
        c = self.ctx()
        values = json.loads(c['attendance_trend_values'])
        self.assertEqual((len(values), values[-2], values[-1]), (7, 2, 1))
        self.assertTrue(c['attendance_has_data'])

    def test_payroll_cost_uses_released_runs_only(self):
        for month, status, net in (('July 2026', 'RELEASED', 36000), ('August 2026', 'DRAFT', 99999)):
            run = PayrollRun.objects.create(company=self.co, month=month, status=status)
            PayrollRunLine.objects.create(payroll_run=run, employee=self.emps[0], basic=20000, gross_salary=40000, net_pay=net)
        c = self.ctx()
        self.assertEqual(json.loads(c['cost_labels']), ['July 2026'])
        self.assertEqual(json.loads(c['cost_values']), [36000.0])

    def test_dashboard_page_renders_the_new_cards(self):
        html = self.client.get(reverse('dashboard')).content.decode()
        for text in ('Present Today', 'On Leave Today', 'Attendance Not Marked', 'Absent Today'):
            self.assertIn(text, html)


class PayrollTrackerTests(TestCase):
    def setUp(self):
        self.co = make_company('Track Co')
        self.owner = make_user('trackowner', 'COMPANY_OWNER', self.co)
        self.client.force_login(self.owner)

    def page(self, status):
        run = PayrollRun.objects.create(company=self.co, month=f'{status.title()} 2026', status=status)
        return self.client.get(reverse('payroll_run_detail', args=[run.pk])).content.decode()

    def test_tracker_follows_the_run_status(self):
        draft, released = self.page('DRAFT'), self.page('RELEASED')
        self.assertEqual((draft.count('class="step active"'), draft.count('class="step done"')), (1, 0))
        self.assertEqual((released.count('class="step done"'), released.count('class="step active"')), (4, 0))
        approved = self.page('APPROVED')
        self.assertEqual((approved.count('class="step done"'), approved.count('class="step active"')), (2, 1))


class ThemeRound2Tests(TestCase):
    def test_round_2_styles_are_in_the_stylesheet(self):
        path = finders.find('css/namma-theme.css')
        self.assertIsNotNone(path)
        self.assertIn('ROUND 2', open(path, encoding='utf-8').read())
