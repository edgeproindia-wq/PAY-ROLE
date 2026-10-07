"""Assign shift with an optional "Effective to" date."""
import datetime

from django.urls import reverse

from .models_phase4 import Shift, ShiftAssignment
from .phase4 import shift_for
from .tests_phase4 import Base

D = datetime.date


class ShiftEffectiveToTests(Base):
    def setUp(self):
        super().setUp()
        self.night = Shift.objects.create(company=self.c1, name='Night', start_time=datetime.time(22),
                                          end_time=datetime.time(6), break_minutes=30)
        self.login(self.owner)

    def assign(self, shift, start, end=''):
        return self.client.post(reverse('hr_shifts'), {
            'form': 'assign', 'employee': self.e1.pk, 'shift': shift.pk,
            'effective_from': start, 'effective_to': end}, follow=True)

    def rows(self):
        return [(a.shift.name, a.effective_from, a.effective_to)
                for a in ShiftAssignment.objects.filter(employee=self.e1).order_by('effective_from')]

    def test_effective_to_box_is_on_the_page(self):
        self.assertContains(self.client.get(reverse('hr_shifts')), 'Effective to')

    def test_blank_end_date_behaves_as_before(self):
        self.assign(self.day, '2026-09-01')
        self.assign(self.night, '2026-09-15')
        self.assertEqual(self.rows(), [('Day', D(2026, 9, 1), D(2026, 9, 14)), ('Night', D(2026, 9, 15), None)])

    def test_short_change_then_back_to_the_old_shift(self):
        self.assign(self.day, '2026-01-01')
        self.assign(self.night, '2026-09-01', '2026-09-07')
        self.assertEqual(self.rows(), [('Day', D(2026, 1, 1), D(2026, 8, 31)),
                                       ('Night', D(2026, 9, 1), D(2026, 9, 7)),
                                       ('Day', D(2026, 9, 8), None)])
        self.assertEqual(shift_for(self.e1, D(2026, 8, 31)), self.day)
        self.assertEqual(shift_for(self.e1, D(2026, 9, 3)), self.night)
        self.assertEqual(shift_for(self.e1, D(2026, 9, 8)), self.day)

    def test_period_in_front_of_a_later_assignment(self):
        self.assign(self.day, '2026-09-10')
        self.assign(self.night, '2026-09-01', '2026-09-12')
        self.assertEqual(self.rows(), [('Night', D(2026, 9, 1), D(2026, 9, 12)), ('Day', D(2026, 9, 13), None)])

    def test_period_that_fully_covers_an_old_one_replaces_it(self):
        self.assign(self.day, '2026-09-05', '2026-09-06')
        self.assign(self.night, '2026-09-01', '2026-09-30')
        self.assertEqual(self.rows(), [('Night', D(2026, 9, 1), D(2026, 9, 30))])

    def test_end_before_start_is_refused(self):
        resp = self.assign(self.night, '2026-09-10', '2026-09-01')
        self.assertContains(resp, 'cannot be before')
        self.assertEqual(self.rows(), [])