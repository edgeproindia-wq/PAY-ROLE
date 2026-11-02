"""HRMS & External System Integration: file imports (preview, then confirm), connection pages and the import centre.

Every page is for company owners only. Nothing is saved until the owner confirms, and every query is scoped to the owner's company.
"""
import csv
import datetime
import io
import time

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .hrms_imports import SPECS, parse_date, parse_time
from .models import Attendance, AuditLog, Employee, LeaveRequest
from .tests import make_company, make_employee, make_user


def csv_file(rows, name='data.csv'):
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow(r)
    return SimpleUploadedFile(name, buf.getvalue().encode('utf-8'), content_type='text/csv')


def xlsx_file(rows, name='data.xlsx'):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile(name, buf.getvalue())


class HrmsBase(TestCase):
    def setUp(self):
        self.co = make_company('Hrms Co')
        self.other = make_company('Other Hrms Co')
        self.owner = make_user('hrms_owner', 'COMPANY_OWNER', self.co)
        self.owner.email = 'owner@hrms.test'
        self.owner.save()
        self.e1 = make_employee(self.co, 'E1', 'e1@h.test')
        self.e2 = make_employee(self.co, 'E2', 'e2@h.test')
        self.e1.department, self.e1.phone = 'Engineering', '9000000001'
        self.e1.save()
        self.client.force_login(self.owner)

    def preview(self, name, rows, upload=None, **opts):
        return self.client.post(reverse(name), dict({'step': 'upload', 'file': upload or csv_file(rows)}, **opts), follow=True)

    def confirm(self, name):
        return self.client.post(reverse(name), {'step': 'confirm'}, follow=True)

    def results(self, response):
        return {r['who'].split()[0] if r['who'] else '': r for r in response.context['preview']['rows']}


class ParsingTests(TestCase):
    def test_dates_in_every_common_form(self):
        d = datetime.date(2026, 10, 5)
        for v in ('2026-10-05', '05-10-2026', '05/10/2026', '05.10.2026', '05 Oct 2026', '05-Oct-2026', datetime.datetime(2026, 10, 5, 0, 0), d, '2026-10-05T00:00:00'):
            self.assertEqual(parse_date(v), d, v)
        serial = (d - datetime.date(1899, 12, 30)).days                      # an Excel date that was not formatted as a date
        self.assertEqual(parse_date(str(serial)), d)
        for bad in ('31/02/2026', 'yesterday', '2026-13-01'):
            with self.assertRaises(ValueError):
                parse_date(bad)
        self.assertIsNone(parse_date(''))
        self.assertIsNone(parse_date(None))

    def test_times_in_every_common_form(self):
        t = datetime.time(9, 30)
        for v in ('09:30', '9:30', '09:30:00', '9:30 AM', datetime.time(9, 30), datetime.datetime(2026, 1, 1, 9, 30), 0.3958333333333333, '0.3958333333333333'):
            self.assertEqual(parse_time(v), t, v)
        self.assertEqual(parse_time('6:05 PM'), datetime.time(18, 5))
        with self.assertRaises(ValueError):
            parse_time('late')
        self.assertIsNone(parse_time(''))


