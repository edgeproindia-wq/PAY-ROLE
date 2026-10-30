"""Financial year 2026-27 has its own income-tax rules (same numbers as 2025-26) and no longer falls back with a warning."""
from decimal import Decimal as D

from django.test import SimpleTestCase

from .payroll_engine import CURRENT_TAX_YEAR, annual_tax, monthly_tds, parse_month, tax_year_for_period


class FY202627Tests(SimpleTestCase):
    def test_2026_27_has_its_own_rules_and_no_fallback_warning(self):
        tax_year_for_period.__dict__.pop('_warned', None)
        with self.assertNoLogs('payroll_app.payroll_engine', level='WARNING'):
            self.assertEqual(tax_year_for_period(parse_month('October 2026')), '2026-27')
            self.assertEqual(tax_year_for_period(parse_month('March 2027')), '2026-27')
        self.assertEqual(tax_year_for_period(parse_month('March 2026')), '2025-26')
        self.assertEqual(CURRENT_TAX_YEAR, '2026-27')

    def test_the_numbers_match_a_hand_calculation(self):
        # taxable = gross - 75,000; 5% on 4-8 lakh, 10% on 8-12 lakh, 15% on 12-16 lakh; no tax up to 12 lakh taxable
        # (section 87A) with marginal relief just above it; 4% cess.
        for gross, tax in ((500000, 0), (1275000, 0), (1300000, 26000), (1500000, 97500), (2400000, 292500)):
            self.assertEqual(annual_tax(D(gross), '2026-27'), D(tax), gross)
        self.assertEqual(monthly_tds(D(1560000), '2026-27'), D('8905.00'))
