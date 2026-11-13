"""Pairing a sync program with a short code: the code is shown by the program, approved on the website, and the key is handed over once."""
import datetime
import json
import re

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .biometric_models import BiometricDevice
from .biometric_pairing_models import CODE_ALPHABET, BiometricPairing
from .models import AuditLog
from .tests import make_company, make_user

NONCE = 'a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90'
OTHER_NONCE = 'f' * 64


def post_json(client, name, payload):
    return client.post(reverse(name), json.dumps(payload), content_type='application/json')


class PairingBase(TestCase):
    def setUp(self):
        cache.clear()
        self.co = make_company('Pair Co')
        self.other = make_company('Pair Other Co')
        self.owner = make_user('pair_owner', 'COMPANY_OWNER', self.co)
        self.other_owner = make_user('pair_other_owner', 'COMPANY_OWNER', self.other)
        self.program = self.client_class()                     # the sync program: not signed in

    def start(self, nonce=NONCE):
        r = post_json(self.program, 'bio_pair_start', {'nonce': nonce})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()['code']

    def poll(self, code, nonce=NONCE):
        return post_json(self.program, 'bio_pair_poll', {'code': code, 'nonce': nonce})

    def approve(self, code, name='Hosur', user=None):
        c = self.client_class()
        c.force_login(user or self.owner)
        return c.post(reverse('bio_pair_approve'), {'code': code, 'name': name}, follow=True)


class StartTests(PairingBase):
    def test_a_code_is_issued_in_the_easy_to_type_format(self):
        code = self.start()
        self.assertRegex(code, r'^[A-Z2-9]{4}-[A-Z2-9]{4}$')
        self.assertTrue(all(ch in CODE_ALPHABET or ch == '-' for ch in code))
        for look_alike in 'ILO01':
            self.assertNotIn(look_alike, code)

    def test_only_hashes_are_stored_never_the_code_or_the_secret(self):
        code = self.start()
        row = BiometricPairing.objects.get()
        stored = ' '.join(str(getattr(row, f.name)) for f in row._meta.fields)
        self.assertNotIn(code.replace('-', ''), stored)
        self.assertNotIn(NONCE, stored)
        self.assertEqual(len(row.code_hash), 64)

    def test_a_bad_secret_or_bad_body_is_refused(self):
        for payload in ({}, {'nonce': 'short'}, {'nonce': 5}, {'nonce': 'x' * 200}, {'nonce': 'has space ' * 6}):
            self.assertEqual(post_json(self.program, 'bio_pair_start', payload).status_code, 400, payload)
        self.assertEqual(self.program.post(reverse('bio_pair_start'), 'not json', content_type='application/json').status_code, 400)
        self.assertEqual(self.program.post(reverse('bio_pair_start'), json.dumps([1, 2]), content_type='application/json').status_code, 400)
        self.assertEqual(BiometricPairing.objects.count(), 0)

    def test_the_endpoints_do_not_answer_to_a_plain_page_visit(self):
        for name in ('bio_pair_start', 'bio_pair_poll', 'bio_pair_approve'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 405 if name != 'bio_pair_approve' else 302, name)

    def test_too_many_open_requests_are_refused(self):
        from .biometric_pairing import ACTIVE_CAP
        soon = timezone.now() + datetime.timedelta(minutes=5)
        for i in range(ACTIVE_CAP):
            BiometricPairing.objects.create(code_hash='%064d' % i, nonce_hash='x', expires_at=soon)
        self.assertEqual(post_json(self.program, 'bio_pair_start', {'nonce': NONCE}).status_code, 429)

    def test_old_requests_are_cleaned_away(self):
        old = BiometricPairing.objects.create(code_hash='1' * 64, nonce_hash='x', expires_at=timezone.now() - datetime.timedelta(days=2))
        BiometricPairing.objects.filter(pk=old.pk).update(created_at=timezone.now() - datetime.timedelta(days=2))
        self.start()
        self.assertFalse(BiometricPairing.objects.filter(pk=old.pk).exists())

    def test_every_response_says_not_to_store_it(self):
        self.assertEqual(post_json(self.program, 'bio_pair_start', {'nonce': NONCE})['Cache-Control'], 'no-store')


