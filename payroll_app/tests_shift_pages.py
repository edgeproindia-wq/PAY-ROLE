import datetime
from django.test import TestCase
from django.urls import reverse
from .models import Attendance
from .models_phase4 import Shift, ShiftAssignment
from .phase4 import shift_for
from .tests import make_company, make_employee, make_user

T = datetime.time


class ShiftPageTests(TestCase):
    def setUp(self):
        self.c = make_company('Acme')
        self.owner = make_user('own', 'OWNER' if False else self._role(), self.c)
        self.e1, self.e2 = make_employee(self.c, 'E1', 'a@x.com'), make_employee(self.c, 'E2', 'b@x.com')
        self.other_co = make_company('Other')
        self.stranger = make_employee(self.other_co, 'X1', 'x@x.com')
        self.day = make_shift = Shift.objects.create(company=self.c, name='Day', start_time=T(9, 0), end_time=T(18, 0), break_minutes=60, grace_minutes=10)
        self.client.force_login(self.owner)

    @staticmethod
    def _role():
        from .models import User
        return [r[0] for r in User._meta.get_field('role').choices if 'OWNER' in r[0].upper() or 'COMPANY' in r[0].upper()][0]

    def test_pages_open(self):
        self.assertEqual(self.client.get(reverse('shift_assign')).status_code, 200)
        self.assertEqual(self.client.get(reverse('hr_day_hours')).status_code, 200)

    def test_bulk_assign_gives_the_shift_to_everyone_ticked(self):
        r = self.client.post(reverse('shift_assign'), {'shift': self.day.pk, 'effective_from': '2026-11-01', 'employee': [self.e1.pk, self.e2.pk]})
        self.assertEqual(r.status_code, 302)
        for e in (self.e1, self.e2):
            self.assertEqual(shift_for(e, datetime.date(2026, 11, 5)), self.day)

    def test_a_new_assignment_ends_the_earlier_one(self):
        night = Shift.objects.create(company=self.c, name='Night', start_time=T(21, 0), end_time=T(6, 0))
        self.client.post(reverse('shift_assign'), {'shift': self.day.pk, 'effective_from': '2026-11-01', 'employee': [self.e1.pk]})
        self.client.post(reverse('shift_assign'), {'shift': night.pk, 'effective_from': '2026-11-16', 'employee': [self.e1.pk]})
        self.assertEqual(shift_for(self.e1, datetime.date(2026, 11, 10)), self.day)
        self.assertEqual(shift_for(self.e1, datetime.date(2026, 11, 20)), night)

    def test_another_companys_employee_is_ignored(self):
        self.client.post(reverse('shift_assign'), {'shift': self.day.pk, 'effective_from': '2026-11-01', 'employee': [self.stranger.pk]})
        self.assertFalse(ShiftAssignment.objects.filter(employee=self.stranger).exists())

    def test_bad_input_changes_nothing(self):
        for data in ({'effective_from': '2026-11-01', 'employee': [self.e1.pk]},
                     {'shift': self.day.pk, 'employee': [self.e1.pk]},
                     {'shift': self.day.pk, 'effective_from': '2026-11-01'},
                     {'shift': self.day.pk, 'effective_from': '2026-11-10', 'effective_to': '2026-11-01', 'employee': [self.e1.pk]}):
            self.client.post(reverse('shift_assign'), data)
        self.assertEqual(ShiftAssignment.objects.count(), 0)

    def test_day_hours_shows_hours_and_late(self):
        ShiftAssignment.objects.create(company=self.c, employee=self.e1, shift=self.day, effective_from=datetime.date(2026, 11, 1))
        d = datetime.date(2026, 11, 10)
        Attendance.objects.create(employee=self.e1, date=d, status='PRESENT', check_in=T(9, 40), check_out=T(19, 0))
        r = self.client.get(reverse('hr_day_hours'), {'date': '2026-11-10'})
        self.assertContains(r, '8h 20m')
        self.assertContains(r, 'Late')
        self.assertContains(r, 'No record')                      # E2 has none
        self.assertEqual(r.context['counts']['late'], 1)

    def test_bad_date_falls_back_to_today_and_csv_downloads(self):
        self.assertEqual(self.client.get(reverse('hr_day_hours'), {'date': 'nonsense'}).status_code, 200)
        r = self.client.get(reverse('hr_day_hours'), {'date': '2026-11-10', 'export': 'csv'})
        self.assertEqual(r['Content-Type'], 'text/csv')
        self.assertIn('Employee code', r.content.decode())

    def test_day_hours_never_lists_another_company(self):
        r = self.client.get(reverse('hr_day_hours'))
        self.assertNotContains(r, 'X1')

    def test_login_is_required(self):
        self.client.logout()
        self.assertNotEqual(self.client.get(reverse('shift_assign')).status_code, 200)
        self.assertNotEqual(self.client.get(reverse('hr_day_hours')).status_code, 200)
