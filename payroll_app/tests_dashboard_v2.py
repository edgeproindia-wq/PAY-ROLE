"""The company owner's dashboard: the old content is all still there, and the new panels (setup, attention, payroll month, coming up) are correct and private."""
import datetime
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import dashboard_extras
from .bank_batch_models import BankBatch
from .models import Attendance, CompanySettings, Employee, LeaveRequest, PayrollRun, SalaryStructure
from .models_documents import EmployeeDocument
from .pay_cycle_models import CalendarDay, PayCycle, WorkingCalendar
from .tests import make_company, make_user


def days(n):
    return datetime.timedelta(days=n)


def person(company, code, email, **extra):
    data = dict(employee_code=code, first_name=code, last_name='Person', email=email, date_of_joining=datetime.date(2020, 1, 1), department='Eng', designation='Dev', company=company)
    data.update(extra)
    return Employee.objects.create(**data)


def complete(emp):
    """Give an employee a salary structure and bank details."""
    SalaryStructure.objects.create(employee=emp, basic=30000)
    Employee.objects.filter(pk=emp.pk).update(bank_account_no='123456789012', ifsc_code='HDFC0001234', bank_status='VERIFIED')


class Base(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.co = make_company('Dash Co')
        self.other = make_company('Other Dash Co')
        self.owner = make_user('dash_owner', 'COMPANY_OWNER', self.co)
        self.owner_b = make_user('dash_owner_b', 'COMPANY_OWNER', self.other)
        self.client.force_login(self.owner)

    def get(self):
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.status_code, 200)
        return r


class PageTests(Base):
    def test_everything_the_old_dashboard_showed_is_still_there(self):
        h = self.get().content.decode()
        for text in ('Active Employees', 'Salary Structures', 'Payroll Runs Processed', 'Pending Approvals', 'Present Today', 'On Leave Today', 'Attendance Not Marked', 'Absent Today',
                     'Latest Payroll Run', 'Attendance - Last 7 Days', 'Payroll Cost - Recent Releases', 'Employees by Department', 'Employee Status Breakdown', 'Payroll Run Status',
                     'To-Do', 'Pending Leave Requests', 'Pending Reimbursements', 'Unverified Investment Declarations', 'Payroll Runs Awaiting Approval', 'Recent Leave Requests',
                     'Recent Reimbursements', '+ New Payroll Run', '+ Add Employee', 'No payroll run yet'):
            self.assertIn(text, h, text)
        for canvas in ('deptChart', 'statusChart', 'runChart'):
            self.assertIn('id="%s"' % canvas, h)
        self.assertNotIn('{{', h)
        self.assertNotIn('{%', h)

    def test_the_new_panels_are_on_the_page(self):
        h = self.get().content.decode()
        for text in ('Setup progress', 'Needs your attention', 'Coming up', "Today's attendance", 'Payroll for %s' % self.today.strftime('%B %Y'), 'Attendance marked', 'Bank payment sent'):
            self.assertIn(text, h, text)

    def test_the_platform_admin_and_an_employee_are_sent_to_their_own_dashboards(self):
        self.client.force_login(make_user('dash_admin', 'ADMIN'))
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 302)
        emp_user = make_user('dash_emp', 'EMPLOYEE', self.co)
        emp = person(self.co, 'E1', 'e1@dash.test')
        emp.user = emp_user
        emp.save()
        self.client.force_login(emp_user)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 302)

    def test_an_employee_login_without_a_profile_never_gets_the_owner_panels(self):
        self.client.force_login(make_user('dash_emp2', 'EMPLOYEE', self.co))
        r = self.client.get(reverse('dashboard'))
        self.assertIn(r.status_code, (200, 302, 403))                  # this project refuses such a login (403); either way it must not see the owner panels
        self.assertNotContains(r, 'Setup progress', status_code=r.status_code)
        self.assertNotContains(r, 'Coming up', status_code=r.status_code)
        if r.status_code == 200:
            self.assertNotIn('dx_setup', r.context)


