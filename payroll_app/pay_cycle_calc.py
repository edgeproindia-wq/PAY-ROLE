"""Working-day arithmetic for pay cycles. Nothing here hard-codes a number of working days:
every count comes from the Working Calendar that applies to the employee and the pay cycle."""
import datetime
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

DEFAULT_WEEKLY_OFFS = frozenset({6})            # used only when a company has not set up a calendar: Sunday off
HOLIDAY_TYPES = ('PUBLIC_HOLIDAY', 'COMPANY_HOLIDAY')


def cycle_for_run(run):
    try:
        return run.pay_cycle
    except ObjectDoesNotExist:
        return None


def period_for_run(run):
    """(first_day, last_day) of a payroll run: from its pay cycle when it has one, else from its month label."""
    cycle = cycle_for_run(run)
    if cycle is not None:
        return cycle.start_date, cycle.end_date
    from .payroll_engine import parse_month
    return parse_month(run.month)


def _dates(start, end):
    d = start
    while d <= end:
        yield d
        d += datetime.timedelta(days=1)


class CalendarRules:
    """Weekly offs plus the dated exceptions of one calendar, loaded once."""

    def __init__(self, weekly_offs, days):
        self.weekly_offs = set(weekly_offs)
        self.days = dict(days)

    @classmethod
    def load(cls, calendar):
        if calendar is None:
            return cls(DEFAULT_WEEKLY_OFFS, {})
        return cls(calendar.weekly_offs(), {d.date: d.day_type for d in calendar.days.all()})

    def kind(self, d):
        t = self.days.get(d)
        if t in HOLIDAY_TYPES:
            return 'HOLIDAY'
        if t in ('SPECIAL_WORKING', 'HALF_DAY', 'OPTIONAL_HOLIDAY'):
            return 'WORKING'
        return 'WEEKLY_OFF' if d.weekday() in self.weekly_offs else 'WORKING'

    def is_working(self, d):
        return self.kind(d) == 'WORKING'

    def working_dates(self, start, end):
        return [d for d in _dates(start, end) if self.is_working(d)]

    def summary(self, start, end):
        out = {'total_days': (end - start).days + 1, 'working_days': 0, 'weekly_offs': 0, 'holidays': 0,
               'special_working_days': 0, 'half_days': 0, 'optional_holidays': 0}
        for d in _dates(start, end):
            k = self.kind(d)
            out['working_days'] += k == 'WORKING'
            out['weekly_offs'] += k == 'WEEKLY_OFF'
            out['holidays'] += k == 'HOLIDAY'
            t = self.days.get(d)
            out['special_working_days'] += t == 'SPECIAL_WORKING'
            out['half_days'] += t == 'HALF_DAY'
            out['optional_holidays'] += t == 'OPTIONAL_HOLIDAY'
        return out


def calendar_for(employee, cycle):
    """The calendar that applies: the cycle's own, else the employee's, else the company default (else None)."""
    from .pay_cycle_models import WorkingCalendar
    if cycle is not None and cycle.calendar_id:
        return cycle.calendar
    try:
        return employee.calendar_assignment.calendar
    except ObjectDoesNotExist:
        pass
    return WorkingCalendar.objects.filter(company_id=employee.company_id, is_default=True).first()


def rules_for(employee, cycle):
    """CalendarRules for this employee, cached on the cycle object for the duration of a run."""
    cache = cycle.__dict__.setdefault('_rules_cache', {})
    cal = calendar_for(employee, cycle)
    key = cal.pk if cal else None
    if key not in cache:
        cache[key] = CalendarRules.load(cal)
    return cache[key]


def cycle_summary(cycle):
    """Calendar facts for the whole cycle using the cycle's calendar or the company default."""
    from .pay_cycle_models import WorkingCalendar
    cal = cycle.calendar or WorkingCalendar.objects.filter(company_id=cycle.company_id, is_default=True).first()
    return CalendarRules.load(cal).summary(cycle.start_date, cycle.end_date)


