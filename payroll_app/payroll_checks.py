"""Checks shown when a payroll run is validated.

An ERROR stops the run from moving to Validated until it is fixed. A WARNING is shown but does not stop it.
Nothing here changes any figure: it only reads the run and the data behind it.
"""
from collections import Counter, namedtuple
from decimal import Decimal

from .models import Attendance, Employee, LeaveRequest, PayrollRun
from .payroll_engine import parse_month

Issue = namedtuple('Issue', 'level code message')
ERROR, WARNING = 'ERROR', 'WARNING'
VARIANCE_LIMIT = Decimal('0.25')            # warn when the net payout moves by more than 25% from the previous run


def _codes(employees, limit=10):
    codes = [e.employee_code for e in employees]
    return ', '.join(codes[:limit]) + (f' and {len(codes) - limit} more' if len(codes) > limit else '')


def run_checks(run):
    issues = []
    lines = list(run.lines.select_related('employee'))
    people = [l.employee for l in lines]

    for l in lines:                                                     # the engine shows a negative net pay as 0, so look at the sums
        if l.total_deductions > l.total_earnings:
            issues.append(Issue(ERROR, 'NEGATIVE_NET',
                                f'{l.employee.full_name} ({l.employee.employee_code}): deductions Rs {l.total_deductions:,.2f} are more '
                                f'than earnings Rs {l.total_earnings:,.2f}. Fix the deductions (loan, pay items), then reprocess.'))

    no_bank = [e for e in people if not (e.bank_account_no and e.ifsc_code)]
    if no_bank:
        issues.append(Issue(WARNING, 'NO_BANK', f'{len(no_bank)} employee(s) have no complete bank details and cannot be paid by bank transfer: {_codes(no_bank)}.'))

    accounts = Counter(e.bank_account_no.strip() for e in people if e.bank_account_no)
    shared = {a: n for a, n in accounts.items() if n > 1}
    if shared:
        issues.append(Issue(WARNING, 'SHARED_ACCOUNT', 'The same bank account is used by more than one employee (account ending '
                                                        + ', '.join(a[-4:] for a in shared) + '). Check for a duplicate payment.'))

    no_pan = [e for e in people if not (e.pan_number or '').strip()]
    if no_pan:
        issues.append(Issue(WARNING, 'NO_PAN', f'{len(no_pan)} employee(s) have no PAN, which TDS needs: {_codes(no_pan)}.'))

    period = parse_month(run.month)
    if run.company_id:
        in_run = {e.pk for e in people}
        missing = Employee.objects.filter(company=run.company, employment_status='ACTIVE').exclude(pk__in=in_run)
        if period:
            missing = missing.filter(date_of_joining__lte=period[1]) | missing.filter(date_of_joining__isnull=True)
        missing = list(missing.distinct())
        if missing:
            issues.append(Issue(WARNING, 'NOT_IN_RUN', f'{len(missing)} active employee(s) are not in this run (usually no salary structure): {_codes(missing)}.'))

        if period and Attendance.objects.filter(employee__company=run.company, date__range=period).exists():
            marked = set(Attendance.objects.filter(employee__in=in_run, date__range=period).values_list('employee_id', flat=True))
            on_leave = set(LeaveRequest.objects.filter(employee__in=in_run, status='APPROVED', from_date__lte=period[1],
                                                       to_date__gte=period[0]).values_list('employee_id', flat=True))
            blank = [e for e in people if e.pk not in marked and e.pk not in on_leave]
            if blank:
                issues.append(Issue(WARNING, 'NO_ATTENDANCE', f'No attendance or leave recorded this month for {len(blank)} employee(s), '
                                                               f'so they are paid the full month: {_codes(blank)}.'))

        previous = (PayrollRun.objects.filter(company=run.company, pk__lt=run.pk, lines__isnull=False).distinct().order_by('-pk').first())
        if previous:
            now_total = sum((l.net_pay for l in lines), Decimal('0'))
            before = sum((l.net_pay for l in previous.lines.all()), Decimal('0'))
            if before and abs(now_total - before) / before > VARIANCE_LIMIT:
                change = (now_total - before) / before * 100
                issues.append(Issue(WARNING, 'VARIANCE', f'Net payout is {abs(change):.0f}% {"higher" if change > 0 else "lower"} than {previous.month} '
                                                          f'(Rs {now_total:,.2f} against Rs {before:,.2f}). Check that this is expected.'))
    return issues
