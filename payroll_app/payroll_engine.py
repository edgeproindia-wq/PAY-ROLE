"""Payroll line calculation.

Before this module existed, every run stored net_pay = gross_salary: no PF,
ESI, TDS, loss-of-pay, arrears or reimbursements were ever applied, even
though the Statutory Compliance / Income Tax pages computed them. The rates
and slabs below are exactly the ones those existing pages already use, so the
payslip now matches the compliance reports.

Loss of pay: ABSENT attendance = 1 day, HALF_DAY = 0.5 day, within the run's
month. Approved leave is paid (attendance status LEAVE is not LOP).
"""
import calendar
import datetime
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from .models import (
    ArrearsRecord, Attendance, CompanySettings, PayrollRunLine, Reimbursement, SalaryStructure,
)

TWO = Decimal('0.01')
ESI_GROSS_LIMIT = Decimal('21000')


def q(value):
    return Decimal(value).quantize(TWO, rounding=ROUND_HALF_UP)


def parse_month(label):
    """'August 2026' / 'Aug 2026' / '2026-08' -> (first_day, last_day) or None."""
    label = (label or '').strip()
    for fmt in ('%B %Y', '%b %Y', '%Y-%m', '%m-%Y', '%m/%Y'):
        try:
            d = datetime.datetime.strptime(label, fmt).date()
            last = calendar.monthrange(d.year, d.month)[1]
            return d.replace(day=1), d.replace(day=last)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Income tax (TDS) - NEW REGIME, FY 2025-26 (Finance Act 2025).
# CONFIRM WITH YOUR CA before production use, and review every budget year:
# only this block needs changing when the rules change.
# ---------------------------------------------------------------------------
TAX_RULES = {
    '2025-26': {
        'standard_deduction': Decimal('75000'),
        'slabs': [(Decimal('400000'), Decimal('0')), (Decimal('800000'), Decimal('0.05')),
                  (Decimal('1200000'), Decimal('0.10')), (Decimal('1600000'), Decimal('0.15')),
                  (Decimal('2000000'), Decimal('0.20')), (Decimal('2400000'), Decimal('0.25')),
                  (None, Decimal('0.30'))],
        'rebate_limit': Decimal('1200000'),   # section 87A: no tax up to this taxable income
        'cess': Decimal('0.04'),
    },
}
CURRENT_TAX_YEAR = '2025-26'


def annual_tax(annual_gross, tax_year=CURRENT_TAX_YEAR):
    """Annual income tax incl. cess on salary income under the new regime."""
    rules = TAX_RULES[tax_year]
    taxable = max(Decimal(annual_gross) - rules['standard_deduction'], Decimal('0'))
    tax, lower = Decimal('0'), Decimal('0')
    for upper, rate in rules['slabs']:
        top = taxable if upper is None else min(taxable, upper)
        if top > lower:
            tax += (top - lower) * rate
        if upper is None or taxable <= upper:
            break
        lower = upper
    if taxable <= rules['rebate_limit']:
        tax = Decimal('0')                                   # 87A rebate
    else:
        tax = min(tax, taxable - rules['rebate_limit'])      # marginal relief just above the limit
    return q(tax * (1 + rules['cess']))


def monthly_tds(annual_gross, tax_year=CURRENT_TAX_YEAR):
    """Monthly TDS = annual tax / 12. Used by payroll AND the Income Tax page."""
    return q(annual_tax(annual_gross, tax_year) / 12)


def _company_rates(company):
    cs = CompanySettings.objects.filter(company=company).first() if company else None
    pf_pct = Decimal(cs.pf_percentage) if cs else Decimal('12')
    esi_pct = Decimal(cs.esi_percentage) if cs else Decimal('0.75')
    return pf_pct / 100, esi_pct / 100


