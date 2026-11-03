"""Biometric integration: the punch API (secret key per device), attendance rules, ID mapping and the owner pages."""
import datetime
import json
import zoneinfo

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .biometric import IST, local_day_bounds, parse_punch_time
from .biometric_models import BiometricDevice, BiometricIdMap, BiometricPunch
from .models import Attendance, AuditLog, Employee
from .tests import make_company, make_employee, make_user


def ts(days_ago=1, hh=9, mm=0, ss=0):
    """A device-style time string for yesterday by default (a punch for later today would be refused as being in the future)."""
    d = (timezone.now().astimezone(IST) - datetime.timedelta(days=days_ago)).date()
    return f'{d.isoformat()}T{hh:02d}:{mm:02d}:{ss:02d}'


def day(days_ago=1):
    return (timezone.now().astimezone(IST) - datetime.timedelta(days=days_ago)).date()


class BioBase(TestCase):
    def setUp(self):
        self.co = make_company('Bio Co')
        self.other = make_company('Other Bio Co')
        self.owner = make_user('bio_owner', 'COMPANY_OWNER', self.co)
        self.e76 = make_employee(self.co, 'EPRO0076', 'e76@b.test')
        self.e5 = make_employee(self.co, 'EPRO0005', 'e5@b.test')
        for e in (self.e76, self.e5):
            e.date_of_joining = datetime.date(2024, 1, 1)
            e.save()
        self.key = BiometricDevice.new_key()
        self.device = BiometricDevice.objects.create(company=self.co, name='Hosur', key_hash=BiometricDevice.hash_key(self.key), key_prefix=self.key[:8])

    def send(self, punches, key=None, raw=None, content_type='application/json'):
        body = raw if raw is not None else json.dumps({'punches': punches})
        return self.client.post(reverse('bio_api_punches'), data=body, content_type=content_type, HTTP_AUTHORIZATION=f'Bearer {key or self.key}')

    def att(self, emp, days_ago=1):
        return Attendance.objects.filter(employee=emp, date=day(days_ago)).first()


class KeyTests(BioBase):
    def test_the_key_is_checked_on_every_request(self):
        url = reverse('bio_api_punches')
        self.assertEqual(self.client.post(url, data='{}', content_type='application/json').status_code, 401)
        self.assertEqual(self.send([], key='bio_wrong').status_code, 401)
        self.assertEqual(self.client.post(url, data='{"punches": []}', content_type='application/json', HTTP_AUTHORIZATION='Basic abc').status_code, 401)
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.send([]).status_code, 200)

    def test_a_revoked_device_key_stops_working(self):
        BiometricDevice.objects.filter(pk=self.device.pk).update(active=False)
        self.assertEqual(self.send([{'id': 'EPRO0076', 'time': ts()}]).status_code, 401)
        self.assertEqual(BiometricPunch.objects.count(), 0)

    def test_only_a_hash_of_the_key_is_stored(self):
        self.assertNotIn(self.key, [self.device.key_hash, self.device.key_prefix])
        self.assertEqual(len(self.device.key_hash), 64)
        self.assertEqual(self.device.key_prefix, self.key[:8])

    def test_the_get_check_updates_last_seen_and_names_the_device(self):
        r = self.client.get(reverse('bio_api_punches'), HTTP_AUTHORIZATION=f'Bearer {self.key}')
        self.assertEqual((r.status_code, r.json()['device']), (200, 'Hosur'))
        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.last_seen)

    def test_no_csrf_cookie_is_needed_and_other_methods_are_refused(self):
        self.client.handler.enforce_csrf_checks = True
        self.assertEqual(self.send([]).status_code, 200)
        self.assertEqual(self.client.put(reverse('bio_api_punches'), HTTP_AUTHORIZATION=f'Bearer {self.key}').status_code, 405)
        self.assertEqual(self.client.delete(reverse('bio_api_punches'), HTTP_AUTHORIZATION=f'Bearer {self.key}').status_code, 405)


