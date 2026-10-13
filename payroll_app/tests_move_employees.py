"""Moving employees between companies: works for clean data, refuses anything unsafe, admin only."""
import datetime

from django.test import TestCase
from django.urls import reverse

from .models import Employee, Loan, PayrollRun, PayrollRunLine, User
from .tests import make_company, make_employee, make_user


class MoveEmployeesTests(TestCase):
    def setUp(self):
        self.old, self.new = make_company('Move Old Co'), make_company('Move New Co')
        self.new_owner = make_user('newowner', 'COMPANY_OWNER', self.new)
        self.emps = [make_employee(self.old, f'MV{i}', f'mv{i}@move.test') for i in range(3)]
        for i, e in enumerate(self.emps):
            Employee.objects.filter(pk=e.pk).update(user=make_user(f'mvlogin{i}', 'EMPLOYEE', self.old))
        self.admin = User.objects.create_superuser('moveadmin', 'moveadmin@move.test', 'Adm1n-Pass#2026')
        self.client.force_login(self.admin)

    def post(self, **extra):
        data = {'source': self.old.pk, 'target': self.new.pk}
        data.update(extra)
        return self.client.post(reverse('admin_move_employees'), data)

    def test_preview_then_confirm_moves_employees_and_logins(self):
        resp = self.post()
        self.assertEqual((resp.context['n_emp'], resp.context['n_login']), (3, 3))
        self.assertEqual(Employee.objects.filter(company=self.old).count(), 3)          # preview moves nothing
        self.post(confirm='1')
        self.assertEqual((Employee.objects.filter(company=self.new).count(), Employee.objects.filter(company=self.old).count()), (3, 0))
        self.assertEqual(User.objects.filter(company=self.new, role='EMPLOYEE').count(), 3)

    def test_the_new_company_owner_now_sees_the_names_in_the_dropdowns(self):
        self.post(confirm='1')
        self.client.force_login(self.new_owner)
        html = self.client.get(reverse('hr_loans')).content.decode()
        for i in range(3):
            self.assertIn(f'MV{i} - ', html)
        self.assertNotIn('No employees found for your company yet', html)

    def test_refuses_when_payroll_or_loans_are_already_attached(self):
        run = PayrollRun.objects.create(company=self.old, month='August 2026', status='RELEASED')
        PayrollRunLine.objects.create(payroll_run=run, employee=self.emps[0], basic=1, gross_salary=1, net_pay=1)
        Loan.objects.create(company=self.old, employee=self.emps[1], reference_no='L1', loan_amount=1000, emi_amount=500, tenure_months=2,
                            start_date=datetime.date.today())
        resp = self.post(confirm='1')
        self.assertTrue(any('PayrollRunLine' in p and 'Loan' in p for p in resp.context['problems']))
        self.assertEqual(Employee.objects.filter(company=self.old).count(), 3)

    def test_refuses_employee_id_clashes_and_same_company(self):
        make_employee(self.new, 'MV1', 'other@move.test')
        resp = self.post(confirm='1')
        self.assertTrue(any('already uses these Employee IDs' in p for p in resp.context['problems']))
        resp = self.client.post(reverse('admin_move_employees'), {'source': self.old.pk, 'target': self.old.pk, 'confirm': '1'})
        self.assertTrue(any('two different companies' in p for p in resp.context['problems']))
        self.assertEqual(Employee.objects.filter(company=self.old).count(), 3)

    def test_only_the_platform_admin_can_use_it(self):
        for user in (self.new_owner, User.objects.get(username='mvlogin0')):
            self.client.force_login(user)
            self.assertEqual(self.client.post(reverse('admin_move_employees'), {'source': self.old.pk, 'target': self.new.pk, 'confirm': '1'}).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('admin_move_employees')).status_code, 302)
        self.assertEqual(Employee.objects.filter(company=self.old).count(), 3)