class CommonTests(HrmsBase):
    def test_every_import_page_opens_and_its_template_downloads(self):
        from openpyxl import load_workbook
        for sp in SPECS.values():
            with self.subTest(import_=sp.key):
                r = self.client.get(reverse(sp.name))
                self.assertEqual(r.status_code, 200)
                self.assertContains(r, sp.title)
                t = self.client.get(reverse(sp.name + '_template'))
                ws = load_workbook(io.BytesIO(t.content)).worksheets[0]
                self.assertEqual([c.value for c in ws[1]], [c[1] for c in sp.columns])
                self.assertEqual(len([c for c in ws[2] if c.value is not None]) > 0, True)

    def test_the_import_centre_lists_every_import(self):
        r = self.client.get(reverse('hrms_import_center'))
        for sp in SPECS.values():
            self.assertContains(r, sp.title)
            self.assertContains(r, reverse(sp.name))
        self.assertContains(r, 'no API with access keys')

    def test_a_preview_saves_nothing_and_discard_clears_it(self):
        r = self.preview('hrms_employee_sync', [['Employee ID', 'Department'], ['E1', 'Accounts']])
        self.assertEqual(r.context['preview']['ok'], 1)
        self.assertContains(r, 'Nothing is saved yet')
        self.e1.refresh_from_db()
        self.assertEqual(self.e1.department, 'Engineering')
        self.client.post(reverse('hrms_employee_sync'), {'step': 'discard'})
        self.assertIsNone(self.client.get(reverse('hrms_employee_sync')).context['preview'])

    def test_unreadable_files_are_refused_with_a_message(self):
        name = 'hrms_employee_sync'
        self.assertContains(self.preview(name, None, upload=SimpleUploadedFile('x.pdf', b'%PDF')), 'Upload a .csv or .xlsx file')
        self.assertContains(self.preview(name, None, upload=SimpleUploadedFile('x.xlsx', b'not a zip')), 'could not be read')
        self.assertContains(self.preview(name, [['Name', 'Value'], ['a', 'b']]), 'Could not find the column headings')
        self.assertContains(self.preview(name, [['Employee ID', 'Department']]), 'header but no rows')
        self.assertContains(self.client.post(reverse(name), {'step': 'upload'}, follow=True), 'Choose the file first')
        big = [['Employee ID', 'Department']] + [['E1', 'x']] * 2100
        self.assertContains(self.preview(name, big), 'more than 2000 rows')
        self.assertContains(self.preview(name, None, upload=SimpleUploadedFile('big.csv', b'a,b\n' + b'x' * (2 * 1024 * 1024))), 'larger than 2 MB')

    def test_confirm_without_a_preview_or_after_it_expired_saves_nothing(self):
        self.assertContains(self.confirm('hrms_employee_sync'), 'preview has expired')
        self.preview('hrms_employee_sync', [['Employee ID', 'Department'], ['E1', 'Accounts']])
        s = self.client.session
        s['hrms_import']['at'] = time.time() - 3600
        s.save()
        self.assertContains(self.confirm('hrms_employee_sync'), 'preview has expired')
        self.e1.refresh_from_db()
        self.assertEqual(self.e1.department, 'Engineering')

    def test_a_preview_made_for_one_import_cannot_be_confirmed_on_another(self):
        self.preview('hrms_employee_sync', [['Employee ID', 'Department'], ['E1', 'Accounts']])
        self.assertContains(self.confirm('hrms_attendance_import'), 'preview has expired')
        self.e1.refresh_from_db()
        self.assertEqual(self.e1.department, 'Engineering')

    def test_headers_are_matched_by_other_names_and_any_order(self):
        r = self.preview('hrms_employee_sync', [['Dept', 'Emp Code', 'Mobile No'], ['Accounts', 'e1', '98765 43210']])
        self.assertEqual(r.context['preview']['ok'], 1)
        self.confirm('hrms_employee_sync')
        self.e1.refresh_from_db()
        self.assertEqual((self.e1.department, self.e1.phone), ('Accounts', '9876543210'))

    def test_only_company_owners_can_use_the_pages(self):
        names = [sp.name for sp in SPECS.values()] + [sp.name + '_template' for sp in SPECS.values()] + ['hrms_biometric', 'hrms_accounting_sw', 'hrms_bank', 'hrms_email', 'hrms_import_center']
        self.client.logout()
        for n in names:
            self.assertEqual(self.client.get(reverse(n)).status_code, 302, n)
        self.client.force_login(make_user('hrms_emp', 'EMPLOYEE', self.co))
        for n in names:
            self.assertIn(self.client.get(reverse(n)).status_code, (302, 403), n)
        self.client.force_login(make_user('hrms_admin', 'ADMIN'))
        for n in names:
            self.assertEqual(self.client.get(reverse(n)).status_code, 403, n)
        self.assertEqual(self.client.post(reverse('hrms_employee_sync'), {'step': 'upload', 'file': csv_file([['Employee ID', 'Department'], ['E1', 'x']])}).status_code, 403)

    def test_another_companys_employees_are_never_matched_or_changed(self):
        stranger = make_employee(self.other, 'S1', 's1@h.test')
        stranger.department = 'Secret'
        stranger.save()
        r = self.preview('hrms_employee_sync', [['Employee ID', 'Department'], ['S1', 'Hacked']])
        self.assertEqual(r.context['preview']['ok'], 0)
        self.assertContains(r, 'No employee with this Employee ID in your company')
        self.assertNotContains(r, 'Secret')
        for name, rows in (('hrms_attendance_import', [['Employee ID', 'Date', 'Status'], ['S1', '2026-10-01', 'P']]),
                           ('hrms_leave_import', [['Employee ID', 'Leave Type', 'From Date', 'To Date'], ['S1', 'CL', '2026-10-01', '2026-10-02']]),
                           ('hrms_joining_exit', [['Employee ID', 'Status'], ['S1', 'RESIGNED']])):
            self.assertEqual(self.preview(name, rows).context['preview']['ok'], 0, name)
        stranger.refresh_from_db()
        self.assertEqual((stranger.department, stranger.employment_status), ('Secret', 'ACTIVE'))


