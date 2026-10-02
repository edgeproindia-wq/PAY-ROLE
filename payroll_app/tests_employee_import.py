"""Bulk Employee Master import from Excel/CSV with preview, validation and logins."""
import io

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import Employee
from .tests_audit import company, employee, user

HEAD = 'Emp Code,Name,Email,Mobile,DOJ,Department,Designation,IFSC,Account No\n'


def csv_file(body, name='emps.csv'):
    return SimpleUploadedFile(name, (HEAD + body).encode(), content_type='text/csv')


class EmployeeImportTests(TestCase):
    def setUp(self):
        cache.clear()
        self.c1, self.c2 = company('Acme'), company('Beta')
        self.owner = user('own', 'COMPANY_OWNER', self.c1)
        self.client.force_login(self.owner)

    def preview(self, f):
        return self.client.post(reverse('hr_employee_import'), {'step': 'preview', 'file': f})

    def do_import(self, pwd='EPRO@2026', logins=True):
        data = {'step': 'import', 'password1': pwd, 'password2': pwd}
        if logins:
            data['create_logins'] = 'on'
        return self.client.post(reverse('hr_employee_import'), data)

    def test_preview_saves_nothing_then_import_creates_employees_and_logins(self):
        body = ('EPRO0119,Padma Shree,padma@ex.com,9876543210,03-06-2024,Detailing,Modeler,HDFC0001234,123456789012\n'
                'TR02,Ravi,ravi@ex.com,,2025-01-15,Training,Trainee,,\n')
        resp = self.preview(csv_file(body))
        self.assertContains(resp, '2 ready, 0 with errors')
        self.assertFalse(Employee.objects.exists())
        self.do_import()
        e = Employee.objects.get(employee_code='EPRO0119')
        self.assertEqual((e.company, e.first_name, e.last_name, str(e.date_of_joining)), (self.c1, 'Padma', 'Shree', '2024-06-03'))
        self.assertIsNotNone(e.user)
        self.client.logout()
        self.client.post(reverse('login'), {'login_as': 'employee', 'username': 'EPRO0119', 'password': 'EPRO@2026'})
        self.assertEqual(str(self.client.session.get('_auth_user_id')), str(e.user_id))

    def test_errors_are_reported_and_only_valid_rows_imported(self):
        employee(self.c1, 'EPRO003', 'old@ex.com')
        body = ('EPRO003,Dup,dup@ex.com,,2024-01-01,D,X,,\n'                 # exists already
                'EPRO0200,Bad Ifsc,bad@ex.com,,2024-01-01,D,X,HDFC123,\n'      # bad IFSC
                'EPRO0201,No Doj,nodoj@ex.com,,,D,X,,\n'                        # missing date
                'EPRO0202,Taken,old@ex.com,,2024-01-01,D,X,,\n'                 # email used
                'EPRO0203,Good One,good@ex.com,,2024-01-01,D,X,,\n')
        resp = self.preview(csv_file(body))
        self.assertContains(resp, '1 ready, 4 with errors')
        for msg in ('already exists', 'IFSC', 'Date of Joining', 'Email'):
            self.assertContains(resp, msg)
        self.do_import(logins=False)
        self.assertTrue(Employee.objects.filter(employee_code='EPRO0203', user__isnull=True).exists())
        self.assertFalse(Employee.objects.filter(employee_code__in=['EPRO0200', 'EPRO0201', 'EPRO0202']).exists())

    def test_xlsx_template_roundtrip(self):
        tpl = self.client.get(reverse('hr_employee_import_template'))
        self.assertEqual(tpl.status_code, 200)
        f = SimpleUploadedFile('t.xlsx', tpl.content)
        self.assertContains(self.preview(f), '1 ready, 0 with errors')

    def test_missing_columns_and_weak_password(self):
        bad = SimpleUploadedFile('x.csv', b'Name,Email\nA,a@ex.com\n')
        self.assertContains(self.preview(bad), 'Missing column')
        self.preview(csv_file('EPRO0300,A B,ab@ex.com,,2024-01-01,D,X,,\n'))
        self.do_import(pwd='123')
        self.assertFalse(Employee.objects.exists())

    def test_import_button_on_employee_master(self):
        self.assertContains(self.client.get(reverse('employee_master')), reverse('hr_employee_import'))

    def test_only_owner_and_company_scope(self):
        e = employee(self.c1, 'X1', 'x@ex.com', login='empx')
        self.client.force_login(e.user)
        self.assertEqual(self.client.get(reverse('hr_employee_import')).status_code, 403)
        owner2 = user('own2', 'COMPANY_OWNER', self.c2)
        self.client.force_login(owner2)
        self.preview(csv_file('X1,Same Code,other@ex.com,,2024-01-01,D,X,,\n'))   # same ID, other company: allowed
        self.do_import(logins=False)
        self.assertEqual(Employee.objects.get(company=self.c2, employee_code='X1').first_name, 'Same')