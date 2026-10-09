"""Big imports must hash the shared password once (not once per employee) and logins must still work."""
import io
from unittest import mock

from django.contrib.auth.hashers import check_password, get_hasher
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from openpyxl import Workbook

from .models import Employee, User
from .tests import make_company, make_employee, make_user

HEAD = ['Employee ID', 'First Name', 'Last Name', 'Email', 'Date of Joining', 'Department', 'Designation']
PASSWORD = 'Strong-Pass#2026'


def counting():
    """Count real password-hash computations (the slow part) of the hasher the project uses."""
    cls = type(get_hasher())
    return mock.patch.object(cls, 'encode', autospec=True, side_effect=cls.encode)


def workbook(n):
    wb = Workbook()
    ws = wb.active
    ws.append(HEAD)
    for i in range(n):
        ws.append([f'SPD{i:03d}', 'Test', f'Person{i}', f'spd{i}@example.com', '2025-06-02', 'Detailing', 'Modeller'])
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile('people.xlsx', buf.getvalue())


class ImportSpeedTests(TestCase):
    def setUp(self):
        self.co = make_company('Speed Co')
        self.owner = make_user('speedowner', 'COMPANY_OWNER', self.co)
        self.client.force_login(self.owner)

    def test_import_hashes_the_password_once_and_every_login_works(self):
        self.client.post(reverse('hr_employee_import'), {'step': 'preview', 'file': workbook(25)})
        with counting() as hashed:
            resp = self.client.post(reverse('hr_employee_import'), {'step': 'import', 'create_logins': 'on',
                                                                    'password1': PASSWORD, 'password2': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(hashed.call_count, 1)
        logins = User.objects.filter(role='EMPLOYEE', company=self.co)
        self.assertEqual((Employee.objects.filter(company=self.co).count(), logins.count()), (25, 25))
        self.assertTrue(all(check_password(PASSWORD, u.password) for u in logins))
        first = Employee.objects.get(employee_code='SPD000')
        self.assertEqual(first.user.email, 'spd0@example.com')
        self.assertTrue(first.user.is_active)
        self.assertTrue(self.client_class().login(username=first.user.username, password=PASSWORD))

    def test_bulk_logins_page_hashes_once_too(self):
        for i in range(12):
            make_employee(self.co, f'BLK{i:02d}', f'blk{i}@example.com')
        Employee.objects.filter(company=self.co).update(user=None)
        with counting() as hashed:
            self.client.post(reverse('hr_employee_logins'), {'action': 'bulk', 'bulk-password1': PASSWORD, 'bulk-password2': PASSWORD})
        self.assertEqual(hashed.call_count, 1)
        self.assertEqual(Employee.objects.filter(company=self.co, user__isnull=False).count(), 12)
        e = Employee.objects.filter(company=self.co).first()
        self.assertTrue(self.client_class().login(username=e.user.username, password=PASSWORD))

    def test_importing_the_same_file_again_is_refused_cleanly_not_a_crash(self):
        data = {'step': 'import', 'create_logins': 'on', 'password1': PASSWORD, 'password2': PASSWORD}
        self.client.post(reverse('hr_employee_import'), {'step': 'preview', 'file': workbook(5)})
        self.client.post(reverse('hr_employee_import'), data)
        resp = self.client.post(reverse('hr_employee_import'), {'step': 'preview', 'file': workbook(5)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context['ok']), 0)
        resp = self.client.post(reverse('hr_employee_import'), data, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Employee.objects.filter(company=self.co).count(), 5)


class ImportQueryCountTests(TestCase):
    """A remote database is slow per query, so a big import must use a handful of queries, not hundreds."""

    def setUp(self):
        self.co = make_company('Query Co')
        self.owner = make_user('queryowner', 'COMPANY_OWNER', self.co)
        self.client.force_login(self.owner)

    def test_forty_people_need_only_a_handful_of_queries(self):
        with CaptureQueriesContext(connection) as preview:
            self.client.post(reverse('hr_employee_import'), {'step': 'preview', 'file': workbook(40)})
        with CaptureQueriesContext(connection) as imported:
            self.client.post(reverse('hr_employee_import'), {'step': 'import', 'create_logins': 'on',
                                                             'password1': PASSWORD, 'password2': PASSWORD})
        self.assertLess(len(preview), 30)
        self.assertLess(len(imported), 40)
        self.assertEqual(Employee.objects.filter(company=self.co).count(), 40)

    def test_existing_email_or_code_is_a_clear_error_row_not_a_crash(self):
        other = make_company('Other Query Co')
        make_employee(other, 'ZZZ001', 'spd1@example.com')                 # same email as the 2nd person in the file
        make_employee(self.co, 'SPD002', 'someone-else@example.com')       # same Employee ID as the 3rd person
        resp = self.client.post(reverse('hr_employee_import'), {'step': 'preview', 'file': workbook(5)})
        self.assertEqual(resp.status_code, 200)
        problems = {r['code']: r['errors'] for r in resp.context['bad']}
        self.assertIn('SPD001', problems)
        self.assertIn('SPD002', problems)
        self.assertEqual(len(resp.context['ok']), 3)

    def test_login_names_never_clash_with_existing_ones(self):
        User.objects.create_user(f'emp{self.co.pk}-spd000', password='x')   # a login already uses that name
        self.client.post(reverse('hr_employee_import'), {'step': 'preview', 'file': workbook(3)})
        self.client.post(reverse('hr_employee_import'), {'step': 'import', 'create_logins': 'on',
                                                         'password1': PASSWORD, 'password2': PASSWORD})
        e = Employee.objects.get(company=self.co, employee_code='SPD000')
        self.assertEqual(e.user.username, f'emp{self.co.pk}-spd000-2')
        self.assertTrue(self.client_class().login(username=e.user.username, password=PASSWORD))
        self.assertEqual(User.objects.filter(role='EMPLOYEE', company=self.co).count(), 3)

    def test_import_without_logins_creates_employees_only(self):
        self.client.post(reverse('hr_employee_import'), {'step': 'preview', 'file': workbook(4)})
        self.client.post(reverse('hr_employee_import'), {'step': 'import'})
        self.assertEqual(Employee.objects.filter(company=self.co).count(), 4)
        self.assertFalse(User.objects.filter(role='EMPLOYEE', company=self.co).exists())
        self.assertFalse(Employee.objects.filter(company=self.co, user__isnull=False).exists())