class SetupTests(Base):
    def steps(self):
        return {s['label']: s['done'] for s in self.get().context['dx_setup']['steps']}

    def test_a_new_company_has_nothing_done_and_each_step_turns_green_by_itself(self):
        s = self.get().context['dx_setup']
        self.assertEqual((s['done'], s['total'], s['pct']), (0, 7, 0))
        CompanySettings.objects.update_or_create(company=self.co, defaults={'pan_number': 'ABCDE1234F'})
        self.assertTrue(self.steps()['Company details and PAN'])
        cal = WorkingCalendar.objects.create(company=self.co, name='Std', is_default=True)
        self.assertTrue(self.steps()['Working calendar and holidays'])
        PayCycle.objects.create(company=self.co, name='Nov', payroll_month='November 2026', start_date=datetime.date(2026, 11, 1), end_date=datetime.date(2026, 11, 30))
        self.assertTrue(self.steps()['Pay cycle created'])
        e = person(self.co, 'S1', 's1@dash.test')
        st = self.steps()
        self.assertTrue(st['Employees added'])
        self.assertFalse(st['Salary structure for every employee'])
        self.assertFalse(st['Bank details for every employee'])
        complete(e)
        st = self.steps()
        self.assertTrue(st['Salary structure for every employee'] and st['Bank details for every employee'])
        self.assertFalse(st['First payroll run'])
        PayrollRun.objects.create(company=self.co, month='October 2026', status='DRAFT')
        s = self.get().context['dx_setup']
        self.assertEqual((s['done'], s['pct']), (7, 100))
        self.assertIsNotNone(cal)

    def test_a_second_employee_without_a_structure_turns_the_step_back_off(self):
        complete(person(self.co, 'S1', 's1@dash.test'))
        person(self.co, 'S2', 's2@dash.test')
        step = {s['label']: s for s in self.get().context['dx_setup']['steps']}['Salary structure for every employee']
        self.assertFalse(step['done'])
        self.assertEqual(step['detail'], '1 of 2 done')

    def test_every_link_is_a_real_address_or_left_out(self):
        for s in self.get().context['dx_setup']['steps']:
            self.assertTrue(s['href'] == '' or s['href'].startswith('/'), s)


class AttentionTests(Base):
    def alerts(self):
        return {a['label']: a['count'] for a in self.get().context['dx_attention']['alerts']}

    def test_what_needs_fixing_is_listed_with_the_right_counts(self):
        a = person(self.co, 'A1', 'a1@dash.test')                                  # no structure, no bank details
        b = person(self.co, 'A2', 'a2@dash.test')
        complete(b)
        run = PayrollRun.objects.create(company=self.co, month='September 2026', status='DRAFT')
        BankBatch.objects.create(company=self.co, reference='B1', payroll_run=run, status=BankBatch.PENDING_APPROVAL)
        EmployeeDocument.objects.create(company=self.co, employee=a, doc_type='OTHER', data=b'x', expiry_date=self.today + days(5))
        EmployeeDocument.objects.create(company=self.co, employee=a, doc_type='OTHER', data=b'x', expiry_date=self.today + days(200))      # far away: not counted
        LeaveRequest.objects.create(employee=a, leave_type='CASUAL', from_date=self.today, to_date=self.today, status='PENDING')
        got = self.alerts()
        self.assertEqual(got['Active employees without a salary structure'], 1)
        self.assertEqual(got['Active employees with incomplete bank details'], 1)
        self.assertEqual(got['Bank payment batches waiting for approval'], 1)
        self.assertEqual(got['Draft payroll runs not yet validated'], 1)
        self.assertEqual(got['Attendance not marked today'], 2)
        self.assertEqual(got['Employee documents expired or expiring within 30 days'], 1)
        legacy = {r['label']: r['count'] for r in self.get().context['dx_attention']['legacy']}
        self.assertEqual(legacy['Pending Leave Requests'], 1)
        self.assertEqual(self.get().context['dx_attention']['total'], sum(got.values()) + 1)

    def test_the_worst_things_come_first(self):
        person(self.co, 'A1', 'a1@dash.test')
        levels = [a['level'] for a in self.get().context['dx_attention']['alerts']]
        self.assertEqual(levels, sorted(levels, key=lambda x: {'danger': 0, 'warn': 1, 'info': 2}[x]))

    def test_when_nothing_is_wrong_the_page_says_so(self):
        e = person(self.co, 'A1', 'a1@dash.test')
        complete(e)
        Attendance.objects.create(employee=e, date=self.today, status='PRESENT')
        r = self.get()
        self.assertEqual(r.context['dx_attention']['alerts'], [])
        self.assertContains(r, 'Nothing is blocking payroll right now.')
        self.assertContains(r, 'All clear')

    def test_the_four_old_approval_rows_are_always_listed_even_at_zero(self):
        h = self.get().content.decode()
        for text in ('Pending Leave Requests', 'Pending Reimbursements', 'Unverified Investment Declarations', 'Payroll Runs Awaiting Approval'):
            self.assertIn(text, h)
        self.assertIn('0 pending', h)


