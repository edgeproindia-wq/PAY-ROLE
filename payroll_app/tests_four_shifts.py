"""Add your four shifts from the Shifts page: exact timings, overnight, all-or-nothing, never changes an existing shift."""
import datetime

from django.test import RequestFactory, TestCase
from django.urls import reverse

from .models import AuditLog
from .models_phase4 import Shift, ShiftAssignment
from .shifts_four import FOUR
from .templatetags.four_shifts import four_shifts_card
from .tests import make_company, make_employee, make_user

T = datetime.time


def form(rows=(0, 1, 2, 3), **over):
    data = {'row': [str(i) for i in rows]}
    for i in rows:
        name, start, end = FOUR[i]
        data.update({f'add_{i}': 'on', f'name_{i}': name, f'start_{i}': start, f'end_{i}': end, f'break_{i}': '0', f'grace_{i}': '10'})
    data.update(over)
    return data


def minutes(s):
    return ((s.end_time.hour * 60 + s.end_time.minute) - (s.start_time.hour * 60 + s.start_time.minute)) % 1440


class Base(TestCase):
    def setUp(self):
        self.co, self.other = make_company('Four Co'), make_company('Other Four Co')
        self.owner = make_user('fourowner', 'COMPANY_OWNER', self.co)
        self.other_owner = make_user('otherfourowner', 'COMPANY_OWNER', self.other)
        self.client.force_login(self.owner)
        self.url = reverse('shift_add_four')
        self.page = reverse('hr_shifts')

    def add(self, data=None):
        return self.client.post(self.url, data if data is not None else form(), follow=True)

    def shifts(self, company=None):
        return {s.name: s for s in Shift.objects.filter(company=company or self.co)}


class CardTests(Base):
    def test_the_card_offers_the_four_shifts_with_the_timings_filled_in(self):
        html = self.client.get(self.page).content.decode()
        self.assertIn('Add your shifts', html)
        for name, start, end in FOUR:
            self.assertIn(f'value="{name}"', html)
            self.assertIn(f'value="{start}"', html)
        self.assertIn('next day', html)                                         # the Second Shift ends after midnight

    def test_the_card_is_gone_once_all_four_exist_and_shows_only_the_missing_ones_before_that(self):
        Shift.objects.create(company=self.co, name='First Shift', start_time=T(6, 30), end_time=T(14, 30))
        Shift.objects.create(company=self.co, name='general shift', start_time=T(9), end_time=T(18))        # a different spelling still counts as existing
        html = self.client.get(self.page).content.decode()
        self.assertNotIn('value="First Shift"', html)
        self.assertNotIn('value="General Shift"', html)
        self.assertIn('value="Second Shift"', html)
        self.assertIn('value="Third Shift"', html)
        Shift.objects.create(company=self.co, name='Second Shift', start_time=T(14, 30), end_time=T(0, 30))
        Shift.objects.create(company=self.co, name='Third Shift', start_time=T(0, 30), end_time=T(6))
        self.assertNotIn('Add your shifts', self.client.get(self.page).content.decode())

    def test_the_existing_new_shift_box_is_still_there(self):
        html = self.client.get(self.page).content.decode()
        self.assertIn('<h2>New shift</h2>', html)
        self.assertIn('<h2>Assign shift</h2>', html)

    def test_only_the_company_owner_sees_the_card(self):
        for user in (make_user('fouremp', 'EMPLOYEE', self.co), make_user('fouradmin', 'ADMIN')):
            request = RequestFactory().get('/x/')
            request.user = user
            self.assertEqual(four_shifts_card(dict(request=request)), {'rows': []})


