"""End-to-end audit tests (success AND failure paths) for the workflows in the
production-readiness brief. Runs on Django's throw-away test database, never
on db.sqlite3."""
import datetime
import re
from decimal import Decimal
from io import StringIO

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    ArrearsRecord, Attendance, AuditLog, BankPayment, Company, DemoRequest, EmailOTP, Employee,
    LeaveRequest, Notification, PayrollRun, PayrollRunLine, Reimbursement, SalaryStructure, User,
)

PWD = 'StrongPass123'


def company(name='Acme', status='APPROVED'):
    return Company.objects.create(name=name, contact_email=f'{name.lower()}@ex.com', contact_phone='9876543210',
                                  status=status)


def user(username, role, comp=None, **extra):
    return User.objects.create_user(username=username, password=PWD, role=role, company=comp,
                                    email=extra.pop('email', f'{username}@ex.com'), **extra)


def employee(comp, code, email, bank=True, login=None):
    e = Employee.objects.create(
        company=comp, employee_code=code, first_name=code, last_name='Emp', email=email,
        date_of_joining=datetime.date(2024, 1, 1), department='Eng', designation='Dev',
        bank_account_no='123456789012' if bank else '', ifsc_code='HDFC0001234' if bank else '',
        bank_name='HDFC' if bank else '',
    )
    if login:
        e.user = user(login, 'EMPLOYEE', comp, email=email)
        e.save()
    return e


def register_payload(**over):
    data = {
        'company_name': 'RegCo', 'owner_full_name': 'Reg Owner', 'contact_email': 'Owner@RegCo.com',
        'contact_phone': '+91 98765 43210', 'address': 'Hosur', 'username': 'regowner',
        'password1': PWD, 'password2': PWD,
    }
    data.update(over)
    return data


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class RegistrationOtpApprovalLoginTests(TestCase):
    def _otp_from_mail(self):
        body = mail.outbox[-1].body
        return re.search(r'\b(\d{6})\b', body).group(1)

    def test_full_flow_register_verify_approve_login_by_email(self):
        resp = self.client.post(reverse('company_register'), register_payload())
        self.assertRedirects(resp, reverse('verify_email'))
        owner = User.objects.get(username='regowner')
        self.assertEqual(owner.email, 'owner@regco.com', 'email must be normalised to lowercase')
        self.assertFalse(owner.is_active)
        self.assertFalse(owner.email_verified)
        self.assertTrue(owner.password.startswith(('pbkdf2_', 'argon2', 'bcrypt')), 'password must be hashed')
        self.assertEqual(owner.company.contact_phone, '9876543210')
        otp_row = EmailOTP.objects.get(user=owner)
        self.assertNotIn(self._otp_from_mail(), otp_row.code_hash, 'OTP must be stored hashed')

        # wrong code
        self.client.post(reverse('verify_email'), {'code': '000000' if self._otp_from_mail() != '000000' else '111111'})
        otp_row.refresh_from_db()
        self.assertEqual(otp_row.attempts, 1)
        # correct code
        resp = self.client.post(reverse('verify_email'), {'code': self._otp_from_mail()})
        self.assertRedirects(resp, reverse('login'))
        owner.refresh_from_db()
        self.assertTrue(owner.email_verified)

        # still pending approval -> login refused with a clear message
        resp = self.client.post(reverse('login'), {'username': 'owner@regco.com', 'password': PWD})
        self.assertContains(resp, 'pending admin approval')

        admin = User.objects.create_superuser('boss', 'boss@ex.com', PWD)
        self.client.force_login(admin)
        self.client.post(reverse('admin_company_decide', args=[owner.company.pk]), {'decision': 'APPROVED'})
        self.client.post(reverse('logout'))

        resp = self.client.post(reverse('login'), {'username': 'OWNER@REGCO.COM', 'password': PWD})
        self.assertRedirects(resp, reverse('post_login_redirect'), fetch_redirect_response=False)
        resp = self.client.get(reverse('post_login_redirect'))
        self.assertRedirects(resp, reverse('dashboard'))

    def test_expired_otp_rejected(self):
        self.client.post(reverse('company_register'), register_payload())
        owner = User.objects.get(username='regowner')
        EmailOTP.objects.filter(user=owner).update(expires_at=timezone.now() - datetime.timedelta(minutes=1))
        code = self._otp_from_mail()
        resp = self.client.post(reverse('verify_email'), {'code': code})
        self.assertContains(resp, 'expired')
        owner.refresh_from_db()
        self.assertFalse(owner.email_verified)

    def test_duplicate_email_and_username_rejected(self):
        for i in range(20):
            user(f'taken{i}' if i else 'taken', 'EMPLOYEE', email='dup@ex.com')
        resp = self.client.post(reverse('company_register'), register_payload(contact_email='DUP@ex.com'))
        self.assertContains(resp, 'already exists')
        resp = self.client.post(reverse('company_register'), register_payload(username='TAKEN', contact_email='new@ex.com'))
        self.assertContains(resp, 'already taken')
        self.assertFalse(Company.objects.filter(name='RegCo').exists())

    def test_password_mismatch_and_weak_password(self):
        resp = self.client.post(reverse('company_register'), register_payload(password2='Different123'))
        self.assertContains(resp, 'Passwords do not match')
        resp = self.client.post(reverse('company_register'), register_payload(password1='12345678', password2='12345678'))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username='regowner').exists())

    def test_admin_approval_blocked_for_unverified_email_without_override(self):
        self.client.post(reverse('company_register'), register_payload())
        comp = Company.objects.get(name='RegCo')
        admin = User.objects.create_superuser('boss', 'boss@ex.com', PWD)
        self.client.force_login(admin)
        self.client.post(reverse('admin_company_decide', args=[comp.pk]), {'decision': 'APPROVED'})
        comp.refresh_from_db()
        self.assertEqual(comp.status, 'PENDING_APPROVAL')

    def test_invalid_credentials_professional_error(self):
        resp = self.client.post(reverse('login'), {'username': 'nobody', 'password': 'x'})
        self.assertContains(resp, 'Invalid username/email or password')
        self.assertTrue(AuditLog.objects.filter(details__icontains='Failed login').exists())


