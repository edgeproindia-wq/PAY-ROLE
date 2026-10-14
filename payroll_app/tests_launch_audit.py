"""Launch audit: attacks across companies and employees, role routing, database duplicate protection, query growth.
Every test states what it proves. A FAIL here is a real finding, not a style issue."""
import datetime
import re

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from .models import (Attendance, Employee, LeaveRequest, Notification, PayrollRun, PayrollRunLine, Reimbursement, SalaryStructure, User)
from .tests import make_company, make_employee, make_user

TODAY = datetime.date.today()
MARK_NAME, MARK_ACCOUNT, MARK_EMAIL = 'ZZAlphaMarker', '555566667777', 'zzalpha@marker.test'
SKIP = ('admin/', 'logout', 'static', 'media', '__debug__')


def walk(patterns, prefix=''):
    for p in patterns:
        route = prefix + str(p.pattern)
        if isinstance(p, URLResolver):
            yield from walk(p.url_patterns, route)
        elif isinstance(p, URLPattern):
            yield route, p.name


def all_routes():
    return [(r, n) for r, n in walk(get_resolver().url_patterns) if not any(s in r for s in SKIP) and not r.startswith('^')]


def fill(route, i):
    u = re.sub(r'<(?:int|pk):[^>]+>', str(i), route)
    u = re.sub(r'<(?:str|slug|path):[^>]+>', 'x', u)
    return '/' + re.sub(r'<[^>]+>', str(i), u)


class World(TestCase):
    """Company A (with rich data) and company B, each with an owner and employees."""

    def setUp(self):
        self.A, self.B = make_company('Audit Alpha'), make_company('Audit Beta')
        self.ownerA = make_user('auditownerA', 'COMPANY_OWNER', self.A)
        self.ownerB = make_user('auditownerB', 'COMPANY_OWNER', self.B)
        self.a1 = make_employee(self.A, 'AA1', MARK_EMAIL)
        self.a2 = make_employee(self.A, 'AA2', 'aa2@audit.test')
        self.b1 = make_employee(self.B, 'BB1', 'bb1@audit.test')
        self.a1u, self.a2u, self.b1u = (make_user('auditA1', 'EMPLOYEE', self.A), make_user('auditA2', 'EMPLOYEE', self.A),
                                        make_user('auditB1', 'EMPLOYEE', self.B))
        Employee.objects.filter(pk=self.a1.pk).update(user=self.a1u, first_name=MARK_NAME, bank_name='HDFC', bank_account_no=MARK_ACCOUNT,
                                                      ifsc_code='HDFC0001234', pan_number='ZZPAN9999Z')
        Employee.objects.filter(pk=self.a2.pk).update(user=self.a2u)
        Employee.objects.filter(pk=self.b1.pk).update(user=self.b1u)
        for e in (self.a1, self.a2, self.b1):
            SalaryStructure.objects.create(employee=e, basic=20000, hra=8000, conveyance=1600, special_allowance=10400)
        self.ids = {1, 2, self.a1.pk, self.a2.pk}
        Attendance.objects.create(employee=self.a1, date=TODAY, status='PRESENT')
        self.leave = LeaveRequest.objects.create(employee=self.a1, leave_type='CASUAL', from_date=TODAY, to_date=TODAY)
        self.claim = Reimbursement.objects.create(employee=self.a1, category='TRAVEL', amount=500, date=TODAY, description='cab')
        self.run = PayrollRun.objects.create(company=self.A, month='September 2026', status='RELEASED')
        self.line = PayrollRunLine.objects.create(payroll_run=self.run, employee=self.a1, basic=20000, gross_salary=40000, net_pay=36000)
        self.note = Notification.objects.create(recipient=self.a1u, message='private note for A1')
        for obj in (self.leave, self.claim, self.run, self.line, self.note):
            self.ids.add(obj.pk)
        self.ids = sorted(self.ids)[:8]

    def attack(self, user, markers, label):
        problems = []
        urls = set()
        for route, name in all_routes():
            if '<' in route:
                urls.update(fill(route, i) for i in self.ids)
            else:
                urls.add('/' + route)
        for url in sorted(urls):
            self.client.logout()
            self.client.force_login(user)
            try:
                r = self.client.get(url)
            except Exception as e:
                problems.append(f'{label} CRASH {url}: {type(e).__name__}')
                continue
            if r.status_code >= 500:
                problems.append(f'{label} HTTP {r.status_code} {url}')
            body = r.content.decode('utf-8', 'ignore') if r.status_code == 200 else ''
            problems += [f'{label} LEAK {url} shows "{m}"' for m in markers if m in body]
        return problems, len(urls)


