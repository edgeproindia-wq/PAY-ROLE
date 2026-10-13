"""Optional payroll deductions: professional tax, loan EMIs and insurance premiums.
Every one of them is 0 unless the company has configured it, so existing payroll
results do not change until HR adds PT slabs, loans or insurance policies."""
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Q


def q(value):
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def professional_tax_for(company, monthly_gross, period):
    from .models_features import ProfessionalTaxSlab
    if company is None:
        return Decimal('0.00')
    slab = (ProfessionalTaxSlab.objects.filter(company=company, min_monthly_gross__lte=monthly_gross)
            .filter(Q(max_monthly_gross__isnull=True) | Q(max_monthly_gross__gte=monthly_gross))
            .order_by('-min_monthly_gross').first())
    if slab is None:
        return Decimal('0.00')
    if period and period[0].month == 2 and slab.february_amount is not None:
        return q(slab.february_amount)
    return q(slab.monthly_amount)


def _released_recovered(loan, run):
    """Amount of this loan already taken in RELEASED payroll runs (this run excluded)."""
    from django.db.models import Sum
    from .models_features import LoanRepayment
    total = (LoanRepayment.objects.filter(loan=loan, line__payroll_run__status='RELEASED')
             .exclude(line__payroll_run=run).aggregate(s=Sum('amount'))['s'])
    return total or Decimal('0')


def loan_deductions(employee, run, period):
    """[(loan, amount)] - the EMI, capped at what is still to be recovered. Amounts
    already taken in OTHER runs count; this run's own earlier draft does not."""
    from .models_features import Loan
    if not period:
        return []
    result = []
    for loan in Loan.objects.filter(employee=employee, status='ACTIVE', start_date__lte=period[1]):
        remaining = loan.loan_amount - loan.recovered(exclude_run=run)
        if remaining <= 0:
            # Nothing more to deduct. Close the loan only when RELEASED payroll has recovered all of it: if the EMIs that
            # cover it are still in draft runs, those drafts may be rejected or reprocessed, and the loan must then deduct again.
            if _released_recovered(loan, run) >= loan.loan_amount:
                loan.status = 'CLOSED'
                loan.save(update_fields=['status'])
            continue
        amount = q(min(loan.emi_amount, max(remaining, Decimal('0'))))
        if amount > 0:
            result.append((loan, amount))
    return result


def insurance_deduction(employee, period):
    from .models_features import InsurancePolicy
    if not period:
        return Decimal('0.00')
    start, end = period
    policies = (InsurancePolicy.objects.filter(employee=employee, status='ACTIVE', effective_from__lte=end)
                .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=start)))
    return q(sum((p.employee_premium_monthly for p in policies), Decimal('0')))


def record_loan_repayments(line, loans):
    from .models_features import LoanRepayment
    LoanRepayment.objects.filter(line=line).delete()      # reprocessing a draft replaces its EMIs
    for loan, amount in loans:
        LoanRepayment.objects.create(loan=loan, line=line, amount=amount)