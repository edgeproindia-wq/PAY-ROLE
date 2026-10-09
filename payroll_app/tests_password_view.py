"""Every password field gets the show/hide eye button (login, registration, password change and reset)."""
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from .tests import make_company, make_user


class PasswordViewTests(TestCase):
    def test_the_script_is_served_by_the_static_system(self):
        path = finders.find('js/password-toggle.js')
        self.assertIsNotNone(path)
        text = open(path, encoding='utf-8').read()
        for word in ('pw-eye', 'Show password', 'CapsLock'):
            self.assertIn(word, text)

    def test_public_pages_with_passwords_load_the_script(self):
        checked = 0
        for name in ('login', 'company_register', 'employee_register', 'password_reset'):
            try:
                url = reverse(name)
            except NoReverseMatch:
                continue
            resp = self.client.get(url)
            html = resp.content.decode()
            if resp.status_code == 200 and '</body>' in html:      # only real pages
                self.assertIn('password-toggle.js', html, f'{name} page does not load the password script')
                checked += 1
        self.assertGreaterEqual(checked, 3)

    def test_signed_in_password_change_page_loads_the_script(self):
        owner = make_user('pwowner', 'COMPANY_OWNER', make_company('Pw Co'))
        self.client.force_login(owner)
        self.assertContains(self.client.get(reverse('password_change')), 'password-toggle.js')

    def test_the_login_form_still_signs_people_in(self):
        make_user('pwlogin', 'COMPANY_OWNER', make_company('Pw Login Co'))
        resp = self.client.post(reverse('login'), {'username': 'pwlogin', 'password': 'StrongPass123'})
        self.assertEqual(resp.status_code, 302)