class IsolationAudit(World):
    def test_company_b_cannot_see_company_a_data_by_changing_ids_in_the_address(self):
        """Company B owner and employee vs EVERY address with A's object IDs: no data of company A may appear"""
        markers = [MARK_NAME, MARK_ACCOUNT, MARK_EMAIL, 'ZZPAN9999Z', 'private note for A1']
        bad, n = [], 0
        for user, label in ((self.ownerB, 'B-owner'), (self.b1u, 'B-employee')):
            p, k = self.attack(user, markers, label)
            bad += p
            n += k
        self.assertEqual(bad, [], f'{len(bad)} problem(s) in {n} addresses: {bad[:6]}')

    def test_an_employee_cannot_see_a_colleagues_private_data(self):
        """Employee A2 vs EVERY address with employee A1's IDs: A1's personal data must not appear"""
        markers = [MARK_NAME, MARK_ACCOUNT, 'ZZPAN9999Z', 'private note for A1']
        bad, n = self.attack(self.a2u, markers, 'A2-employee')
        self.assertEqual(bad, [], f'{len(bad)} problem(s): {bad[:6]}')

    def test_company_b_cannot_change_company_a_records(self):
        """Company B owner POSTs decisions/edits/deletes on A's records: nothing of A may change"""
        self.client.force_login(self.ownerB)
        posts = [(reverse('leave_decision', args=[self.leave.pk]), {'decision': 'APPROVED', 'comment': 'x'}),
                 (reverse('reimbursement_decision', args=[self.claim.pk]), {'decision': 'APPROVED', 'comment': 'x'}),
                 (reverse('employee_delete', args=[self.a1.pk]), {}),
                 (reverse('employee_edit', args=[self.a1.pk]), {'first_name': 'Hacked'}),
                 (reverse('notification_mark_read', args=[self.note.pk]), {}),
                 (reverse('payroll_run_detail', args=[self.run.pk]), {'action': 'unlock'})]
        try:
            posts.append((reverse('bank_payment_update', args=[self.line.pk]), {'status': 'PAID'}))
        except Exception:
            pass
        for url, data in posts:
            try:
                self.client.post(url, data)
            except Exception:
                pass
        self.leave.refresh_from_db(); self.claim.refresh_from_db(); self.run.refresh_from_db(); self.note.refresh_from_db()
        self.assertEqual((self.leave.status, self.claim.status, self.note.is_read), ('PENDING', 'PENDING', False))
        self.assertEqual(Employee.objects.get(pk=self.a1.pk).first_name, MARK_NAME)
        self.assertTrue(Employee.objects.filter(pk=self.a1.pk).exists())
        self.assertFalse(self.run.is_locked)


