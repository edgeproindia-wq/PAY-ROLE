"""Hours worked (labels, break, night shifts) and the automatic Half day rule for biometric attendance."""
import datetime
import json
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .attendance_hours import AUTO_TAG, clock, finalize_statuses, label, net_hours_decimal, worked
from .biometric import IST
from .biometric_models import BiometricDevice
from .models import Attendance
from .tests import make_company, make_employee

T = datetime.time
DAY = datetime.date(2026, 10, 5)


class WorkedTests(SimpleTestCase):
    def net(self, a, b):
        w = worked(DAY, a, b)
        return (w.state, w.net)

    def test_a_normal_day_has_the_break_taken_off(self):
        self.assertEqual(self.net(T(9, 0), T(18, 0)), ('ok', datetime.timedelta(hours=8)))
        self.assertEqual(label(DAY, T(9, 0), T(18, 5)), '8h 05m')
        self.assertEqual(label(DAY, T(9, 2, 11), T(18, 31)), '8h 28m')                        # seconds are used, then shown in whole minutes

    def test_a_short_stay_has_no_break_taken_off(self):
        self.assertEqual(label(DAY, T(18, 38), T(18, 39)), '0h 01m')
        self.assertEqual(label(DAY, T(9, 0), T(13, 0)), '4h 00m')                              # exactly 4 hours: no break
        self.assertEqual(label(DAY, T(9, 0), T(13, 1)), '3h 01m')                              # a minute more and the break comes off

    def test_the_break_never_makes_the_time_negative(self):
        for end in (T(13, 1), T(13, 30), T(14, 0)):
            self.assertGreaterEqual(worked(DAY, T(9, 0), end).net, datetime.timedelta(hours=3))

    def test_a_night_shift_ends_the_next_day(self):
        w = worked(DAY, T(22, 0), T(6, 0))
        self.assertEqual((w.state, w.overnight, w.gross, w.net), ('ok', True, datetime.timedelta(hours=8), datetime.timedelta(hours=7)))
        self.assertEqual(label(DAY, T(22, 0), T(6, 30)), '7h 30m (night)')
        self.assertEqual(label(DAY, T(9, 0), T(18, 0)).endswith('(night)'), False)

    def test_impossible_times_are_flagged_not_calculated(self):
        self.assertEqual(label(DAY, T(18, 0), T(17, 0)), 'Check times look wrong')            # would be 23 hours
        self.assertEqual(worked(DAY, T(9, 0), T(1, 0)).state, 'ok')                           # exactly 16 hours overnight is allowed
        self.assertEqual(worked(DAY, T(9, 0), T(1, 1)).state, 'bad')                          # a minute more is not
        self.assertEqual(worked(DAY, T(10, 0), T(1, 0)).state, 'ok')                          # 15 hours overnight

    def test_missing_times_are_named(self):
        self.assertEqual(label(DAY, None, None), '\u2014')
        self.assertEqual(label(DAY, T(9, 0), None), 'Check-out missing')
        self.assertEqual(label(DAY, None, T(18, 0)), 'Check-in missing')

    def test_midnight_is_a_real_time(self):
        self.assertEqual(worked(DAY, T(0, 0), T(8, 0)).state, 'ok')
        self.assertEqual(label(DAY, T(0, 0), T(8, 0)), '7h 00m')

    def test_the_same_time_twice_is_zero_hours(self):
        self.assertEqual(label(DAY, T(9, 0), T(9, 0)), '0h 00m')

    def test_clock_formatting(self):
        self.assertEqual(clock(datetime.timedelta(hours=9, minutes=5, seconds=59)), '9h 05m')
        self.assertEqual(clock(datetime.timedelta(minutes=0)), '0h 00m')
        self.assertEqual(clock(datetime.timedelta(hours=12, minutes=0)), '12h 00m')


