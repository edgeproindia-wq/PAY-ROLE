"""Regression tests for must-fix bugs BUG-02..BUG-07 (proposed)."""
import datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from .models import PayrollRun, PayrollRunLine, SalaryStructure, User
from .tests_audit import PWD, company, employee, user


def structure(emp, basic=20000, hra=8000, conv=1600, special=10400):
    return SalaryStructure.objects.create(employee=emp, basic=basic, hra=hra, conveyance=conv, special_allowance=special)


class MustFixBase(TestCase):
    def setUp(self):
        self.c1, self.c2 = company('Acme'), company('Beta')
        self.admin = User.objects.create_superuser('root', 'root@ex.com', PWD)
        self.owner = user('own', 'COMPANY_OWNER', self.c1)
        self.e1 = employee(self.c1, 'A1', 'a1@ex.com', login='emp1')
        self.b1 = employee(self.c2, 'B1', 'b1@ex.com')
        structure(self.e1)
        structure(self.b1)

    def create_run(self, month):
        self.client.force_login(self.owner)
        self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': month})
        return PayrollRun.objects.get(company=self.c1, month__iexact=month)


class Bug02AdminIsolationTests(MustFixBase):
    def test_admin_blocked_from_company_payroll_pages(self):
        self.client.force_login(self.admin)
        for name in ('employee_master', 'payslips', 'bank_transfer', 'payroll_combined', 'attendance',
                     'salary_structure', 'employee_master_export_csv'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)

    def test_admin_home_goes_to_admin_panel_and_admin_pages_still_work(self):
        self.client.force_login(self.admin)
        self.assertRedirects(self.client.get(reverse('dashboard')), reverse('admin_dashboard'),
                             fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse('admin_dashboard')).status_code, 200)
        self.assertEqual(self.client.get(reverse('admin_demo_requests')).status_code, 200)

    def test_admin_cannot_open_a_payslip_by_id(self):
        run = self.create_run('August 2026')
        for action in ('validate', 'approve', 'release'):
            self.client.post(reverse('payroll_combined'), {'action': action, 'run_id': run.pk})
        run.refresh_from_db()
        self.assertEqual(run.status, 'RELEASED')
        line = run.lines.first()
        self.client.force_login(self.admin)
        self.assertIn(self.client.get(reverse('payslip_pdf', args=[line.pk])).status_code, (403, 404))


class Bug03ChartXssTests(MustFixBase):
    def test_department_name_is_escaped_in_chart_script(self):
        self.e1.department = '</script><script>alert(1)</script>'
        self.e1.save()
        self.client.force_login(self.owner)
        for name in ('dashboard', 'salary_structure'):
            body = self.client.get(reverse(name)).content.decode()
            self.assertNotIn('</script><script>alert(1)', body, name)


class Bug04TdsTests(TestCase):
    """New tax regime, FY 2025-26 (confirm with your CA): standard deduction 75,000;
    slabs 0-4L nil, 4-8L 5%, 8-12L 10%, 12-16L 15%, 16-20L 20%, 20-24L 25%, above 30%;
    87A rebate: no tax up to 12L taxable (with marginal relief); 4% cess."""

    def test_income_up_to_12_75_lakh_gross_has_no_tds(self):
        from .payroll_engine import monthly_tds
        for annual in (240000, 480000, 960000, 1275000):
            self.assertEqual(monthly_tds(Decimal(annual)), Decimal('0.00'), annual)

    def test_15_lakh_gross(self):
        from .payroll_engine import monthly_tds
        # taxable 14,25,000 -> 20,000 + 40,000 + 33,750 = 93,750 + 4% cess = 97,500 / 12
        self.assertEqual(monthly_tds(Decimal(1500000)), Decimal('8125.00'))


class Bug05MidMonthJoinerTests(MustFixBase):
    def test_joiner_is_paid_from_joining_date(self):
        mid = employee(self.c1, 'A3', 'a3@ex.com')
        mid.date_of_joining = datetime.date(2026, 9, 16)
        mid.save()
        structure(mid)
        run = self.create_run('September 2026')
        full = run.lines.get(employee=self.e1)
        part = run.lines.get(employee=mid)
        self.assertEqual(full.total_earnings, Decimal('40000.00'))
        self.assertEqual(part.total_earnings, Decimal('20000.00'))   # 15 of 30 days


class Bug06FutureJoinerTests(MustFixBase):
    def test_employee_joining_after_the_month_is_not_in_the_run(self):
        later = employee(self.c1, 'A5', 'a5@ex.com')
        later.date_of_joining = datetime.date(2026, 11, 1)
        later.save()
        structure(later)
        run = self.create_run('September 2026')
        self.assertFalse(run.lines.filter(employee=later).exists())


class Bug07StorageTests(TestCase):
    FS = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
          'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
    S3 = {'default': {'BACKEND': 'storages.backends.s3.S3Storage'},
          'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}

    def test_render_with_container_disk_is_flagged(self):
        from unittest import mock
        from django.test import override_settings
        from .checks import upload_storage_check
        with mock.patch.dict('os.environ', {'RENDER': 'true'}), override_settings(STORAGES=self.FS):
            self.assertEqual([m.id for m in upload_storage_check(None)], ['payroll.W001'])

    def test_render_with_object_storage_is_clean(self):
        from unittest import mock
        from django.test import override_settings
        from .checks import upload_storage_check
        with mock.patch.dict('os.environ', {'RENDER': 'true'}), override_settings(STORAGES=self.S3):
            self.assertEqual(upload_storage_check(None), [])