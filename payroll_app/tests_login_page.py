"""The sign-in page: the dark green design keeps every field, message and link of the old page and still logs people in."""
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from .tests import make_company, make_user

PASSWORD = 'Str0ng-pass!'


class LoginPageTests(TestCase):
    def setUp(self):
        self.co = make_company('Login Co')
        self.user = make_user('login_owner', 'COMPANY_OWNER', self.co)
        self.user.set_password(PASSWORD)
        self.user.save()

    def test_the_page_opens_with_every_piece_of_the_form(self):
        r = self.client.get(reverse('login'))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for text in ('Welcome Back', 'Sign in to access your Namma Payroll workspace.', 'Username, Email or Employee ID', 'Forgot password?', 'Sign In'):
            self.assertIn(text, html)
        self.assertIn('name="csrfmiddlewaretoken"', html)
        self.assertIn('name="username"', html)
        self.assertIn('name="password"', html)
        self.assertIn('type="password"', html)
        self.assertIn('href="%s"' % reverse('password_reset'), html)
        self.assertIn('namma-payroll-logo', html)
        self.assertIn('login-dark-green', html)

    def test_it_is_a_standalone_page_with_no_template_leftovers(self):
        html = self.client.get(reverse('login')).content.decode()
        self.assertEqual((html.count('<html'), html.count('</html>')), (1, 1))
        self.assertNotIn('{{', html)
        self.assertNotIn('{%', html)

    def test_a_correct_login_works(self):
        r = self.client.post(reverse('login'), {'username': 'login_owner', 'password': PASSWORD})
        self.assertEqual(r.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)

    def test_a_wrong_password_shows_an_error_and_does_not_log_in(self):
        r = self.client.post(reverse('login'), {'username': 'login_owner', 'password': 'wrong-password'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '<div class="alert">')
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertContains(r, 'value="login_owner"')                    # the username is kept
        self.assertNotContains(r, 'wrong-password')                      # the password never is

    def test_next_is_kept_in_a_hidden_field(self):
        self.assertContains(self.client.get(reverse('login') + '?next=/dashboard/'), '<input type="hidden" name="next" value="/dashboard/">')

    def test_special_characters_in_the_username_are_escaped(self):
        r = self.client.post(reverse('login'), {'username': '"><script>alert(1)</script>', 'password': 'x'})
        self.assertNotContains(r, '<script>alert(1)</script>')

    def test_an_already_signed_in_person_is_sent_on(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('login')).status_code, 302)

    def test_the_register_link_is_shown(self):
        try:
            url = reverse('company_register')
        except NoReverseMatch:
            self.skipTest('this project has no company_register page')
        self.assertContains(self.client.get(reverse('login')), 'href="%s"' % url)

    def test_the_password_eye_and_double_click_protection_are_in_the_page(self):
        html = self.client.get(reverse('login')).content.decode()
        self.assertIn('id="pwToggle"', html)
        self.assertIn('Signing in...', html)