class SuperuserRoleTests(TestCase):
    def test_createsuperuser_gets_admin_role_and_notifications(self):
        admin = User.objects.create_superuser('root', 'root@ex.com', PWD)
        self.assertEqual(admin.role, 'ADMIN')
        company(name='NotifyCo', status='PENDING_APPROVAL')
        self.assertTrue(Notification.objects.filter(recipient=admin).exists())

    def test_admin_login_goes_to_admin_dashboard(self):
        User.objects.create_superuser('root', 'root@ex.com', PWD)
        self.client.post(reverse('login'), {'username': 'root', 'password': PWD})
        self.assertRedirects(self.client.get(reverse('post_login_redirect')), reverse('admin_dashboard'))


class PasswordAndSessionTests(TestCase):
    def setUp(self):
        self.comp = company()
        self.owner = user('own', 'COMPANY_OWNER', self.comp, email='own@ex.com')

    def test_password_reset_full_cycle(self):
        resp = self.client.post(reverse('password_reset'), {'email': 'OWN@ex.com'})
        self.assertRedirects(resp, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)
        link = re.search(r'(/reset/\S+/\S+/)', mail.outbox[0].body).group(1)
        resp = self.client.get(link, follow=True)
        set_url = resp.redirect_chain[-1][0]
        resp = self.client.post(set_url, {'new_password1': 'BrandNewPass456', 'new_password2': 'BrandNewPass456'})
        self.assertRedirects(resp, reverse('password_reset_complete'))
        self.assertFalse(self.client.login(username='own', password=PWD), 'old password must stop working')
        self.assertTrue(self.client.login(username='own', password='BrandNewPass456'))
        self.client.logout()
        resp = self.client.get(link, follow=True)
        self.assertContains(resp, 'invalid', msg_prefix='reset token must be single-use')

    def test_password_change(self):
        self.client.force_login(self.owner)
        resp = self.client.post(reverse('password_change'), {
            'old_password': PWD, 'new_password1': 'ChangedPass789', 'new_password2': 'ChangedPass789'})
        self.assertRedirects(resp, reverse('password_change_done'))
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password('ChangedPass789'))

    def test_logout_requires_post_and_ends_session(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('logout')).status_code, 405)
        self.client.post(reverse('logout'))
        self.assertRedirects(self.client.get(reverse('dashboard')), '/login/?next=/')
        self.assertTrue(AuditLog.objects.filter(action='LOGOUT', actor=self.owner).exists())

    def test_anonymous_blocked_everywhere(self):
        for name in ['dashboard', 'employee_master', 'payslips', 'bank_transfer', 'admin_dashboard', 'attendance']:
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 302, name)
            self.assertIn('/login/', resp['Location'])

    def test_csrf_enforced(self):
        from django.test import Client
        c = Client(enforce_csrf_checks=True)
        resp = c.post(reverse('login'), {'username': 'own', 'password': PWD})
        self.assertEqual(resp.status_code, 403)


