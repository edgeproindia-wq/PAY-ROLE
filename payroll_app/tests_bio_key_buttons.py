"""The Biometric page shows a newly created device key in a box that scrolls into view, with Copy key and Download key file buttons. The key is still shown only once and is never sent anywhere."""
import re

from django.test import TestCase
from django.urls import reverse

from .tests import make_company, make_user


class KeyBoxTests(TestCase):
    def setUp(self):
        self.co = make_company('Key Co')
        self.owner = make_user('key_owner', 'COMPANY_OWNER', self.co)
        self.client.force_login(self.owner)
        self.url = reverse('bio_setup')

    def add(self, name='Hosur'):
        return self.client.post(self.url, {'action': 'add_device', 'name': name})

    def test_the_new_key_box_has_both_buttons_and_scrolls_into_view(self):
        r = self.add()
        html = r.content.decode()
        for needle in ('id="newKeyCard"', 'id="copyKeyBtn"', 'Copy key', 'id="downloadKeyBtn"', 'Download key file', 'namma-payroll-device-key.txt', 'scrollIntoView', 'new Blob'):
            self.assertIn(needle, html, needle)

    def test_the_key_appears_exactly_once_in_the_page_and_never_inside_the_script(self):
        r = self.add()
        key = r.context['new_key']
        html = r.content.decode()
        self.assertEqual(html.count(key), 1)
        script = re.search(r'<script>\s*\(function \(\) \{\s*var card = document\.getElementById\("newKeyCard"\).*?</script>', html, re.S).group(0)
        self.assertNotIn(key, script)

    def test_without_a_new_key_the_page_has_no_key_box_and_no_buttons(self):
        self.add()
        html = self.client.get(self.url).content.decode()
        for needle in ('newKeyCard', 'copyKeyBtn', 'downloadKeyBtn', 'device-key.txt'):
            self.assertNotIn(needle, html, needle)

    def test_a_missing_name_still_gives_no_key_box(self):
        html = self.client.post(self.url, {'action': 'add_device', 'name': '  '}, follow=True).content.decode()
        self.assertNotIn('newKeyCard', html)
        self.assertIn('Give the device a name', html)

    def test_the_script_sends_nothing_anywhere_and_runs_no_text_as_code(self):
        html = self.add().content.decode()
        script = re.search(r'<script>\s*\(function \(\) \{\s*var card = document\.getElementById\("newKeyCard"\).*?</script>', html, re.S).group(0)
        for banned in ('fetch(', 'XMLHttpRequest', 'sendBeacon', 'eval(', 'new Function', 'document.write', 'innerHTML', 'WebSocket'):
            self.assertNotIn(banned, script, banned)

    def test_the_key_is_still_stored_only_as_a_scrambled_value(self):
        from .biometric_models import BiometricDevice
        key = self.add().context['new_key']
        d = BiometricDevice.objects.get(company=self.co, name='Hosur')
        self.assertNotIn(key, (d.key_hash, d.key_prefix))
        self.assertEqual(d.key_hash, BiometricDevice.hash_key(key))