class IngestTests(BioBase):
    def test_punches_are_stored_and_attendance_is_filled_in(self):
        r = self.send([{'id': 'EPRO0076', 'time': ts(hh=9, mm=2, ss=11)}, {'id': 'EPRO0076', 'time': ts(hh=18, mm=31)}, {'id': 'epro0005', 'time': ts(hh=10)}])
        body = r.json()
        self.assertEqual((body['ok'], body['received'], body['new'], body['unmapped'], body['attendance_created']), (True, 3, 3, 0, 2))
        a = self.att(self.e76)
        self.assertEqual((a.status, a.check_in, a.check_out, a.remarks), ('PRESENT', datetime.time(9, 2, 11), datetime.time(18, 31), 'Biometric'))
        b = self.att(self.e5)
        self.assertEqual((b.check_in, b.check_out), (datetime.time(10, 0), None))                 # one punch: check-in only
        self.assertEqual(BiometricPunch.objects.filter(employee=self.e76).count(), 2)

    def test_sending_the_same_punches_again_changes_nothing(self):
        punches = [{'id': 'EPRO0076', 'time': ts(hh=9)}, {'id': 'EPRO0076', 'time': ts(hh=18)}]
        self.send(punches)
        again = self.send(punches).json()
        self.assertEqual((again['new'], again['duplicates']), (0, 2))
        self.assertEqual((BiometricPunch.objects.count(), Attendance.objects.count()), (2, 1))

    def test_duplicates_inside_one_request_count_once(self):
        r = self.send([{'id': 'EPRO0076', 'time': ts(hh=9)}] * 3).json()
        self.assertEqual((r['new'], r['duplicates']), (1, 2))

    def test_a_later_punch_moves_the_check_out_and_an_earlier_one_moves_the_check_in(self):
        self.send([{'id': 'EPRO0076', 'time': ts(hh=9, mm=30)}, {'id': 'EPRO0076', 'time': ts(hh=17)}])
        self.send([{'id': 'EPRO0076', 'time': ts(hh=19)}])
        self.send([{'id': 'EPRO0076', 'time': ts(hh=8, mm=45)}])
        a = self.att(self.e76)
        self.assertEqual((a.check_in, a.check_out), (datetime.time(8, 45), datetime.time(19, 0)))

    def test_two_punches_less_than_a_minute_apart_are_only_a_check_in(self):
        self.send([{'id': 'EPRO0076', 'time': ts(hh=9, mm=0, ss=0)}, {'id': 'EPRO0076', 'time': ts(hh=9, mm=0, ss=30)}])
        self.assertEqual((self.att(self.e76).check_in, self.att(self.e76).check_out), (datetime.time(9, 0), None))

    def test_the_device_number_can_be_mapped_to_an_employee(self):
        BiometricIdMap.objects.create(company=self.co, device_user_id='76', employee=self.e76)
        r = self.send([{'id': '76', 'time': ts(hh=9)}]).json()
        self.assertEqual((r['unmapped'], r['attendance_created']), (0, 1))
        self.assertEqual(BiometricPunch.objects.get().employee, self.e76)

    def test_an_unknown_number_is_kept_and_waits(self):
        r = self.send([{'id': '999', 'time': ts(hh=9)}]).json()
        self.assertEqual((r['new'], r['unmapped']), (1, 1))
        self.assertEqual((BiometricPunch.objects.get().employee, Attendance.objects.count()), (None, 0))

    def test_absent_and_leave_records_are_never_overwritten(self):
        Attendance.objects.create(employee=self.e76, date=day(), status='ABSENT')
        Attendance.objects.create(employee=self.e5, date=day(), status='LEAVE')
        r = self.send([{'id': 'EPRO0076', 'time': ts(hh=9)}, {'id': 'EPRO0005', 'time': ts(hh=9)}]).json()
        self.assertEqual(r['attendance_conflict'], 2)
        self.assertEqual((self.att(self.e76).status, self.att(self.e5).status), ('ABSENT', 'LEAVE'))
        self.assertIsNone(self.att(self.e76).check_in)

    def test_times_typed_by_a_person_are_kept(self):
        Attendance.objects.create(employee=self.e76, date=day(), status='PRESENT', check_in=datetime.time(9, 30), check_out=datetime.time(17, 30), remarks='Edited by HR')
        r = self.send([{'id': 'EPRO0076', 'time': ts(hh=8, mm=50)}, {'id': 'EPRO0076', 'time': ts(hh=18)}]).json()
        self.assertEqual(r['attendance_manual_kept'], 1)
        a = self.att(self.e76)
        self.assertEqual((a.check_in, a.check_out, a.remarks), (datetime.time(9, 30), datetime.time(17, 30), 'Edited by HR'))

    def test_an_empty_present_record_gets_the_times_and_a_half_day_keeps_its_status(self):
        Attendance.objects.create(employee=self.e76, date=day(), status='PRESENT')
        Attendance.objects.create(employee=self.e5, date=day(), status='HALF_DAY')
        self.send([{'id': 'EPRO0076', 'time': ts(hh=9)}, {'id': 'EPRO0076', 'time': ts(hh=18)}, {'id': 'EPRO0005', 'time': ts(hh=9)}, {'id': 'EPRO0005', 'time': ts(hh=13)}])
        self.assertEqual((self.att(self.e76).check_in, self.att(self.e76).check_out), (datetime.time(9), datetime.time(18)))
        self.assertEqual((self.att(self.e5).status, self.att(self.e5).check_out), ('HALF_DAY', datetime.time(13)))

    def test_a_punch_before_the_joining_date_is_stored_but_makes_no_attendance(self):
        Employee.objects.filter(pk=self.e76.pk).update(date_of_joining=day() + datetime.timedelta(days=5))
        r = self.send([{'id': 'EPRO0076', 'time': ts(hh=9)}]).json()
        self.assertEqual((r['new'], r.get('attendance_before_joining')), (1, 1))
        self.assertEqual(Attendance.objects.count(), 0)

    def test_days_use_india_time(self):
        self.send([{'id': 'EPRO0076', 'time': ts(days_ago=1, hh=23, mm=59, ss=30)}, {'id': 'EPRO0076', 'time': ts(days_ago=0, hh=0, mm=0, ss=10)}])
        self.assertEqual((self.att(self.e76, 1).check_in, self.att(self.e76, 0).check_in), (datetime.time(23, 59, 30), datetime.time(0, 0, 10)))
        start, end = local_day_bounds(datetime.date(2026, 11, 2))
        self.assertEqual((end - start).days, 1)
        self.assertEqual(parse_punch_time('2026-11-02T09:00:00').utcoffset(), datetime.timedelta(hours=5, minutes=30))

    def test_bad_times_future_punches_and_old_punches_are_rejected_not_stored(self):
        future = (timezone.now().astimezone(IST) + datetime.timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%S')
        old = ts(days_ago=100)
        r = self.send([{'id': 'EPRO0076', 'time': 'yesterday'}, {'id': 'EPRO0076', 'time': future}, {'id': 'EPRO0076', 'time': old}, {'id': '', 'time': ts()}, {'id': 'x' * 31, 'time': ts()},
                       'junk', {'time': ts()}, {'id': 'EPRO0076', 'time': '2026-13-45T25:61:61'}, {'id': 'EPRO0076', 'time': ts(hh=9)}]).json()
        self.assertEqual((r['rejected'], r['new']), (8, 1))
        self.assertEqual(BiometricPunch.objects.count(), 1)

    def test_bad_requests_get_a_clear_400(self):
        for raw in ('not json', '{}', '{"punches": "x"}', '[1, 2]', '{"punches": null}'):
            self.assertEqual(self.send(None, raw=raw).status_code, 400, raw)
        too_many = [{'id': 'EPRO0076', 'time': ts(hh=9)}] * 2001
        self.assertEqual(self.send(too_many).status_code, 400)
        huge = json.dumps({'punches': [], 'pad': 'x' * (1024 * 1024 + 10)})
        self.assertEqual(self.send(None, raw=huge).status_code, 413)

    def test_last_seen_and_batch_size_are_recorded(self):
        self.send([{'id': 'EPRO0076', 'time': ts(hh=9)}, {'id': '1', 'time': ts(hh=9)}])
        self.device.refresh_from_db()
        self.assertEqual(self.device.last_batch, 2)
        self.assertIsNotNone(self.device.last_seen)


class IsolationTests(BioBase):
    def test_a_key_can_only_write_to_its_own_company(self):
        stranger = make_employee(self.other, 'EPRO0076', 's76@b.test')            # same code in another company
        stranger.date_of_joining = datetime.date(2024, 1, 1)
        stranger.save()
        self.send([{'id': 'EPRO0076', 'time': ts(hh=9)}])
        self.assertEqual(Attendance.objects.filter(employee=stranger).count(), 0)
        self.assertEqual(BiometricPunch.objects.filter(company=self.other).count(), 0)
        self.assertEqual(BiometricPunch.objects.get().employee, self.e76)

    def test_a_payload_cannot_name_a_company_or_employee(self):
        body = json.dumps({'punches': [{'id': 'NOBODY', 'time': ts(hh=9), 'company': self.other.pk, 'employee': self.e76.pk}], 'company': self.other.pk})
        self.send(None, raw=body)
        p = BiometricPunch.objects.get()
        self.assertEqual((p.company, p.employee), (self.co, None))

    def test_an_id_map_of_another_company_is_not_used(self):
        stranger = make_employee(self.other, 'OTHER1', 's1@b.test')
        BiometricIdMap.objects.create(company=self.other, device_user_id='55', employee=stranger)
        self.send([{'id': '55', 'time': ts(hh=9)}])
        self.assertIsNone(BiometricPunch.objects.get().employee)

    def test_the_same_punch_may_exist_in_two_companies(self):
        other_key = BiometricDevice.new_key()
        BiometricDevice.objects.create(company=self.other, name='Other', key_hash=BiometricDevice.hash_key(other_key), key_prefix=other_key[:8])
        self.send([{'id': '7', 'time': ts(hh=9)}])
        self.send([{'id': '7', 'time': ts(hh=9)}], key=other_key)
        self.assertEqual(BiometricPunch.objects.count(), 2)


class SetupPageTests(BioBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_adding_a_device_shows_the_key_once_and_stores_only_its_hash(self):
        r = self.client.post(reverse('bio_setup'), {'action': 'add_device', 'name': 'Bangalore'})
        key = r.context['new_key']
        self.assertTrue(key.startswith('bio_') and len(key) > 30)
        self.assertContains(r, key)
        d = BiometricDevice.objects.get(name='Bangalore')
        self.assertEqual((d.key_hash, d.key_prefix, d.company), (BiometricDevice.hash_key(key), key[:8], self.co))
        self.assertNotIn(key, d.key_hash)
        self.assertNotContains(self.client.get(reverse('bio_setup')), key)
        self.assertTrue(AuditLog.objects.filter(action='CREATE', details__contains='Bangalore').exists())
        self.assertFalse(AuditLog.objects.filter(details__contains=key).exists())
        self.assertEqual(self.send([], key=key).status_code, 200)

    def test_a_device_needs_a_name(self):
        r = self.client.post(reverse('bio_setup'), {'action': 'add_device', 'name': '  '}, follow=True)
        self.assertContains(r, 'Give the device a name')
        self.assertEqual(BiometricDevice.objects.count(), 1)

    def test_revoking_stops_the_key(self):
        self.client.post(reverse('bio_setup'), {'action': 'revoke', 'device': self.device.pk})
        self.device.refresh_from_db()
        self.assertFalse(self.device.active)
        self.assertEqual(self.send([]).status_code, 401)
        self.assertContains(self.client.get(reverse('bio_setup')), 'Revoked')

    def test_the_page_shows_status_and_the_https_address(self):
        self.send([{'id': 'EPRO0076', 'time': ts(hh=9)}])
        r = self.client.get(reverse('bio_setup'))
        self.assertContains(r, 'Connected')
        self.assertContains(r, '/api/biometric/punches/')
        BiometricDevice.objects.filter(pk=self.device.pk).update(last_seen=timezone.now() - datetime.timedelta(hours=2))
        self.assertContains(self.client.get(reverse('bio_setup')), 'Not heard from for a while')

    def test_unmatched_numbers_can_be_mapped_and_then_applied(self):
        self.send([{'id': '76', 'time': ts(hh=9)}, {'id': '76', 'time': ts(hh=18)}, {'id': '999', 'time': ts(hh=9)}])
        r = self.client.get(reverse('bio_setup'))
        self.assertContains(r, 'Punches waiting')
        self.assertEqual({u['device_user_id']: u['suggest'] for u in r.context['unmapped']}, {'76': self.e76, '999': None})
        self.client.post(reverse('bio_setup'), {'action': 'map', 'device_user_id': '76', 'employee': self.e76.pk})
        self.assertEqual(Attendance.objects.count(), 0)                                   # mapping alone does not change attendance
        r = self.client.post(reverse('bio_setup'), {'action': 'reprocess'}, follow=True)
        self.assertContains(r, '2 waiting punch(es) matched')
        self.assertEqual((self.att(self.e76).check_in, self.att(self.e76).check_out), (datetime.time(9), datetime.time(18)))
        self.assertEqual(BiometricPunch.objects.filter(employee__isnull=True).count(), 1)

    def test_accept_all_suggestions_matches_by_the_number_at_the_end(self):
        self.send([{'id': '76', 'time': ts(hh=9)}, {'id': '5', 'time': ts(hh=9)}, {'id': '999', 'time': ts(hh=9)}])
        r = self.client.post(reverse('bio_setup'), {'action': 'accept_suggestions'}, follow=True)
        self.assertContains(r, '2 device number(s) matched')
        self.assertEqual(set(BiometricIdMap.objects.values_list('device_user_id', flat=True)), {'76', '5'})
        self.client.post(reverse('bio_setup'), {'action': 'reprocess'})
        self.assertEqual((self.att(self.e76) is not None, self.att(self.e5) is not None), (True, True))

    def test_an_ambiguous_number_is_not_suggested(self):
        make_employee(self.co, 'ABC0076', 'dup76@b.test')                                  # two employees end in 76
        self.send([{'id': '76', 'time': ts(hh=9)}])
        self.assertIsNone(self.client.get(reverse('bio_setup')).context['unmapped'][0]['suggest'])

    def test_mapping_to_an_employee_of_another_company_is_refused(self):
        stranger = make_employee(self.other, 'S1', 's1@b.test')
        self.client.post(reverse('bio_setup'), {'action': 'map', 'device_user_id': '9', 'employee': stranger.pk})
        self.assertEqual(BiometricIdMap.objects.count(), 0)

    def test_punches_on_absent_or_leave_days_are_listed_for_a_decision(self):
        Attendance.objects.create(employee=self.e76, date=day(), status='ABSENT')
        self.send([{'id': 'EPRO0076', 'time': ts(hh=9)}])
        r = self.client.get(reverse('bio_setup'))
        self.assertContains(r, 'Needs a decision')
        self.assertEqual([a.employee for a in r.context['conflicts']], [self.e76])

    def test_another_company_cannot_revoke_or_see_these_devices(self):
        other_owner = make_user('bio_other_owner', 'COMPANY_OWNER', self.other)
        self.client.force_login(other_owner)
        self.assertEqual(self.client.post(reverse('bio_setup'), {'action': 'revoke', 'device': self.device.pk}).status_code, 404)
        self.assertNotContains(self.client.get(reverse('bio_setup')), 'Hosur')
        self.device.refresh_from_db()
        self.assertTrue(self.device.active)


class PunchLogTests(BioBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.send([{'id': 'EPRO0076', 'time': ts(hh=9)}, {'id': '999', 'time': ts(hh=9, mm=5)}, {'id': 'EPRO0005', 'time': ts(days_ago=2, hh=9)}])

    def test_filters(self):
        url = reverse('bio_punches')
        self.assertEqual(self.client.get(url).context['page'].paginator.count, 3)
        self.assertEqual(self.client.get(url, {'state': 'unmapped'}).context['page'].paginator.count, 1)
        self.assertEqual(self.client.get(url, {'state': 'mapped'}).context['page'].paginator.count, 2)
        self.assertEqual(self.client.get(url, {'date': day().isoformat()}).context['page'].paginator.count, 2)
        self.assertEqual(self.client.get(url, {'q': 'EPRO0005'}).context['page'].paginator.count, 1)
        self.assertEqual(self.client.get(url, {'date': 'garbage'}).status_code, 200)

    def test_another_company_sees_none_of_it(self):
        self.client.force_login(make_user('bio_other_owner2', 'COMPANY_OWNER', self.other))
        self.assertEqual(self.client.get(reverse('bio_punches')).context['page'].paginator.count, 0)

    def test_only_company_owners_can_open_the_pages(self):
        for name in ('bio_setup', 'bio_punches'):
            self.client.logout()
            self.assertEqual(self.client.get(reverse(name)).status_code, 302, name)
            self.client.force_login(make_user('bio_emp_' + name, 'EMPLOYEE', self.co))
            self.assertIn(self.client.get(reverse(name)).status_code, (302, 403), name)
            self.client.force_login(make_user('bio_admin_' + name, 'ADMIN'))
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)
        self.client.force_login(make_user('bio_emp3', 'EMPLOYEE', self.co))
        self.assertIn(self.client.post(reverse('bio_setup'), {'action': 'add_device', 'name': 'x'}).status_code, (302, 403))
        self.assertEqual(BiometricDevice.objects.count(), 1)