class RoleRoutingAudit(World):
    def test_each_role_lands_on_its_own_dashboard(self):
        """Owner, employee and admin each reach their own dashboard after login"""
        admin = User.objects.create_superuser('auditadmin', 'auditadmin@audit.test', 'Adm1n-Pass#2026')
        expect = {self.ownerA: 'Dashboard', self.a1u: 'My Dashboard', admin: 'Admin Dashboard'}
        for user, title in expect.items():
            self.client.logout()
            self.client.force_login(user)
            r = self.client.get('/', follow=True)
            self.assertEqual(r.status_code, 200)
            html = r.content.decode()
            if '</body>' in html:
                self.assertIn(title, html, f'{user.username} did not land on a page titled {title}')

    def test_employee_is_blocked_from_every_owner_page(self):
        """An employee opening owner-only pages directly gets refused"""
        self.client.force_login(self.a1u)
        for name in ('employee_master', 'salary_structure', 'payroll_processing', 'bank_transfer', 'settings', 'pay_cycles'):
            try:
                url = reverse(name)
            except Exception:
                continue
            r = self.client.get(url)
            self.assertIn(r.status_code, (302, 403, 404), f'employee can open {name} (HTTP {r.status_code})')
            if r.status_code == 302:
                self.assertNotIn(url, r['Location'])

    def test_the_reports_page_an_employee_may_open_shows_only_that_employees_own_numbers(self):
        """Employees may open Reports, but it must be scoped to their own records (not company totals)"""
        for i in range(5):
            make_employee(self.A, f'EXTRA{i}', f'extra{i}@audit.test')
        self.client.force_login(self.a2u)
        r = self.client.get(reverse('reports_analytics'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['total_employees'], 1)

    def test_owner_is_blocked_from_the_platform_admin_pages(self):
        """A company owner cannot open the platform admin pages"""
        self.client.force_login(self.ownerA)
        for name in ('admin_dashboard', 'admin_company_list', 'admin_company_approvals', 'admin_audit_log'):
            try:
                url = reverse(name)
            except Exception:
                continue
            self.assertIn(self.client.get(url).status_code, (302, 403, 404), f'owner can open {name}')

    def test_anonymous_visitors_only_reach_public_pages(self):
        """Anonymous GET: any page that answers 200 must be on the public list"""
        public_words = ('home', 'login', 'register', 'features', 'about', 'demo', 'contact', 'pricing', 'privacy', 'terms', 'password', 'verify',
                        'otp', 'logout', 'landing', 'faq', 'security', 'integrat', 'solution', 'blog', 'help', 'docs', 'support', 'robots', 'sitemap')
        open_pages = []
        for route, name in all_routes():
            if '<' in route or route.startswith('sbx-'):
                continue
            self.client.logout()
            r = self.client.get('/' + route)
            if r.status_code == 200 and not any(w in (name or route).lower() for w in public_words):
                open_pages.append(f'/{route} ({name})')
        self.assertEqual(open_pages, [], f'private-looking pages open to anonymous visitors: {open_pages}')


class IntegrityAudit(World):
    def test_database_refuses_a_second_payroll_run_for_the_same_month(self):
        """Database-level: two runs for the same company and month cannot exist"""
        with self.assertRaises(IntegrityError), transaction.atomic():
            PayrollRun.objects.create(company=self.A, month='September 2026', status='DRAFT')

    def test_database_refuses_two_payroll_lines_for_one_employee_in_a_run(self):
        """Database-level: one payroll line per employee per run"""
        with self.assertRaises(IntegrityError), transaction.atomic():
            PayrollRunLine.objects.create(payroll_run=self.run, employee=self.a1, basic=1, gross_salary=1, net_pay=1)

    def test_creating_the_same_month_twice_through_the_page_makes_one_run(self):
        """Through the page: the second 'create run' for a month is refused"""
        self.client.force_login(self.ownerB)
        for _ in range(2):
            self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': 'September 2026'}, follow=True)
        self.assertEqual(PayrollRun.objects.filter(company=self.B, month='September 2026').count(), 1)

    def test_duplicate_logins_attendance_and_salary_rows_are_refused(self):
        """Database-level: one login per employee, one attendance row per day, one salary structure per employee"""
        with self.assertRaises(IntegrityError), transaction.atomic():
            Employee.objects.filter(pk=self.a2.pk).update(user=self.a1u)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Attendance.objects.create(employee=self.a1, date=TODAY, status='ABSENT')
        with self.assertRaises(IntegrityError), transaction.atomic():
            SalaryStructure.objects.create(employee=self.a1, basic=1, hra=1, conveyance=1, special_allowance=1)


class QueryGrowthAudit(World):
    def pages(self):
        out = []
        for name in ('dashboard', 'employee_master', 'attendance', 'leave_management', 'reimbursement', 'payroll_processing', 'payslips',
                     'bank_transfer', 'salary_structure', 'reports_analytics'):
            try:
                out.append((name, reverse(name)))
            except Exception:
                pass
        return out

    def count(self, url):
        self.client.force_login(self.ownerA)
        with CaptureQueriesContext(connection) as q:
            r = self.client.get(url)
        return len(q), r.status_code

    def test_pages_do_not_run_more_queries_when_there_are_more_employees(self):
        """N+1 check: queries at 8 employees vs 48 employees must not grow with the number of employees"""
        base = {n: self.count(u) for n, u in self.pages()}
        for i in range(40):
            e = make_employee(self.A, f'GRW{i:03d}', f'grw{i}@audit.test')
            SalaryStructure.objects.create(employee=e, basic=20000, hra=8000, conveyance=1600, special_allowance=10400)
            Attendance.objects.create(employee=e, date=TODAY, status='PRESENT')
            LeaveRequest.objects.create(employee=e, leave_type='CASUAL', from_date=TODAY, to_date=TODAY)
            Reimbursement.objects.create(employee=e, category='TRAVEL', amount=100, date=TODAY, description='x')
            PayrollRunLine.objects.create(payroll_run=self.run, employee=e, basic=20000, gross_salary=40000, net_pay=36000)
        grown = []
        for n, u in self.pages():
            after, status = self.count(u)
            if after > base[n][0] + 10:
                grown.append(f'{n}: {base[n][0]} -> {after} queries')
        self.assertEqual(grown, [], f'queries grow with employee count (N+1): {grown}')