class SuspensionTests(TestCase):
    def test_suspend_blocks_active_session_and_login_then_reactivate(self):
        comp = company()
        owner = user('own', 'COMPANY_OWNER', comp)
        admin = User.objects.create_superuser('root', 'root@ex.com', PWD)
        self.client.force_login(owner)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)

        admin_client = self.client_class()
        admin_client.force_login(admin)
        admin_client.post(reverse('admin_company_decide', args=[comp.pk]), {'decision': 'SUSPENDED', 'next': 'admin_company_list'})
        comp.refresh_from_db()
        self.assertEqual(comp.status, 'SUSPENDED')
        self.assertTrue(User.objects.filter(pk=owner.pk).exists(), 'suspension must not delete the account')

        self.assertIn('/login/', self.client.get(reverse('dashboard'))['Location'])
        resp = self.client.post(reverse('login'), {'username': 'own', 'password': PWD})
        self.assertContains(resp, 'suspended')

        admin_client.post(reverse('admin_company_decide', args=[comp.pk]), {'decision': 'REACTIVATE'})
        self.assertTrue(self.client.login(username='own', password=PWD))

    def test_owner_cannot_suspend(self):
        comp = company()
        owner = user('own', 'COMPANY_OWNER', comp)
        self.client.force_login(owner)
        resp = self.client.post(reverse('admin_company_decide', args=[comp.pk]), {'decision': 'SUSPENDED'})
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# Demo request + notifications
# ---------------------------------------------------------------------------

