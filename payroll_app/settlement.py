"""Full & Final settlement helpers.

The pending salary suggestion follows the same rules as payroll: salary is spread over the calendar
days of the month, days before joining are not paid, absent days, half days and approved unpaid leave
are loss of pay."""
import calendar
from decimal import Decimal


def suggest_pending_salary(employee, last_working_day):
    """Return (amount, note): salary for the last month, up to and including the last working day."""
    from .models import Attendance, PayrollRunLine, SalaryStructure
    from .payroll_engine import q
    from .phase5 import unpaid_leave_days

    start = last_working_day.replace(day=1)
    days_in_month = calendar.monthrange(start.year, start.month)[1]
    label = start.strftime('%B %Y')
    if PayrollRunLine.objects.filter(employee=employee, payroll_run__month__iexact=label).exists():
        return Decimal('0.00'), f'{label} is already in a payroll run, so no pending salary was added.'
    try:
        structure = employee.salary_structure
    except SalaryStructure.DoesNotExist:
        return Decimal('0.00'), 'This employee has no salary structure, so pending salary is 0. Enter it by hand if needed.'
    first = max(start, employee.date_of_joining) if employee.date_of_joining else start
    if last_working_day < first:
        return Decimal('0.00'), 'The last working day is before the joining date.'
    days = (last_working_day - first).days + 1
    records = Attendance.objects.filter(employee=employee, date__range=(first, last_working_day))
    lop = Decimal(records.filter(status='ABSENT').count()) + Decimal(records.filter(status='HALF_DAY').count()) * Decimal('0.5')
    marked = set(records.filter(status__in=['ABSENT', 'HALF_DAY']).values_list('date', flat=True))
    lop += unpaid_leave_days(employee, first, last_working_day, marked)
    paid_days = max(Decimal(days) - lop, Decimal('0'))
    amount = q(q(structure.gross_salary) / days_in_month * paid_days)
    return amount, f'Calculated from {days} day(s) in {label} less {lop} loss-of-pay day(s).'