"""Play Store web pieces: manifest, service worker, app link file, public legal pages, account deletion request."""
import json
from unittest import mock

from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from .models import AuditLog, Notification
from .readiness import run_checks
from .tests import make_company, make_user

FP = ':'.join(['AB'] * 32)


class ManifestAndWorkerTests(TestCase):
    def test_the_manifest_has_what_android_needs_and_its_icons_exist(self):
        resp = self.client.get('/manifest.webmanifest')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('manifest+json', resp['Content-Type'])
        m = json.loads(resp.content)
        self.assertEqual((m['name'], m['display'], m['scope']), ('Namma Payroll', 'standalone', '/'))
        self.assertTrue(m['start_url'] and m['theme_color'] and m['background_color'])
        sizes = {i['sizes'] for i in m['icons']}
        self.assertTrue({'192x192', '512x512'} <= sizes)
        self.assertTrue(any(i['purpose'] == 'maskable' for i in m['icons']))
        for name in ('icon-192.png', 'icon-512.png', 'maskable-512.png'):
            self.assertIsNotNone(finders.find(f'pwa/{name}'), name)

    def test_the_service_worker_never_caches_pages(self):
        resp = self.client.get('/sw.js')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('javascript', resp['Content-Type'])
        self.assertEqual(resp['Service-Worker-Allowed'], '/')
        self.assertIn('no-cache', resp['Cache-Control'])
        js = resp.content.decode()
        self.assertIn("addEventListener('fetch'", js)
        self.assertIn("req.mode === 'navigate'", js)
        nav = js.split("req.mode === 'navigate'")[1].split('return;')[0]
        self.assertNotIn('cache.put', nav)                     # pages come from the network only
        self.assertEqual(js.count('cache.put'), 1)             # the only thing stored is /static/ files

    def test_offline_page_is_public(self):
        resp = self.client.get('/offline/')
        self.assertContains(resp, 'You are offline')


class AssetLinksTests(TestCase):
    def get(self, **env):
        with mock.patch.dict('os.environ', env):
            return json.loads(self.client.get('/.well-known/assetlinks.json').content)

    def test_empty_until_the_android_app_is_signed(self):
        self.assertEqual(self.get(ANDROID_PACKAGE_NAME='', ANDROID_SHA256_FINGERPRINTS=''), [])

    def test_valid_settings_produce_the_link(self):
        data = self.get(ANDROID_PACKAGE_NAME='com.example.namma', ANDROID_SHA256_FINGERPRINTS=FP.lower())
        t = data[0]['target']
        self.assertEqual((t['namespace'], t['package_name'], t['sha256_cert_fingerprints']), ('android_app', 'com.example.namma', [FP]))

    def test_a_malformed_fingerprint_is_refused(self):
        self.assertEqual(self.get(ANDROID_PACKAGE_NAME='com.example.namma', ANDROID_SHA256_FINGERPRINTS='not-a-fingerprint'), [])


class LegalPagesTests(TestCase):
    def test_public_pages_open_without_login_and_say_the_right_things(self):
        priv = self.client.get('/privacy/')
        self.assertEqual(priv.status_code, 200)
        html = priv.content.decode()
        self.assertGreater(len(html), 1500)
        for word in ('Bank details', 'Attendance', 'We do not sell', 'delete-account'):
            self.assertIn(word, html)
        self.assertContains(self.client.get('/terms/'), 'Terms of service')
        dele = self.client.get('/delete-account/')
        self.assertContains(dele, 'Delete my account')
        self.assertContains(dele, '/account/delete-request/')

    def test_your_company_details_appear_when_set(self):
        with mock.patch.dict('os.environ', {'LEGAL_ENTITY_NAME': 'Acme Payroll Pvt Ltd', 'SUPPORT_EMAIL': 'help@acme.test', 'DELETION_RESPONSE_DAYS': '15'}):
            self.assertContains(self.client.get('/privacy/'), 'Acme Payroll Pvt Ltd')
            self.assertContains(self.client.get('/privacy/'), 'help@acme.test')
            self.assertContains(self.client.get('/delete-account/'), 'within 15 days')

    def test_login_page_and_app_pages_carry_the_manifest_service_worker_and_legal_links(self):
        html = self.client.get('/login/').content.decode()
        self.assertIn('rel="manifest" href="/manifest.webmanifest"', html)
        self.assertIn("serviceWorker.register('/sw.js'", html)
        self.assertIn('theme-color', html)
        self.assertIn('href="/privacy/"', html)
        co = make_company('Pwa Co')
        self.client.force_login(make_user('pwaowner', 'COMPANY_OWNER', co))
        app = self.client.get(reverse('dashboard'))
        self.assertContains(app, 'rel="manifest" href="/manifest.webmanifest"')
        self.assertContains(app, 'href="/privacy/"')


class DeletionRequestTests(TestCase):
    def setUp(self):
        self.co = make_company('Del Co')
        self.owner = make_user('delowner', 'COMPANY_OWNER', self.co)
        self.emp_user = make_user('delemp', 'EMPLOYEE', self.co)
        self.admin = make_user('deladmin', 'ADMIN', None)
        self.url = reverse('account_delete_request')

    def test_it_needs_a_login(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.post(self.url, {'confirm': 'yes'}).status_code, 302)

    def test_without_the_tick_nothing_is_recorded(self):
        self.client.force_login(self.emp_user)
        self.client.post(self.url, {'reason': 'leaving'})
        self.assertFalse(AuditLog.objects.filter(details__icontains='deletion requested').exists())

    def test_a_confirmed_request_is_logged_and_the_decision_makers_are_told(self):
        self.client.force_login(self.emp_user)
        resp = self.client.post(self.url, {'confirm': 'yes', 'reason': 'I have left the company'}, follow=True)
        self.assertContains(resp, 'request was received')
        log = AuditLog.objects.get(details__icontains='deletion requested')
        self.assertIn('delemp', log.details)
        told = set(Notification.objects.filter(message__icontains='delemp').values_list('recipient__username', flat=True))
        self.assertEqual(told, {'delowner', 'deladmin'})

    def test_asking_twice_in_a_day_records_it_once(self):
        self.client.force_login(self.emp_user)
        self.client.post(self.url, {'confirm': 'yes'})
        self.client.post(self.url, {'confirm': 'yes'})
        self.assertEqual(AuditLog.objects.filter(details__icontains='deletion requested').count(), 1)


class ReadinessAdditionsTests(TestCase):
    def test_readiness_flags_missing_legal_details_and_the_android_link(self):
        with mock.patch.dict('os.environ', {'LEGAL_ENTITY_NAME': '', 'SUPPORT_EMAIL': '', 'ANDROID_PACKAGE_NAME': '', 'ANDROID_SHA256_FINGERPRINTS': ''}):
            by = {c['name']: c['status'] for c in run_checks()}
        self.assertEqual(by['Legal contact details are set'], 'ACTION NEEDED')
        self.assertEqual(by['Android app link (assetlinks) is set'], 'WARNING')
        with mock.patch.dict('os.environ', {'LEGAL_ENTITY_NAME': 'Acme', 'SUPPORT_EMAIL': 'a@b.test', 'ANDROID_PACKAGE_NAME': 'com.a.b', 'ANDROID_SHA256_FINGERPRINTS': FP}):
            by = {c['name']: c['status'] for c in run_checks()}
        self.assertEqual((by['Legal contact details are set'], by['Android app link (assetlinks) is set']), ('PASS', 'PASS'))