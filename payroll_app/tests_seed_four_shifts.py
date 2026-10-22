"""The step that adds the four shifts to one named company when the site is deployed."""
import datetime
import glob
import importlib
import os
from unittest import mock

from django.apps import apps as django_apps
from django.test import TestCase

from .models_phase4 import Shift
from .tests import make_company

T = datetime.time


def seed_module():
    here = os.path.dirname(__file__)
    found = glob.glob(os.path.join(here, 'migrations', '0*_seed_four_shifts.py'))
    assert len(found) == 1, found
    return importlib.import_module('payroll_app.migrations.' + os.path.basename(found[0])[:-3])


class SeedTests(TestCase):
    def setUp(self):
        self.mod = seed_module()
        self.co, self.other = make_company('Seed Co'), make_company('Another Client Co')

    def run_for(self, name):
        with mock.patch.object(self.mod, 'COMPANY_NAME', name):
            self.mod.add_shifts(django_apps, None)

    def shifts(self, company):
        return {s.name: s for s in Shift.objects.filter(company=company)}

    def test_it_adds_the_four_shifts_with_your_timings_to_the_named_company_only(self):
        self.run_for('Seed Co')
        s = self.shifts(self.co)
        self.assertEqual({n: (v.start_time, v.end_time) for n, v in s.items()},
                         {'First Shift': (T(6, 30), T(14, 30)), 'Second Shift': (T(14, 30), T(0, 30)),
                          'General Shift': (T(10), T(20)), 'Third Shift': (T(0, 30), T(6))})
        self.assertTrue(all((v.break_minutes, v.grace_minutes, v.active) == (0, 10, True) for v in s.values()))
        self.assertEqual(Shift.objects.filter(company=self.other).count(), 0)            # no other company is touched

    def test_the_name_is_matched_ignoring_capitals_and_spaces_around_it(self):
        self.run_for('  seed co ')
        self.assertEqual(len(self.shifts(self.co)), 4)

    def test_a_shift_that_already_exists_is_never_changed_and_only_the_missing_ones_are_added(self):
        mine = Shift.objects.create(company=self.co, name='first shift', start_time=T(7), end_time=T(15), break_minutes=45, grace_minutes=3)
        self.run_for('Seed Co')
        mine.refresh_from_db()
        self.assertEqual((mine.start_time, mine.end_time, mine.break_minutes, mine.grace_minutes), (T(7), T(15), 45, 3))
        self.assertEqual(Shift.objects.filter(company=self.co).count(), 4)
        self.assertEqual(sorted(self.shifts(self.co)), ['General Shift', 'Second Shift', 'Third Shift', 'first shift'])

    def test_running_it_again_adds_nothing(self):
        self.run_for('Seed Co')
        ids = sorted(Shift.objects.values_list('pk', flat=True))
        self.run_for('Seed Co')
        self.assertEqual(sorted(Shift.objects.values_list('pk', flat=True)), ids)

    def test_a_name_that_matches_no_company_adds_nothing_and_does_not_fail(self):
        self.run_for('No Such Company')
        self.assertEqual(Shift.objects.count(), 0)

    def test_going_backwards_removes_nothing(self):
        self.run_for('Seed Co')
        reverse = self.mod.Migration.operations[0].reverse_code
        reverse(django_apps, None)
        self.assertEqual(Shift.objects.filter(company=self.co).count(), 4)

    def test_it_depends_on_the_latest_migration_so_it_runs_after_the_shift_tables_exist(self):
        deps = self.mod.Migration.dependencies
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0][0], 'payroll_app')


class ShiftsPageTests(TestCase):
    """The Shifts page has no add-your-shifts card any more: the four shifts are simply in the Assign shift list."""

    def setUp(self):
        from django.urls import reverse
        from .tests import make_user
        self.reverse = reverse
        self.mod = seed_module()
        self.co = make_company('Page Co')
        self.owner = make_user('pageowner', 'COMPANY_OWNER', self.co)
        self.client.force_login(self.owner)

    def test_the_card_and_its_button_are_gone(self):
        html = self.client.get(self.reverse('hr_shifts')).content.decode()
        self.assertNotIn('Add your shifts', html)
        self.assertNotIn('Add the ticked shifts', html)
        self.assertIn('<h2>Assign shift</h2>', html)                       # the Assign shift box is still there
        self.assertIn('<h2>New shift</h2>', html)
        from django.urls import NoReverseMatch
        with self.assertRaises(NoReverseMatch):
            self.reverse('shift_add_four')

    def test_after_the_step_runs_the_four_shifts_are_in_the_assign_shift_list_with_their_timings(self):
        with mock.patch.object(self.mod, 'COMPANY_NAME', 'Page Co'):
            self.mod.add_shifts(django_apps, None)
        html = self.client.get(self.reverse('hr_shifts')).content.decode()
        for label in ('First Shift (06:30-14:30)', 'Second Shift (14:30-00:30)', 'General Shift (10:00-20:00)', 'Third Shift (00:30-06:00)'):
            self.assertIn(label, html, label)
        self.assertNotIn('No shifts yet', html)

    def test_an_employee_can_be_assigned_to_one_of_them(self):
        from .models_phase4 import ShiftAssignment
        from .tests import make_employee
        with mock.patch.object(self.mod, 'COMPANY_NAME', 'Page Co'):
            self.mod.add_shifts(django_apps, None)
        emp = make_employee(self.co, 'PG1', 'pg1@page.test')
        shift = Shift.objects.get(company=self.co, name='Third Shift')
        self.client.post(self.reverse('hr_shifts'), {'form': 'assign', 'employee': emp.pk, 'shift': shift.pk, 'effective_from': '2026-10-01', 'effective_to': ''}, follow=True)
        self.assertEqual(ShiftAssignment.objects.get(employee=emp).shift, shift)
