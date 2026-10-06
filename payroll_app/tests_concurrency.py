"""Tests for: many users at once, one client never seeing another client's data,
two people editing the same record, and half-saved data.

Run all of them:      python manage.py test payroll_app.tests_concurrency
The "at the same moment" tests need PostgreSQL (SQLite cannot lock rows) and are skipped on SQLite.
"""
import threading
from datetime import date
from unittest import mock, skipUnless

from django.conf import settings
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from .models import (AuditLog, Attendance, Company, CompanySettings, Employee, LeaveRequest, PayrollRun,
                     SalaryStructure, User)

PASSWORD = 'Test-pass-12345'
POSTGRES = connection.vendor == 'postgresql'


def make_company(name, tag):
    company = Company.objects.create(name=name, contact_email=f'{tag}@example.test', status='APPROVED')
    owner = User.objects.create_user(f'{tag}_owner', f'{tag}_owner@example.test', PASSWORD,
                                     role='COMPANY_OWNER', company=company)
    return company, owner


def make_second_owner(company, tag):
    return User.objects.create_user(f'{tag}_owner2', f'{tag}_owner2@example.test', PASSWORD,
                                    role='COMPANY_OWNER', company=company)


def make_employee(company, code, login=False):
    emp = Employee.objects.create(
        company=company, employee_code=code, first_name=f'First{code}', last_name='Test',
        email=f'{code.lower()}@c{company.pk}.example.test', date_of_joining=date(2024, 1, 1),
        department='Ops', designation='Staff',
    )
    if login:
        emp.user = User.objects.create_user(f'emp_{code.lower()}_{company.pk}', emp.email, PASSWORD,
                                            role='EMPLOYEE', company=company)
        emp.save()
    return emp


def employee_post_data(emp, **changes):
    data = {
        'employee_code': emp.employee_code, 'first_name': emp.first_name, 'last_name': emp.last_name,
        'email': emp.email, 'phone': '', 'gender': '', 'date_of_birth': '',
        'date_of_joining': emp.date_of_joining.isoformat(), 'department': emp.department,
        'designation': emp.designation, 'employment_status': emp.employment_status,
        'pan_number': '', 'aadhar_number': '', 'bank_name': '', 'account_holder_name': '',
        'bank_account_no': '', 'ifsc_code': '', 'bank_branch': '', 'account_type': '',
        'bank_status': emp.bank_status,
    }
    data.update(changes)
    return data


def login(user):
    client = Client()
    assert client.login(username=user.username, password=PASSWORD), f'could not log in {user.username}'
    return client


def texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class ClientIsolationTests(TestCase):
    """One client must never see or change another client's data."""

    def setUp(self):
        self.a, self.owner_a = make_company('Client A', 'a')
        self.b, self.owner_b = make_company('Client B', 'b')
        self.emp_b = make_employee(self.b, 'B1')
        self.leave_b = LeaveRequest.objects.create(employee=self.emp_b, leave_type='CASUAL',
                                                   from_date=date(2026, 5, 4), to_date=date(2026, 5, 5))

    def test_owner_of_a_cannot_open_or_edit_employee_of_b(self):
        client = login(self.owner_a)
        url = reverse('employee_edit', args=[self.emp_b.pk])
        self.assertEqual(client.get(url).status_code, 403)
        response = client.post(url, employee_post_data(self.emp_b, department='Hacked'))
        self.assertEqual(response.status_code, 403)
        self.emp_b.refresh_from_db()
        self.assertEqual(self.emp_b.department, 'Ops')

    def test_owner_of_a_cannot_decide_leave_of_b(self):
        client = login(self.owner_a)
        response = client.post(reverse('leave_decision', args=[self.leave_b.pk]), {'decision': 'APPROVED'})
        self.assertEqual(response.status_code, 403)
        self.leave_b.refresh_from_db()
        self.assertEqual(self.leave_b.status, 'PENDING')

    def test_users_of_the_same_client_share_that_clients_data(self):
        emp_a = make_employee(self.a, 'A1')
        second = make_second_owner(self.a, 'a')
        for user in (self.owner_a, second):
            self.assertEqual(login(user).get(reverse('employee_edit', args=[emp_a.pk])).status_code, 200)

    def test_each_user_has_their_own_session(self):
        one, two = login(self.owner_a), login(self.owner_b)
        self.assertNotEqual(one.session['_auth_user_id'], two.session['_auth_user_id'])
        self.assertNotEqual(one.session.session_key, two.session.session_key)