def _leave_days(employee, start, end, unpaid_only):
    from .models import LeaveRequest
    qs = LeaveRequest.objects.filter(employee=employee, status='APPROVED', from_date__lte=end, to_date__gte=start)
    qs = qs.filter(leave_type='UNPAID') if unpaid_only else qs.exclude(leave_type='UNPAID')
    days = set()
    for lv in qs:
        days.update(_dates(max(lv.from_date, start), min(lv.to_date, end)))
    return days


def _attendance_facts(employee, cycle, period):
    from .models import Attendance
    start, end = period
    rules = rules_for(employee, cycle)
    working = rules.working_dates(start, end)
    working_set = set(working)
    doj = employee.date_of_joining
    att = Attendance.objects.filter(employee=employee, date__range=(start, end))
    rows = list(att.values_list('date', 'status'))
    return rules, working, working_set, doj, rows


def cycle_basis(run, employee, period):
    """(basis_days, not_joined_days, lop_days) for a run whose pay cycle uses working days, else None.

    basis_days  = working days of the period in the employee's calendar
    not_joined  = working days before the joining date
    lop_days    = absences, half days, unpaid leave (and optionally unmarked days) that fall on WORKING days only"""
    cycle = cycle_for_run(run)
    if cycle is None or cycle.salary_policy != 'WORKING_DAYS':
        return None
    start, end = period
    rules, working, working_set, doj, rows = _attendance_facts(employee, cycle, period)
    basis = len(working)
    if basis == 0:
        return None
    not_joined = Decimal(sum(1 for d in working if doj and d < doj))
    lop = Decimal('0')
    marked, absent_dates = set(), set()
    for d, status in rows:
        marked.add(d)
        if status in ('ABSENT', 'HALF_DAY'):
            absent_dates.add(d)
            if d in working_set and not (doj and d < doj):
                lop += Decimal('1') if status == 'ABSENT' else Decimal('0.5')
    for d in _leave_days(employee, start, end, unpaid_only=True):
        if d in working_set and d not in absent_dates and not (doj and d < doj):
            lop += 1
    if cycle.unmarked_as_lop:
        today = timezone.localdate()
        leave_days = _leave_days(employee, start, end, unpaid_only=False) | _leave_days(employee, start, end, unpaid_only=True)
        for d in working:
            if (not doj or d >= doj) and d < today and d not in marked and d not in leave_days:
                lop += 1
    lop = min(lop, Decimal(basis) - not_joined)
    return basis, not_joined, max(lop, Decimal('0'))


def employee_cycle_summary(employee, cycle):
    """Attendance breakdown of one employee for a pay cycle (what payroll looks at)."""
    period = (cycle.start_date, cycle.end_date)
    rules, working, working_set, doj, rows = _attendance_facts(employee, cycle, period)
    start, end = period
    status_by_date = dict(rows)
    on_working = lambda s: sum(1 for d, st in rows if st == s and d in working_set and not (doj and d < doj))
    paid_leave = {d for d in _leave_days(employee, start, end, False) if d in working_set}
    unpaid_leave = {d for d in _leave_days(employee, start, end, True) if d in working_set}
    calendar_counts = rules.summary(start, end)
    basis = cycle_basis_for(employee, cycle)
    return {
        'working_days': calendar_counts['working_days'], 'weekly_offs': calendar_counts['weekly_offs'],
        'holidays': calendar_counts['holidays'], 'calendar_days': calendar_counts['total_days'],
        'present_days': on_working('PRESENT'), 'absent_days': on_working('ABSENT'), 'half_days': on_working('HALF_DAY'),
        'paid_leave_days': len(paid_leave), 'unpaid_leave_days': len(unpaid_leave),
        'lop_days': basis[2] if basis else None,
    }


def cycle_basis_for(employee, cycle):
    """Same as cycle_basis but without needing a run (used for display)."""
    class _Run:                                       # a minimal stand-in carrying only the pay cycle
        pay_cycle = cycle
    return cycle_basis(_Run, employee, (cycle.start_date, cycle.end_date))