class SyncTests(HrmsBase):
    def test_only_the_changed_fields_are_updated_and_blank_cells_leave_values_alone(self):
        rows = [['Employee ID', 'First Name', 'Last Name', 'Email', 'Phone', 'Gender', 'DOB', 'Department', 'Designation'],
                ['E1', '', '', 'new.e1@h.test', '+91 98765-43210', 'female', '12-04-1995', 'Accounts', ''],
                ['E2', '', '', '', '', '', '', '', '']]
        r = self.preview('hrms_employee_sync', rows)
        res = self.results(r)
        self.assertEqual((res['E1']['outcome'], res['E2']['outcome']), ('ok', 'skip'))
        self.assertIn('Department: Engineering -> Accounts', res['E1']['summary'])
        self.assertContains(self.confirm('hrms_employee_sync'), '1 employee(s) updated')
        e = Employee.objects.get(pk=self.e1.pk)
        self.assertEqual((e.email, e.phone, e.gender, e.date_of_birth, e.department, e.first_name), ('new.e1@h.test', '+919876543210', 'F', datetime.date(1995, 4, 12), 'Accounts', 'Test'))
        self.assertTrue(AuditLog.objects.filter(action='UPDATE', company=self.co, details__startswith='Employee Master Synchronisation').exists())

    def test_bad_values_are_errors_and_not_saved(self):
        rows = [['Employee ID', 'Email', 'Phone', 'Gender', 'DOB'], ['E1', 'not-an-email', '12', 'x', '2999-01-01'], ['E2', 'e1@h.test', '', '', 'yesterday'], ['NOPE', '', '', '', '']]
        r = self.preview('hrms_employee_sync', rows)
        self.assertEqual((r.context['preview']['ok'], r.context['preview']['error']), (0, 3))
        for text in ('valid address', 'Phone must be', 'Gender must be', 'must be in the past', 'already used by another employee', 'not a date'):
            self.assertContains(r, text)

    def test_an_email_used_in_another_company_or_twice_in_the_file_is_refused(self):
        make_employee(self.other, 'S1', 'taken@h.test')
        rows = [['Employee ID', 'Email'], ['E1', 'taken@h.test'], ['E1', 'dup@h.test'], ['E2', 'DUP@h.test']]
        res = self.preview('hrms_employee_sync', rows).context['preview']['rows']
        self.assertEqual([r['outcome'] for r in res], ['error', 'ok', 'error'])

    def test_bank_pan_and_aadhaar_columns_are_ignored(self):
        self.e1.bank_account_no, self.e1.pan_number = '111111111111', 'ABCDE1234F'
        self.e1.save()
        rows = [['Employee ID', 'Department', 'Bank Account No', 'IFSC', 'PAN', 'Aadhaar'], ['E1', 'Accounts', '999999999999', 'HDFC0009999', 'ZZZZZ9999Z', '123412341234']]
        self.preview('hrms_employee_sync', rows)
        self.confirm('hrms_employee_sync')
        e = Employee.objects.get(pk=self.e1.pk)
        self.assertEqual((e.department, e.bank_account_no, e.ifsc_code, e.pan_number, e.aadhar_number), ('Accounts', '111111111111', '', 'ABCDE1234F', ''))

    def test_data_that_changes_after_the_preview_is_checked_again_at_confirm(self):
        self.preview('hrms_employee_sync', [['Employee ID', 'Email'], ['E1', 'late@h.test']])
        make_employee(self.other, 'S9', 'late@h.test')                  # someone else takes the address after the preview
        r = self.confirm('hrms_employee_sync')
        self.assertContains(r, '0 employee(s) updated')
        self.assertContains(r, '1 row(s) were skipped because they changed after the preview')
        self.e1.refresh_from_db()
        self.assertEqual(self.e1.email, 'e1@h.test')

    def test_excel_files_work_too(self):
        r = self.preview('hrms_employee_sync', None, upload=xlsx_file([['Employee ID', 'Department'], ['E2', 'Stores']]))
        self.assertEqual(r.context['preview']['ok'], 1)


