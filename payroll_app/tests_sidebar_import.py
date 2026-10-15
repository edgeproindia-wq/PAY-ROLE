"""The Import Employees sidebar item is gone; importing is done from the popup, and the old page stays as a backup address."""
from django.test import TestCase
from django.urls import reverse

from .tests import make_company, make_user


class SidebarTests(TestCase):
    def setUp(self):
        self.co = make_company('Side Co')
        self.owner = make_user('sideowner', 'COMPANY_OWNER', self.co)
        self.client.force_login(self.owner)

    def test_the_sidebar_no_longer_lists_import_employees(self):
        for name in ('dashboard', 'employee_master', 'salary_structure'):
            html = self.client.get(reverse(name)).content.decode()
            self.assertNotIn('<span class="tx">Import Employees</span>', html, name)

    def test_importing_is_still_one_click_away(self):
        html = self.client.get(reverse('employee_master')).content.decode()
        self.assertIn('openEmpImport()', html)                      # the Import Excel button
        self.assertIn('id="empImportModal"', html)                  # the popup itself

    def test_the_old_page_still_answers_as_a_backup_address(self):
        self.assertEqual(self.client.get(reverse('hr_employee_import')).status_code, 200)