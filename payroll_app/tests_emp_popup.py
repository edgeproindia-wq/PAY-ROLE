"""Employee import inside a popup: check and confirm as JSON, links that open it, and the old page still working."""
import io
import json
import time
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from openpyxl import Workbook

from .models import AuditLog, Employee, User
from .tests import make_company, make_employee, make_user

HEAD = ['Employee ID', 'First Name', 'Last Name', 'Email', 'Date of Joining', 'Department', 'Designation', 'Status']
PW = 'Strong-Pass#2026'


def sheet(rows):
    wb = Workbook()
    ws = wb.active
    ws.append(HEAD)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile('people.xlsx', buf.getvalue())


def person(code, **over):
    row = {'code': code, 'first': 'First' + code, 'last': 'Last', 'email': f'{code.lower()}@pop.test', 'doj': '2025-06-02', 'dept': 'Detailing', 'desig': 'Modeller', 'status': 'Working'}
    row.update(over)
    return [row['code'], row['first'], row['last'], row['email'], row['doj'], row['dept'], row['desig'], row['status']]


class World(TestCase):
    def setUp(self):
        self.co, self.other = make_company('Pop Co'), make_company('Pop Other')
        self.owner = make_user('popowner', 'COMPANY_OWNER', self.co)
        self.client.force_login(self.owner)

    def check(self, rows, **extra):
        resp = self.client.post(reverse('emp_popup_check'), dict({'file': sheet(rows)}, **extra))
        return resp, json.loads(resp.content)

    def confirm(self, token, logins=True, p1=PW, p2=PW):
        data = {'token': token, 'password1': p1, 'password2': p2}
        if logins:
            data['create_logins'] = 'on'
        resp = self.client.post(reverse('emp_popup_confirm'), data)
        return resp, json.loads(resp.content)


class CheckTests(World):
    def test_a_check_reports_ready_and_problem_rows_and_saves_nothing(self):
        resp, d = self.check([person('P001'), person('P002', email=''), person('P003')])
        self.assertEqual((resp.status_code, d['ok'], d['total'], d['ready'], d['errors']), (200, True, 3, 2, 1))
        self.assertFalse(d['rows'][0]['ok'])                                   # problems are listed first
        self.assertIn('Email', d['rows'][0]['text'])
        self.assertEqual(Employee.objects.count(), 0)

    def test_the_fill_the_gaps_switch_works_inside_the_popup(self):
        rows = [person('P001', email=''), person('P002', dept='')]
        self.assertEqual(self.check(rows)[1]['ready'], 0)
        self.assertEqual(self.check(rows, fill_gaps='on')[1]['ready'], 2)

    def test_people_who_already_exist_are_refused(self):
        make_employee(self.co, 'P001', 'p001@pop.test')
        _, d = self.check([person('P001'), person('P004')])
        self.assertEqual((d['ready'], d['errors']), (1, 1))

    def test_bad_files_get_a_plain_message(self):
        resp = self.client.post(reverse('emp_popup_check'), {'file': SimpleUploadedFile('x.exe', b'MZ')})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(json.loads(resp.content)['ok'])
        self.assertEqual(self.client.post(reverse('emp_popup_check'), {}).status_code, 400)