class JoiningExitTests(HrmsBase):
    def test_status_and_joining_date_updates_with_warnings(self):
        rows = [['Employee ID', 'Joining Date', 'Status'], ['E1', '2023-05-01', 'Resigned'], ['E2', '', 'on leave']]
        res = self.results(self.preview('hrms_joining_exit', rows))
        self.assertTrue(any('Full & Final' in w for w in res['E1']['warnings']))
        self.assertTrue(any('joining date' in w for w in res['E1']['warnings']))
        self.confirm('hrms_joining_exit')
        e1, e2 = Employee.objects.get(pk=self.e1.pk), Employee.objects.get(pk=self.e2.pk)
        self.assertEqual((e1.employment_status, e1.date_of_joining, e2.employment_status), ('RESIGNED', datetime.date(2023, 5, 1), 'ON_LEAVE'))

    def test_errors_and_no_change(self):
        rows = [['Employee ID', 'Joining Date', 'Status'], ['E1', '', ''], ['E2', '', 'gone'], ['E1', '1900-01-01', ''], ['E2', '', 'active']]
        res = self.preview('hrms_joining_exit', rows).context['preview']['rows']
        self.assertEqual([r['outcome'] for r in res], ['error', 'error', 'error', 'skip'])

    def test_reactivating_a_former_employee_is_flagged(self):
        Employee.objects.filter(pk=self.e1.pk).update(employment_status='RESIGNED')
        res = self.results(self.preview('hrms_joining_exit', [['Employee ID', 'Status'], ['E1', 'ACTIVE']]))
        self.assertTrue(any('Re-activating' in w for w in res['E1']['warnings']))


