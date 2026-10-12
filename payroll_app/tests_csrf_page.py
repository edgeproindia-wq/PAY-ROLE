"""A failed CSRF check shows a clear page, logs the reason, and the protection itself stays ON."""
from django.test import Client, TestCase
from django.urls import reverse

from .tests import make_company, make_user


class CsrfPageTests(TestCase):
    def test_a_post_without_a_token_gets_the_friendly_page_and_a_log_line(self):
        client = Client(enforce_csrf_checks=True)
        with self.assertLogs('payroll_app.csrf', level='WARNING') as logs:
            resp = client.post(reverse('login'), {'username': 'x', 'password': 'y'})
        self.assertEqual(resp.status_code, 403)
        html = resp.content.decode()
        self.assertIn('Your page timed out', html)
        self.assertNotIn('CSRF verification failed', html)
        self.assertIn('reason:', logs.output[0])

    def test_the_protection_is_still_on_for_signed_in_actions(self):
        owner = make_user('csrfowner', 'COMPANY_OWNER', make_company('Csrf Co'))
        client = Client(enforce_csrf_checks=True)
        client.force_login(owner)
        resp = client.post(reverse('notification_mark_all_read'), {})
        self.assertEqual(resp.status_code, 403)

    def test_a_normal_page_with_its_token_still_works(self):
        make_user('csrfok', 'COMPANY_OWNER', make_company('Csrf Ok Co'))
        client = Client(enforce_csrf_checks=True)
        token = client.get(reverse('login')).cookies['csrftoken'].value
        resp = client.post(reverse('login'), {'username': 'csrfok', 'password': 'StrongPass123', 'csrfmiddlewaretoken': token})
        self.assertEqual(resp.status_code, 302)