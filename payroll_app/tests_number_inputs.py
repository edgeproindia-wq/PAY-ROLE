"""Number boxes behave like proper numeric entry everywhere in the app; saved values and validation are unchanged."""
from django.test import TestCase
from django.urls import reverse

from .models import SalaryStructure
from .tests import make_company, make_employee, make_user


class NumberInputTests(TestCase):
    def setUp(self):
        self.co = make_company('Num Co')
        self.owner = make_user('numowner', 'COMPANY_OWNER', self.co)
        self.emp = make_employee(self.co, 'NM1', 'nm1@num.test')
        self.client.force_login(self.owner)

    def test_every_app_page_carries_the_number_box_behaviour(self):
        for name in ('salary_structure', 'attendance', 'dashboard'):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn("input[type=number]", html, name)
            self.assertIn("setAttribute('inputmode'", html, name)                    # numeric keypad
            self.assertIn("e.target.blur()", html, name)                              # wheel cannot change an amount
            self.assertIn("k === 'e'", html, name)                                    # no letters in amounts

    def test_the_public_login_page_is_not_changed(self):
        self.client.logout()
        self.assertNotIn('setAttribute(\'inputmode\'', self.client.get('/login/').content.decode())

    def test_saving_a_salary_structure_works_exactly_as_before(self):
        resp = self.client.post(reverse('salary_structure'), {'employee': self.emp.pk, 'basic': '25000', 'hra': '10000', 'conveyance': '1600', 'special_allowance': '0'})
        self.assertEqual(resp.status_code, 302)
        s = SalaryStructure.objects.get(employee=self.emp)
        self.assertEqual((int(s.basic), int(s.hra), int(s.conveyance)), (25000, 10000, 1600))

    def test_the_server_still_refuses_a_zero_basic(self):
        resp = self.client.post(reverse('salary_structure'), {'employee': self.emp.pk, 'basic': '0', 'hra': '0', 'conveyance': '0', 'special_allowance': '0'})
        self.assertEqual(SalaryStructure.objects.count(), 0)