class PropertyTests(TestCase):
    def setUp(self):
        self.co = make_company('Hours Co')
        self.e = make_employee(self.co, 'H1', 'h1@h.test')

    def test_the_attendance_record_exposes_the_label_and_the_number(self):
        a = Attendance.objects.create(employee=self.e, date=DAY, status='PRESENT', check_in=T(9, 0), check_out=T(18, 30))
        self.assertEqual((a.hours_label, a.net_hours), ('8h 30m', Decimal('8.50')))
        self.assertEqual(a.hours_worked, 9.5)                                                  # the original gross figure is unchanged
        b = Attendance.objects.create(employee=self.e, date=DAY + datetime.timedelta(days=1), status='PRESENT', check_in=T(9, 0))
        self.assertEqual((b.hours_label, b.net_hours), ('Check-out missing', None))
        c = Attendance.objects.create(employee=self.e, date=DAY + datetime.timedelta(days=2), status='PRESENT')
        self.assertEqual((c.hours_label, c.net_hours), ('\u2014', None))
        self.assertEqual(net_hours_decimal(a), Decimal('8.50'))


class FinalizeTests(TestCase):
    """The Half day rule: only finished days, only records the biometric wrote, never HR decisions."""

    def setUp(self):
        self.co = make_company('Final Co')
        self.other = make_company('Other Final Co')
        self.e = make_employee(self.co, 'F1', 'f1@h.test')
        self.today = datetime.date(2026, 10, 20)

    def rec(self, days_ago, cin, cout, status='PRESENT', remarks='Biometric', emp=None):
        return Attendance.objects.create(employee=emp or self.e, date=self.today - datetime.timedelta(days=days_ago), status=status, check_in=cin, check_out=cout, remarks=remarks)

    def run_it(self, **kw):
        return finalize_statuses(self.co, today=self.today, **kw)

    def test_a_finished_day_under_six_hours_after_the_break_becomes_a_half_day(self):
        a = self.rec(1, T(9, 0), T(14, 0))                                                      # 5 hours, 4 after the break
        self.assertEqual(self.run_it(), 1)
        a.refresh_from_db()
        self.assertEqual((a.status, a.remarks), ('HALF_DAY', f'{AUTO_TAG} (4h 00m)'))

    def test_the_limit_is_six_hours_after_the_break(self):
        full = self.rec(1, T(9, 0), T(16, 0))                                                   # 7h, 6h after the break: a full day
        low = self.rec(2, T(9, 0), T(15, 59))                                                   # 6h59, 5h59 after the break: half
        self.run_it()
        full.refresh_from_db()
        low.refresh_from_db()
        self.assertEqual((full.status, low.status), ('PRESENT', 'HALF_DAY'))

    def test_today_is_never_changed_while_the_day_is_still_going(self):
        a = self.rec(0, T(9, 0), T(12, 0))
        self.assertEqual(self.run_it(), 0)
        a.refresh_from_db()
        self.assertEqual(a.status, 'PRESENT')

    def test_only_the_last_few_days_are_looked_at(self):
        old = self.rec(4, T(9, 0), T(12, 0))
        self.assertEqual(self.run_it(), 0)
        self.assertEqual(self.run_it(days_back=5), 1)
        old.refresh_from_db()
        self.assertEqual(old.status, 'HALF_DAY')

    def test_records_not_written_by_the_biometric_are_never_touched(self):
        manual = self.rec(1, T(9, 0), T(12, 0), remarks='')
        hr = self.rec(2, T(9, 0), T(12, 0), remarks='Edited by HR')
        self.assertEqual(self.run_it(), 0)
        for a in (manual, hr):
            a.refresh_from_db()
            self.assertEqual(a.status, 'PRESENT')

    def test_absent_leave_and_a_missing_checkout_are_left_alone(self):
        ab = self.rec(1, T(9, 0), T(12, 0), status='ABSENT')
        lv = self.rec(2, T(9, 0), T(12, 0), status='LEAVE')
        nocheck = self.rec(3, T(9, 0), None)
        self.assertEqual(self.run_it(), 0)
        for a, s in ((ab, 'ABSENT'), (lv, 'LEAVE'), (nocheck, 'PRESENT')):
            a.refresh_from_db()
            self.assertEqual(a.status, s)

    def test_impossible_times_are_not_judged(self):
        a = self.rec(1, T(18, 0), T(17, 0))
        self.assertEqual(self.run_it(), 0)
        a.refresh_from_db()
        self.assertEqual(a.status, 'PRESENT')

    def test_a_late_punch_that_fixes_the_hours_puts_the_day_back_to_present(self):
        a = self.rec(1, T(9, 0), T(12, 0))
        self.run_it()
        Attendance.objects.filter(pk=a.pk).update(check_out=T(18, 0))                          # the sync was offline; the real check-out arrives later
        self.assertEqual(self.run_it(), 1)
        a.refresh_from_db()
        self.assertEqual((a.status, a.remarks), ('PRESENT', 'Biometric'))

    def test_when_hr_sets_the_day_back_to_present_the_rule_does_not_flip_it_again(self):
        a = self.rec(1, T(9, 0), T(12, 0))
        self.run_it()
        Attendance.objects.filter(pk=a.pk).update(status='PRESENT')                            # HR decision; the note still says auto half day
        self.assertEqual(self.run_it(), 0)
        a.refresh_from_db()
        self.assertEqual((a.status, a.remarks.startswith(AUTO_TAG)), ('PRESENT', True))

    def test_running_it_twice_changes_nothing_more(self):
        self.rec(1, T(9, 0), T(12, 0))
        self.assertEqual((self.run_it(), self.run_it()), (1, 0))

    def test_another_company_is_never_touched(self):
        stranger = make_employee(self.other, 'S1', 's1@h.test')
        a = self.rec(1, T(9, 0), T(12, 0), emp=stranger)
        self.assertEqual(self.run_it(), 0)
        a.refresh_from_db()
        self.assertEqual(a.status, 'PRESENT')


