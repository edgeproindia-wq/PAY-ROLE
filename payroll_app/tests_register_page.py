"""The company sign-up page: the light design keeps every step, field, message and the sign-up itself working."""
import time

from django.contrib.auth.hashers import make_password
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Company
from .views import REG_OTP_KEY, REG_VERIFIED_KEY

STRONG = 'Str0ng-pass!23'
FIELDS = ('company_name', 'owner_full_name', 'contact_email', 'contact_phone', 'address', 'username', 'password1', 'password2')


def details(**changes):
    data = {'action': 'register', 'company_name': 'Light Test Co', 'owner_full_name': 'Asha Rao', 'contact_email': 'owner@light.test', 'contact_phone': '9876543210',
            'address': 'Hosur', 'username': 'asha_owner', 'password1': STRONG, 'password2': STRONG}
    data.update(changes)
    return data


class PageBase(TestCase):
    def url(self):
        return reverse('company_register')

    def set_otp(self, email='owner@light.test', code='123456'):
        s = self.client.session
        now = time.time()
        s[REG_OTP_KEY] = {'email': email, 'hash': make_password(code), 'sent': now - 120, 'exp': now + 600, 'attempts': 0}
        s.save()

    def set_verified(self, email='owner@light.test'):
        s = self.client.session
        s[REG_VERIFIED_KEY] = {'email': email, 'at': time.time()}
        s.save()


@override_settings(REQUIRE_EMAIL_VERIFICATION=True)
class ThreeStepTests(PageBase):
    def test_every_step_is_the_light_standalone_page(self):
        pages = []
        pages.append(self.client.get(self.url()).content.decode())
        self.set_otp()
        pages.append(self.client.get(self.url()).content.decode())
        self.client.session.flush()
        self.set_verified()
        s = self.client.session
        s.pop(REG_OTP_KEY, None)
        s.save()
        pages.append(self.client.get(self.url()).content.decode())
        for html in pages:
            self.assertIn('register-light', html)
            self.assertEqual((html.count('<html'), html.count('</html>')), (1, 1))
            self.assertNotIn('{{', html)
            self.assertNotIn('{%', html)
            self.assertIn('Register Your Company', html)
            self.assertIn('name="csrfmiddlewaretoken"', html)
            self.assertIn('href="%s"' % reverse('login'), html)

    def test_step_1_asks_for_the_email_and_sends_a_code(self):
        r = self.client.get(self.url())
        self.assertContains(r, 'Send OTP')
        self.assertContains(r, 'name="email"')
        self.assertContains(r, 'name="action" value="send_otp"')
        self.assertNotContains(r, 'Create Account')
        bad = self.client.post(self.url(), {'action': 'send_otp', 'email': 'not-an-email'})
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, 'class="errorlist"')
        ok = self.client.post(self.url(), {'action': 'send_otp', 'email': 'owner@light.test'})
        self.assertEqual(ok.status_code, 302)
        self.assertIn(REG_OTP_KEY, self.client.session)

    def test_step_2_asks_for_the_code_and_moves_on_when_it_is_right(self):
        self.set_otp(code='123456')
        r = self.client.get(self.url())
        self.assertContains(r, 'Verify Email')
        self.assertContains(r, 'Resend code')
        self.assertContains(r, 'Change email')
        self.assertContains(r, 'owner@light.test')
        wrong = self.client.post(self.url(), {'action': 'verify_otp', 'code': '000000'})
        self.assertContains(wrong, 'Incorrect code.')
        right = self.client.post(self.url(), {'action': 'verify_otp', 'code': '123456'})
        self.assertEqual(right.status_code, 302)
        step3 = self.client.get(self.url())
        for name in FIELDS:
            self.assertContains(step3, 'name="%s"' % name)
        self.assertContains(step3, 'Verified')
        self.assertContains(step3, 'readonly')
        self.assertContains(step3, 'Registering with')

    def test_change_email_goes_back_to_step_1(self):
        self.set_otp()
        self.client.post(self.url(), {'action': 'change_email'})
        self.assertNotIn(REG_OTP_KEY, self.client.session)
        self.assertContains(self.client.get(self.url()), 'Send OTP')

    def test_step_3_creates_the_company_and_sends_the_person_to_the_login(self):
        self.set_verified()
        r = self.client.post(self.url(), details())
        self.assertRedirects(r, reverse('login'), fetch_redirect_response=False)
        self.assertTrue(Company.objects.filter(name='Light Test Co').exists())
        self.assertNotIn(REG_VERIFIED_KEY, self.client.session)

    def test_step_3_shows_the_errors_under_the_fields(self):
        self.set_verified()
        r = self.client.post(self.url(), details(company_name='', password2='different'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'This field is required.')
        self.assertContains(r, 'Passwords do not match.')
        self.assertContains(r, 'class="errorlist"')
        self.assertFalse(Company.objects.filter(contact_email='owner@light.test').exists())


@override_settings(REQUIRE_EMAIL_VERIFICATION=False)
class OnePageTests(PageBase):
    def test_the_one_page_version_has_every_field_and_no_steps(self):
        r = self.client.get(self.url())
        self.assertEqual(r.status_code, 200)
        for name in FIELDS:
            self.assertContains(r, 'name="%s"' % name)
        self.assertContains(r, 'Fill in your details to create your company account.')
        self.assertContains(r, 'Create Account')
        self.assertContains(r, 'register-light')
        self.assertNotContains(r, 'Verify OTP')

    def test_the_one_page_sign_up_works_and_shows_errors(self):
        bad = self.client.post(self.url(), details(contact_phone='123'))
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, 'class="errorlist"')
        self.assertFalse(Company.objects.filter(name='Light Test Co').exists())
        ok = self.client.post(self.url(), details())
        self.assertRedirects(ok, reverse('login'), fetch_redirect_response=False)
        self.assertTrue(Company.objects.filter(name='Light Test Co').exists())

    def test_a_duplicate_company_name_is_refused(self):
        Company.objects.create(name='Light Test Co', contact_email='x@x.test', contact_phone='9876543210')
        r = self.client.post(self.url(), details())
        self.assertContains(r, 'A company with this name is already registered.')