@override_settings(SMS_PROVIDER='')
class DemoRequestTests(TestCase):
    def payload(self, **over):
        d = {'full_name': 'Ravi', 'company_name': 'DemoCo', 'email': 'Ravi@DemoCo.com', 'phone': '9876543210',
             'team_size': '50', 'preferred_datetime': (timezone.localtime() + datetime.timedelta(days=2)).strftime('%Y-%m-%dT%H:%M'),
             'message': 'Need payroll for 50 staff'}
        d.update(over)
        return d

    def test_submit_saves_notifies_admin_and_emails_recipient(self):
        admin = User.objects.create_superuser('root', 'root@ex.com', PWD)
        resp = self.client.post(reverse('request_demo'), self.payload(), follow=True)
        self.assertContains(resp, 'Thank you')
        demo = DemoRequest.objects.get()
        self.assertEqual(demo.email, 'ravi@democo.com')
        self.assertIsNotNone(demo.preferred_datetime)
        self.assertTrue(Notification.objects.filter(recipient=admin, message__icontains='DemoCo').exists())
        self.assertEqual(mail.outbox[-1].to, ['jaganbharath46@gmail.com'])
        for field in ['Ravi', 'ravi@democo.com', '9876543210', 'DemoCo', '50', 'Need payroll']:
            self.assertIn(field, mail.outbox[-1].body)
        self.assertTrue(demo.email_notified)
        self.assertFalse(demo.sms_notified)
        self.assertIn('SMS provider not configured', demo.notification_error)

        self.client.force_login(admin)
        self.assertContains(self.client.get(reverse('admin_demo_requests')), 'DemoCo')
        self.assertContains(self.client.get(reverse('admin_dashboard')), 'DemoCo')

    def test_duplicate_and_invalid_submissions_rejected(self):
        self.client.post(reverse('request_demo'), self.payload())
        resp = self.client.post(reverse('request_demo'), self.payload())
        self.assertContains(resp, 'already received')
        resp = self.client.post(reverse('request_demo'), self.payload(company_name='Other', phone='12345'))
        self.assertContains(resp, 'valid 10-digit')
        resp = self.client.post(reverse('request_demo'), self.payload(company_name='Other2', email='not-an-email'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(DemoRequest.objects.count(), 1)

    @override_settings(EMAIL_BACKEND='payroll_app.tests_audit.BrokenEmailBackend')
    def test_email_failure_does_not_crash(self):
        resp = self.client.post(reverse('request_demo'), self.payload(), follow=True)
        self.assertEqual(resp.status_code, 200)
        demo = DemoRequest.objects.get()
        self.assertFalse(demo.email_notified)
        self.assertIn('Email failed', demo.notification_error)

    @override_settings(SMS_PROVIDER='fast2sms', FAST2SMS_API_KEY='x')
    def test_sms_provider_unreachable_does_not_crash(self):
        from unittest import mock
        import urllib.error
        with mock.patch('urllib.request.urlopen', side_effect=urllib.error.URLError('down')):
            self.client.post(reverse('request_demo'), self.payload())
        demo = DemoRequest.objects.get()
        self.assertFalse(demo.sms_notified)
        self.assertIn('SMS failed', demo.notification_error)


class BrokenEmailBackend:
    def __init__(self, *a, **k):
        pass

    def send_messages(self, messages):
        raise ConnectionRefusedError('smtp down')


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------

class AdminDashboardTests(TestCase):
    def test_numbers_come_from_database(self):
        company('A')
        company('B', status='PENDING_APPROVAL')
        company('C', status='SUSPENDED')
        employee(Company.objects.get(name='A'), 'A1', 'a1@ex.com')
        admin = User.objects.create_superuser('root', 'root@ex.com', PWD)
        self.client.force_login(admin)
        ctx = self.client.get(reverse('admin_dashboard')).context
        self.assertEqual(ctx['total_clients'], 3)
        self.assertEqual(ctx['active_clients'], 1)
        self.assertEqual(ctx['pending_clients'], 1)
        self.assertEqual(ctx['suspended_clients'], 1)
        self.assertEqual(ctx['total_employees'], 1)

    def test_client_and_employee_blocked_from_admin_pages(self):
        comp = company()
        owner = user('own', 'COMPANY_OWNER', comp)
        employee(comp, 'E1', 'e1@ex.com', login='emp1')
        for u in [owner, User.objects.get(username='emp1')]:
            self.client.force_login(u)
            for name in ['admin_dashboard', 'admin_company_list', 'admin_company_approvals', 'admin_demo_requests',
                         'admin_client_complaints', 'admin_audit_log']:
                self.assertEqual(self.client.get(reverse(name)).status_code, 403, (u.username, name))


# ---------------------------------------------------------------------------
# Employees, bank details
# ---------------------------------------------------------------------------

class EmployeeManagementTests(TestCase):
    def setUp(self):
        self.comp = company()
        self.owner = user('own', 'COMPANY_OWNER', self.comp)

    def post_employee(self, **over):
        data = {'code_prefix': 'EPRO', 'employee_code': '001', 'first_name': 'Anu', 'last_name': 'K',
                'email': 'ANU@ex.com', 'phone': '9876543210', 'date_of_joining': '2025-01-01', 'department': 'Eng',
                'designation': 'Dev', 'employment_status': 'ACTIVE', 'bank_name': 'SBI',
                'account_holder_name': 'Anu K', 'bank_account_no': '123456789012', 'ifsc_code': 'sbin0001234',
                'bank_branch': 'Hosur', 'account_type': 'SAVINGS', 'bank_status': 'VERIFIED'}
        data.update(over)
        return self.client.post(reverse('employee_master'), data)

    def test_owner_creates_edits_employee_with_bank_details(self):
        self.client.force_login(self.owner)
        self.assertRedirects(self.post_employee(), reverse('employee_master'))
        e = Employee.objects.get(employee_code='EPRO001')
        self.assertEqual(e.company, self.comp)
        self.assertEqual(e.ifsc_code, 'SBIN0001234')
        self.assertEqual(e.email, 'anu@ex.com')
        self.assertTrue(e.has_bank_details)
        resp = self.client.post(reverse('employee_edit', args=[e.pk]), {
            'employee_code': 'EPRO001', 'first_name': 'Anu', 'last_name': 'Kumar', 'email': 'anu@ex.com',
            'date_of_joining': '2025-01-01', 'department': 'Eng', 'designation': 'Lead', 'employment_status': 'ACTIVE',
            'bank_status': 'VERIFIED', 'bank_account_no': '123456789012', 'ifsc_code': 'SBIN0001234'})
        self.assertRedirects(resp, reverse('employee_master'))
        e.refresh_from_db()
        self.assertEqual(e.designation, 'Lead')
        self.assertTrue(AuditLog.objects.filter(action='UPDATE', model_name='Employee').exists())

    def test_invalid_bank_details_rejected(self):
        self.client.force_login(self.owner)
        resp = self.post_employee(ifsc_code='BAD')
        self.assertContains(resp, 'valid 11-character IFSC')
        resp = self.post_employee(bank_account_no='12ab')
        self.assertContains(resp, '9 to 18 digits')
        self.assertFalse(Employee.objects.exists())

    def test_admin_can_create_employee_for_selected_company(self):
        admin = User.objects.create_superuser('root', 'root@ex.com', PWD)
        self.client.force_login(admin)
        self.assertRedirects(self.post_employee(company=self.comp.pk), reverse('employee_master'))
        self.assertEqual(Employee.objects.get().company, self.comp)

    def test_owner_cannot_plant_employee_in_other_company(self):
        other = company('Other')
        self.client.force_login(self.owner)
        self.post_employee(company=other.pk)
        self.assertEqual(Employee.objects.get().company, self.comp)

    def test_employee_login_account_and_role_dashboard(self):
        self.client.force_login(self.owner)
        e = employee(self.comp, 'E9', 'e9@ex.com')
        self.client.post(reverse('employee_create_account', args=[e.pk]),
                         {'username': 'e9user', 'password1': 'EmpPass12345', 'password2': 'EmpPass12345'})
        e.refresh_from_db()
        self.assertEqual(e.user.role, 'EMPLOYEE')
        self.client.logout()
        self.assertTrue(self.client.login(username='e9@ex.com', password='EmpPass12345'))
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)
        self.assertEqual(self.client.get(reverse('employee_master')).status_code, 403)

    def test_missing_bank_details_shown_not_crash(self):
        employee(self.comp, 'NB1', 'nb@ex.com', bank=False)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('employee_master')), 'Missing Bank Details')


# ---------------------------------------------------------------------------
# Attendance / leave / reimbursement
# ---------------------------------------------------------------------------