class EndToEndTests(TestCase):
    """Punches sent by the sync program become hours and, after the day ends, a Half day when the hours are low."""

    def setUp(self):
        self.co = make_company('E2E Co')
        self.e = make_employee(self.co, 'EPRO0001', 'e2e@h.test')
        self.e.date_of_joining = datetime.date(2024, 1, 1)
        self.e.save()
        self.key = BiometricDevice.new_key()
        BiometricDevice.objects.create(company=self.co, name='Test', key_hash=BiometricDevice.hash_key(self.key), key_prefix=self.key[:8])

    def send(self, *times):
        from django.utils import timezone
        d = (timezone.now().astimezone(IST) - datetime.timedelta(days=1)).date()
        punches = [{'id': 'EPRO0001', 'time': f'{d.isoformat()}T{t}'} for t in times]
        r = self.client.post(reverse('bio_api_punches'), data=json.dumps({'punches': punches}), content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {self.key}')
        return r.json(), Attendance.objects.get(employee=self.e, date=d)

    def test_a_full_day_stays_present_and_shows_its_hours(self):
        body, a = self.send('09:00:00', '18:00:00')
        self.assertEqual((a.status, a.hours_label), ('PRESENT', '8h 00m'))
        self.assertNotIn('attendance_status_updated', body)

    def test_a_short_day_becomes_a_half_day_once_it_is_finished(self):
        body, a = self.send('09:00:00', '14:00:00')
        self.assertEqual((a.status, body['attendance_status_updated']), ('HALF_DAY', 1))
        self.assertTrue(a.remarks.startswith(AUTO_TAG))

    def test_a_late_second_batch_corrects_a_half_day(self):
        self.send('09:00:00', '13:00:00')
        body, a = self.send('18:00:00')
        self.assertEqual((a.status, a.remarks, a.hours_label), ('PRESENT', 'Biometric', '8h 00m'))

    def test_a_day_edited_by_hr_is_not_overwritten_by_the_biometric_rule(self):
        d = (datetime.datetime.now(IST) - datetime.timedelta(days=1)).date()
        Attendance.objects.create(employee=self.e, date=d, status='PRESENT', check_in=T(10, 0), check_out=T(13, 0), remarks='Edited by HR')
        body, a = self.send('09:00:00', '18:00:00')
        self.assertEqual((a.status, a.check_in, a.remarks), ('PRESENT', T(10, 0), 'Edited by HR'))
