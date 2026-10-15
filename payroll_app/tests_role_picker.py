"""Assign Role popup: search + select-all picker, several employees at once, and the original single save unchanged."""
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import AuditLog, UserRoleAssignment
from .tests import make_company, make_employee, make_user


class World(TestCase):
    def setUp(self):
        self.co, self.other = make_company('Role Co'), make_company('Role Other')
        self.owner = make_user('roleowner', 'COMPANY_OWNER', self.co)
        self.emps = [make_employee(self.co, f'R{i:03d}', f'r{i}@role.test') for i in range(5)]
        self.stranger = make_employee(self.other, 'RX', 'rx@role.test')
        self.client.force_login(self.owner)
        self.url = reverse('user_roles_permissions')

    def post(self, ids, role='HR'):
        return self.client.post(self.url, {'employee': [str(i) for i in ids], 'role': role}, follow=True)


class PageTests(World):
    def test_the_popup_carries_the_picker_and_keeps_the_plain_dropdown_as_a_fallback(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("emp-picker", html)
        self.assertIn("Search by name or ID", html)
        self.assertIn('Select all', html)
        self.assertIn('<select name="employee"', html)               # the original dropdown is still in the page

    def test_an_empty_company_keeps_its_own_message(self):
        owner = make_user('roleowner2', 'COMPANY_OWNER', make_company('Empty Role Co'))
        self.client.force_login(owner)
        self.assertContains(self.client.get(self.url), 'No employees found for your company yet')


class SaveTests(World):
    def test_one_employee_still_uses_the_original_save(self):
        resp = self.post([self.emps[0].pk])
        self.assertEqual(UserRoleAssignment.objects.filter(role='HR').count(), 1)
        self.assertEqual(UserRoleAssignment.objects.get().employee, self.emps[0])

    def test_several_employees_get_the_same_role_in_one_go(self):
        resp = self.post([e.pk for e in self.emps])
        self.assertContains(resp, 'HR assigned to 5 employee(s).')
        self.assertEqual(UserRoleAssignment.objects.filter(role='HR').count(), 5)
        self.assertTrue(AuditLog.objects.filter(details__icontains='Role HR assigned to 5 employees').exists())

    def test_people_who_already_have_the_role_are_not_doubled(self):
        UserRoleAssignment.objects.create(employee=self.emps[0], role='HR')
        resp = self.post([self.emps[0].pk, self.emps[1].pk, self.emps[2].pk])
        self.assertContains(resp, 'HR assigned to 2 employee(s); 1 already had it.')
        self.assertEqual(UserRoleAssignment.objects.filter(employee=self.emps[0], role='HR').count(), 1)

    def test_another_companys_employee_is_ignored(self):
        self.post([self.emps[0].pk, self.stranger.pk])
        self.assertFalse(UserRoleAssignment.objects.filter(employee=self.stranger).exists())
        self.assertEqual(UserRoleAssignment.objects.count(), 1)

    def test_a_made_up_role_or_junk_ids_save_nothing(self):
        self.post([self.emps[0].pk, self.emps[1].pk], role='SUPERUSER')
        self.client.post(self.url, {'employee': ['abc', '', ' '], 'role': 'HR'})
        self.assertEqual(UserRoleAssignment.objects.count(), 0)

    def test_ninety_employees_need_only_a_few_queries(self):
        many = [make_employee(self.co, f'M{i:03d}', f'm{i}@role.test') for i in range(90)]
        with CaptureQueriesContext(connection) as q:
            self.post([e.pk for e in many], role='FINANCE')
        self.assertEqual(UserRoleAssignment.objects.filter(role='FINANCE').count(), 90)
        self.assertLess(len(q), 40)


class AccessTests(World):
    def test_only_owners_can_use_it_and_never_for_another_company(self):
        self.client.force_login(make_user('roleemp', 'EMPLOYEE', self.co))
        self.assertEqual(self.client.post(self.url, {'employee': [str(self.emps[0].pk), str(self.emps[1].pk)], 'role': 'HR'}).status_code, 403)
        self.client.force_login(make_user('roleowner3', 'COMPANY_OWNER', self.other))
        self.post([self.emps[0].pk, self.emps[1].pk])
        self.assertEqual(UserRoleAssignment.objects.count(), 0)