class AttendanceTests(HrmsBase):
    def test_new_rows_are_added_and_the_status_follows_the_check_in(self):
        rows = [['Employee ID', 'Date', 'Status', 'Check In', 'Check Out'], ['E1', '2026-10-01', 'P', '09:02', '18:31'], ['E2', '01-10-2026', '', '10:00', ''], ['E2', '2026-10-02', 'A', '', '']]
        r = self.preview('hrms_attendance_import', rows)
        self.assertEqual(r.context['preview']['ok'], 3)
        self.assertContains(self.confirm('hrms_attendance_import'), '3 attendance record(s) added, 0 updated')
        a = Attendance.objects.get(employee=self.e1)
        self.assertEqual((a.status, a.check_in, a.check_out), ('PRESENT', datetime.time(9, 2), datetime.time(18, 31)))
        self.assertEqual(Attendance.objects.get(employee=self.e2, date=datetime.date(2026, 10, 1)).status, 'PRESENT')
        self.assertEqual(Attendance.objects.get(employee=self.e2, date=datetime.date(2026, 10, 2)).status, 'ABSENT')

    def test_existing_days_are_left_alone_unless_overwrite_is_ticked(self):
        Attendance.objects.create(employee=self.e1, date=datetime.date(2026, 10, 1), status='ABSENT')
        rows = [['Employee ID', 'Date', 'Status'], ['E1', '2026-10-01', 'P']]
        r = self.preview('hrms_attendance_import', rows)
        self.assertEqual((r.context['preview']['ok'], r.context['preview']['skip']), (0, 1))
        self.assertContains(r, 'left unchanged')
        r = self.preview('hrms_attendance_import', rows, overwrite='1')
        self.assertEqual(r.context['preview']['ok'], 1)
        self.assertContains(self.confirm('hrms_attendance_import'), '0 attendance record(s) added, 1 updated')
        self.assertEqual(Attendance.objects.get(employee=self.e1).status, 'PRESENT')
        again = self.preview('hrms_attendance_import', rows, overwrite='1')
        self.assertEqual(again.context['preview']['skip'], 1)            # now identical: no change

    def test_refused_rows(self):
        future = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
        rows = [['Employee ID', 'Date', 'Status', 'Check In', 'Check Out'], ['E1', future, 'P', '', ''], ['E1', '2020-01-01', 'P', '', ''], ['E1', '2026-10-01', 'X', '', ''],
                ['E1', '2026-10-01', '', '', ''], ['E1', '2026-10-01', 'P', '18:00', '09:00'], ['E1', 'soon', 'P', '', ''], ['E1', '2026-10-01', 'P', 'late', ''],
                ['E2', '2026-10-01', 'P', '', ''], ['E2', '2026-10-01', 'A', '', '']]
        res = self.preview('hrms_attendance_import', rows).context['preview']['rows']
        self.assertEqual([r['outcome'] for r in res], ['error'] * 7 + ['ok', 'error'])
        text = ' '.join(e for r in res for e in r['errors'])
        for part in ('future date', 'before the joining date', 'Status must be', 'Give a status', 'after check-in', 'not a date', 'not a time', 'appear twice'):
            self.assertIn(part, text)

    def test_a_biometric_export_with_its_own_column_names_is_understood(self):
        rows = [['Emp Code', 'Punch Date', 'In Time', 'Out Time', 'Status'], ['E1', '02/10/2026', '09:15:00', '18:05:00', 'P'], ['E2', '02/10/2026', '', '', 'HD']]
        r = self.preview('hrms_attendance_import', rows)
        self.assertEqual(r.context['preview']['ok'], 2)
        self.confirm('hrms_attendance_import')
        self.assertEqual(Attendance.objects.get(employee=self.e2).status, 'HALF_DAY')

    def test_excel_date_and_time_cells(self):
        rows = [['Employee ID', 'Date', 'Check In', 'Check Out'], ['E1', datetime.datetime(2026, 10, 3), datetime.time(9, 0), datetime.time(17, 30)]]
        r = self.preview('hrms_attendance_import', None, upload=xlsx_file(rows))
        self.assertEqual(r.context['preview']['ok'], 1)
        self.confirm('hrms_attendance_import')
        a = Attendance.objects.get(employee=self.e1)
        self.assertEqual((a.date, a.check_in, a.check_out), (datetime.date(2026, 10, 3), datetime.time(9, 0), datetime.time(17, 30)))

    def test_a_present_day_inside_approved_leave_is_flagged_not_blocked(self):
        LeaveRequest.objects.create(employee=self.e1, leave_type='CASUAL', from_date=datetime.date(2026, 10, 1), to_date=datetime.date(2026, 10, 3), status='APPROVED')
        res = self.results(self.preview('hrms_attendance_import', [['Employee ID', 'Date', 'Status'], ['E1', '2026-10-02', 'P']]))
        self.assertEqual(res['E1']['outcome'], 'ok')
        self.assertTrue(any('approved leave' in w for w in res['E1']['warnings']))

    def test_a_record_added_by_someone_else_after_the_preview_is_not_overwritten(self):
        self.preview('hrms_attendance_import', [['Employee ID', 'Date', 'Status'], ['E1', '2026-10-01', 'P']])
        Attendance.objects.create(employee=self.e1, date=datetime.date(2026, 10, 1), status='ABSENT')
        r = self.confirm('hrms_attendance_import')
        self.assertContains(r, '0 attendance record(s) added, 0 updated')
        self.assertEqual(Attendance.objects.get(employee=self.e1).status, 'ABSENT')


