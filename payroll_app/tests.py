import datetime

from django.test import TestCase, Client
from django.urls import reverse

from .models import (
    Company, User, Employee, SalaryStructure, LeaveRequest, Reimbursement,
    DemoRequest, PayrollRun, PayrollRunLine,
)


def make_company(name='Acme Inc', status='APPROVED'):
    return Company.objects.create(name=name, contact_email=f'{name.lower()}@example.com', status=status)


def make_user(username, role, company=None, is_active=True, is_superuser=False):
    user = User.objects.create_user(username=username, password='StrongPass123', role=role, company=company, is_active=is_active)
    if is_superuser:
        user.is_superuser = True
        user.is_staff = True
        user.save()
    return user


def make_employee(company, code='EPRO001', email='e1@example.com'):
    return Employee.objects.create(
        employee_code=code, first_name='Test', last_name='Employee', email=email,
        date_of_joining=datetime.date(2024, 1, 1), department='Engineering', designation='Dev',
        company=company,
    )


class RegistrationAndDemoWorkflowTests(TestCase):
    def test_demo_request_public_submit(self):
        resp = self.client.post(reverse('request_demo'), {
            'full_name': 'Jane', 'company_name': 'Jane Co', 'email': 'jane@example.com',
            'phone': '', 'team_size': '10-50', 'message': 'Interested',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(DemoRequest.objects.count(), 1)
        self.assertEqual(DemoRequest.objects.first().status, 'PENDING')

    def test_company_self_registration_creates_inactive_owner(self):
        resp = self.client.post(reverse('company_register'), {
            'company_name': 'NewCo', 'contact_email': 'owner@newco.com', 'contact_phone': '',
            'address': '', 'username': 'newco_owner', 'password1': 'StrongPass123', 'password2': 'StrongPass123',
        })
        self.assertEqual(resp.status_code, 302)
        company = Company.objects.get(name='NewCo')
        self.assertEqual(company.status, 'PENDING_APPROVAL')
        owner = User.objects.get(username='newco_owner')
        self.assertFalse(owner.is_active, "Owner must not be able to log in before admin approval")
        self.assertEqual(owner.role, 'COMPANY_OWNER')

    def test_unapproved_owner_cannot_login(self):
        self.client.post(reverse('company_register'), {
            'company_name': 'PendingCo', 'contact_email': 'x@pendingco.com', 'contact_phone': '',
            'address': '', 'username': 'pending_owner', 'password1': 'StrongPass123', 'password2': 'StrongPass123',
        })
        logged_in = self.client.login(username='pending_owner', password='StrongPass123')
        self.assertFalse(logged_in, "Inactive (unapproved) users must not be able to authenticate")

    def test_admin_can_approve_company_and_activate_owner(self):
        self.client.post(reverse('company_register'), {
            'company_name': 'ApproveCo', 'contact_email': 'x@approveco.com', 'contact_phone': '',
            'address': '', 'username': 'approve_owner', 'password1': 'StrongPass123', 'password2': 'StrongPass123',
        })
        admin = make_user('platform_admin', 'ADMIN', is_superuser=True)
        self.client.login(username='platform_admin', password='StrongPass123')
        company = Company.objects.get(name='ApproveCo')
        resp = self.client.post(reverse('admin_company_decide', args=[company.pk]), {'decision': 'APPROVED'})
        self.assertEqual(resp.status_code, 302)
        company.refresh_from_db()
        self.assertEqual(company.status, 'APPROVED')
        owner = User.objects.get(username='approve_owner')
        owner.refresh_from_db()
        self.assertTrue(owner.is_active)

    def test_non_admin_cannot_approve_companies(self):
        company_a = make_company('CompanyA')
        owner_a = make_user('owner_a', 'COMPANY_OWNER', company=company_a)
        pending = make_company('PendingXYZ', status='PENDING_APPROVAL')
        self.client.login(username='owner_a', password='StrongPass123')
        resp = self.client.get(reverse('admin_company_approvals'))
        self.assertEqual(resp.status_code, 403, "A company owner must never reach the admin approval screen")


class RoleBasedAccessControlTests(TestCase):
    def setUp(self):
        self.company_a = make_company('CompanyA')
        self.company_b = make_company('CompanyB')
        self.admin = make_user('admin1', 'ADMIN', is_superuser=True)
        self.owner_a = make_user('owner_a', 'COMPANY_OWNER', company=self.company_a)
        self.owner_b = make_user('owner_b', 'COMPANY_OWNER', company=self.company_b)
        self.emp_a = make_employee(self.company_a, code='A001', email='a1@example.com')
        self.emp_b = make_employee(self.company_b, code='B001', email='b1@example.com')
        self.emp_a_user = make_user('emp_a_user', 'EMPLOYEE', company=self.company_a)
        self.emp_a.user = self.emp_a_user
        self.emp_a.save()

    def test_owner_cannot_see_other_companys_employees(self):
        self.client.login(username='owner_a', password='StrongPass123')
        resp = self.client.get(reverse('employee_master'))
        self.assertContains(resp, 'A001')
        self.assertNotContains(resp, 'B001')

    def test_owner_cannot_edit_other_companys_employee_by_url(self):
        self.client.login(username='owner_a', password='StrongPass123')
        resp = self.client.get(reverse('employee_edit', args=[self.emp_b.pk]))
        self.assertEqual(resp.status_code, 403, "IDOR: owner A must not access owner B's employee via direct URL")

    def test_owner_cannot_delete_other_companys_employee(self):
        self.client.login(username='owner_a', password='StrongPass123')
        resp = self.client.post(reverse('employee_delete', args=[self.emp_b.pk]))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Employee.objects.filter(pk=self.emp_b.pk).exists())

    def test_employee_cannot_access_employee_master(self):
        self.client.login(username='emp_a_user', password='StrongPass123')
        resp = self.client.get(reverse('employee_master'))
        self.assertEqual(resp.status_code, 403, "Employees must not reach the Employee Master admin screen")

    def test_employee_can_only_see_own_leave_requests(self):
        other_emp = make_employee(self.company_a, code='A002', email='a2@example.com')
        LeaveRequest.objects.create(employee=self.emp_a, leave_type='CASUAL', from_date='2026-01-01', to_date='2026-01-02')
        LeaveRequest.objects.create(employee=other_emp, leave_type='SICK', from_date='2026-02-01', to_date='2026-02-02')
        self.client.login(username='emp_a_user', password='StrongPass123')
        resp = self.client.get(reverse('leave_management'))
        self.assertEqual(len(resp.context['leave_requests']), 1, "An employee must only see their own leave requests, not a colleague's")
        self.assertEqual(resp.context['leave_requests'][0].employee_id, self.emp_a.pk)

    def test_employee_cannot_approve_own_leave(self):
        leave = LeaveRequest.objects.create(employee=self.emp_a, leave_type='CASUAL', from_date='2026-01-01', to_date='2026-01-02')
        self.client.login(username='emp_a_user', password='StrongPass123')
        resp = self.client.post(reverse('leave_decision', args=[leave.pk]), {'decision': 'APPROVED'})
        self.assertEqual(resp.status_code, 403, "Employees must never be able to approve their own leave")
        leave.refresh_from_db()
        self.assertEqual(leave.status, 'PENDING')

    def test_employee_filing_leave_is_forced_to_own_record_not_client_supplied(self):
        other_emp = make_employee(self.company_a, code='A003', email='a3@example.com')
        self.client.login(username='emp_a_user', password='StrongPass123')
        # The employee dropdown is already scoped to the caller's own record, so
        # submitting a colleague's pk must be rejected as an invalid choice —
        # this is the first line of defense against IDOR on this form.
        resp = self.client.post(reverse('leave_management'), {
            'employee': other_emp.pk, 'leave_type': 'CASUAL', 'from_date': '2026-03-01', 'to_date': '2026-03-02', 'reason': 'x',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(LeaveRequest.objects.filter(from_date='2026-03-01').exists(),
                          "A leave request for another employee's pk must never be saved")

        # Submitting the correct (own) employee pk succeeds normally.
        resp = self.client.post(reverse('leave_management'), {
            'employee': self.emp_a.pk, 'leave_type': 'CASUAL', 'from_date': '2026-04-01', 'to_date': '2026-04-02', 'reason': 'x',
        })
        self.assertEqual(resp.status_code, 302)
        leave = LeaveRequest.objects.get(from_date='2026-04-01')
        self.assertEqual(leave.employee_id, self.emp_a.pk)

    def test_owner_can_approve_own_companys_leave(self):
        leave = LeaveRequest.objects.create(employee=self.emp_a, leave_type='CASUAL', from_date='2026-01-01', to_date='2026-01-02')
        self.client.login(username='owner_a', password='StrongPass123')
        resp = self.client.post(reverse('leave_decision', args=[leave.pk]), {'decision': 'APPROVED'})
        self.assertEqual(resp.status_code, 302)
        leave.refresh_from_db()
        self.assertEqual(leave.status, 'APPROVED')

    def test_owner_cannot_approve_other_companys_leave(self):
        leave_b = LeaveRequest.objects.create(employee=self.emp_b, leave_type='CASUAL', from_date='2026-01-01', to_date='2026-01-02')
        self.client.login(username='owner_a', password='StrongPass123')
        resp = self.client.post(reverse('leave_decision', args=[leave_b.pk]), {'decision': 'APPROVED'})
        self.assertEqual(resp.status_code, 403)

    def test_admin_sees_all_companies_employees(self):
        self.client.login(username='admin1', password='StrongPass123')
        resp = self.client.get(reverse('employee_master'))
        self.assertContains(resp, 'A001')
        self.assertContains(resp, 'B001')

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('login'), resp.url)

    def test_admin_panel_forbidden_for_employee(self):
        self.client.login(username='emp_a_user', password='StrongPass123')
        resp = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(resp.status_code, 403)


class PayslipIsolationTests(TestCase):
    def setUp(self):
        self.company_a = make_company('CoA')
        self.company_b = make_company('CoB')
        self.emp_a = make_employee(self.company_a, code='CA1', email='ca1@example.com')
        self.emp_b = make_employee(self.company_b, code='CB1', email='cb1@example.com')
        self.emp_a_user = make_user('u_emp_a', 'EMPLOYEE', company=self.company_a)
        self.emp_a.user = self.emp_a_user
        self.emp_a.save()
        run = PayrollRun.objects.create(month='August 2026', status='RELEASED')
        PayrollRunLine.objects.create(payroll_run=run, employee=self.emp_a, basic=20000, gross_salary=25000, net_pay=23000)
        PayrollRunLine.objects.create(payroll_run=run, employee=self.emp_b, basic=30000, gross_salary=35000, net_pay=33000)

    def test_employee_only_sees_own_payslip(self):
        self.client.login(username='u_emp_a', password='StrongPass123')
        resp = self.client.get(reverse('payslips'))
        self.assertContains(resp, 'CA1')
        self.assertNotContains(resp, 'CB1')


class PayrollWorkflowTests(TestCase):
    def setUp(self):
        self.company = make_company('PayCo')
        self.owner = make_user('pay_owner', 'COMPANY_OWNER', company=self.company)
        self.emp = make_employee(self.company, code='P001', email='p1@example.com')
        SalaryStructure.objects.create(employee=self.emp, basic=20000, hra=8000, conveyance=2000, special_allowance=1000)

    def test_full_payroll_lifecycle(self):
        self.client.login(username='pay_owner', password='StrongPass123')
        resp = self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': 'September 2026'})
        self.assertEqual(resp.status_code, 302)
        run = PayrollRun.objects.get(month='September 2026')
        self.assertEqual(run.status, 'DRAFT')
        line = PayrollRunLine.objects.get(payroll_run=run, employee=self.emp)
        self.assertEqual(line.gross_salary, 31000)

        for action, expected_status in [('validate', 'VALIDATED'), ('approve', 'APPROVED'), ('release', 'RELEASED')]:
            self.client.post(reverse('payroll_combined'), {'action': action, 'run_id': run.pk})
            run.refresh_from_db()
            self.assertEqual(run.status, expected_status)

    def test_released_payroll_cannot_be_reprocessed(self):
        self.client.login(username='pay_owner', password='StrongPass123')
        run = PayrollRun.objects.create(month='October 2026', status='RELEASED')
        PayrollRunLine.objects.create(payroll_run=run, employee=self.emp, basic=20000, gross_salary=31000, net_pay=31000)
        self.client.post(reverse('payroll_combined'), {'action': 'reprocess', 'run_id': run.pk})
        self.assertEqual(PayrollRunLine.objects.filter(payroll_run=run).count(), 1, "Released runs must not be reprocessed")


class ReimbursementWorkflowTests(TestCase):
    def setUp(self):
        self.company = make_company('ReimCo')
        self.owner = make_user('reim_owner', 'COMPANY_OWNER', company=self.company)
        self.emp = make_employee(self.company, code='R001', email='r1@example.com')
        self.emp_user = make_user('reim_emp', 'EMPLOYEE', company=self.company)
        self.emp.user = self.emp_user
        self.emp.save()

    def test_employee_can_submit_and_owner_can_approve(self):
        self.client.login(username='reim_emp', password='StrongPass123')
        resp = self.client.post(reverse('reimbursement'), {
            'employee': self.emp.pk, 'category': 'TRAVEL', 'amount': '500', 'date': '2026-01-05', 'description': 'Taxi',
        })
        self.assertEqual(resp.status_code, 302)
        reimb = Reimbursement.objects.get(employee=self.emp)
        self.assertEqual(reimb.status, 'PENDING')

        self.client.logout()
        self.client.login(username='reim_owner', password='StrongPass123')
        resp = self.client.post(reverse('reimbursement_decision', args=[reimb.pk]), {'decision': 'APPROVED'})
        self.assertEqual(resp.status_code, 302)
        reimb.refresh_from_db()
        self.assertEqual(reimb.status, 'APPROVED')

    def test_negative_amount_rejected_by_form_validation(self):
        self.client.login(username='reim_emp', password='StrongPass123')
        resp = self.client.post(reverse('reimbursement'), {
            'employee': self.emp.pk, 'category': 'TRAVEL', 'amount': '-50', 'date': '2026-01-05', 'description': 'Bad',
        })
        self.assertEqual(resp.status_code, 200)  # re-rendered with form errors, not saved
        self.assertFalse(Reimbursement.objects.filter(amount=-50).exists())


class GenericPageWhitelistTests(TestCase):
    def test_arbitrary_template_path_returns_404_not_disclosure(self):
        company = make_company('SecCo')
        user = make_user('sec_owner', 'COMPANY_OWNER', company=company)
        self.client.login(username='sec_owner', password='StrongPass123')
        resp = self.client.get('/page/../../../etc/passwd/')
        self.assertIn(resp.status_code, (404, 400))