class ApproveTests(PairingBase):
    def test_the_owner_approves_and_the_program_receives_its_key_once(self):
        code = self.start()
        self.assertEqual(self.poll(code).json(), {'ok': True, 'status': 'waiting'})
        r = self.approve(code)
        self.assertContains(r, 'Approved')
        got = self.poll(code)
        body = got.json()
        self.assertEqual(got.status_code, 200)
        self.assertEqual((body['status'], body['device']), ('approved', 'Hosur'))
        self.assertRegex(body['api_key'], r'^bio_[A-Za-z0-9_\-]{20,}$')
        second = self.poll(code)
        self.assertEqual((second.status_code, second.json()['status']), (410, 'delivered'))
        self.assertNotIn('api_key', second.json())

    def test_the_device_belongs_to_the_company_that_approved_and_the_key_works(self):
        code = self.start()
        self.approve(code, name='Hubli office')
        key = self.poll(code).json()['api_key']
        device = BiometricDevice.objects.get(name='Hubli office')
        self.assertEqual(device.company, self.co)
        self.assertEqual(device.key_hash, BiometricDevice.hash_key(key))
        self.assertTrue(device.active)
        r = self.client_class().get(reverse('bio_api_punches'), HTTP_AUTHORIZATION='Bearer ' + key)
        self.assertEqual(r.json().get('ok'), True)

    def test_a_different_company_owner_gets_a_device_in_their_own_company(self):
        code = self.start()
        self.approve(code, user=self.other_owner)
        self.poll(code)
        self.assertEqual(BiometricDevice.objects.get().company, self.other)

    def test_the_key_is_never_stored_in_the_pairing_row(self):
        code = self.start()
        self.approve(code)
        key = self.poll(code).json()['api_key']
        row = BiometricPairing.objects.get()
        self.assertNotIn(key, ' '.join(str(getattr(row, f.name)) for f in row._meta.fields))

    def test_the_code_can_be_typed_in_lower_case_with_or_without_the_dash_and_spaces(self):
        code = self.start()
        typed = ' ' + code.lower().replace('-', ' ') + ' '
        self.assertContains(self.approve(typed), 'Approved')

    def test_a_blank_name_becomes_a_default_name(self):
        code = self.start()
        self.approve(code, name='   ')
        self.assertEqual(self.poll(code).json()['device'], 'Sync program')

    def test_the_name_is_cut_to_the_allowed_length(self):
        code = self.start()
        self.approve(code, name='N' * 200)
        self.poll(code)                                                              # the device is created when the program collects its key
        self.assertEqual(len(BiometricDevice.objects.get().name), 80)

    def test_the_approval_is_written_to_the_audit_log(self):
        code = self.start()
        self.approve(code, name='Hosur')
        self.assertTrue(AuditLog.objects.filter(actor=self.owner, details__contains='pairing code approved').exists())

    def test_a_code_can_be_approved_only_once(self):
        code = self.start()
        self.approve(code)
        self.assertContains(self.approve(code), 'not found or has expired')

    def test_an_expired_code_cannot_be_approved(self):
        code = self.start()
        BiometricPairing.objects.update(expires_at=timezone.now() - datetime.timedelta(seconds=1))
        self.assertContains(self.approve(code), 'not found or has expired')
        self.assertEqual(self.poll(code).status_code, 410)

    def test_an_approved_code_that_is_not_collected_in_time_expires(self):
        code = self.start()
        self.approve(code)
        BiometricPairing.objects.update(expires_at=timezone.now() - datetime.timedelta(seconds=1))
        r = self.poll(code)
        self.assertEqual((r.status_code, r.json()['status']), (410, 'expired'))
        self.assertEqual(BiometricDevice.objects.count(), 0)

    def test_the_approval_gives_five_minutes_to_collect(self):
        code = self.start()
        BiometricPairing.objects.update(expires_at=timezone.now() + datetime.timedelta(seconds=30))
        self.approve(code)
        left = (BiometricPairing.objects.get().expires_at - timezone.now()).total_seconds()
        self.assertTrue(240 < left <= 300, left)


class SafetyTests(PairingBase):
    def test_the_wrong_secret_gets_nothing_even_with_the_right_code(self):
        code = self.start()
        self.approve(code)
        r = self.poll(code, nonce=OTHER_NONCE)
        self.assertEqual((r.status_code, r.json()['status']), (404, 'unknown'))
        self.assertEqual(BiometricDevice.objects.count(), 0)
        self.assertEqual(self.poll(code).json()['status'], 'approved')                 # the real program still gets it

    def test_an_unknown_or_malformed_code_is_not_found(self):
        for code in ('ZZZZ-ZZZZ', '', 'abc', 'A' * 40):
            self.assertEqual(self.poll(code).status_code, 404, code)

    def test_wrong_codes_are_limited_per_user(self):
        for _ in range(8):
            self.approve('ZZZZ-ZZZZ')
        code = self.start()
        self.assertContains(self.approve(code), 'Too many wrong codes')
        self.assertEqual(self.poll(code).json()['status'], 'waiting')                  # even the right code is refused while blocked
        self.assertContains(self.approve(code, user=self.other_owner), 'Approved')    # another owner is not affected

    def test_a_right_code_clears_the_count_of_wrong_ones(self):
        for _ in range(5):
            self.approve('ZZZZ-ZZZZ')
        self.approve(self.start())
        for _ in range(5):
            self.approve('ZZZZ-ZZZZ')
        self.assertContains(self.approve(self.start(nonce=OTHER_NONCE), user=self.owner), 'Approved')

    def test_only_a_signed_in_company_owner_can_approve(self):
        code = self.start()
        r = self.client.post(reverse('bio_pair_approve'), {'code': code, 'name': 'x'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r['Location'])
        emp = make_user('pair_emp', 'EMPLOYEE', self.co)
        c = self.client_class()
        c.force_login(emp)
        self.assertIn(c.post(reverse('bio_pair_approve'), {'code': code, 'name': 'x'}).status_code, (302, 403))
        self.assertEqual(self.poll(code).json()['status'], 'waiting')

    def test_the_poll_never_reveals_which_part_was_wrong(self):
        code = self.start()
        a = self.poll('ZZZZ-ZZZZ').json()
        b = self.poll(code, nonce=OTHER_NONCE).json()
        self.assertEqual(a, b)


class PageTests(PairingBase):
    def test_the_biometric_page_has_the_approve_form(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse('bio_setup')).content.decode()
        self.assertIn(reverse('bio_pair_approve'), html)
        self.assertIn('name="code"', html)
        self.assertIn('Approve', html)
        self.assertEqual(len(re.findall(r'name="csrfmiddlewaretoken"', html)) >= 2, True)

    def test_a_wrong_code_shows_a_clear_message_and_a_right_one_shows_the_new_device(self):
        self.client.force_login(self.owner)
        r = self.client.post(reverse('bio_pair_approve'), {'code': 'ZZZZ-ZZZZ', 'name': 'x'}, follow=True)
        self.assertContains(r, 'not found or has expired')
        code = self.start()
        self.client.post(reverse('bio_pair_approve'), {'code': code, 'name': 'Hosur'})
        self.poll(code)
        self.assertContains(self.client.get(reverse('bio_setup')), 'Hosur')
