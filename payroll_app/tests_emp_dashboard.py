"""Employee dashboard: small tables must not be forced wider than their cards, and the columns are balanced."""
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from .models import Employee
from .tests import make_company, make_employee, make_user


class EmployeeDashboardLayoutTests(TestCase):
    def test_wide_table_minimum_only_applies_inside_the_scroll_wrappers(self):
        css = open(finders.find('css/namma-theme.css'), encoding='utf-8').read()
        self.assertIn('body table{ min-width:0; }', css)
        self.assertIn('body .table-wrap table, body .fx-table table{ min-width:720px; }', css)

    def test_employee_dashboard_renders_with_balanced_columns(self):
        co = make_company('Emp Layout Co')
        user = make_user('emplayout', 'EMPLOYEE', co)
        emp = make_employee(co, 'EL001', 'el001@example.com')
        Employee.objects.filter(pk=emp.pk).update(user=user)
        self.client.force_login(user)
        resp = self.client.get(reverse('employee_home'))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('BALANCED-COLUMNS', html)
        self.assertLess(html.index('Recent leave'), html.index('Quick actions'))
        self.assertLess(html.index('Announcements'), html.index('Quick actions'))