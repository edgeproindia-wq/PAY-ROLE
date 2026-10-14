"""Import: the optional 'Fill the gaps for me' switch, and the extra status words."""
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from .employee_import import STATUS, read_rows, validate_rows
from .models import Employee, User
from .tests import make_company, make_user

HEAD = ['Employee ID', 'First Name', 'Last Name', 'Email', 'Date of Joining', 'Department', 'Designation', 'Status']
ROWS = [
    ['G001', 'No', 'Mail', '', '2025-06-02', 'Detailing', 'Modeller', 'Working'],            # missing email
    ['G002', 'Bad', 'Mail', 'not an email', '2025-06-02', 'Detailing', 'Modeller', 'Working'],   # invalid email
    ['G003', 'No', 'Dept', 'g003@example.com', '2025-06-02', '', '', 'Working'],             # missing department and designation
    ['G004', 'Ab', 'Scond', 'g004@example.com', '2025-06-02', 'Detailing', 'Checker', 'ABSCOND'],
    ['G005', 'Real', 'One', 'same@example.com', '2025-06-02', 'Detailing', 'Checker', 'Working'],
    ['G006', 'Real', 'Two', 'same@example.com', '2025-06-02', 'Detailing', 'Checker', 'Working'],   # a REAL repeated email
]


def workbook():
    wb = Workbook()
    ws = wb.active
    ws.append(HEAD)
    for r in ROWS:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile('people.xlsx', buf.getvalue())


class GapTests(TestCase):
    def setUp(self):
        self.co = make_company('Gap Co')
        self.owner = make_user('gapowner', 'COMPANY_OWNER', self.co)
        self.client.force_login(self.owner)
        self.rows, _ = read_rows(workbook())

    def verdict(self, fill):
        return {r['code']: r['errors'] for r in validate_rows(self.rows, self.co, fill_gaps=fill)}

    def test_without_the_switch_nothing_changes_the_gaps_are_refused(self):
        v = self.verdict(False)
        for code in ('G001', 'G002', 'G003'):
            self.assertTrue(v[code], code)

    def test_with_the_switch_the_gaps_are_filled_but_real_problems_still_refuse(self):
        v = self.verdict(True)
        for code in ('G001', 'G002', 'G003', 'G004', 'G005'):
            self.assertEqual(v[code], [], code)
        self.assertTrue(any('repeated' in e for e in v['G006']))          # genuine duplicate email is never hidden

    def test_absconded_and_yet_to_join_are_understood(self):
        self.assertEqual((STATUS['abscond'], STATUS['absconded'], STATUS['yet to join']), ('TERMINATED', 'TERMINATED', 'ACTIVE'))

    def test_through_the_page_the_filled_rows_are_saved_with_logins(self):
        self.client.post(reverse('hr_employee_import'), {'step': 'preview', 'file': workbook(), 'fill_gaps': 'on'})
        self.client.post(reverse('hr_employee_import'), {'step': 'import', 'create_logins': 'on', 'password1': 'Strong-Pass#2026', 'password2': 'Strong-Pass#2026'})
        e1 = Employee.objects.get(employee_code='G001')
        self.assertEqual(e1.email, 'g001@no-email.invalid')
        e3 = Employee.objects.get(employee_code='G003')
        self.assertEqual((e3.department, e3.designation), ('Unassigned', 'Unassigned'))
        self.assertEqual(Employee.objects.get(employee_code='G004').employment_status, 'TERMINATED')
        self.assertEqual(Employee.objects.filter(company=self.co).count(), 5)           # G006 refused
        self.assertEqual(User.objects.filter(company=self.co, role='EMPLOYEE').count(), 5)