import tempfile

_TEST_MEDIA = tempfile.mkdtemp(prefix='payroll_test_media_')


@override_settings(MEDIA_ROOT=_TEST_MEDIA)
class AttendanceLeaveReimbursementTests(TestCase):
    def setUp(self):
        self.comp = company()
        self.owner = user('own', 'COMPANY_OWNER', self.comp)
        self.e1 = employee(self.comp, 'E1', 'e1@ex.com', login='emp1')
        self.e2 = employee(self.comp, 'E2', 'e2@ex.com', login='emp2')

    def test_employee_check_in_out_own_only(self):
        self.client.force_login(self.e1.user)
        self.client.post(reverse('attendance_check', args=['in']), {'employee': self.e2.pk})
        self.client.post(reverse('attendance_check', args=['out']))
        rec = Attendance.objects.get(employee=self.e1)
        self.assertIsNotNone(rec.check_in)
        self.assertIsNotNone(rec.check_out)
        self.assertFalse(Attendance.objects.filter(employee=self.e2).exists())
        # employee may not post the manual attendance form
        self.client.post(reverse('attendance'), {'employee': self.e2.pk, 'date': '2026-01-05', 'status': 'ABSENT'})
        self.assertFalse(Attendance.objects.filter(employee=self.e2).exists())
        # and only sees own records
        records = self.client.get(reverse('attendance')).context['records']
        self.assertEqual({r.employee_id for r in records}, {self.e1.pk})

    def test_leave_balance_overlap_and_self_approval(self):
        self.client.force_login(self.e1.user)
        resp = self.client.post(reverse('leave_management'), {
            'employee': self.e2.pk, 'leave_type': 'CASUAL', 'from_date': '2026-03-02', 'to_date': '2026-03-03'})
        self.assertRedirects(resp, reverse('leave_management'))
        leave = LeaveRequest.objects.get()
        self.assertEqual(leave.employee, self.e1, 'employee is forced to own record')
        resp = self.client.post(reverse('leave_management'), {
            'leave_type': 'CASUAL', 'from_date': '2026-03-03', 'to_date': '2026-03-04'})
        self.assertContains(resp, 'overlap')
        resp = self.client.post(reverse('leave_management'), {
            'leave_type': 'SICK', 'from_date': '2026-05-01', 'to_date': '2026-05-20'})
        self.assertContains(resp, 'Insufficient leave balance')
        self.assertEqual(self.client.post(reverse('leave_decision', args=[leave.pk]), {'decision': 'APPROVED'}).status_code, 403)

        self.client.force_login(self.owner)
        self.client.post(reverse('leave_decision', args=[leave.pk]), {'decision': 'APPROVED'})
        leave.refresh_from_db()
        self.assertEqual(leave.status, 'APPROVED')
        self.assertEqual(leave.decided_by, self.owner)
        self.client.post(reverse('leave_decision', args=[leave.pk]), {'decision': 'REJECTED'})
        leave.refresh_from_db()
        self.assertEqual(leave.status, 'APPROVED', 'decided leave cannot be flipped')
        self.assertTrue(Notification.objects.filter(recipient=self.e1.user, message__icontains='approved').exists())

    def test_reimbursement_receipt_upload_and_isolation(self):
        self.client.force_login(self.e1.user)
        pdf = SimpleUploadedFile('bill.pdf', b'%PDF-1.4 test', content_type='application/pdf')
        resp = self.client.post(reverse('reimbursement'), {
            'category': 'TRAVEL', 'amount': '500', 'date': '2026-01-10', 'receipt': pdf})
        self.assertRedirects(resp, reverse('reimbursement'))
        r = Reimbursement.objects.get()
        self.assertTrue(r.receipt)
        dl = self.client.get(reverse('reimbursement_receipt', args=[r.pk]))
        self.assertEqual(dl.status_code, 200)
        dl.close()  # release the file handle (Windows cannot delete open files)

        exe = SimpleUploadedFile('virus.exe', b'MZ', content_type='application/octet-stream')
        resp = self.client.post(reverse('reimbursement'), {'category': 'TRAVEL', 'amount': '5', 'date': '2026-01-10', 'receipt': exe})
        self.assertContains(resp, 'Unsupported file type')

        self.client.force_login(self.e2.user)
        self.assertEqual(self.client.get(reverse('reimbursement_receipt', args=[r.pk])).status_code, 403)
        r.receipt.delete(save=False)


# ---------------------------------------------------------------------------
# Payroll, payslips, bank transfer
# ---------------------------------------------------------------------------

