"""Extra panels for the company owner's dashboard: setup progress, things that need attention, the payroll month tracker and upcoming events.

Everything here is read-only and scoped to the owner's own company. Each panel is worked out on its own: if one of them ever fails, the others and the
rest of the dashboard still show (the error is logged), so an extra panel can never take the dashboard down.
"""
import datetime
import logging

from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

logger = logging.getLogger(__name__)
EVENT_DAYS = 14
HOLIDAY_DAYS = 30
DOCUMENT_DAYS = 30


def _href(name):
    try:
        return reverse(name)
    except NoReverseMatch:
        return ''


def _safe(label, default, fn, *args):
    try:
        return fn(*args)
    except Exception:                                    # an extra panel must never break the dashboard
        logger.exception('Dashboard panel "%s" failed', label)
        return default


def _next_occurrence(born, today):
    """The next date (today or later) on which this day and month fall; 29 February falls on 28 February in a year without one."""
    for year in (today.year, today.year + 1):
        try:
            when = born.replace(year=year)
        except ValueError:
            when = datetime.date(year, 2, 28)
        if when >= today:
            return when
    return None


def _setup_progress(company, employees, runs):
    from .models import CompanySettings
    from .pay_cycle_models import PayCycle, WorkingCalendar
    active = employees.filter(employment_status='ACTIVE')
    n = active.count()
    missing_structure = active.filter(salary_structure__isnull=True).count() if n else 0
    missing_bank = active.filter(Q(bank_account_no='') | Q(ifsc_code='') | Q(bank_status='INACTIVE')).count() if n else 0
    cs = CompanySettings.objects.filter(company=company).first() if company else None
    steps = [
        ('Company details and PAN', bool(cs and cs.pan_number), '', 'settings'),
        ('Pay cycle created', PayCycle.objects.filter(company=company).exists(), '', 'pay_cycles'),
        ('Working calendar and holidays', WorkingCalendar.objects.filter(company=company).exists(), '', 'working_calendar'),
        ('Employees added', n > 0, '%d active' % n if n else '', 'employee_master'),
        ('Salary structure for every employee', n > 0 and missing_structure == 0, '%d of %d done' % (n - missing_structure, n) if n else '', 'salary_structure'),
        ('Bank details for every employee', n > 0 and missing_bank == 0, '%d of %d done' % (n - missing_bank, n) if n else '', 'bank_validation'),
        ('First payroll run', runs.exists(), '', 'payroll_processing'),
    ]
    done = sum(1 for s in steps if s[1])
    return {'steps': [{'label': l, 'done': d, 'detail': det, 'href': _href(u)} for l, d, det, u in steps], 'done': done, 'total': len(steps), 'pct': round(done * 100 / len(steps))}


def _attention(company, employees, runs, ctx, today):
    """'legacy' = the four approval counts the dashboard always showed (always listed, even at zero); 'alerts' = the new things that need attention (listed only when above zero)."""
    from .bank_batch_models import BankBatch
    active = employees.filter(employment_status='ACTIVE')
    legacy = [
        ('Pending Leave Requests', ctx.get('pending_leave', 0), 'leave_management'),
        ('Pending Reimbursements', ctx.get('pending_reimbursements', 0), 'reimbursement'),
        ('Unverified Investment Declarations', ctx.get('pending_investment_declarations', 0), 'investment_declaration'),
        ('Payroll Runs Awaiting Approval', ctx.get('pending_approvals', 0), 'payroll_processing'),
    ]
    alerts = [
        ('Failed salary payments', ctx.get('failed_payments', 0), 'danger', 'failed_transaction_report', 'Retry or correct them'),
        ('Active employees without a salary structure', active.filter(salary_structure__isnull=True).count(), 'danger', 'salary_structure', 'Needed before payroll'),
        ('Bank payment batches waiting for approval', BankBatch.objects.filter(company=company, status=BankBatch.PENDING_APPROVAL).count(), 'warn', 'bank_approvals', 'Approve the batch'),
        ('Active employees with incomplete bank details', active.filter(Q(bank_account_no='') | Q(ifsc_code='') | Q(bank_status='INACTIVE')).count(), 'warn', 'bank_validation', 'Needed for bank payment'),
        ('Draft payroll runs not yet validated', runs.filter(status='DRAFT').count(), 'info', 'payroll_processing', 'Finish and validate'),
        ('Attendance not marked today', ctx.get('not_marked_today', 0), 'info', 'attendance', 'Mark attendance'),
    ]
    try:
        from .models_documents import EmployeeDocument
        soon = EmployeeDocument.objects.filter(company=company, expiry_date__isnull=False, expiry_date__lte=today + datetime.timedelta(days=DOCUMENT_DAYS)).count()
        alerts.append(('Employee documents expired or expiring within %d days' % DOCUMENT_DAYS, soon, 'warn', 'hr_documents', 'Renew or replace them'))
    except ImportError:
        pass
    order = {'danger': 0, 'warn': 1, 'info': 2}
    shown = sorted(({'label': l, 'count': c, 'level': lv, 'href': _href(u), 'hint': h} for l, c, lv, u, h in alerts if c), key=lambda i: order[i['level']])
    rows = [{'label': l, 'count': c, 'href': _href(u)} for l, c, u in legacy]
    return {'legacy': rows, 'alerts': shown, 'total': sum(r['count'] for r in rows) + sum(i['count'] for i in shown)}


