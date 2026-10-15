"""Salary amount boxes show Indian-style commas and decimals while typing; the server still receives plain numbers."""
from django.test import TestCase
from django.urls import reverse

from .models import SalaryStructure
from .tests import make_company, make_employee, make_user


class AmountFormatTests(TestCase):
    def setUp(self):
        self.co = make_company('Amt Co')
        self.owner = make_user('amtowner', 'COMPANY_OWNER', self.co)
        self.emp = make_employee(self.co, 'AM1', 'am1@amt.test')
        self.client.force_login(self.owner)

    def test_the_formatting_script_is_on_the_salary_pages(self):
        pages = [self.client.get(reverse('salary_structure')), self.client.get(reverse('salary_history'), {'employee': self.emp.pk})]
        for resp in pages:
            html = resp.content.decode()
            self.assertIn("var NAMES = ['basic', 'hra', 'conveyance', 'special_allowance']", html)
            self.assertIn('name="basic"', html)

    def test_plain_numbers_are_saved_exactly_as_before(self):
        resp = self.client.post(reverse('salary_structure'), {'employee': self.emp.pk, 'basic': '2500000.50', 'hra': '0', 'conveyance': '1600', 'special_allowance': '0'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(str(SalaryStructure.objects.get(employee=self.emp).basic), '2500000.50')

    def test_a_number_with_commas_is_refused_by_the_server_so_the_hidden_plain_twin_matters(self):
        self.client.post(reverse('salary_structure'), {'employee': self.emp.pk, 'basic': '25,00,000.00', 'hra': '0', 'conveyance': '0', 'special_allowance': '0'})
        self.assertEqual(SalaryStructure.objects.count(), 0)

    def test_the_first_salary_in_salary_history_still_saves(self):
        resp = self.client.post(reverse('salary_history'), {'employee': self.emp.pk, 'effective_from': '2027-01-01', 'basic': '30000', 'hra': '10000', 'conveyance': '0', 'special_allowance': '0'}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '30000')