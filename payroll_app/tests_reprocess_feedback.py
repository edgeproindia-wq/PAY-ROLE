"""Reprocessing a draft payroll run: it recalculates, says who was left out and why, and the page's double-submit guard
never blocks a click after a cancelled confirmation."""
import datetime
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from .models import Attendance, PayrollRun, SalaryStructure
from .tests import make_company, make_employee, make_user


class ReprocessTests(TestCase):
    def setUp(self):
        self.co = make_company('Reproc Co')
        self.owner = make_user('reproc_owner', 'COMPANY_OWNER', self.co)
        self.emp = make_employee(self.co, 'RP1', 'rp1@example.com')
        self.emp.date_of_joining = datetime.date(2024, 1, 1)
        self.emp.save()
        SalaryStructure.objects.create(employee=self.emp, basic=30000, hra=12000, conveyance=1600, special_allowance=6400)
        self.client.force_login(self.owner)
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': 'November 2026'})
        self.run = PayrollRun.objects.get(company=self.co)

    def reprocess(self):
        r = self.client.post(reverse('payroll_combined'), {'action': 'reprocess', 'run_id': self.run.pk}, follow=True)
        return [(m.level_tag, str(m)) for m in r.context['messages']]

    def test_reprocess_recalculates_after_an_attendance_change(self):
        self.assertEqual(self.run.lines.get().lop_days, D('0'))
        Attendance.objects.create(employee=self.emp, date=datetime.date(2026, 11, 3), status='ABSENT')
        msgs = self.reprocess()
        self.assertIn(('success', "'November 2026' reprocessed successfully with 1 employee(s)."), msgs)
        line = self.run.lines.get()
        self.assertEqual(line.lop_days, D('1.0'))
        self.assertEqual(self.run.lines.count(), 1)                    # replaced, never duplicated
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, 'DRAFT')

    def test_reprocess_names_an_employee_who_was_left_out_for_having_no_salary_structure(self):
        make_employee(self.co, 'RP2', 'rp2@example.com')               # active, joined 2024, no salary structure
        msgs = self.reprocess()
        self.assertTrue(any(level == 'warning' and 'RP2' in text and 'no salary structure' in text for level, text in msgs), msgs)
        self.assertEqual(self.run.lines.count(), 1)

    def test_reprocess_says_when_people_join_after_the_month(self):
        late = make_employee(self.co, 'RP3', 'rp3@example.com')
        late.date_of_joining = datetime.date(2027, 2, 1)
        late.save()
        msgs = self.reprocess()
        self.assertTrue(any(level == 'info' and 'join after November 2026' in text for level, text in msgs), msgs)

    def test_a_locked_or_released_run_is_refused_with_a_reason(self):
        PayrollRun.objects.filter(pk=self.run.pk).update(is_locked=True)
        self.assertTrue(any('is locked' in text for _, text in self.reprocess()))
        PayrollRun.objects.filter(pk=self.run.pk).update(is_locked=False, status='RELEASED')
        self.assertTrue(any('already been released' in text for _, text in self.reprocess()))


class DoubleSubmitGuardTests(TestCase):
    def test_the_guard_counts_only_a_submit_that_went_ahead(self):
        co = make_company('Guard Co')
        owner = make_user('guard_owner', 'COMPANY_OWNER', co)
        self.client.force_login(owner)
        page = self.client.get(reverse('dashboard')).content.decode()
        guard = page.split('data-submit-guard')[1].split('</script>')[0]
        self.assertIn('e.defaultPrevented', guard)                     # a cancelled confirm() does not mark the form as sent
        self.assertIn('true);', guard)                                  # the first listener (capture phase) only refuses a second submit