def calculate_line(run, employee, period):
    """Return a dict of PayrollRunLine field values, or None when the employee
    has no salary structure (they are skipped, as before)."""
    try:
        ss = employee.salary_structure
    except SalaryStructure.DoesNotExist:
        return None

    pf_rate, esi_rate = _company_rates(employee.company)
    gross = q(ss.gross_salary)

    days_in_month, lop_days = 0, Decimal('0')
    cycle_values = None
    if period:
        from .pay_cycle_calc import cycle_basis
        cycle_values = cycle_basis(run, employee, period)     # None unless the run's pay cycle uses working days
    if period and cycle_values is not None:
        start, end = period
        doj = employee.date_of_joining
        if doj and doj > end:
            return None
        days_in_month, not_joined_days, lop_days = cycle_values
    elif period:
        start, end = period
        days_in_month = (end - start).days + 1
        att = Attendance.objects.filter(employee=employee, date__range=(start, end))
        doj = employee.date_of_joining
        if doj and doj > end:
            return None                              # BUG-06 safety net: joins after this month
        # BUG-05: days of the month before the joining date are not paid. They are kept
        # separate from LOP (absences) so nothing is counted twice.
        not_joined_days = Decimal(max((doj - start).days, 0)) if doj else Decimal('0')
        lop_days = Decimal(att.filter(status='ABSENT').count()) + Decimal(att.filter(status='HALF_DAY').count()) * Decimal('0.5')
        # Approved UNPAID leave is loss of pay too (days already marked ABSENT are not counted twice).
        from .phase5 import unpaid_leave_days
        absent_dates = set(att.filter(status__in=['ABSENT', 'HALF_DAY']).values_list('date', flat=True))
        lop_days += unpaid_leave_days(employee, max(start, doj) if doj else start, end, absent_dates)
        lop_days = min(lop_days, Decimal(days_in_month) - not_joined_days)
    else:
        not_joined_days = Decimal('0')

    lop_amount = q(gross / days_in_month * lop_days) if days_in_month and lop_days else Decimal('0.00')
    not_joined_amount = q(gross / days_in_month * not_joined_days) if days_in_month and not_joined_days else Decimal('0.00')
    pay_factor = (Decimal(days_in_month) - lop_days - not_joined_days) / days_in_month if days_in_month else Decimal('1')
    earned_basic = q(ss.basic * pay_factor)
    earned_gross = gross - lop_amount - not_joined_amount

    # Claim unpaid arrears / approved reimbursements for THIS run only, so they
    # can never be paid twice across runs.
    arrears_qs = ArrearsRecord.objects.filter(employee=employee, paid_in_run__isnull=True)
    arrears_total = q(sum((a.total_arrears for a in arrears_qs), Decimal('0')))
    arrears_qs.update(paid_in_run=run)

    reimb_qs = Reimbursement.objects.filter(employee=employee, status='APPROVED', paid_in_run__isnull=True)
    reimb_total = q(sum((r.amount for r in reimb_qs), Decimal('0')))
    reimb_qs.update(paid_in_run=run)

    pf = q(earned_basic * pf_rate)
    esi = q(earned_gross * esi_rate) if gross <= ESI_GROSS_LIMIT else Decimal('0.00')
    tds = monthly_tds(gross * 12)
    # Optional deductions: all 0 unless HR configured PT slabs, loans or insurance.
    from .payroll_extras import insurance_deduction, loan_deductions, professional_tax_for
    pt = professional_tax_for(employee.company, earned_gross, period)
    loans = loan_deductions(employee, run, period)
    loan_total = q(sum((amount for _, amount in loans), Decimal('0')))
    insurance = insurance_deduction(employee, period)
    # Other recurring allowances / deductions set by HR (0 unless configured).
    from .phase4 import component_totals
    other_earn, other_taxable, other_ded, _ = component_totals(employee, period, pay_factor)
    if other_taxable:
        tds = monthly_tds((gross + other_taxable) * 12)

    total_earnings = q(earned_gross + arrears_total + reimb_total + other_earn)
    total_deductions = q(pf + esi + tds + pt + loan_total + insurance + other_ded)
    net = q(max(total_earnings - total_deductions, Decimal('0')))

    return {
        'payslip_number': f"PS-{run.pk:05d}-{employee.employee_code}",
        'basic': q(ss.basic), 'hra': q(ss.hra), 'conveyance': q(ss.conveyance),
        'special_allowance': q(ss.special_allowance), 'gross_salary': gross,
        'days_in_month': days_in_month, 'lop_days': lop_days, 'lop_amount': lop_amount,
        'arrears': arrears_total, 'reimbursements': reimb_total,
        'total_earnings': total_earnings, 'pf': pf, 'esi': esi, 'tds': tds,
        'professional_tax': pt, 'loan_deduction': loan_total, 'insurance_deduction': insurance,
        'other_earnings': other_earn, 'other_deductions': other_ded,
        'total_deductions': total_deductions, 'net_pay': net,
        '_loans': loans,
    }


@transaction.atomic
def build_run_lines(run, employees):
    """Create one line per employee (idempotent: an existing line for the same
    employee in this run is replaced, never duplicated). Returns (created, skipped)."""
    from .pay_cycle_calc import period_for_run
    period = period_for_run(run)          # the pay cycle's own dates, or the month label for runs without one
    created, skipped = 0, []
    for emp in employees:
        values = calculate_line(run, emp, period)
        if values is None:
            skipped.append(emp)
            continue
        loans = values.pop('_loans', [])
        line, _ = PayrollRunLine.objects.update_or_create(payroll_run=run, employee=emp, defaults=values)
        from .payroll_extras import record_loan_repayments
        record_loan_repayments(line, loans)
        created += 1
    return created, skipped


@transaction.atomic
def release_claims(run, employees):
    """Before reprocessing, give back the arrears/reimbursements this run had
    claimed so they are recalculated instead of lost."""
    ArrearsRecord.objects.filter(paid_in_run=run, employee__in=employees).update(paid_in_run=None)
    Reimbursement.objects.filter(paid_in_run=run, employee__in=employees).update(paid_in_run=None)