class PayrollCalculationAndIsolationTests(TestCase):
    def setUp(self):
        self.comp = company('PayCo')
        self.owner = user('own', 'COMPANY_OWNER', self.comp)
        self.e1 = employee(self.comp, 'P1', 'p1@ex.com', login='emp1')
        self.e2 = employee(self.comp, 'P2', 'p2@ex.com', login='emp2', bank=False)
        SalaryStructure.objects.create(employee=self.e1, basic=Decimal('15000'), hra=Decimal('3000'),
                                       conveyance=Decimal('1000'), special_allowance=Decimal('1000'))  # gross 20000
        SalaryStructure.objects.create(employee=self.e2, basic=Decimal('50000'), hra=Decimal('20000'),
                                       conveyance=Decimal('0'), special_allowance=Decimal('10000'))  # gross 80000
        Attendance.objects.create(employee=self.e1, date=datetime.date(2026, 6, 3), status='ABSENT')
        Attendance.objects.create(employee=self.e1, date=datetime.date(2026, 6, 4), status='HALF_DAY')
        ArrearsRecord.objects.create(employee=self.e1, effective_from=datetime.date(2026, 4, 1),
                                     old_basic=Decimal('14000'), new_basic=Decimal('15000'), months=2)
        Reimbursement.objects.create(employee=self.e1, category='TRAVEL', amount=Decimal('750'),
                                     date=datetime.date(2026, 6, 1), status='APPROVED')
        Reimbursement.objects.create(employee=self.e1, category='FOOD', amount=Decimal('999'),
                                     date=datetime.date(2026, 6, 1), status='PENDING')
        self.other = company('OtherCo')
        self.other_owner = user('other', 'COMPANY_OWNER', self.other)

    def create_run(self, month='June 2026'):
        self.client.force_login(self.owner)
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': month})
        return PayrollRun.objects.get(company=self.comp, month=month)

    def test_line_includes_lop_arrears_reimbursement_and_deductions(self):
        run = self.create_run()
        l1 = PayrollRunLine.objects.get(payroll_run=run, employee=self.e1)
        self.assertEqual(l1.days_in_month, 30)
        self.assertEqual(l1.lop_days, Decimal('1.5'))
        self.assertEqual(l1.lop_amount, Decimal('1000.00'))        # 20000/30*1.5
        self.assertEqual(l1.arrears, Decimal('2000.00'))
        self.assertEqual(l1.reimbursements, Decimal('750.00'))     # pending claim excluded
        self.assertEqual(l1.total_earnings, Decimal('21750.00'))   # 19000 + 2000 + 750
        self.assertEqual(l1.pf, Decimal('1710.00'))                # 12% of earned basic 14250
        self.assertEqual(l1.esi, Decimal('142.50'))                # 0.75% of 19000 (gross <= 21000)
        self.assertEqual(l1.tds, Decimal('0.00'))                  # annual 240000
        self.assertEqual(l1.net_pay, Decimal('19897.50'))
        self.assertTrue(l1.payslip_number.startswith('PS-'))

        l2 = PayrollRunLine.objects.get(payroll_run=run, employee=self.e2)
        self.assertEqual(l2.esi, Decimal('0.00'))                  # above ESI limit
        self.assertEqual(l2.tds, Decimal('3833.33'))               # annual 960000 -> 20000 + 26000 = 46000 / 12
        self.assertEqual(l2.net_pay, l2.total_earnings - l2.pf - l2.esi - l2.tds)

    def test_arrears_and_reimbursement_not_paid_twice(self):
        self.create_run('June 2026')
        run2 = self.create_run('July 2026')
        l = PayrollRunLine.objects.get(payroll_run=run2, employee=self.e1)
        self.assertEqual(l.arrears, 0)
        self.assertEqual(l.reimbursements, 0)

    def test_reprocess_recalculates_without_losing_claims(self):
        run = self.create_run()
        self.client.post(reverse('payroll_combined'), {'action': 'reprocess', 'run_id': run.pk})
        l1 = PayrollRunLine.objects.get(payroll_run=run, employee=self.e1)
        self.assertEqual(l1.arrears, Decimal('2000.00'))
        self.assertEqual(l1.reimbursements, Decimal('750.00'))
        self.assertEqual(PayrollRunLine.objects.filter(payroll_run=run).count(), 2)

    def test_duplicate_month_and_bad_month_rejected(self):
        self.create_run()
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': 'june 2026'})
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': 'whenever'})
        self.assertEqual(PayrollRun.objects.count(), 1)

    def test_status_cannot_skip_steps(self):
        run = self.create_run()
        self.client.post(reverse('payroll_combined'), {'action': 'release', 'run_id': run.pk})
        run.refresh_from_db()
        self.assertEqual(run.status, 'DRAFT')

    def test_other_company_cannot_see_or_act_on_run(self):
        run = self.create_run()
        empty = PayrollRun.objects.create(company=self.comp, month='May 2026')
        self.client.force_login(self.other_owner)
        runs = list(self.client.get(reverse('payroll_combined')).context['payroll_runs'])
        self.assertEqual(runs, [], 'another tenant\'s runs (even empty ones) must be invisible')
        for pk in (run.pk, empty.pk):
            resp = self.client.post(reverse('payroll_combined'), {'action': 'validate', 'run_id': pk})
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(self.client.get(reverse('payroll_run_detail', args=[pk])).status_code, 403)

    def release(self, run):
        for action in ('validate', 'approve', 'release'):
            self.client.post(reverse('payroll_combined'), {'action': action, 'run_id': run.pk})
        run.refresh_from_db()
        self.assertEqual(run.status, 'RELEASED')

    def test_payslip_pdf_access_control(self):
        run = self.create_run()
        l1 = PayrollRunLine.objects.get(payroll_run=run, employee=self.e1)
        l2 = PayrollRunLine.objects.get(payroll_run=run, employee=self.e2)
        self.client.force_login(self.e1.user)
        self.assertEqual(self.client.get(reverse('payslip_pdf', args=[l1.pk])).status_code, 404, 'unreleased')
        self.client.force_login(self.owner)
        self.release(run)
        self.client.force_login(self.e1.user)
        resp = self.client.get(reverse('payslip_pdf', args=[l1.pk]))
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        self.assertTrue(resp.content.startswith(b'%PDF'))
        self.assertEqual(self.client.get(reverse('payslip_pdf', args=[l2.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('payslip_detail', args=[l2.pk])).status_code, 404)
        self.assertContains(self.client.get(reverse('payslip_detail', args=[l1.pk])), l1.payslip_number)
        self.assertEqual(self.client.get(reverse('payslips_export_csv')).status_code, 403)
        self.assertTrue(Notification.objects.filter(recipient=self.e1.user, message__icontains='payslip').exists())
        self.client.force_login(self.other_owner)
        self.assertEqual(self.client.get(reverse('payslip_pdf', args=[l1.pk])).status_code, 404)

    def test_bank_transfer_lifecycle(self):
        run = self.create_run()
        self.release(run)
        self.client.post(reverse('bank_transfer'), {'action': 'prepare'})
        self.client.post(reverse('bank_transfer'), {'action': 'prepare'})  # idempotent
        self.assertEqual(BankPayment.objects.count(), 1, 'employee with missing bank details skipped; no duplicates')
        p = BankPayment.objects.get()
        self.assertEqual(p.status, 'PENDING')
        self.assertEqual(p.amount, PayrollRunLine.objects.get(payroll_run=run, employee=self.e1).net_pay)
        self.assertContains(self.client.get(reverse('bank_transfer')), 'Missing Bank Details')

        csv_body = self.client.get(reverse('salary_transfer_file')).content.decode()
        self.assertIn('HDFC0001234', csv_body)

        url = reverse('bank_payment_update', args=[p.pk])
        self.client.post(url, {'action': 'PAID', 'reference_number': ''})
        p.refresh_from_db()
        self.assertEqual(p.status, 'PENDING', 'never mark paid without a bank reference')
        self.client.post(url, {'action': 'FAILED', 'failure_reason': 'Account frozen'})
        p.refresh_from_db()
        self.assertEqual(p.status, 'FAILED')
        self.assertContains(self.client.get(reverse('failed_transaction_report')), 'Account frozen')
        self.client.post(url, {'action': 'RETRY'})
        self.client.post(url, {'action': 'PAID', 'reference_number': 'UTR123456'})
        p.refresh_from_db()
        self.assertEqual((p.status, p.reference_number, p.attempts, p.verified_by), ('PAID', 'UTR123456', 1, self.owner))
        self.client.post(url, {'action': 'FAILED', 'failure_reason': 'oops'})
        p.refresh_from_db()
        self.assertEqual(p.status, 'PAID', 'paid is final')
        self.assertTrue(AuditLog.objects.filter(action='PAYMENT_STATUS_CHANGE').count() >= 4)

        self.client.force_login(self.other_owner)
        self.assertEqual(self.client.post(url, {'action': 'RETRY'}).status_code, 403)
        self.assertNotContains(self.client.get(reverse('payment_states')), 'UTR123456')

    def test_reports_and_exports_scoped(self):
        run = self.create_run()
        self.release(run)
        csv_body = self.client.get(reverse('payslips_export_csv')).content.decode()
        self.assertIn('P1', csv_body)
        xlsx = self.client.get(reverse('payslips_export_excel'))
        self.assertEqual(xlsx.status_code, 200)
        self.client.force_login(self.other_owner)
        self.assertNotIn('P1', self.client.get(reverse('payslips_export_csv')).content.decode())
        self.assertNotIn('P1', self.client.get(reverse('employee_master_export_csv')).content.decode())
        self.assertEqual(self.client.get(reverse('payroll_run_download_docx', args=[run.pk])).status_code, 403)


# ---------------------------------------------------------------------------
# Every page renders for every role (no 500s, correct 403s)
# ---------------------------------------------------------------------------

class SmokeAllPagesTests(TestCase):
    OWNER_OR_ADMIN = ['employee_master', 'salary_structure', 'statutory_compliance', 'income_tax',
                      'compliance_reports', 'total_employees_report', 'new_joiners_report', 'payroll_cost_report',
                      'pending_payroll_report', 'employees_on_leave_report', 'payroll_combined', 'arrears',
                      'full_final_settlement', 'bank_transfer', 'payment_states', 'failed_transaction_report',
                      'user_roles_permissions', 'settings']
    ANY = ['dashboard', 'attendance', 'leave_management', 'reimbursement', 'payslips', 'investment_declaration',
           'reports_analytics', 'ess', 'notifications', 'payslip_history', 'email_payslip', 'download_pdf',
           'password_change']
    ADMIN_ONLY = ['admin_dashboard', 'admin_demo_requests', 'admin_company_approvals', 'admin_company_list',
                  'admin_client_complaints', 'admin_client_requests', 'admin_audit_log']
    PUBLIC = ['landing', 'request_demo', 'company_register', 'login', 'password_reset']

    def setUp(self):
        self.comp = company()
        self.admin = User.objects.create_superuser('root', 'root@ex.com', PWD)
        self.owner = user('own', 'COMPANY_OWNER', self.comp)
        self.emp = employee(self.comp, 'S1', 's1@ex.com', login='emp1')
        SalaryStructure.objects.create(employee=self.emp, basic=20000, hra=1000, conveyance=0, special_allowance=0)

    def test_every_page(self):
        for name in self.PUBLIC:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        cases = [
            (self.admin, self.ADMIN_ONLY + self.OWNER_OR_ADMIN + self.ANY, []),
            (self.owner, self.OWNER_OR_ADMIN + self.ANY + ['client_complaints', 'client_requests'], self.ADMIN_ONLY),
            (self.emp.user, self.ANY, self.ADMIN_ONLY + self.OWNER_OR_ADMIN),
        ]
        for u, ok, forbidden in cases:
            self.client.force_login(u)
            for name in ok:
                self.assertEqual(self.client.get(reverse(name)).status_code, 200, (u.username, name))
            for name in forbidden:
                self.assertEqual(self.client.get(reverse(name)).status_code, 403, (u.username, name))


class SchemaAuditCommandTests(TestCase):
    def test_reports_clean_schema(self):
        out = StringIO()
        call_command('schema_audit', stdout=out)
        self.assertIn('Schema matches the models', out.getvalue())


class EmailFirstRegistrationTests(TestCase):
    """Email -> OTP -> company details, and up to 20 accounts per email."""

    def _code(self):
        return re.search(r'\b(\d{6})\b', mail.outbox[-1].body).group(1)

    def test_email_otp_then_details(self):
        url = reverse('company_register')
        self.assertContains(self.client.get(url), 'Send OTP')
        self.client.post(url, {'action': 'send_otp', 'email': 'First@Co.com'})
        self.assertEqual(len(mail.outbox), 1)
        wrong = '111111' if self._code() != '111111' else '222222'
        self.assertContains(self.client.post(url, {'action': 'verify_otp', 'code': wrong}), 'Incorrect code')
        self.client.post(url, {'action': 'verify_otp', 'code': self._code()})
        resp = self.client.get(url)
        self.assertContains(resp, 'first@co.com')
        self.assertContains(resp, 'Verified')
        payload = register_payload(contact_email='someone.else@x.com')
        payload['action'] = 'register'
        resp = self.client.post(url, payload)
        self.assertRedirects(resp, reverse('login'), fetch_redirect_response=False)
        owner = User.objects.get(username=payload['username'])
        self.assertEqual(owner.email, 'first@co.com', 'email must be the verified one, not the posted one')
        self.assertTrue(owner.email_verified)

    def test_details_step_needs_verified_email(self):
        url = reverse('company_register')
        self.client.post(url, {'action': 'send_otp', 'email': 'x@co.com'})
        resp = self.client.get(url)
        self.assertContains(resp, 'Verify Email')
        self.assertNotContains(resp, 'Create Account')

    def test_up_to_twenty_accounts_per_email(self):
        for i in range(19):
            user(f'team{i}', 'EMPLOYEE', email='team@co.com')
        url = reverse('company_register')
        self.client.post(url, {'action': 'send_otp', 'email': 'team@co.com'})
        self.assertEqual(len(mail.outbox), 1, '20th account on the same email is allowed')
        user('team19', 'EMPLOYEE', email='team@co.com')
        self.client.post(url, {'action': 'change_email'})
        resp = self.client.post(url, {'action': 'send_otp', 'email': 'team@co.com'})
        self.assertContains(resp, 'maximum allowed')
        self.assertEqual(len(mail.outbox), 1, 'no OTP for a 21st account')