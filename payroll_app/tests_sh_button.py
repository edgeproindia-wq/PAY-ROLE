"""Salary history: choosing a name does nothing until the Confirm button is pressed, and an employee with no salary yet does not crash the page."""
import datetime

from django.test import TestCase
from django.urls import reverse

from .models import SalaryStructure
from .tests import make_company, make_employee, make_user


class SalaryHistoryButtonTests(TestCase):
    def setUp(self):
        self.co = make_company('SH Btn Co')
        self.owner = make_user('shbtnowner', 'COMPANY_OWNER', self.co)
        self.emp = make_employee(self.co, 'SB1', 'sb1@shbtn.test')
        SalaryStructure.objects.create(employee=self.emp, basic=20000, hra=8000, conveyance=1600, special_allowance=1400)
        self.client.force_login(self.owner)
        self.url = reverse('salary_history')

    def test_the_page_has_a_visible_confirm_button_and_no_automatic_reload(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('Confirm - show salary history', html)
        self.assertNotIn("getElementById('pickForm').submit()", html)         # picking a name no longer reloads the page
        self.assertIn('<select name="employee" required>', html)               # the browser asks for a choice before confirming

    def test_confirming_shows_that_employees_history(self):
        resp = self.client.get(self.url, {'employee': self.emp.pk})
        self.assertContains(resp, 'salary timeline')
        self.assertContains(resp, 'Opening salary')

    def test_without_a_choice_no_history_is_shown(self):
        self.assertNotContains(self.client.get(self.url), 'salary timeline')

    def test_adding_a_salary_still_works(self):
        resp = self.client.post(self.url, {'employee': self.emp.pk, 'effective_from': '2027-01-01', 'basic': '30000', 'hra': '10000', 'conveyance': '0', 'special_allowance': '0'}, follow=True)
        self.assertContains(resp, 'timeline')


class FirstSalaryTests(TestCase):
    """An employee with no salary yet must not crash the page (the normal state right after importing employees)."""

    def setUp(self):
        self.co = make_company('First Sal Co')
        self.owner = make_user('firstsalowner', 'COMPANY_OWNER', self.co)
        self.emp = make_employee(self.co, 'FS1', 'fs1@firstsal.test')
        self.client.force_login(self.owner)
        self.url = reverse('salary_history')

    def test_an_employee_with_no_salary_opens_with_a_clear_message_and_empty_boxes(self):
        resp = self.client.get(self.url, {'employee': self.emp.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'No salary saved for this employee yet')
        self.assertContains(resp, 'name="basic"')

    def test_the_first_salary_can_be_added_and_then_shows_in_the_timeline(self):
        resp = self.client.post(self.url, {'employee': self.emp.pk, 'effective_from': '2026-11-01', 'basic': '25000', 'hra': '10000', 'conveyance': '0', 'special_allowance': '0'}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'No salary saved for this employee yet')
        self.assertContains(resp, '25000')