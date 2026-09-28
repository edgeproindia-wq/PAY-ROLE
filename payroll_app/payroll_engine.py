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


def monthly_tds(annual_gross):
    """Same slab logic as the existing Income Tax page (views.income_tax)."""
    if annual_gross <= 300000:
        tds_annual = Decimal('0')
    elif annual_gross <= 700000:
        tds_annual = (annual_gross - Decimal('300000')) * Decimal('0.05')
    else:
        tds_annual = Decimal('400000') * Decimal('0.05') + (annual_gross - Decimal('700000')) * Decimal('0.10')
    return q(tds_annual / 12)


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
    if period:
        start, end = period
        days_in_month = (end - start).days + 1
        att = Attendance.objects.filter(employee=employee, date__range=(start, end))
        lop_days = Decimal(att.filter(status='ABSENT').count()) + Decimal(att.filter(status='HALF_DAY').count()) * Decimal('0.5')
        lop_days = min(lop_days, Decimal(days_in_month))

    lop_amount = q(gross / days_in_month * lop_days) if days_in_month and lop_days else Decimal('0.00')
    pay_factor = (Decimal(days_in_month) - lop_days) / days_in_month if days_in_month else Decimal('1')
    earned_basic = q(ss.basic * pay_factor)
    earned_gross = gross - lop_amount

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

    total_earnings = q(earned_gross + arrears_total + reimb_total)
    total_deductions = q(pf + esi + tds)
    net = q(max(total_earnings - total_deductions, Decimal('0')))

    return {
        'payslip_number': f"PS-{run.pk:05d}-{employee.employee_code}",
        'basic': q(ss.basic), 'hra': q(ss.hra), 'conveyance': q(ss.conveyance),
        'special_allowance': q(ss.special_allowance), 'gross_salary': gross,
        'days_in_month': days_in_month, 'lop_days': lop_days, 'lop_amount': lop_amount,
        'arrears': arrears_total, 'reimbursements': reimb_total,
        'total_earnings': total_earnings, 'pf': pf, 'esi': esi, 'tds': tds,
        'total_deductions': total_deductions, 'net_pay': net,
    }


@transaction.atomic
def build_run_lines(run, employees):
    """Create one line per employee (idempotent: an existing line for the same
    employee in this run is replaced, never duplicated). Returns (created, skipped)."""
    period = parse_month(run.month)
    created, skipped = 0, []
    for emp in employees:
        values = calculate_line(run, emp, period)
        if values is None:
            skipped.append(emp)
            continue
        PayrollRunLine.objects.update_or_create(payroll_run=run, employee=emp, defaults=values)
        created += 1
    return created, skipped


@transaction.atomic
def release_claims(run, employees):
    """Before reprocessing, give back the arrears/reimbursements this run had
    claimed so they are recalculated instead of lost."""
    ArrearsRecord.objects.filter(paid_in_run=run, employee__in=employees).update(paid_in_run=None)
    Reimbursement.objects.filter(paid_in_run=run, employee__in=employees).update(paid_in_run=None)
