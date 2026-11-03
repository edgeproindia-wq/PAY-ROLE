"""Smoke test for the whole site: every page that can be opened by address must answer without a server error (HTTP 500),
for a company owner, a platform admin, an employee and someone who is not logged in.

A page may answer 200, a redirect, 403, 404 or 405 (a page that only accepts a form post). It must never crash.
Pages that need an id are opened with an id that does not exist, which must give 404 and not an error.
"""
import datetime
import re

from django.test import Client, TestCase
from django.urls import URLPattern, URLResolver, get_resolver

from .models import Employee, PayrollRun, PayrollRunLine
from .tests import make_company, make_employee, make_user

SKIP_PREFIXES = ('admin/', 'static/', 'media/', '__debug__/', 'api/')       # the API takes only POST with a key and is tested on its own
FILLERS = {'int': '999999', 'str': 'x', 'slug': 'x', 'uuid': '00000000-0000-0000-0000-000000000000', 'path': 'x'}


def routes(patterns=None, prefix=''):
    """Every address the site knows, as plain text with the id parts filled in."""
    patterns = get_resolver().url_patterns if patterns is None else patterns
    for p in patterns:
        text = str(p.pattern)
        if isinstance(p, URLResolver):
            if '(' in text:
                continue
            yield from routes(p.url_patterns, prefix + text.lstrip('^'))
        elif isinstance(p, URLPattern):
            if '(' in text:                                      # a regular-expression address with groups: skipped
                continue
            full = prefix + text.lstrip('^').rstrip('$')

            def fill(m):
                return FILLERS.get(m.group(1) or 'str', 'x')

            full = re.sub(r'<(?:(\w+):)?\w+>', fill, full)
            if '<' in full or full.startswith(SKIP_PREFIXES):
                continue
            yield '/' + full


class SmokeAllPages(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company('Smoke Co')
        cls.owner = make_user('smoke_owner', 'COMPANY_OWNER', cls.company)
        cls.admin = make_user('smoke_admin', 'ADMIN')
        cls.employee_user = make_user('smoke_emp', 'EMPLOYEE', cls.company)
        emp = make_employee(cls.company, 'SMK1', 'smk1@example.com')
        emp.user = cls.employee_user
        emp.save()
        other = make_employee(cls.company, 'SMK2', 'smk2@example.com')
        run = PayrollRun.objects.create(company=cls.company, month='October 2026', status='RELEASED')
        for e in (emp, other):
            PayrollRunLine.objects.create(payroll_run=run, employee=e, payslip_number=f'PS-{e.employee_code}', basic=10000, gross_salary=10000,
                                          total_earnings=10000, total_deductions=1000, net_pay=9000, days_in_month=31)

    def open_everything(self, user):
        failures, count = [], 0
        for url in sorted(set(routes())):
            client = Client(raise_request_exception=False)       # a crash comes back as a 500 answer instead of stopping the test
            if user is not None:
                client.force_login(user)
            response = client.get(url)
            count += 1
            if response.status_code >= 500:
                failures.append(f'{response.status_code}  {url}')
        return failures, count

    def check(self, label, user):
        failures, count = self.open_everything(user)
        self.assertGreater(count, 20, 'the list of pages looks too short')
        self.assertEqual(failures, [], f'\n{label}: {len(failures)} of {count} page(s) crashed:\n  ' + '\n  '.join(failures[:60]))

    def test_every_page_for_a_company_owner(self):
        self.check('company owner', self.owner)

    def test_every_page_for_a_platform_admin(self):
        self.check('platform admin', self.admin)

    def test_every_page_for_an_employee(self):
        self.check('employee', self.employee_user)

    def test_every_page_for_someone_not_logged_in(self):
        self.check('not logged in', None)