class LeaveTests(HrmsBase):
    def test_approved_and_pending_rows_are_recorded_with_the_right_decider(self):
        rows = [['Employee ID', 'Leave Type', 'From Date', 'To Date', 'Reason', 'Status', 'Comment'],
                ['E1', 'CL', '2026-10-05', '2026-10-06', 'Family', '', 'ok by HR'], ['E2', 'Sick leave', '2026-10-07', '2026-10-07', '', 'Pending', ''], ['E2', 'LOP', '2026-10-20', '2026-10-21', '', 'Rejected', '']]
        r = self.preview('hrms_leave_import', rows)
        self.assertEqual(r.context['preview']['ok'], 3)
        self.assertContains(self.confirm('hrms_leave_import'), '3 leave record(s) added')
        a = LeaveRequest.objects.get(employee=self.e1)
        self.assertEqual((a.leave_type, a.status, a.decided_by, a.approver_comment), ('CASUAL', 'APPROVED', self.owner, 'Imported. ok by HR'))
        self.assertIsNotNone(a.decided_at)
        p = LeaveRequest.objects.get(employee=self.e2, status='PENDING')
        self.assertEqual((p.leave_type, p.decided_by, p.decided_at), ('SICK', None, None))
        self.assertEqual(LeaveRequest.objects.get(employee=self.e2, leave_type='UNPAID').status, 'REJECTED')

    def test_overlaps_are_refused_against_existing_leave_and_inside_the_file(self):
        LeaveRequest.objects.create(employee=self.e1, leave_type='CASUAL', from_date=datetime.date(2026, 10, 5), to_date=datetime.date(2026, 10, 6), status='PENDING')
        LeaveRequest.objects.create(employee=self.e2, leave_type='CASUAL', from_date=datetime.date(2026, 10, 5), to_date=datetime.date(2026, 10, 6), status='REJECTED')
        rows = [['Employee ID', 'Leave Type', 'From Date', 'To Date'], ['E1', 'CL', '2026-10-06', '2026-10-08'], ['E2', 'CL', '2026-10-05', '2026-10-06'],
                ['E2', 'CL', '2026-10-06', '2026-10-09'], ['E1', 'SL', '2026-11-01', '2026-11-02'], ['E1', 'SL', '2026-11-02', '2026-11-03']]
        res = self.preview('hrms_leave_import', rows).context['preview']['rows']
        self.assertEqual([r['outcome'] for r in res], ['error', 'ok', 'error', 'ok', 'error'])

    def test_a_leave_above_the_balance_is_flagged_but_can_be_imported(self):
        rows = [['Employee ID', 'Leave Type', 'From Date', 'To Date'], ['E1', 'CL', '2026-10-05', '2026-10-09']]      # 5 days against 2 remaining
        res = self.results(self.preview('hrms_leave_import', rows))
        self.assertEqual(res['E1']['outcome'], 'ok')
        self.assertTrue(any('More than the balance' in w for w in res['E1']['warnings']))
        self.confirm('hrms_leave_import')
        self.assertEqual(LeaveRequest.objects.count(), 1)

    def test_refused_rows(self):
        rows = [['Employee ID', 'Leave Type', 'From Date', 'To Date', 'Status'], ['E1', 'holiday', '2026-10-05', '2026-10-06', ''], ['E1', 'CL', '2026-10-09', '2026-10-05', ''],
                ['E1', 'CL', 'x', '2026-10-05', ''], ['E1', 'CL', '2026-10-05', '2026-10-06', 'maybe'], ['E1', 'CL', '2025-01-01', '2026-12-31', ''], ['E1', 'CL', '2026-10-05', '', '']]
        res = self.preview('hrms_leave_import', rows).context['preview']['rows']
        self.assertEqual([r['outcome'] for r in res], ['error'] * 6)
        text = ' '.join(e for r in res for e in r['errors'])
        for part in ('Leave type must be', 'earlier than from date', 'not a date', 'Status must be', 'longer than a year', 'dates are needed'):
            self.assertIn(part, text)


