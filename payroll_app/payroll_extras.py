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


def loan_deductions(employee, run, period):
    """[(loan, amount)] - the EMI, capped at what is still to be recovered. Amounts
    already taken in OTHER runs count; this run's own earlier draft does not."""
    from .models_features import Loan
    if not period:
        return []
    result = []
    for loan in Loan.objects.filter(employee=employee, status='ACTIVE', start_date__lte=period[1]):
        remaining = loan.loan_amount - loan.recovered(exclude_run=run)
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