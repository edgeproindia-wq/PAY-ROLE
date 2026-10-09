"""Pay Cycle + Working Calendar: working days come from the calendar, never from a fixed number."""
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .models import (Attendance, AuditLog, CalendarDay, Employee, LeaveRequest, Notification, PayCycle,
                     PayrollRun, PayrollRunLine, SalaryStructure, WorkingCalendar)
from .pay_cycle_calc import CalendarRules
from .tests import make_company, make_employee, make_user

D = Decimal
d = datetime.date


def salary(emp, basic=15000, hra=6000, conv=1600, special=7400):          # gross 30,000
    SalaryStructure.objects.create(employee=emp, basic=basic, hra=hra, conveyance=conv, special_allowance=special)


class CalendarMathTests(TestCase):
    def setUp(self):
        self.co = make_company('Cal Math Co')
        self.cal = WorkingCalendar.objects.create(company=self.co, name='Std', is_default=True)
        CalendarDay.objects.create(calendar=self.cal, date=d(2026, 10, 2), day_type='PUBLIC_HOLIDAY', name='Gandhi Jayanti')

    def summary(self):
        return CalendarRules.load(WorkingCalendar.objects.get(pk=self.cal.pk)).summary(d(2026, 10, 1), d(2026, 10, 31))

    def test_october_2026_has_26_working_days(self):
        s = self.summary()
        self.assertEqual((s['total_days'], s['working_days'], s['weekly_offs'], s['holidays']), (31, 26, 4, 1))

    def test_special_working_half_day_and_optional_holiday(self):
        CalendarDay.objects.create(calendar=self.cal, date=d(2026, 10, 11), day_type='SPECIAL_WORKING')   # a Sunday worked
        CalendarDay.objects.create(calendar=self.cal, date=d(2026, 10, 9), day_type='OPTIONAL_HOLIDAY')   # still a working day
        CalendarDay.objects.create(calendar=self.cal, date=d(2026, 10, 10), day_type='HALF_DAY')          # still a working day
        s = self.summary()
        self.assertEqual((s['working_days'], s['special_working_days'], s['half_days'], s['optional_holidays']), (27, 1, 1, 1))

    def test_other_weekly_off_pattern(self):
        WorkingCalendar.objects.filter(pk=self.cal.pk).update(weekly_off_days='5,6')                      # Saturday and Sunday
        s = self.summary()
        self.assertEqual((s['weekly_offs'], s['working_days']), (9, 21))


class PayCycleRulesTests(TestCase):
    def setUp(self):
        self.co = make_company('Rules Co')

    def cycle(self, **kw):
        base = dict(company=self.co, name='October 2026 Payroll', cycle_type='MONTHLY', payroll_month='October 2026',
                    start_date=d(2026, 10, 1), end_date=d(2026, 10, 31))
        base.update(kw)
        return PayCycle(**base)

    def test_a_valid_cycle_saves_and_normalises_the_month(self):
        c = self.cycle(payroll_month='oct 2026')
        c.full_clean(); c.save()
        self.assertEqual(c.payroll_month, 'October 2026')

    def test_overlap_duplicate_and_bad_dates_are_refused(self):
        self.cycle().save()
        with self.assertRaises(ValidationError):
            self.cycle(name='Overlap', payroll_month='November 2026', start_date=d(2026, 10, 20), end_date=d(2026, 11, 19)).full_clean()
        with self.assertRaises(ValidationError):
            self.cycle(name='Same month again').full_clean()
        with self.assertRaises(ValidationError):
            self.cycle(name='Backwards', payroll_month='December 2026', start_date=d(2026, 12, 31), end_date=d(2026, 12, 1)).full_clean()
        with self.assertRaises(ValidationError):
            self.cycle(name='Bad month', payroll_month='Octobr', start_date=d(2026, 12, 1), end_date=d(2026, 12, 31)).full_clean()
        with self.assertRaises(ValidationError):
            self.cycle(name='Pay before processing', payroll_month='November 2026', start_date=d(2026, 11, 1), end_date=d(2026, 11, 30),
                       processing_date=d(2026, 12, 1), pay_date=d(2026, 11, 28)).full_clean()

    def test_next_month_is_fine(self):
        self.cycle().save()
        self.cycle(name='November 2026 Payroll', payroll_month='November 2026', start_date=d(2026, 11, 1), end_date=d(2026, 11, 30)).full_clean()

    def test_other_companies_can_use_the_same_month(self):
        self.cycle().save()
        other = make_company('Other Rules Co')
        PayCycle(company=other, name='October 2026 Payroll', cycle_type='MONTHLY', payroll_month='October 2026',
                 start_date=d(2026, 10, 1), end_date=d(2026, 10, 31)).full_clean()


