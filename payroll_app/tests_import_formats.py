"""The company's own master list can be uploaded as it is (no conversion)."""
import datetime
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from .employee_import import read_rows, validate_rows
from .models import Company

HEAD = ['Emp Code', 'Name', 'Manager', 'Location', 'Dept', 'Team', 'Designation', 'Date of Joining',
        'Status', 'Last Working day Date', 'Official Mail ID', 'Remarks']


def xlsx(rows, header=HEAD):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile('master.xlsx', buf.getvalue())


class MasterListFormatTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(name='Fmt Co', contact_email='f@x.com', status='APPROVED')

    def test_master_list_headings_and_status_words_are_accepted(self):
        d = datetime.datetime(2025, 6, 2)
        rows, err = read_rows(xlsx([
            ['EPRO901', 'Anu Kumar', 'Mgr', 'Hubli', 'Tekla', 'Tekla', 'Modeller', d, 'Working', None, 'anu901@example.com', None],
            ['EPRO902', 'Ravi', 'Mgr', 'Hosur', 'Tekla', 'Tekla', 'Checker', d, 'In notice', None, 'ravi902@example.com', None],
        ]))
        self.assertEqual(err, '')
        rep = validate_rows(rows, self.co)
        self.assertEqual([r['errors'] for r in rep], [[], []])
        self.assertEqual([r['data']['employment_status'] for r in rep], ['ACTIVE', 'ACTIVE'])
        self.assertEqual((rep[0]['data']['first_name'], rep[0]['data']['last_name']), ('Anu', 'Kumar'))
        self.assertEqual(rep[0]['data']['email'], 'anu901@example.com')

    def test_bad_rows_are_reported_not_imported(self):
        d = datetime.datetime(2025, 6, 2)
        rows, err = read_rows(xlsx([
            ['EPRO911', 'No Mail', 'M', 'Hubli', 'Tekla', 'Tekla', 'Modeller', d, 'Working', None, None, None],
            ['EPRO912', 'Ab Sconder', 'M', 'Hubli', 'Tekla', 'Tekla', 'Modeller', d, 'Abscond', None, 'ab912@example.com', None],
            ['EPRO913', 'Good One', 'M', 'Hubli', 'Tekla', 'Tekla', 'Modeller', d, 'Working', None, 'good913@example.com', None],
        ]))
        self.assertEqual(err, '')
        rep = validate_rows(rows, self.co)
        self.assertTrue(rep[0]['errors'])      # email missing
        self.assertTrue(rep[1]['errors'])      # Abscond is not a status this system has
        self.assertFalse(rep[2]['errors'])

    def test_the_standard_template_still_works(self):
        head = ['Employee ID', 'First Name', 'Last Name', 'Email', 'Date of Joining', 'Department', 'Designation', 'Status']
        rows, err = read_rows(xlsx([['EPRO921', 'Anu', 'Kumar', 'anu921@example.com', '2024-06-03', 'Detailing', 'Modeler', 'ACTIVE']], head))
        self.assertEqual(err, '')
        self.assertEqual(validate_rows(rows, self.co)[0]['errors'], [])

    def test_a_file_without_any_email_column_still_gets_a_clear_message(self):
        _, err = read_rows(xlsx([['EPRO931', 'A B']], ['Emp Code', 'Name']))
        self.assertIn('Missing column(s)', err)