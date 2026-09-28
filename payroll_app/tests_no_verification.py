from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Company, User

PWD = 'StrongPass123'


def payload(**over):
    data = {
        'company_name': 'PlainCo', 'owner_full_name': 'Plain Owner', 'contact_email': 'Owner@PlainCo.com',
        'contact_phone': '9876543210', 'address': 'Hosur', 'username': 'plainowner',
        'password1': PWD, 'password2': PWD,
    }
    data.update(over)
    return data


@override_settings(REQUIRE_EMAIL_VERIFICATION=False)
class RegistrationWithoutEmailVerificationTests(TestCase):
    def test_form_is_one_step_without_otp(self):
        resp = self.client.get(reverse('company_register'))
        self.assertContains(resp, 'Create Account')
        self.assertNotContains(resp, 'Send OTP')
        self.assertNotContains(resp, 'Verify OTP')

    def test_register_sends_no_email_and_waits_for_approval(self):
        resp = self.client.post(reverse('company_register'), payload())
        self.assertRedirects(resp, reverse('login'), fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 0)
        owner = User.objects.get(username='plainowner')
        self.assertFalse(owner.is_active)
        self.assertEqual(owner.company.status, 'PENDING_APPROVAL')
        self.assertTrue(owner.password.startswith(('pbkdf2_', 'argon2', 'bcrypt')))

    def test_bad_input_shows_errors_and_creates_nothing(self):
        resp = self.client.post(reverse('company_register'), payload(password2='different'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Company.objects.count(), 0)

    def test_owner_can_sign_in_after_admin_approval(self):
        self.client.post(reverse('company_register'), payload())
        owner = User.objects.get(username='plainowner')
        resp = self.client.post(reverse('login'), {'username': 'plainowner', 'password': PWD})
        self.assertContains(resp, 'pending admin approval')
        admin = User.objects.create_superuser('boss', 'boss@ex.com', PWD)
        self.client.force_login(admin)
        self.client.post(reverse('admin_company_decide', args=[owner.company.pk]), {'decision': 'APPROVED'})
        self.client.post(reverse('logout'))
        resp = self.client.post(reverse('login'), {'username': 'plainowner', 'password': PWD})
        self.assertRedirects(resp, reverse('post_login_redirect'), fetch_redirect_response=False)
