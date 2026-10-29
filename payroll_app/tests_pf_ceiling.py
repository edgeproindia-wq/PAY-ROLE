"""Provident fund is capped at the statutory wage ceiling: Rs 15,000 until 16 Sep 2026, Rs 25,000 from 17 Sep 2026,
with September 2026 split by days. The payslip, the compliance page and the settings page all follow the same rule."""
import datetime
from decimal import Decimal as D

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Attendance, CompanySettings, PayrollRun, SalaryStructure
from .payroll_engine import calculate_line, parse_month, pf_ceiling_on, pf_wage_for_month, q
from .tests import make_company, make_employee, make_user

DATE = datetime.date


class CeilingTests(SimpleTestCase):
    def test_the_ceiling_changes_on_17_september_2026(self):
        self.assertEqual(pf_ceiling_on(DATE(2025, 1, 1)), D('15000'))
        self.assertEqual(pf_ceiling_on(DATE(2026, 9, 16)), D('15000'))
        self.assertEqual(pf_ceiling_on(DATE(2026, 9, 17)), D('25000'))
        self.assertEqual(pf_ceiling_on(DATE(2027, 3, 31)), D('25000'))

    def test_pf_wages_for_a_full_month(self):
        aug = (DATE(2026, 8, 1), DATE(2026, 8, 31))
        sep = (DATE(2026, 9, 1), DATE(2026, 9, 30))
        octo = (DATE(2026, 10, 1), DATE(2026, 10, 31))
        self.assertEqual(pf_wage_for_month(12000, *aug), D('12000'))                    # under every ceiling: the basic itself
        self.assertEqual(pf_wage_for_month(30000, *aug), D('15000'))                    # old ceiling
        self.assertEqual(pf_wage_for_month(30000, *octo), D('25000'))                   # new ceiling
        self.assertEqual(q(pf_wage_for_month(30000, *sep)), q(D('19666.67')))           # (15,000 x 16 + 25,000 x 14) / 30
        self.assertEqual(q(pf_wage_for_month(20000, *sep)), q(D('17333.33')))           # (15,000 x 16 + 20,000 x 14) / 30


class PayslipTests(TestCase):
    def line(self, month, basic, hra=0, conv=0, spec=0, absent=0, code='P1'):
        co = make_company('PF Co ' + code)
        emp = make_employee(co, code, code.lower() + '@ex.com')
        emp.date_of_joining = DATE(2024, 1, 1)
        emp.save()
        SalaryStructure.objects.create(employee=emp, basic=basic, hra=hra, conveyance=conv, special_allowance=spec)
        first, last = parse_month(month)
        for d in range(absent):
            Attendance.objects.create(employee=emp, date=first + datetime.timedelta(days=d), status='ABSENT')
        return calculate_line(PayrollRun.objects.create(month=month, company=co), emp, (first, last))

    def test_basic_above_the_new_ceiling_is_capped_at_25000(self):
        r = self.line('October 2026', 30000, 12000, 1600, 6400)
        self.assertEqual((r['pf'], r['net_pay']), (D('3000.00'), D('47000.00')))         # 12% of 25,000, not of 30,000

    def test_basic_under_the_ceiling_is_unchanged(self):
        self.assertEqual(self.line('October 2026', 12000, 4800, 0, 3200, code='P2')['pf'], D('1440.00'))

    def test_august_uses_the_old_ceiling(self):
        self.assertEqual(self.line('August 2026', 30000, 12000, 1600, 6400, code='P3')['pf'], D('1800.00'))   # 12% of 15,000

    def test_september_2026_is_split_by_days(self):
        self.assertEqual(self.line('September 2026', 30000, 12000, 1600, 6400, code='P4')['pf'], D('2360.00'))  # 12% of 19,666.67

    def test_loss_of_pay_reduces_the_capped_wages(self):
        r = self.line('October 2026', 30000, 12000, 1600, 6400, absent=2, code='P5')
        self.assertEqual(r['pf'], D('2806.45'))                                         # 12% x 25,000 x 29/31


class CompliancePageAndSettingsTests(TestCase):
    def setUp(self):
        self.co = make_company('PF Page Co')
        self.owner = make_user('pf_owner', 'COMPANY_OWNER', self.co)
        emp = make_employee(self.co, 'PF01', 'pf1@ex.com')
        SalaryStructure.objects.create(employee=emp, basic=30000, hra=12000, conveyance=1600, special_allowance=6400)
        self.client.force_login(self.owner)

    def test_the_compliance_page_shows_the_same_capped_pf_as_the_payslip(self):
        today = timezone.localdate()
        first = today.replace(day=1)
        last = (first + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)
        expected = q(pf_wage_for_month(30000, first, last) * D('0.12'))
        page = self.client.get(reverse('statutory_compliance'))
        self.assertContains(page, '&#8377;%s' % expected)
        self.assertContains(page, 'statutory wage ceiling')

    def test_the_settings_page_shows_the_ceiling_but_it_cannot_be_changed(self):
        settings_row, _ = CompanySettings.objects.get_or_create(company=self.co, defaults={'company_name': 'PF Page Co'})
        before = settings_row.pf_wage_ceiling
        page = self.client.get(reverse('settings'))
        self.assertContains(page, 'Set by law, not editable')
        data = {f: getattr(settings_row, f) for f in ('company_name', 'pan_number', 'gst_number', 'address')}
        data.update({'pf_percentage': '12.00', 'esi_percentage': '0.75', 'pf_wage_ceiling': '99999.00',
                     'casual_leave_days': 12, 'sick_leave_days': 8, 'earned_leave_days': 15})
        self.client.post(reverse('settings'), data)
        settings_row.refresh_from_db()
        self.assertEqual(settings_row.pf_wage_ceiling, before)                           # the posted value is ignored