class ConnectionTests(HrmsBase):
    def test_the_connection_pages_say_plainly_there_is_no_live_link(self):
        for name, text in (('hrms_biometric', 'No live connection'), ('hrms_accounting_sw', 'No live connection'), ('hrms_bank', 'No live connection')):
            r = self.client.get(reverse(name))
            self.assertContains(r, text, msg_prefix=name)
            self.assertContains(r, 'What is not available', msg_prefix=name)
        self.assertContains(self.client.get(reverse('hrms_biometric')), reverse('hrms_attendance_import'))

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend', EMAIL_HOST='', EMAIL_HOST_USER='u', EMAIL_HOST_PASSWORD='SECRET-PASSWORD-XYZ')
    def test_the_email_page_shows_status_and_never_a_password(self):
        r = self.client.get(reverse('hrms_email'))
        self.assertContains(r, 'Not configured')
        self.assertContains(r, 'EMAIL_HOST is empty')
        self.assertNotContains(r, 'SECRET-PASSWORD-XYZ')
        self.assertContains(r, 'Password')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend')
    def test_a_backend_that_does_not_send_is_reported(self):
        self.assertContains(self.client.get(reverse('hrms_email')), 'Emails are not sent')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend', EMAIL_HOST='h', EMAIL_HOST_USER='u', EMAIL_HOST_PASSWORD='')
    def test_an_incomplete_login_is_reported(self):
        self.assertContains(self.client.get(reverse('hrms_email')), 'Incomplete')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_a_test_email_goes_to_the_owner_only_and_is_rate_limited(self):
        self.client.post(reverse('hrms_email'))
        self.assertEqual([m.to for m in mail.outbox], [['owner@hrms.test']])
        r = self.client.post(reverse('hrms_email'), follow=True)
        self.assertContains(r, 'wait a few seconds')
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(AuditLog.objects.filter(details='Test email sent').exists())

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_no_test_email_without_an_address(self):
        self.owner.email = ''
        self.owner.save()
        self.assertContains(self.client.post(reverse('hrms_email'), follow=True), 'no email address')
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend', EMAIL_HOST='127.0.0.1', EMAIL_PORT=1, EMAIL_TIMEOUT=2, EMAIL_HOST_USER='', EMAIL_HOST_PASSWORD='')
    def test_a_failed_test_email_shows_the_reason(self):
        r = self.client.post(reverse('hrms_email'), follow=True)
        self.assertContains(r, 'The test email failed')
        self.assertTrue(AuditLog.objects.filter(details__startswith='Test email failed').exists())