class ConfirmTests(World):
    def test_confirm_saves_employees_and_working_logins(self):
        _, d = self.check([person('P001'), person('P002')])
        resp, out = self.confirm(d['token'])
        self.assertEqual((out['ok'], out['created'], out['logins'], out['skipped']), (True, 2, 2, 0))
        e = Employee.objects.get(employee_code='P001')
        self.assertEqual((e.company, e.department, e.user.role), (self.co, 'Detailing', 'EMPLOYEE'))
        self.assertTrue(self.client.login(username=e.user.username, password=PW))
        self.assertTrue(AuditLog.objects.filter(details__icontains='Employee import: 2 employees, 2 logins').exists())

    def test_without_logins_no_users_are_made(self):
        _, d = self.check([person('P001')])
        before = User.objects.count()
        _, out = self.confirm(d['token'], logins=False, p1='', p2='')
        self.assertEqual((out['created'], out['logins']), (1, 0))
        self.assertEqual(User.objects.count(), before)

    def test_a_wrong_password_is_refused_and_the_check_stays_valid_for_a_retry(self):
        _, d = self.check([person('P001')])
        resp, out = self.confirm(d['token'], p2='different')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('do not match', out['error'])
        self.assertEqual(Employee.objects.count(), 0)
        resp, out = self.confirm(d['token'], p1='123', p2='123')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.confirm(d['token'])[1]['created'], 1)           # the same check still works

    def test_a_check_is_single_use_and_tokens_must_match_and_expire(self):
        _, d = self.check([person('P001')])
        self.assertEqual(self.confirm('wrong')[0].status_code, 400)
        self.assertEqual(self.confirm(d['token'])[1]['created'], 1)
        self.assertEqual(self.confirm(d['token'])[0].status_code, 400)
        self.assertEqual(Employee.objects.count(), 1)
        _, d2 = self.check([person('P002')])
        with mock.patch('payroll_app.employee_import_popup.time.time', return_value=time.time() + 31 * 60):
            resp, out = self.confirm(d2['token'])
        self.assertEqual(resp.status_code, 400)
        self.assertIn('expired', out['error'])

    def test_a_clash_at_the_last_moment_saves_nothing(self):
        _, d = self.check([person('P001'), person('P002')])
        with mock.patch('payroll_app.employee_import_popup.Employee.objects.bulk_create', side_effect=IntegrityError('dup')):
            resp, out = self.confirm(d['token'])
        self.assertEqual(resp.status_code, 409)
        self.assertEqual((Employee.objects.count(), User.objects.filter(role='EMPLOYEE').count()), (0, 0))

    def test_ninety_people_need_only_a_few_queries(self):
        _, d = self.check([person(f'Q{i:03d}') for i in range(90)])
        with CaptureQueriesContext(connection) as q:
            _, out = self.confirm(d['token'])
        self.assertEqual(out['created'], 90)
        self.assertLess(len(q), 40)


class AccessTests(World):
    def test_only_owners_can_use_it(self):
        self.client.logout()
        for name in ('emp_popup_check', 'emp_popup_confirm'):
            self.assertEqual(self.client.post(reverse(name), {}).status_code, 302, name)
        for role, company in (('EMPLOYEE', self.co), ('ADMIN', None)):
            self.client.force_login(make_user(f'pop_{role}', role, company))
            self.assertEqual(self.client.post(reverse('emp_popup_check'), {'file': sheet([person('Z1')])}).status_code, 403, role)
            self.assertEqual(self.client.post(reverse('emp_popup_confirm'), {'token': 'x'}).status_code, 403, role)
        self.assertEqual(Employee.objects.count(), 0)

    def test_another_owner_cannot_use_my_check_and_imports_only_into_their_own_company(self):
        _, d = self.check([person('P001')])
        self.client.force_login(make_user('popowner2', 'COMPANY_OWNER', self.other))
        self.assertEqual(self.confirm(d['token'])[0].status_code, 400)
        _, d2 = self.check([person('P001')])
        self.assertEqual(self.confirm(d2['token'])[1]['created'], 1)
        self.assertEqual(Employee.objects.get(employee_code='P001').company, self.other)


class PageTests(World):
    def test_owner_pages_carry_the_popup_and_the_links_open_it(self):
        for name in ('salary_structure', 'employee_master', 'attendance'):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn('id="empImportModal"', html, name)
        sal = self.client.get(reverse('salary_structure')).content.decode()
        self.assertIn("onclick=\"if(window.openEmpImport){openEmpImport();return false;}\"", sal)       # the yellow message link
        master = self.client.get(reverse('employee_master')).content.decode()
        self.assertIn('openEmpImport()', master)                                                         # the Import Excel button

    def test_employees_do_not_get_the_popup_and_the_old_page_still_works(self):
        resp = self.client.get(reverse('hr_employee_import'))
        self.assertEqual(resp.status_code, 200)
        self.client.force_login(make_user('popemp', 'EMPLOYEE', self.co))
        self.assertNotIn('empImportModal', self.client.get(reverse('dashboard')).content.decode())