class TrackerTests(Base):
    def state(self):
        t = self.get().context['dx_tracker']
        return [s['state'] for s in t['steps']], t

    def test_the_steps_follow_the_payroll_month(self):
        e = person(self.co, 'T1', 't1@dash.test')
        month = self.today.strftime('%B %Y')
        self.assertEqual(self.state()[0], ['current', 'todo', 'todo', 'todo', 'todo', 'todo'])
        Attendance.objects.create(employee=e, date=self.today, status='PRESENT')
        self.assertEqual(self.state()[0], ['done', 'current', 'todo', 'todo', 'todo', 'todo'])
        run = PayrollRun.objects.create(company=self.co, month=month, status='DRAFT')
        self.assertEqual(self.state()[0], ['done', 'done', 'current', 'todo', 'todo', 'todo'])
        for status, expect in (('VALIDATED', ['done', 'done', 'done', 'current', 'todo', 'todo']), ('APPROVED', ['done', 'done', 'done', 'done', 'current', 'todo']),
                               ('RELEASED', ['done', 'done', 'done', 'done', 'done', 'current'])):
            PayrollRun.objects.filter(pk=run.pk).update(status=status)
            self.assertEqual(self.state()[0], expect, status)
        BankBatch.objects.create(company=self.co, reference='B9', payroll_run=run, status=BankBatch.COMPLETED)
        states, tracker = self.state()
        self.assertEqual(states, ['done'] * 6)
        self.assertTrue(tracker['complete'])

    def test_the_pay_date_comes_from_the_pay_cycle_of_the_month(self):
        month = self.today.strftime('%B %Y')
        PayCycle.objects.create(company=self.co, name='Cycle', payroll_month=month, start_date=self.today.replace(day=1), end_date=self.today.replace(day=28), pay_date=self.today.replace(day=28))
        self.assertEqual(self.get().context['dx_tracker']['pay_date'], self.today.replace(day=28))

    def test_another_companys_bank_payment_never_completes_my_bank_step(self):
        month = self.today.strftime('%B %Y')
        Attendance.objects.create(employee=person(self.co, 'B1', 'b1@dash.test'), date=self.today, status='PRESENT')
        PayrollRun.objects.create(company=self.co, month=month, status='RELEASED')
        theirs = PayrollRun.objects.create(company=self.other, month=month, status='RELEASED')
        BankBatch.objects.create(company=self.other, reference='O1', payroll_run=theirs, status=BankBatch.COMPLETED)
        steps = self.get().context['dx_tracker']['steps']
        self.assertEqual(steps[5]['state'], 'current')                              # my bank payment is still to do

    def test_another_companys_run_for_the_same_month_is_not_mine(self):
        PayrollRun.objects.create(company=self.other, month=self.today.strftime('%B %Y'), status='RELEASED')
        self.assertIsNone(self.get().context['dx_tracker']['run'])


