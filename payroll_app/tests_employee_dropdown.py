"""Every Employee dropdown lists the company's employees, says clearly when there are none, and never lists another company's people."""
import re

from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from .models import Loan
from .tests import make_company, make_employee, make_user

PAGES = ('hr_loans', 'hr_insurance', 'hr_pay_components')


def options(html):
    m = re.search(r'<select[^>]*name="employee".*?</select>', html, re.S)
    return re.findall(r'<option[^>]*>([^<]*)</option>', m.group(0)) if m else None


class EmployeeDropdownTests(TestCase):
    def setUp(self):
        self.a, self.b = make_company('Dropdown Alpha'), make_company('Dropdown Beta')
        self.owner = make_user('dropowner', 'COMPANY_OWNER', self.a)
        make_employee(self.b, 'BETA1', 'beta1@drop.test')

    def pages(self):
        out = []
        for n in PAGES:
            try:
                out.append((n, reverse(n)))
            except NoReverseMatch:
                pass
        return out

    def test_empty_company_gets_a_clear_message_with_links(self):
        self.client.force_login(self.owner)
        for name, url in self.pages():
            html = self.client.get(url).content.decode()
            self.assertIn('No employees found for your company yet', html, name)
            self.assertIn('Import employees', html, name)

    def test_names_are_listed_and_no_message_when_employees_exist(self):
        make_employee(self.a, 'ALPHA1', 'alpha1@drop.test')
        make_employee(self.a, 'ALPHA2', 'alpha2@drop.test')
        self.client.force_login(self.owner)
        for name, url in self.pages():
            html = self.client.get(url).content.decode()
            opts = options(html)
            self.assertTrue(any(o.startswith('ALPHA1 - ') for o in opts), f'{name}: {opts}')
            self.assertTrue(any(o.startswith('ALPHA2 - ') for o in opts), name)
            self.assertNotIn('No employees found for your company yet', html, name)

    def test_another_companys_employees_are_never_listed(self):
        make_employee(self.a, 'ALPHA1', 'alpha1@drop.test')
        self.client.force_login(self.owner)
        for name, url in self.pages():
            self.assertFalse(any('BETA1' in o for o in options(self.client.get(url).content.decode())), name)

    def test_a_loan_can_be_added_by_picking_a_name_from_the_dropdown(self):
        emp = make_employee(self.a, 'ALPHA1', 'alpha1@drop.test')
        self.client.force_login(self.owner)
        resp = self.client.post(reverse('hr_loans'), {'employee': emp.pk, 'reference_no': 'LN-1', 'loan_amount': '10000', 'interest_rate': '0',
                                                      'tenure_months': '2', 'emi_amount': '5000', 'start_date': '2026-10-01', 'notes': ''})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Loan.objects.filter(company=self.a, employee=emp).count(), 1)