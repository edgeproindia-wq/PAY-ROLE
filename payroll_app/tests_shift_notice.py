"""Shifts page: a clear message while no shift exists, and shifts show their timing in the Assign shift list."""
import re

from django.test import TestCase
from django.urls import reverse

from .models import Shift, ShiftAssignment
from .tests import make_company, make_employee, make_user


def shift_options(html):
    m = re.search(r'<select[^>]*name="shift".*?</select>', html, re.S)
    return re.findall(r'<option[^>]*>([^<]*)</option>', m.group(0)) if m else None


class ShiftsPageTests(TestCase):
    def setUp(self):
        self.co, self.other = make_company('Shift Co'), make_company('Shift Other')
        self.owner = make_user('shiftowner', 'COMPANY_OWNER', self.co)
        self.emp = make_employee(self.co, 'SH1', 'sh1@shift.test')
        self.client.force_login(self.owner)
        self.url = reverse('hr_shifts')

    def test_with_no_shift_the_page_explains_what_to_do(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('in the <b>New shift</b> box above', html)

    def test_a_created_shift_appears_in_the_dropdown_with_its_timing_and_the_message_goes(self):
        self.client.post(self.url, {'form': 'shift', 'name': 'General', 'start_time': '09:00', 'end_time': '18:00', 'break_minutes': '60', 'grace_minutes': '10'})
        html = self.client.get(self.url).content.decode()
        self.assertIn('General (09:00-18:00)', shift_options(html))
        self.assertNotIn('in the <b>New shift</b> box above', html)

    def test_an_overnight_shift_shows_both_times_and_another_companys_shift_is_not_listed(self):
        Shift.objects.create(company=self.co, name='Night', start_time='22:00', end_time='06:00')
        Shift.objects.create(company=self.other, name='Secret', start_time='08:00', end_time='17:00')
        opts = shift_options(self.client.get(self.url).content.decode())
        self.assertIn('Night (22:00-06:00)', opts)
        self.assertFalse(any('Secret' in o for o in opts))

    def test_a_deactivated_shift_is_not_offered_and_the_message_returns(self):
        s = Shift.objects.create(company=self.co, name='Old', start_time='07:00', end_time='15:00')
        self.client.post(self.url, {'deactivate': s.pk})
        html = self.client.get(self.url).content.decode()
        self.assertFalse(any('Old' in o for o in shift_options(html)))
        self.assertIn('in the <b>New shift</b> box above', html)

    def test_assigning_a_shift_still_works(self):
        s = Shift.objects.create(company=self.co, name='General', start_time='09:00', end_time='18:00')
        resp = self.client.post(self.url, {'form': 'assign', 'employee': self.emp.pk, 'shift': s.pk, 'effective_from': '2026-10-01'}, follow=True)
        self.assertEqual(ShiftAssignment.objects.filter(employee=self.emp, shift=s).count(), 1)
        self.assertContains(resp, 'assigned to General')

    def test_other_forms_do_not_get_the_shift_message(self):
        for name in ('salary_structure', 'attendance'):
            self.assertNotIn('in the <b>New shift</b> box above', self.client.get(reverse(name)).content.decode(), name)