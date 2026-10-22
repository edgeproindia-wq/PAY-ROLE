"""Multi-company isolation tests.

Two independent companies (Alpha, Bravo), each with an owner, employees with
logins and a released payroll run. The tests prove, on the server side, that:
  - every signed-in page shows only the caller's own company/employee data,
  - the platform admin dashboard shows company-management data only,
  - changing a record ID in a URL cannot open another company's records,
  - a new company's settings page shows its own name (not 'My Company').
Runs on Django's throw-away test database, never on db.sqlite3.
"""
import datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import get_resolver, reverse

from .models import Company, CompanySettings, Employee, PayrollRun, PayrollRunLine, User

PWD = 'StrongPass123'


def make_company(name, **extra):
    return Company.objects.create(name=name, contact_email=f'{name.split()[0].lower()}@ex.com',
                                  contact_phone='9876543210', address=extra.pop('address', f'{name} Street'),
                                  status='APPROVED', **extra)


def make_user(username, role, comp):
    return User.objects.create_user(username=username, password=PWD, role=role, company=comp,
                                    email=f'{username}@ex.com')


def make_employee(comp, code, first, last, login=None):
    e = Employee.objects.create(
        company=comp, employee_code=code, first_name=first, last_name=last,
        email=f'{first.lower()}.{last.lower()}@ex.com', date_of_joining=datetime.date(2024, 1, 1),
        department='Eng', designation='Dev', bank_account_no='123456789012', ifsc_code='HDFC0001234',
        bank_name='HDFC')
    if login:
        e.user = make_user(login, 'EMPLOYEE', comp)
        e.save()
    return e


def make_released_run(comp, employee, payslip_no, month='August 2026'):
    run = PayrollRun.objects.create(company=comp, month=month, status='RELEASED', is_locked=True)
    line = PayrollRunLine.objects.create(
        payroll_run=run, employee=employee, payslip_number=payslip_no, basic=Decimal('20000'),
        gross_salary=Decimal('30000'), total_earnings=Decimal('30000'), net_pay=Decimal('27000'),
        total_deductions=Decimal('3000'), days_in_month=31)
    return run, line


class TwoCompanyFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.A = make_company('Alpha Textiles Pvt Ltd')
        cls.B = make_company('Bravo Industries Pvt Ltd')
        cls.ownerA = make_user('ownerA', 'COMPANY_OWNER', cls.A)
        cls.ownerB = make_user('ownerB', 'COMPANY_OWNER', cls.B)
        # Unique, searchable markers so a leak is unmistakable in any page body.
        cls.a1 = make_employee(cls.A, 'A001', 'Kestrel', 'Alphaone', login='empA1')
        cls.a2 = make_employee(cls.A, 'A002', 'Yarnwick', 'Alphatwo', login='empA2')
        cls.b1 = make_employee(cls.B, 'B001', 'Zorbax', 'Bravoone', login='empB1')
        cls.b2 = make_employee(cls.B, 'B002', 'Quillfeather', 'Bravotwo')
        cls.runA, cls.lineA = make_released_run(cls.A, cls.a1, 'PS-ALPHA-0001')
        cls.runB, cls.lineB = make_released_run(cls.B, cls.b1, 'PS-BRAVO-0001')
        cls.admin = User.objects.create_superuser('root', 'root@ex.com', PWD)

    B_MARKERS = ['Bravo Industries', 'Zorbax', 'Bravoone', 'Quillfeather', 'Bravotwo', 'PS-BRAVO-0001',
                 'B001', 'B002']
    A_MARKERS = ['Alpha Textiles', 'Kestrel', 'Alphaone', 'Yarnwick', 'Alphatwo', 'PS-ALPHA-0001',
                 'A001', 'A002']


class OwnerDashboardTests(TwoCompanyFixture):
    def test_owner_a_dashboard_counts_only_company_a(self):
        self.client.force_login(self.ownerA)
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['total_employees'], 2)              # A001, A002 only
        self.assertEqual(r.context['latest_run_employee_count'], 1)
        self.assertEqual(r.context['latest_run_net_pay'], Decimal('27000'))
        self.assertContains(r, 'Alpha Textiles')
        for marker in self.B_MARKERS:
            self.assertNotContains(r, marker)

    def test_owner_b_dashboard_counts_only_company_b(self):
        self.client.force_login(self.ownerB)
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.context['total_employees'], 2)
        self.assertContains(r, 'Bravo Industries')
        for marker in self.A_MARKERS:
            self.assertNotContains(r, marker)

    def test_employee_master_lists_only_own_company(self):
        self.client.force_login(self.ownerA)
        r = self.client.get(reverse('employee_master'))
        self.assertContains(r, 'Kestrel')
        for marker in self.B_MARKERS:
            self.assertNotContains(r, marker)

    def test_csv_export_contains_only_own_company(self):
        self.client.force_login(self.ownerA)
        r = self.client.get(reverse('employee_master_export_csv'))
        body = r.content.decode('utf-8', 'ignore')
        self.assertIn('Kestrel', body)
        for marker in self.B_MARKERS:
            self.assertNotIn(marker, body)


