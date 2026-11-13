"""The Login and Register pages have their own show/hide password button. They load the shared password script (the app expects it on every page with a password box) with a flag that keeps it out of the way, so there is never a second eye button."""
import re

from django.test import TestCase
from django.urls import reverse

FLAG = 'window.__nmPasswordToggle = true'
SHARED = re.compile(r'<script src="[^"]*password-toggle[^"]*\.js"[^>]*></script>')


class OwnEyeButtonTests(TestCase):
    def page(self, name):
        resp = self.client.get(reverse(name))
        self.assertEqual(resp.status_code, 200, name)
        return resp.content.decode()

    def test_both_pages_load_the_shared_script_exactly_once(self):
        for name in ('login', 'company_register'):
            self.assertEqual(len(SHARED.findall(self.page(name))), 1, name)

    def test_the_flag_comes_before_the_shared_script_so_that_the_script_stays_out_of_the_way(self):
        for name in ('login', 'company_register'):
            html = self.page(name)
            self.assertEqual(html.count(FLAG), 1, name)
            self.assertLess(html.index(FLAG), SHARED.search(html).start(), name)

    def test_the_login_page_has_exactly_one_eye_button_of_its_own(self):
        html = self.page('login')
        self.assertEqual(html.count('id="pwToggle"'), 1)
        self.assertNotIn('pw-eye', html)                                   # the shared script's own button is never in the page itself

    def test_both_scripts_sit_at_the_end_of_the_page(self):
        for name in ('login', 'company_register'):
            html = self.page(name)
            self.assertLess(html.index('</body>') - SHARED.search(html).end(), 400, name)