class PayCycleProcessingTests(TestCase):
    def setUp(self):
        self.co, self.other = make_company('Cycle Co'), make_company('Cycle Other')
        self.owner = make_user('cycleowner', 'COMPANY_OWNER', self.co)
        self.owner2 = make_user('cycleowner2', 'COMPANY_OWNER', self.other)
        self.cal = WorkingCalendar.objects.create(company=self.co, name='Std', is_default=True)
        CalendarDay.objects.create(calendar=self.cal, date=d(2026, 10, 2), day_type='PUBLIC_HOLIDAY')
        self.emp = make_employee(self.co, 'PC1', 'pc1@example.com')
        salary(self.emp)
        self.cycle = PayCycle.objects.create(company=self.co, name='October 2026 Payroll', payroll_month='October 2026',
                                             start_date=d(2026, 10, 1), end_date=d(2026, 10, 31),
                                             processing_date=d(2026, 11, 1), pay_date=d(2026, 11, 5))
        self.client.force_login(self.owner)

    def act(self, action, cycle=None):
        return self.client.post(reverse('pay_cycle_action', args=[(cycle or self.cycle).pk]), {'action': action}, follow=True)

    def line(self, emp=None):
        return PayrollRunLine.objects.get(payroll_run__pay_cycle=self.cycle, employee=emp or self.emp)

    def absent(self, *days):
        for day in days:
            Attendance.objects.create(employee=self.emp, date=day, status='ABSENT')

    # ---- the worked example from the requirement
    def test_30000_salary_26_working_days_2_lop_days(self):
        self.absent(d(2026, 10, 5), d(2026, 10, 6), d(2026, 10, 4))        # 4 Oct is a Sunday: not a working day
        self.act('process')
        ln = self.line()
        self.assertEqual((ln.days_in_month, ln.lop_days, ln.lop_amount), (26, D('2.0'), D('2307.69')))   # 30,000 / 26 * 2
        self.assertEqual((ln.pf, ln.net_pay), (D('1661.54'), D('26030.77')))
        self.assertEqual(self.cycle.__class__.objects.get(pk=self.cycle.pk).status, 'PROCESSING')

    def test_calendar_change_flows_into_recalculation(self):
        self.absent(d(2026, 10, 5), d(2026, 10, 6))
        self.act('process')
        self.assertEqual(self.line().lop_amount, D('2307.69'))
        CalendarDay.objects.create(calendar=self.cal, date=d(2026, 10, 20), day_type='COMPANY_HOLIDAY')    # 26 -> 25 working days
        self.act('recalculate')
        ln = self.line()
        self.assertEqual((ln.days_in_month, ln.lop_amount), (25, D('2400.00')))                           # 30,000 / 25 * 2
        self.assertEqual(PayrollRunLine.objects.filter(payroll_run__pay_cycle=self.cycle).count(), 1)     # no duplicates
        self.assertEqual(PayrollRun.objects.filter(company=self.co).count(), 1)

    def test_calendar_days_policy_keeps_the_original_method(self):
        PayCycle.objects.filter(pk=self.cycle.pk).update(salary_policy='CALENDAR_DAYS')
        self.absent(d(2026, 10, 5), d(2026, 10, 6))
        self.act('process')
        ln = self.line()
        self.assertEqual((ln.days_in_month, ln.lop_amount), (31, D('1935.48')))                           # 30,000 / 31 * 2

    def test_joiners_and_future_joiners(self):
        mid = make_employee(self.co, 'PC2', 'pc2@example.com'); salary(mid)
        late = make_employee(self.co, 'PC3', 'pc3@example.com'); salary(late)
        Employee.objects.filter(pk=mid.pk).update(date_of_joining=d(2026, 10, 15))
        Employee.objects.filter(pk=late.pk).update(date_of_joining=d(2026, 11, 5))
        self.act('process')
        self.assertFalse(PayrollRunLine.objects.filter(payroll_run__pay_cycle=self.cycle, employee=late).exists())
        ln = self.line(mid)
        # working days before 15 Oct: 1, 3, 5-9, 10, 12-14 = 11 (2 Oct holiday, 4 and 11 Sundays are not counted)
        self.assertEqual((ln.days_in_month, ln.lop_days, ln.total_earnings), (26, D('0.0'), D('17307.69')))   # 30,000 - 30,000/26*11

    def test_unpaid_leave_only_counts_working_days(self):
        LeaveRequest.objects.create(employee=self.emp, leave_type='UNPAID', status='APPROVED',
                                    from_date=d(2026, 10, 9), to_date=d(2026, 10, 13))                    # Fri, Sat, SUN, Mon, Tue
        self.act('process')
        self.assertEqual(self.line().lop_days, D('4.0'))

    def test_half_day_counts_half(self):
        Attendance.objects.create(employee=self.emp, date=d(2026, 10, 7), status='HALF_DAY')
        self.act('process')
        self.assertEqual(self.line().lop_days, D('0.5'))

    def test_unmarked_working_days_can_count_as_lop(self):
        past = PayCycle.objects.create(company=self.co, name='January 2026 Payroll', payroll_month='January 2026',
                                       start_date=d(2026, 1, 1), end_date=d(2026, 1, 31), unmarked_as_lop=True)
        working = [x for x in (d(2026, 1, 1) + datetime.timedelta(days=i) for i in range(31)) if x.weekday() != 6]
        for day in working[:20]:
            Attendance.objects.create(employee=self.emp, date=day, status='PRESENT')
        self.client.post(reverse('pay_cycle_action', args=[past.pk]), {'action': 'process'})
        ln = PayrollRunLine.objects.get(payroll_run__pay_cycle=past, employee=self.emp)
        self.assertEqual((ln.days_in_month, ln.lop_days), (27, D('7.0')))                                  # 27 working days, 20 marked
        off = PayCycle.objects.create(company=self.co, name='February 2026 Payroll', payroll_month='February 2026',
                                      start_date=d(2026, 2, 1), end_date=d(2026, 2, 28))
        self.client.post(reverse('pay_cycle_action', args=[off.pk]), {'action': 'process'})
        self.assertEqual(PayrollRunLine.objects.get(payroll_run__pay_cycle=off, employee=self.emp).lop_days, D('0.0'))

    # ---- status flow, locking, audit
    def test_full_flow_locking_and_audit_trail(self):
        login = make_user('pcemp', 'EMPLOYEE', self.co)
        Employee.objects.filter(pk=self.emp.pk).update(user=login)
        self.act('process')
        self.act('approve')                                                   # refused: not validated yet
        self.assertEqual(PayrollRun.objects.get(pay_cycle=self.cycle).status, 'DRAFT')
        for a in ('validate', 'approve'):
            self.act(a)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.sync_status(), 'PROCESSED')
        self.act('release')
        self.assertTrue(Notification.objects.filter(recipient=login).exists())
        self.act('lock')
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.status, 'LOCKED')
        before = self.line().net_pay
        CalendarDay.objects.create(calendar=self.cal, date=d(2026, 10, 21), day_type='COMPANY_HOLIDAY')
        resp = self.act('recalculate')                                         # refused: locked / released
        self.assertContains(resp, 'locked')
        self.assertEqual(self.line().net_pay, before)
        entries = AuditLog.objects.filter(model_name='PayCycle', object_id=str(self.cycle.pk))
        self.assertGreaterEqual(entries.count(), 4)
        self.assertTrue(all(e.actor_id == self.owner.pk for e in entries))

    def test_weekly_cycle_can_be_defined_but_not_processed_yet(self):
        wk = PayCycle.objects.create(company=self.co, name='Week 41', cycle_type='WEEKLY', payroll_month='October 2026',
                                     start_date=d(2026, 11, 2), end_date=d(2026, 11, 8))
        resp = self.act('process', wk)
        self.assertContains(resp, 'monthly pay cycles')
        self.assertFalse(PayrollRun.objects.filter(pay_cycle=wk).exists())

    def test_existing_manual_run_for_the_month_blocks_processing(self):
        PayrollRun.objects.create(company=self.co, month='October 2026')
        resp = self.act('process')
        self.assertContains(resp, 'already exists')
        self.cycle.refresh_from_db()
        self.assertIsNone(self.cycle.payroll_run_id)

    def test_delete_only_before_processing(self):
        keep = PayCycle.objects.create(company=self.co, name='November 2026 Payroll', payroll_month='November 2026',
                                       start_date=d(2026, 11, 1), end_date=d(2026, 11, 30))
        self.act('delete', keep)
        self.assertFalse(PayCycle.objects.filter(pk=keep.pk).exists())
        self.act('process'); self.act('delete')
        self.assertTrue(PayCycle.objects.filter(pk=self.cycle.pk).exists())

    # ---- pages
    def test_dashboard_numbers_and_pages(self):
        other_emp = make_employee(self.co, 'PC9', 'pc9@example.com')                     # active, no salary structure -> pending
        self.act('process')
        page = self.client.get(reverse('pay_cycles'))
        s = page.context['latest'].stats
        self.assertEqual((s['working_days'], s['eligible'], s['processed'], s['pending']), (26, 2, 1, 1))
        self.assertContains(page, 'October 2026 Payroll')
        detail = self.client.get(reverse('pay_cycle_detail', args=[self.cycle.pk]))
        self.assertContains(detail, 'PC9')                                                # listed as pending
        emp_page = self.client.get(reverse('pay_cycle_employee', args=[self.cycle.pk, self.emp.pk]))
        self.assertEqual(emp_page.context['summary']['working_days'], 26)

    def test_export_csv(self):
        self.act('process')
        resp = self.client.get(reverse('pay_cycle_export', args=[self.cycle.pk]))
        self.assertIn('PC1', resp.content.decode())
        self.assertIn('text/csv', resp['Content-Type'])

    def test_create_and_edit_through_the_form(self):
        resp = self.client.post(reverse('pay_cycle_new'), {
            'name': 'November 2026 Payroll', 'cycle_type': 'MONTHLY', 'payroll_month': 'November 2026',
            'start_date': '2026-11-01', 'end_date': '2026-11-30', 'processing_date': '2026-12-01', 'pay_date': '2026-12-05',
            'salary_policy': 'WORKING_DAYS'}, follow=True)
        new = PayCycle.objects.get(name='November 2026 Payroll')
        self.assertContains(resp, 'saved')
        bad = self.client.post(reverse('pay_cycle_new'), {
            'name': 'Clash', 'cycle_type': 'MONTHLY', 'payroll_month': 'November 2026', 'start_date': '2026-11-10',
            'end_date': '2026-11-20', 'salary_policy': 'WORKING_DAYS'})
        self.assertContains(bad, 'overlaps')
        self.assertEqual(PayCycle.objects.filter(company=self.co).count(), 2)
        self.act('process', new)
        locked_edit = self.client.post(reverse('pay_cycle_edit', args=[new.pk]), {'name': 'Renamed', 'processing_date': '2026-12-02', 'pay_date': '2026-12-06'}, follow=True)
        new.refresh_from_db()
        self.assertEqual((new.name, new.start_date), ('Renamed', d(2026, 11, 1)))                  # name changes, period does not

    def test_working_calendar_page(self):
        r = self.client.post(reverse('working_calendar'), {'action': 'save_calendar', 'name': 'Std', 'weekly_offs': ['5', '6']}, follow=True)
        self.cal.refresh_from_db()
        self.assertEqual(self.cal.weekly_off_days, '5,6')
        self.client.post(reverse('working_calendar'), {'action': 'add_day', 'date': '2026-10-20', 'day_type': 'COMPANY_HOLIDAY', 'name': 'Founders day'})
        self.assertTrue(CalendarDay.objects.filter(calendar=self.cal, date=d(2026, 10, 20)).exists())
        dup = self.client.post(reverse('working_calendar'), {'action': 'add_day', 'date': '2026-10-20', 'day_type': 'PUBLIC_HOLIDAY'}, follow=True)
        self.assertContains(dup, 'already has an entry')
        entry = CalendarDay.objects.get(calendar=self.cal, date=d(2026, 10, 20))
        self.client.post(reverse('working_calendar'), {'action': 'delete_day', 'day_id': entry.pk})
        self.assertFalse(CalendarDay.objects.filter(pk=entry.pk).exists())
        page = self.client.get(reverse('working_calendar'), {'month': '2026-10'})
        self.assertEqual(page.context['summary']['working_days'], 21 - 0)                         # Sat+Sun off, 2 Oct holiday: 31-9-1

    # ---- isolation
    def test_other_company_and_employees_cannot_reach_it(self):
        self.act('process')
        self.client.force_login(self.owner2)
        self.assertEqual(self.client.get(reverse('pay_cycle_detail', args=[self.cycle.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('pay_cycle_action', args=[self.cycle.pk]), {'action': 'lock'}).status_code, 404)
        self.assertEqual(self.client.get(reverse('pay_cycle_export', args=[self.cycle.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('pay_cycle_employee', args=[self.cycle.pk, self.emp.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse('pay_cycles')), 'October 2026 Payroll')
        emp_user = make_user('pcemp2', 'EMPLOYEE', self.co)
        self.client.force_login(emp_user)
        for name, args in (('pay_cycles', []), ('working_calendar', []), ('pay_cycle_detail', [self.cycle.pk])):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403, name)

    def test_runs_without_a_pay_cycle_are_calculated_as_before(self):
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': 'November 2026'}, follow=True)
        ln = PayrollRunLine.objects.get(payroll_run__month='November 2026', employee=self.emp)
        self.assertEqual(ln.days_in_month, 30)                                                         # calendar days, no cycle involved