def _tracker(company, employees, runs, today):
    from .bank_batch_models import BankBatch
    from .models import Attendance
    from .pay_cycle_models import PayCycle
    label = today.strftime('%B %Y')
    run = runs.filter(month=label).first()
    rank = {'DRAFT': 1, 'VALIDATED': 2, 'APPROVED': 3, 'RELEASED': 4}.get(run.status, 0) if run else 0
    attendance = Attendance.objects.filter(employee__in=employees.filter(employment_status='ACTIVE'), date__year=today.year, date__month=today.month).exists()
    bank = bool(run) and BankBatch.objects.filter(company=company, payroll_run=run, status__in=[BankBatch.SENT, BankBatch.COMPLETED]).exists()
    steps = [('Attendance marked', attendance, 'attendance'), ('Payroll run created', rank >= 1, 'payroll_processing'), ('Validated', rank >= 2, 'payroll_validation'),
             ('Approved', rank >= 3, 'payroll_approval'), ('Released', rank >= 4, 'payroll_release'), ('Bank payment sent', bank, 'bank_batches')]
    current = next((i for i, s in enumerate(steps) if not s[1]), None)
    cycle = PayCycle.objects.filter(company=company, payroll_month=label).first()
    return {'month': label, 'run': run, 'pay_date': cycle.pay_date if cycle else None,
            'steps': [{'label': l, 'state': 'done' if d else ('current' if i == current else 'todo'), 'href': _href(u)} for i, (l, d, u) in enumerate(steps)],
            'complete': current is None}


def _events(company, employees, today):
    from .pay_cycle_models import CalendarDay
    out = []
    horizon = today + datetime.timedelta(days=EVENT_DAYS)
    for e in employees.filter(employment_status='ACTIVE'):
        if e.date_of_birth:
            when = _next_occurrence(e.date_of_birth, today)
            if when and when <= horizon:
                out.append({'date': when, 'kind': 'birthday', 'title': e.full_name, 'text': 'Birthday'})
        if e.date_of_joining:
            when = _next_occurrence(e.date_of_joining, today)
            years = when.year - e.date_of_joining.year if when else 0
            if when and when <= horizon and years >= 1:
                out.append({'date': when, 'kind': 'anniversary', 'title': e.full_name, 'text': '%d year%s with the company' % (years, '' if years == 1 else 's')})
    for e in employees.filter(date_of_joining__gt=today, date_of_joining__lte=horizon):
        out.append({'date': e.date_of_joining, 'kind': 'joining', 'title': e.full_name, 'text': 'Joins the company'})
    holiday_types = ('PUBLIC_HOLIDAY', 'COMPANY_HOLIDAY', 'OPTIONAL_HOLIDAY')
    for d in CalendarDay.objects.filter(calendar__company=company, day_type__in=holiday_types, date__gte=today, date__lte=today + datetime.timedelta(days=HOLIDAY_DAYS)).order_by('date')[:6]:
        out.append({'date': d.date, 'kind': 'holiday', 'title': d.name or d.get_day_type_display(), 'text': d.get_day_type_display()})
    seen, unique = set(), []
    for ev in sorted(out, key=lambda x: (x['date'], x['kind'], x['title'])):
        key = (ev['date'], ev['kind'], ev['title'])
        if key not in seen:
            seen.add(key)
            ev['in_days'] = (ev['date'] - today).days
            unique.append(ev)
    return unique[:10]


def _attendance_bar(ctx):
    parts = [('Present', ctx.get('present_today', 0), 'present'), ('On leave', ctx.get('on_leave_today', 0), 'leave'),
             ('Absent', ctx.get('absent_today', 0), 'absent'), ('Not marked', ctx.get('not_marked_today', 0), 'unmarked')]
    total = sum(p[1] for p in parts)
    bar, used = [], 0
    for i, (label, n, css) in enumerate(parts):
        pct = round(n * 100 / total) if total else 0
        if i == len(parts) - 1:
            pct = max(0, 100 - used) if total else 0
        used += pct
        bar.append({'label': label, 'n': n, 'css': css, 'pct': pct})
    return {'total': total, 'parts': bar, 'present_pct': round(ctx.get('present_today', 0) * 100 / total) if total else 0}


def _greeting(request, today):
    hour = timezone.localtime().hour
    user = request.user
    return {'hello': 'Good morning' if hour < 12 else ('Good afternoon' if hour < 17 else 'Good evening'),
            'name': (user.first_name or user.username), 'company': getattr(getattr(user, 'company', None), 'name', '')}


def owner_extras(request, employees, today, runs, ctx):
    """The extra dashboard panels, as a dict of context entries (all prefixed dx_)."""
    company = request.user.company
    attention = _safe('attention', {'legacy': [], 'alerts': [], 'total': 0}, _attention, company, employees, runs, ctx, today)
    return {
        'dx_greeting': _safe('greeting', {'hello': 'Hello', 'name': '', 'company': ''}, _greeting, request, today),
        'dx_today': today,
        'dx_setup': _safe('setup', None, _setup_progress, company, employees, runs),
        'dx_attention': attention,
        'dx_tracker': _safe('tracker', None, _tracker, company, employees, runs, today),
        'dx_events': _safe('events', [], _events, company, employees, today),
        'dx_att_bar': _safe('attendance bar', {'total': 0, 'parts': [], 'present_pct': 0}, _attendance_bar, ctx),
    }