class SameRecordEditTests(TestCase):
    """Second person to save gets a clear warning and nothing is overwritten."""

    def setUp(self):
        self.company, self.owner1 = make_company('Client A', 'a')
        self.owner2 = make_second_owner(self.company, 'a')
        self.emp = make_employee(self.company, 'A1')
        self.url = reverse('employee_edit', args=[self.emp.pk])

    def test_second_save_is_refused_with_a_warning(self):
        one, two = login(self.owner1), login(self.owner2)
        one.get(self.url)                       # both people open the edit form (same version)
        two.get(self.url)
        saved = two.post(self.url, employee_post_data(self.emp, department='Finance'))
        self.assertEqual(saved.status_code, 302)
        late = one.post(self.url, employee_post_data(self.emp, department='Sales'))
        self.assertEqual(late.status_code, 200)                                  # page shown again, not saved
        self.assertTrue(any('Someone else saved' in m for m in texts(late)), texts(late))
        self.emp.refresh_from_db()
        self.assertEqual(self.emp.department, 'Finance')                         # first person's change survives
        self.assertEqual(self.emp.version, 2)

    def test_after_the_warning_the_user_can_save_again(self):
        one, two = login(self.owner1), login(self.owner2)
        one.get(self.url)
        two.get(self.url)
        two.post(self.url, employee_post_data(self.emp, department='Finance'))
        one.post(self.url, employee_post_data(self.emp, department='Sales'))     # refused, form reloaded
        again = one.post(self.url, employee_post_data(self.emp, department='Sales'))
        self.assertEqual(again.status_code, 302)
        self.emp.refresh_from_db()
        self.assertEqual((self.emp.department, self.emp.version), ('Sales', 3))

    def test_a_form_that_was_never_opened_is_not_saved(self):
        response = login(self.owner1).post(self.url, employee_post_data(self.emp, department='Sales'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any('opened too long ago' in m for m in texts(response)))
        self.emp.refresh_from_db()
        self.assertEqual(self.emp.department, 'Ops')

    def test_company_settings_use_the_same_protection(self):
        url = reverse('settings')
        one, two = login(self.owner1), login(self.owner2)
        one.get(url)
        two.get(url)
        current = CompanySettings.objects.get(company=self.company)

        def post(client, name):
            return client.post(url, {
                'company_name': name, 'address': '', 'pan_number': '', 'gst_number': '',
                'pf_percentage': '12.00', 'esi_percentage': '0.75', 'pf_wage_ceiling': '21000.00',
                'casual_leave_days': '12', 'sick_leave_days': '12', 'earned_leave_days': '15'})
        self.assertEqual(post(two, 'Second person Ltd').status_code, 302)
        late = post(one, 'First person Ltd')
        self.assertEqual(late.status_code, 200)
        self.assertTrue(any('Someone else saved' in m for m in texts(late)))
        current.refresh_from_db()
        self.assertEqual(current.company_name, 'Second person Ltd')


class HalfSavedDataTests(TransactionTestCase):
    """If a request fails part-way, nothing it did may stay in the database."""

    def test_atomic_requests_is_on(self):
        self.assertTrue(settings.DATABASES['default'].get('ATOMIC_REQUESTS'))

    def test_failed_request_leaves_nothing_behind(self):
        company, _ = make_company('Client A', 'a')
        emp = make_employee(company, 'A1', login=True)
        client = Client(raise_request_exception=False)
        client.login(username=emp.user.username, password=PASSWORD)
        with mock.patch('payroll_app.views.log_action', side_effect=RuntimeError('simulated crash')):
            response = client.post(reverse('attendance_check', args=['in']))
        self.assertEqual(response.status_code, 500)
        self.assertFalse(Attendance.objects.filter(employee=emp).exists())      # rolled back, not half-saved


def run_in_threads(jobs):
    """Run each job() in its own thread, all released at the same moment. Returns the results."""
    barrier = threading.Barrier(len(jobs))
    results = [None] * len(jobs)

    def runner(i, job):
        try:
            barrier.wait()
            results[i] = job()
        except Exception as exc:                     # keep going so the test can report it
            results[i] = exc
        finally:
            connections.close_all()
    threads = [threading.Thread(target=runner, args=(i, j)) for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


@skipUnless(POSTGRES, 'row locking needs PostgreSQL (SQLite cannot lock rows)')
class AtTheSameMomentTests(TransactionTestCase):
    ROUNDS = 12

    def setUp(self):
        self.company, self.owner1 = make_company('Client A', 'a')
        self.owner2 = make_second_owner(self.company, 'a')

    def test_two_approvers_decide_the_same_leave_only_one_wins(self):
        emp = make_employee(self.company, 'A1')
        for round_no in range(self.ROUNDS):
            leave = LeaveRequest.objects.create(employee=emp, leave_type='CASUAL',
                                                from_date=date(2026, 5, 4), to_date=date(2026, 5, 5))
            url = reverse('leave_decision', args=[leave.pk])
            clients = [login(self.owner1), login(self.owner2)]
            run_in_threads([lambda c=c, d=d: c.post(url, {'decision': d})
                            for c, d in zip(clients, ('APPROVED', 'REJECTED'))])
            decisions = AuditLog.objects.filter(model_name='LeaveRequest', object_id=str(leave.pk),
                                                action__in=('APPROVE', 'REJECT')).count()
            self.assertEqual(decisions, 1, f'round {round_no}: the leave was decided {decisions} times')
            leave.refresh_from_db()
            self.assertEqual(leave.version, 2)

    def test_same_employee_saved_by_two_people_at_once_only_one_wins(self):
        for round_no in range(self.ROUNDS):
            emp = make_employee(self.company, f'R{round_no}')
            url = reverse('employee_edit', args=[emp.pk])
            clients = [login(self.owner1), login(self.owner2)]
            for c in clients:
                c.get(url)
            results = run_in_threads([lambda c=c, d=d: c.post(url, employee_post_data(emp, department=d))
                                      for c, d in zip(clients, ('Finance', 'Sales'))])
            codes = sorted(r.status_code for r in results)
            self.assertEqual(codes, [200, 302], f'round {round_no}: {codes}')
            emp.refresh_from_db()
            self.assertEqual(emp.version, 2)

    def test_two_people_creating_the_same_payroll_month_make_one_run(self):
        for round_no in range(self.ROUNDS):
            month = f'{["January","February","March","April","May","June","July","August","September","October","November","December"][round_no % 12]} 2026'
            clients = [login(self.owner1), login(self.owner2)]
            run_in_threads([lambda c=c: c.post(reverse('payroll_combined'), {'action': 'create', 'month': month})
                            for c in clients])
            self.assertEqual(PayrollRun.objects.filter(company=self.company, month=month).count(), 1, month)


class SeedUsersTests(TestCase):
    def test_seed_users_creates_clients_and_can_be_repeated(self):
        User.objects.create_superuser('boss', 'boss@example.test', PASSWORD)
        call_command('seed_users', '--force', '--password', PASSWORD, '--link-existing', 'boss', verbosity=0)
        self.assertEqual(Company.objects.filter(name__startswith='Sample Client').count(), 2)
        self.assertEqual(Employee.objects.filter(company__name__startswith='Sample Client').count(), 6)
        self.assertTrue(login(User.objects.get(username='owner1')))
        boss = User.objects.get(username='boss')
        self.assertTrue(boss.check_password(PASSWORD))                    # existing login untouched
        self.assertEqual(boss.role, 'ADMIN')
        call_command('seed_users', '--force', '--password', PASSWORD, verbosity=0)   # second run: no duplicates
        self.assertEqual(Employee.objects.filter(company__name__startswith='Sample Client').count(), 6)
        self.assertEqual(User.objects.filter(username__startswith='owner').count(), 4)      # 2 owners per client