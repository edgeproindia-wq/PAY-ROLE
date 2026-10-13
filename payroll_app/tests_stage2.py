"""Stage 2: bulk attendance and payroll preview."""
import datetime

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import Attendance, Employee, LeaveRequest, PayrollRun, PayrollRunLine, SalaryStructure
from .tests import make_company, make_employee, make_user

D = datetime.date
DAY = D(2026, 9, 15)


def staff(company, code, joined=D(2025, 1, 1), salary=True):
    e = make_employee(company, code, f'{code.lower()}@stage2.test')
    Employee.objects.filter(pk=e.pk).update(date_of_joining=joined)
    e.refresh_from_db()
    if salary:
        SalaryStructure.objects.create(employee=e, basic=30000, hra=10000, conveyance=0, special_allowance=0)
    return e


class BulkAttendanceTests(TestCase):
    def setUp(self):
        self.co, self.other = make_company('Bulk Co'), make_company('Bulk Other')
        self.owner = make_user('bulkowner', 'COMPANY_OWNER', self.co)
        self.e1, self.e2, self.e3 = staff(self.co, 'B1'), staff(self.co, 'B2'), staff(self.co, 'B3')
        self.future = staff(self.co, 'BF', joined=D(2026, 12, 1))
        self.stranger = staff(self.other, 'BX')
        LeaveRequest.objects.create(employee=self.e3, leave_type='CASUAL', status='APPROVED', from_date=D(2026, 9, 14), to_date=D(2026, 9, 16))
        self.client.force_login(self.owner)
        self.url = reverse('attendance_bulk')

    def post(self, **data):
        data.setdefault('date', DAY.isoformat())
        return self.client.post(self.url, data)

    def status(self, emp):
        rec = Attendance.objects.filter(employee=emp, date=DAY).first()
        return rec.status if rec else None

    def test_the_page_lists_only_eligible_employees_of_my_company(self):
        resp = self.client.get(self.url + f'?date={DAY.isoformat()}')
        html = resp.content.decode()
        self.assertEqual(resp.context['total'], 3)
        for code in ('B1', 'B2', 'B3'):
            self.assertIn(f'{code} - ', html)
        self.assertNotIn('BF - ', html)            # joins later
        self.assertNotIn('BX - ', html)            # another company

    def test_mark_all_present_keeps_existing_records_uses_approved_leave_and_can_be_repeated(self):
        Attendance.objects.create(employee=self.e1, date=DAY, status='ABSENT')
        self.post(action='present_all')
        self.assertEqual([self.status(self.e1), self.status(self.e2), self.status(self.e3)], ['ABSENT', 'PRESENT', 'LEAVE'])
        self.assertIsNone(self.status(self.future))
        self.assertIsNone(self.status(self.stranger))
        before = Attendance.objects.count()
        self.post(action='present_all')
        self.assertEqual(Attendance.objects.count(), before)

    def test_saving_rows_changes_only_valid_rows_of_my_company(self):
        self.post(action='save_rows', emp=[self.e1.pk, self.e2.pk, self.stranger.pk],
                  **{f'status_{self.e1.pk}': 'HALF_DAY', f'status_{self.e2.pk}': 'BOGUS', f'status_{self.stranger.pk}': 'ABSENT',
                     f'remarks_{self.e1.pk}': 'doctor'})
        self.assertEqual(self.status(self.e1), 'HALF_DAY')
        self.assertEqual(Attendance.objects.get(employee=self.e1, date=DAY).remarks, 'doctor')
        self.assertIsNone(self.status(self.e2))
        self.assertIsNone(self.status(self.stranger))
        self.post(action='save_rows', emp=[self.e1.pk], **{f'status_{self.e1.pk}': 'PRESENT'})          # an existing record can be corrected
        self.assertEqual(self.status(self.e1), 'PRESENT')

    def test_marking_sixty_employees_needs_only_a_handful_of_queries(self):
        for i in range(60):
            staff(self.co, f'BM{i:02d}', salary=False)
        with CaptureQueriesContext(connection) as q:
            self.post(action='present_all')
        self.assertLess(len(q), 20)
        self.assertEqual(Attendance.objects.filter(date=DAY, status='PRESENT').count(), 62)      # 60 + B1 + B2 (B3 is on leave)

    def test_employees_cannot_open_the_page(self):
        emp_user = make_user('bulkemp', 'EMPLOYEE', self.co)
        Employee.objects.filter(pk=self.e1.pk).update(user=emp_user)
        self.client.force_login(emp_user)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.post(action='present_all').status_code, 403)


class PayrollPreviewTests(TestCase):
    MONTH = 'September 2026'

    def setUp(self):
        self.co, self.other = make_company('Preview Co'), make_company('Preview Other')
        self.owner = make_user('previewowner', 'COMPANY_OWNER', self.co)
        self.a, self.b = staff(self.co, 'P1'), staff(self.co, 'P2')
        self.nosalary = staff(self.co, 'P3', salary=False)
        self.future = staff(self.co, 'PF', joined=D(2026, 12, 1))
        self.stranger = staff(self.other, 'PX')
        for day in (D(2026, 9, 10), D(2026, 9, 11)):
            Attendance.objects.create(employee=self.a, date=day, status='ABSENT')           # two days of loss of pay for P1
        self.client.force_login(self.owner)
        self.url = reverse('payroll_preview')

    def preview(self):
        return self.client.get(self.url, {'month': self.MONTH})

    def test_it_shows_who_is_ready_and_who_is_skipped_and_saves_nothing(self):
        resp = self.preview()
        codes = [l.employee.employee_code for l in resp.context['lines']]
        self.assertEqual(codes, ['P1', 'P2'])
        self.assertEqual([e.employee_code for e in resp.context['skipped']], ['P3'])
        self.assertEqual((PayrollRun.objects.count(), PayrollRunLine.objects.count()), (0, 0))

    def test_loss_of_pay_from_attendance_is_visible_in_the_preview(self):
        lines = {l.employee.employee_code: l for l in self.preview().context['lines']}
        self.assertEqual(float(lines['P1'].lop_days), 2.0)
        self.assertLess(lines['P1'].net_pay, lines['P2'].net_pay)

    def test_the_real_payroll_gives_exactly_the_previewed_amounts(self):
        previewed = {l.employee.employee_code: (l.gross_salary, l.total_deductions, l.net_pay, l.lop_days) for l in self.preview().context['lines']}
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': self.MONTH})
        real = {l.employee.employee_code: (l.gross_salary, l.total_deductions, l.net_pay, l.lop_days)
                for l in PayrollRunLine.objects.filter(payroll_run__company=self.co).select_related('employee')}
        self.assertEqual(previewed, real)
        self.assertEqual(len(real), 2)

    def test_an_existing_month_points_to_the_run_instead_of_previewing(self):
        PayrollRun.objects.create(company=self.co, month=self.MONTH, status='APPROVED')
        resp = self.preview()
        self.assertIsNotNone(resp.context['existing'])
        self.assertIsNone(resp.context['lines'])

    def test_a_made_up_month_is_refused_and_employees_cannot_open_it(self):
        resp = self.client.get(self.url, {'month': 'next tuesday'})
        self.assertTrue(resp.context['error'])
        self.assertIsNone(resp.context['lines'])
        emp_user = make_user('previewemp', 'EMPLOYEE', self.co)
        Employee.objects.filter(pk=self.a.pk).update(user=emp_user)
        self.client.force_login(emp_user)
        self.assertEqual(self.preview().status_code, 403)
