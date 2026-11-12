"""The application's starting page: visitors see the home page first, logged-in people still get their dashboard."""
import json

from django.test import TestCase
from django.urls import reverse

from .models import Employee
from .tests import make_company, make_user


class HomeEntryTests(TestCase):
    def test_a_visitor_who_is_not_logged_in_sees_the_home_page_at_the_empty_address(self):
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertTemplateUsed(r, 'public/landing.html')
        for text in ('Modern Payroll Management', 'Made Simple', 'Login', 'Register', 'Request a Demo', 'Get Started'):
            self.assertContains(r, text)

    def test_there_is_no_redirect_and_no_loop(self):
        r = self.client.get('/', follow=True)
        self.assertEqual(r.redirect_chain, [])
        self.assertEqual(r.status_code, 200)

    def test_the_empty_address_keeps_its_name(self):
        self.assertEqual(reverse('dashboard'), '/')

    def test_the_home_page_buttons_lead_to_login_and_register(self):
        h = self.client.get('/').content.decode()
        self.assertIn('href="%s"' % reverse('login'), h)
        self.assertIn('href="%s"' % reverse('company_register'), h)

    def test_the_welcome_address_and_the_login_page_still_work_for_visitors(self):
        self.assertEqual(self.client.get(reverse('landing')).status_code, 200)
        self.assertEqual(self.client.get(reverse('login')).status_code, 200)

    def test_a_company_owner_who_is_logged_in_gets_the_dashboard_not_the_home_page(self):
        owner = make_user('home_owner', 'COMPANY_OWNER', make_company('Home Co'))
        self.client.force_login(owner)
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertTemplateNotUsed(r, 'public/landing.html')
        self.assertNotContains(r, 'Made Simple')

    def test_the_platform_admin_and_an_employee_are_still_sent_to_their_own_pages(self):
        self.client.force_login(make_user('home_admin', 'ADMIN'))
        r = self.client.get('/')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], reverse('admin_dashboard'))
        co = make_company('Home Co 2')
        emp_user = make_user('home_emp', 'EMPLOYEE', co)
        Employee.objects.create(employee_code='HE1', first_name='H', last_name='E', email='he1@home.test', date_of_joining='2020-01-01', department='x', designation='y', company=co, user=emp_user)
        self.client.force_login(emp_user)
        r = self.client.get('/')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], reverse('employee_home'))

    def test_the_installed_app_starts_at_the_home_page(self):
        found = False
        for address in ('/manifest.webmanifest', '/manifest.json', '/pwa/manifest.json', '/static/manifest.json', '/static/pwa/manifest.json'):
            r = self.client.get(address)
            if r.status_code == 200 and b'start_url' in r.content:
                found = True
                self.assertEqual(json.loads(r.content.decode())['start_url'], '/', address)
        if not found:
            self.skipTest('no manifest was found at the usual addresses')