class SettingsBrandingTests(TwoCompanyFixture):
    def test_new_company_settings_show_its_own_name_not_placeholder(self):
        self.client.force_login(self.ownerA)
        r = self.client.get(reverse('settings'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Alpha Textiles Pvt Ltd')
        self.assertNotContains(r, 'My Company')
        self.assertEqual(CompanySettings.objects.get(company=self.A).company_name, 'Alpha Textiles Pvt Ltd')

    def test_legacy_placeholder_row_displays_real_name(self):
        CompanySettings.objects.create(company=self.B)              # old row: default 'My Company'
        self.client.force_login(self.ownerB)
        r = self.client.get(reverse('settings'))
        self.assertContains(r, 'Bravo Industries Pvt Ltd')
        self.assertNotContains(r, 'My Company')

    def test_settings_update_is_isolated(self):
        self.client.force_login(self.ownerA)
        form = self.client.get(reverse('settings')).context['form']
        data = {n: ('' if form[n].value() is None else form[n].value()) for n in form.fields}
        data['company_name'] = 'Alpha Renamed Ltd'
        self.client.post(reverse('settings'), data)
        self.assertEqual(CompanySettings.objects.get(company=self.A).company_name, 'Alpha Renamed Ltd')
        self.assertFalse(CompanySettings.objects.filter(company=self.B, company_name='Alpha Renamed Ltd').exists())


class AdminDashboardPolicyTests(TwoCompanyFixture):
    def test_admin_dashboard_shows_company_details_only(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['total_clients'], 2)
        self.assertEqual(r.context['active_clients'], 2)
        for key in ('total_employees', 'active_employees', 'total_payroll_runs', 'total_net_released',
                    'payments_pending', 'payments_failed', 'runs_awaiting_approval'):
            self.assertNotIn(key, r.context)
        for marker in ('Kestrel', 'Zorbax', 'Quillfeather', 'PS-ALPHA-0001', 'PS-BRAVO-0001', '27,000'):
            self.assertNotContains(r, marker)

    def test_admin_company_list_shows_company_details(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('admin_company_list'))
        self.assertContains(r, 'Alpha Textiles')
        self.assertContains(r, 'Bravo Industries')
        for marker in ('Kestrel', 'Zorbax', 'PS-ALPHA-0001'):
            self.assertNotContains(r, marker)

    def test_owner_cannot_open_admin_dashboard(self):
        self.client.force_login(self.ownerA)
        self.assertEqual(self.client.get(reverse('admin_dashboard')).status_code, 403)


class IdTamperingTests(TwoCompanyFixture):
    """Owner A / employee A1 must not reach Company B's records by changing an ID."""

    def _blocked(self, resp):
        self.assertIn(resp.status_code, (403, 404), f'got HTTP {resp.status_code}')
        body = resp.content.decode('utf-8', 'ignore')
        for marker in self.B_MARKERS:
            self.assertNotIn(marker, body)

    def test_owner_cannot_open_other_company_records(self):
        self.client.force_login(self.ownerA)
        for name, pk in [('employee_edit', self.b1.pk), ('payslip_detail', self.lineB.pk),
                         ('payslip_pdf', self.lineB.pk), ('payroll_run_detail', self.runB.pk),
                         ('payroll_run_download_docx', self.runB.pk)]:
            self._blocked(self.client.get(reverse(name, args=[pk])))

    def test_owner_cannot_modify_or_delete_other_company_employee(self):
        self.client.force_login(self.ownerA)
        self._blocked(self.client.post(reverse('employee_delete', args=[self.b1.pk])))
        self.assertTrue(Employee.objects.filter(pk=self.b1.pk).exists())
        self._blocked(self.client.post(reverse('employee_edit', args=[self.b1.pk]), {'first_name': 'Hacked'}))
        self.assertEqual(Employee.objects.get(pk=self.b1.pk).first_name, 'Zorbax')

    def test_employee_cannot_open_colleague_or_other_company_payslips(self):
        make_released_run(self.A, self.a2, 'PS-ALPHA-0002', month='September 2026')
        line_a2 = PayrollRunLine.objects.get(payslip_number='PS-ALPHA-0002')
        self.client.force_login(self.a1.user)
        for pk in (line_a2.pk, self.lineB.pk):
            for name in ('payslip_detail', 'payslip_pdf'):
                resp = self.client.get(reverse(name, args=[pk]))
                self.assertIn(resp.status_code, (403, 404))
        own = self.client.get(reverse('payslip_detail', args=[self.lineA.pk]))
        self.assertEqual(own.status_code, 200)

    def test_employee_cannot_open_company_admin_pages(self):
        self.client.force_login(self.a1.user)
        for name in ('employee_master', 'settings'):
            self.assertIn(self.client.get(reverse(name)).status_code, (302, 403))

    def test_querystring_company_switch_is_ignored(self):
        self.client.force_login(self.ownerA)
        r = self.client.get(reverse('employee_master'), {'company': self.B.pk, 'company_id': self.B.pk})
        for marker in self.B_MARKERS:
            self.assertNotContains(r, marker)


def _crawlable_urls():
    """Every parameter-free, non-admin URL name in the project."""
    found = []

    def walk(patterns, prefix=''):
        for p in patterns:
            route = prefix + str(p.pattern)
            if hasattr(p, 'url_patterns'):
                walk(p.url_patterns, route)
            elif p.name and '<' not in route and '(' not in route:
                found.append(p.name)
    walk(get_resolver().url_patterns)
    skip_prefixes = ('admin:', 'logout', 'password_reset', 'view_on_site', 'app_list')
    return sorted({n for n in found if not n.startswith(skip_prefixes)
                   and 'logout' not in n and not n.endswith(('_history',))})


class FullPageCrawlTests(TwoCompanyFixture):
    """GET every parameter-free page as each role; no page may show the other
    company's data. This is the check that catches a forgotten company filter."""

    def _crawl(self, user, forbidden):
        self.client.force_login(user)
        leaks, errors = [], []
        for name in _crawlable_urls():
            try:
                url = reverse(name)
            except Exception:
                continue
            resp = self.client.get(url)
            if resp.status_code >= 500:
                errors.append((name, resp.status_code))
                continue
            if resp.status_code != 200:
                continue
            ctype = resp.get('Content-Type', '')
            if resp.streaming:
                body = b''.join(resp.streaming_content).decode('utf-8', 'ignore')
            elif ctype.startswith(('text/', 'application/json', 'application/csv')):
                body = resp.content.decode('utf-8', 'ignore')
            else:
                continue                                        # xlsx/pdf/zip are binary
            hits = [m for m in forbidden if m in body]
            if hits:
                leaks.append((name, hits))
        return leaks, errors

    def test_owner_a_pages_never_show_company_b_data(self):
        leaks, errors = self._crawl(self.ownerA, self.B_MARKERS)
        self.assertEqual(leaks, [], f'Company B data visible to owner A on: {leaks}')
        self.assertEqual(errors, [], f'pages returned HTTP 5xx for owner A: {errors}')

    def test_owner_b_pages_never_show_company_a_data(self):
        leaks, errors = self._crawl(self.ownerB, self.A_MARKERS)
        self.assertEqual(leaks, [], f'Company A data visible to owner B on: {leaks}')
        self.assertEqual(errors, [], f'pages returned HTTP 5xx for owner B: {errors}')

    def test_employee_pages_never_show_colleague_or_other_company_data(self):
        forbidden = self.B_MARKERS + ['Yarnwick', 'Alphatwo', 'PS-ALPHA-0002', 'A002']
        leaks, errors = self._crawl(self.a1.user, forbidden)
        self.assertEqual(leaks, [], f'Other people data visible to employee A1 on: {leaks}')
        self.assertEqual(errors, [], f'pages returned HTTP 5xx for employee A1: {errors}')

    def test_platform_admin_pages_never_show_employee_or_payroll_data(self):
        forbidden = ['Kestrel', 'Yarnwick', 'Zorbax', 'Quillfeather', 'PS-ALPHA-0001', 'PS-BRAVO-0001']
        leaks, errors = self._crawl(self.admin, forbidden)
        self.assertEqual(leaks, [], f'Employee/payroll data visible to platform admin on: {leaks}')
        self.assertEqual(errors, [], f'pages returned HTTP 5xx for admin: {errors}')