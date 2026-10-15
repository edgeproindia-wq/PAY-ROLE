"""Mark Attendance popup: a search box for the Employee choice, with the save logic unchanged."""
import datetime

from django.test import TestCase
from django.urls import reverse

from .models import Attendance, Employee
from .tests import make_company, make_employee, make_user


class AttendanceSearchTests(TestCase):
    def setUp(self):
        self.co = make_company('Att Search Co')
        self.owner = make_user('attsearchowner', 'COMPANY_OWNER', self.co)
        self.emps = [make_employee(self.co, f'A{i:03d}', f'a{i}@att.test') for i in range(4)]
        self.client.force_login(self.owner)
        self.url = reverse('attendance')

    def test_the_popup_carries_the_search_box_and_keeps_the_plain_dropdown_as_a_fallback(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('emp-search', html)
        self.assertIn('Search by name or ID', html)
        self.assertIn('<select name="employee"', html)
        self.assertIn("['addAttModal']", html)

    def test_saving_attendance_still_works_exactly_as_before(self):
        resp = self.client.post(self.url, {'employee': self.emps[1].pk, 'date': '2026-09-15', 'status': 'PRESENT', 'check_in': '', 'check_out': '', 'remarks': 'ok'})
        self.assertEqual(resp.status_code, 302)
        rec = Attendance.objects.get()
        self.assertEqual((rec.employee, rec.date, rec.status), (self.emps[1], datetime.date(2026, 9, 15), 'PRESENT'))

    def test_a_post_without_an_employee_is_still_refused(self):
        resp = self.client.post(self.url, {'date': '2026-09-15', 'status': 'PRESENT'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Attendance.objects.count(), 0)

    def test_other_pages_are_not_changed(self):
        for name in ('leave_management', 'salary_structure', 'reimbursement'):
            self.assertNotIn('emp-search', self.client.get(reverse(name)).content.decode(), name)

    def test_an_empty_company_keeps_its_own_message(self):
        self.client.force_login(make_user('attsearchowner2', 'COMPANY_OWNER', make_company('Empty Att Co')))
        self.assertContains(self.client.get(self.url), 'No employees found for your company yet')

    def test_an_employee_login_can_still_open_the_page(self):
        emp_user = make_user('attsearchemp', 'EMPLOYEE', self.co)
        Employee.objects.filter(pk=self.emps[0].pk).update(user=emp_user)
        self.client.force_login(emp_user)
        self.assertEqual(self.client.get(self.url).status_code, 200)