class EventTests(Base):
    def events(self):
        return {(e['kind'], e['title']): e for e in self.get().context['dx_events']}

    def test_birthdays_anniversaries_joiners_and_holidays_in_the_next_days(self):
        person(self.co, 'BD', 'bd@dash.test', date_of_birth=datetime.date(1990, self.today.month, self.today.day) + days(3))
        person(self.co, 'FAR', 'far@dash.test', date_of_birth=datetime.date(1990, 1, 1) if abs((datetime.date(self.today.year, 1, 1) - self.today).days) > 40 else datetime.date(1990, 6, 15))
        person(self.co, 'AN', 'an@dash.test', date_of_joining=(self.today + days(5)).replace(year=self.today.year - 3))
        person(self.co, 'NEW', 'new@dash.test', date_of_joining=self.today + days(4))
        cal = WorkingCalendar.objects.create(company=self.co, name='Std', is_default=True)
        CalendarDay.objects.create(calendar=cal, date=self.today + days(10), day_type='PUBLIC_HOLIDAY', name='Festival day')
        CalendarDay.objects.create(calendar=cal, date=self.today + days(60), day_type='PUBLIC_HOLIDAY', name='Too far away')
        got = self.events()
        self.assertIn(('birthday', 'BD Person'), got)
        self.assertEqual(got[('birthday', 'BD Person')]['in_days'], 3)
        self.assertNotIn(('birthday', 'FAR Person'), got)
        self.assertEqual(got[('anniversary', 'AN Person')]['text'], '3 years with the company')
        self.assertIn(('joining', 'NEW Person'), got)
        self.assertIn(('holiday', 'Festival day'), got)
        self.assertNotIn(('holiday', 'Too far away'), got)
        dates = [e['date'] for e in self.get().context['dx_events']]
        self.assertEqual(dates, sorted(dates))

    def test_a_leap_day_birthday_and_a_joiner_today_do_not_break_anything(self):
        self.assertEqual(dashboard_extras._next_occurrence(datetime.date(2000, 2, 29), datetime.date(2027, 1, 1)), datetime.date(2027, 2, 28))
        self.assertEqual(dashboard_extras._next_occurrence(datetime.date(2000, 2, 29), datetime.date(2028, 1, 1)), datetime.date(2028, 2, 29))
        person(self.co, 'LP', 'lp@dash.test', date_of_birth=datetime.date(2000, 2, 29))
        person(self.co, 'T0', 't0@dash.test', date_of_joining=self.today)
        self.get()

    def test_people_who_left_are_not_congratulated(self):
        person(self.co, 'GONE', 'gone@dash.test', employment_status='RESIGNED', date_of_birth=datetime.date(1990, self.today.month, self.today.day))
        self.assertNotIn(('birthday', 'GONE Person'), self.events())

    def test_the_names_are_escaped_on_the_page(self):
        person(self.co, 'X1', 'x1@dash.test', first_name='<script>alert(1)</script>', date_of_birth=datetime.date(1990, self.today.month, self.today.day) + days(2))
        h = self.get().content.decode()
        self.assertNotIn('<script>alert(1)</script>', h)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', h)


class PrivacyAndSafetyTests(Base):
    def test_nothing_of_another_company_shows_up_or_is_counted(self):
        theirs = person(self.other, 'Z1', 'z1@dash.test', first_name='Zed', date_of_birth=datetime.date(1990, self.today.month, self.today.day) + days(1))
        LeaveRequest.objects.create(employee=theirs, leave_type='CASUAL', from_date=self.today, to_date=self.today, status='PENDING')
        PayrollRun.objects.create(company=self.other, month='August 2026', status='DRAFT')
        EmployeeDocument.objects.create(company=self.other, employee=theirs, doc_type='OTHER', data=b'x', expiry_date=self.today + days(2))
        cal = WorkingCalendar.objects.create(company=self.other, name='Theirs', is_default=True)
        CalendarDay.objects.create(calendar=cal, date=self.today + days(3), day_type='PUBLIC_HOLIDAY', name='Their holiday')
        r = self.get()
        self.assertNotContains(r, 'Zed')
        self.assertNotContains(r, 'Their holiday')
        self.assertEqual(r.context['dx_events'], [])
        self.assertEqual((r.context['dx_attention']['alerts'], r.context['dx_attention']['total']), ([], 0 + len(r.context['dx_attention']['alerts'])))
        self.assertEqual(r.context['dx_setup']['done'], 0)
        self.assertFalse(r.context['dx_setup']['steps'][1]['done'])

    def test_a_panel_that_fails_is_skipped_and_logged_and_the_rest_of_the_dashboard_still_shows(self):
        with mock.patch('payroll_app.dashboard_extras._events', side_effect=RuntimeError('boom')):
            with self.assertLogs('payroll_app.dashboard_extras', level='ERROR'):
                r = self.get()
        self.assertEqual(r.context['dx_events'], [])
        self.assertContains(r, 'Setup progress')
        self.assertContains(r, 'Active Employees')
        with mock.patch('payroll_app.dashboard_extras._setup_progress', side_effect=RuntimeError('boom')):
            with self.assertLogs('payroll_app.dashboard_extras', level='ERROR'):
                r = self.get()
        self.assertNotContains(r, 'Setup progress')
        self.assertContains(r, 'Needs your attention')
        self.assertContains(r, 'Latest Payroll Run')

    def test_the_dashboard_does_not_run_a_query_for_every_employee(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def queries():
            with CaptureQueriesContext(connection) as ctx:
                self.get()
            return len(ctx)

        for i in range(5):
            person(self.co, 'Q%d' % i, 'q%d@dash.test' % i, date_of_birth=datetime.date(1990, 1, 1))
        few = queries()
        for i in range(5, 40):
            person(self.co, 'Q%d' % i, 'q%d@dash.test' % i, date_of_birth=datetime.date(1990, 1, 1))
        self.assertEqual(few, queries())