class CreateTests(Base):
    def test_the_four_shifts_are_created_with_exactly_your_timings(self):
        resp = self.add()
        self.assertContains(resp, 'Added 4 shift(s)')
        s = self.shifts()
        self.assertEqual({n: (v.start_time, v.end_time) for n, v in s.items()},
                         {'First Shift': (T(6, 30), T(14, 30)), 'Second Shift': (T(14, 30), T(0, 30)),
                          'General Shift': (T(10), T(20)), 'Third Shift': (T(0, 30), T(6))})
        self.assertEqual({n: minutes(v) for n, v in s.items()}, {'First Shift': 480, 'Second Shift': 600, 'General Shift': 600, 'Third Shift': 330})
        self.assertTrue(all(v.active for v in s.values()))

    def test_the_second_shift_ends_after_midnight(self):
        self.add()
        s = self.shifts()['Second Shift']
        self.assertLess(s.end_time, s.start_time)
        self.assertEqual(minutes(s), 10 * 60)

    def test_they_show_in_the_assign_shift_list_with_their_timings(self):
        self.add()
        html = self.client.get(self.page).content.decode()
        for label in ('First Shift (06:30-14:30)', 'Second Shift (14:30-00:30)', 'General Shift (10:00-20:00)', 'Third Shift (00:30-06:00)'):
            self.assertIn(label, html, label)
        self.assertNotIn('No shifts yet', html)

    def test_an_employee_can_then_be_assigned_to_one(self):
        self.add()
        emp = make_employee(self.co, 'FS1', 'fs1@four.test')
        shift = self.shifts()['Second Shift']
        resp = self.client.post(self.page, {'form': 'assign', 'employee': emp.pk, 'shift': shift.pk, 'effective_from': '2026-10-01', 'effective_to': ''}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ShiftAssignment.objects.get(employee=emp).shift, shift)

    def test_the_break_and_the_grace_time_are_saved_as_typed(self):
        data = form(**{'break_0': '30', 'grace_0': '5', 'break_3': '0', 'grace_3': '0'})
        self.add(data)
        s = self.shifts()
        self.assertEqual((s['First Shift'].break_minutes, s['First Shift'].grace_minutes), (30, 5))
        self.assertEqual((s['Third Shift'].break_minutes, s['Third Shift'].grace_minutes), (0, 0))
        self.assertEqual((s['General Shift'].break_minutes, s['General Shift'].grace_minutes), (0, 10))      # the defaults: no break deducted, 10 minutes grace

    def test_the_times_can_be_adjusted_before_adding(self):
        self.add(form(**{'start_2': '09:30', 'end_2': '19:00', 'name_2': 'General'}))
        g = self.shifts()['General']
        self.assertEqual((g.start_time, g.end_time), (T(9, 30), T(19)))

    def test_an_unticked_shift_is_skipped_and_nothing_ticked_adds_nothing(self):
        data = form()
        del data['add_1']
        self.add(data)
        self.assertEqual(set(self.shifts()), {'First Shift', 'General Shift', 'Third Shift'})
        before = Shift.objects.count()
        self.assertContains(self.add({'row': ['0', '1']}), 'nothing was added')
        self.assertEqual(Shift.objects.count(), before)

    def test_a_second_press_adds_nothing_and_changes_nothing(self):
        self.add()
        first_ids = sorted(Shift.objects.values_list('pk', flat=True))
        resp = self.add()
        self.assertContains(resp, 'already exists')
        self.assertEqual(sorted(Shift.objects.values_list('pk', flat=True)), first_ids)

    def test_an_existing_shift_is_never_changed(self):
        mine = Shift.objects.create(company=self.co, name='First Shift', start_time=T(7), end_time=T(15), break_minutes=45, grace_minutes=3)
        self.add(form(rows=(0,)))
        mine.refresh_from_db()
        self.assertEqual((mine.start_time, mine.end_time, mine.break_minutes, mine.grace_minutes), (T(7), T(15), 45, 3))
        self.assertEqual(Shift.objects.filter(company=self.co).count(), 1)

    def test_every_creation_is_audited(self):
        self.add()
        self.assertTrue(AuditLog.objects.filter(action='CREATE', details__icontains='Shifts added: First Shift, Second Shift, General Shift, Third Shift').exists())


class ValidationTests(Base):
    def check_refused(self, over, text):
        resp = self.add(form(**over))
        self.assertContains(resp, text)
        self.assertContains(resp, 'Nothing was saved')
        self.assertEqual(Shift.objects.filter(company=self.co).count(), 0, over)           # one bad row stops all four

    def test_equal_start_and_end(self):
        self.check_refused({'end_0': '06:30'}, 'cannot be the same')

    def test_a_bad_or_missing_time(self):
        self.check_refused({'start_1': '25:99'}, 'valid start and end')
        self.check_refused({'end_2': ''}, 'valid start and end')

    def test_a_break_as_long_as_the_shift(self):
        self.check_refused({'break_3': '330'}, 'shorter than the shift')

    def test_negative_or_text_numbers(self):
        self.check_refused({'break_0': '-5'}, 'cannot be negative')
        self.check_refused({'grace_0': 'ten'}, 'whole numbers')
        self.check_refused({'grace_0': '999'}, 'cannot be negative')

    def test_an_empty_or_too_long_name_or_a_repeated_name(self):
        self.check_refused({'name_0': '  '}, 'needs a name')
        self.check_refused({'name_0': 'x' * 61}, 'too long')
        self.check_refused({'name_1': 'first shift'}, 'already exists')                    # the same name twice in one press


class IsolationTests(Base):
    def test_another_companys_shift_does_not_block_this_one_and_each_owner_adds_to_their_own_company(self):
        Shift.objects.create(company=self.other, name='First Shift', start_time=T(6), end_time=T(14))
        self.add()
        self.assertEqual(len(self.shifts()), 4)
        self.assertEqual(self.shifts(self.other)['First Shift'].start_time, T(6))        # theirs is untouched
        self.client.force_login(self.other_owner)
        self.add(form(rows=(1, 2, 3)))
        self.assertEqual(set(self.shifts(self.other)), {'First Shift', 'Second Shift', 'General Shift', 'Third Shift'})
        self.assertEqual(len(self.shifts()), 4)                                          # mine did not gain any

    def test_only_the_company_owner_can_add(self):
        self.client.force_login(make_user('fouremp2', 'EMPLOYEE', self.co))
        self.assertEqual(self.client.post(self.url, form()).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(self.url, form()).status_code, 302)
        self.assertEqual(Shift.objects.count(), 0)

    def test_opening_the_address_by_hand_does_not_create_anything(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Shift.objects.count(), 0)
