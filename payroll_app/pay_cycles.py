"""Pay Cycle and Working Calendar: forms, services, views and URLs (company owners only)."""
import csv
import datetime

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse

from .audit import log_action
from .models import Employee, Notification, PayrollRun, PayrollRunLine
from .pay_cycle_calc import CalendarRules, cycle_summary, employee_cycle_summary
from .pay_cycle_models import CalendarDay, PayCycle, WorkingCalendar
from .payroll_engine import build_run_lines, release_claims
from .permissions import company_owner_required

PAGE_SIZE = 20
WEEKDAYS = [(0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'), (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday')]


def _company(request):
    company = getattr(request.user, 'company', None)
    if company is None:
        raise PermissionDenied('This page is for company owners.')
    return company


def _cycle(request, pk):
    return get_object_or_404(PayCycle.objects.select_related('payroll_run', 'calendar'), pk=pk, company=_company(request))


def eligible_employees(cycle):
    """Active employees who have joined by the end of the cycle."""
    return Employee.objects.filter(company=cycle.company, employment_status='ACTIVE').filter(
        Q(date_of_joining__isnull=True) | Q(date_of_joining__lte=cycle.end_date))


def cycle_stats(cycle):
    run = cycle.payroll_run
    eligible = eligible_employees(cycle).count()
    stats = {'eligible': eligible, 'processed': 0, 'pending': eligible, 'gross': 0, 'deductions': 0, 'net': 0}
    if run is not None:
        lines = run.lines.all()
        agg = lines.aggregate(g=Sum('total_earnings'), d=Sum('total_deductions'), n=Sum('net_pay'))
        processed = lines.count()
        stats.update(processed=processed, pending=max(eligible - processed, 0),
                     gross=agg['g'] or 0, deductions=agg['d'] or 0, net=agg['n'] or 0)
    stats.update(cycle_summary(cycle))
    return stats


# --------------------------------------------------------------------------- forms
class PayCycleForm(forms.ModelForm):
    class Meta:
        model = PayCycle
        fields = ['name', 'cycle_type', 'payroll_month', 'start_date', 'end_date', 'processing_date', 'pay_date',
                  'salary_policy', 'unmarked_as_lop', 'calendar']
        widgets = {k: forms.DateInput(attrs={'type': 'date'}) for k in ('start_date', 'end_date', 'processing_date', 'pay_date')}
        labels = {'name': 'Pay cycle name', 'pay_date': 'Salary pay date', 'processing_date': 'Payroll processing date',
                  'unmarked_as_lop': 'Unmarked working days are loss of pay'}

    def __init__(self, company, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.company = company
        self.fields['calendar'].queryset = WorkingCalendar.objects.filter(company=company)
        self.fields['calendar'].required = False
        started = bool(self.instance.pk and self.instance.payroll_run_id)
        if started:                                       # the basis of a processed cycle cannot change under it
            for name in ('cycle_type', 'payroll_month', 'start_date', 'end_date', 'salary_policy', 'unmarked_as_lop', 'calendar'):
                self.fields[name].disabled = True
        for f in self.fields.values():
            if not isinstance(f.widget, forms.CheckboxInput):
                f.widget.attrs.setdefault('class', 'form-control')


class CalendarForm(forms.ModelForm):
    weekly_offs = forms.MultipleChoiceField(choices=[(str(i), n) for i, n in WEEKDAYS], required=False,
                                            widget=forms.CheckboxSelectMultiple, label='Weekly offs')

    class Meta:
        model = WorkingCalendar
        fields = ['name']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial['weekly_offs'] = [str(i) for i in sorted(self.instance.weekly_offs())]

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.weekly_off_days = ','.join(sorted(self.cleaned_data.get('weekly_offs') or []))
        if commit:
            obj.save()
        return obj


class CalendarDayForm(forms.ModelForm):
    class Meta:
        model = CalendarDay
        fields = ['date', 'day_type', 'name']
        widgets = {'date': forms.DateInput(attrs={'type': 'date'})}

    def __init__(self, calendar, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calendar = calendar
        for f in self.fields.values():
            f.widget.attrs.setdefault('class', 'form-control')

    def clean_date(self):
        d = self.cleaned_data['date']
        if CalendarDay.objects.filter(calendar=self.calendar, date=d).exists():
            raise forms.ValidationError('This date already has an entry. Delete it first to change it.')
        return d


# --------------------------------------------------------------------------- services
def _guard(cycle):
    run = cycle.payroll_run
    if run is not None and run.is_locked:
        return f'"{cycle.name}" is locked. Unlock it first.'
    if run is not None and run.status == 'RELEASED':
        return f'"{cycle.name}" has already been released and cannot be recalculated.'
    return None


def process_cycle(cycle, user, request=None):
    """Create or recalculate the payroll run of a pay cycle. Returns (ok, message)."""
    if cycle.cycle_type != PayCycle.MONTHLY:
        return False, ('Processing is available for monthly pay cycles. Weekly, bi-weekly and custom cycles can be '
                       'defined, but the rule for splitting a monthly salary across them has not been set up yet.')
    with transaction.atomic():
        cycle = PayCycle.objects.select_for_update().select_related('payroll_run', 'company').get(pk=cycle.pk)
        problem = _guard(cycle)
        if problem:
            return False, problem
        if cycle.salary_policy == PayCycle.WORKING_DAYS and cycle_summary(cycle)['working_days'] == 0:
            return False, 'The Working Calendar gives 0 working days for this period. Check the weekly offs and holidays.'
        run = cycle.payroll_run
        if run is None:
            if PayrollRun.objects.filter(company=cycle.company, month__iexact=cycle.payroll_month).exists():
                return False, (f'A payroll run for {cycle.payroll_month} already exists outside this pay cycle. '
                               'Use Payroll Processing for it, or choose a different month.')
            run = PayrollRun.objects.create(company=cycle.company, month=cycle.payroll_month, created_by=user)
            cycle.payroll_run = run
            cycle.save(update_fields=['payroll_run', 'updated_at'])
        else:
            release_claims(run, Employee.objects.filter(pk__in=run.lines.values('employee')))
            run.lines.all().delete()
        created, skipped = build_run_lines(run, eligible_employees(cycle))
        run.status = 'DRAFT'
        run.save(update_fields=['status'])
        cycle.sync_status()
    note = ''
    if skipped:
        note = ' Skipped (no salary structure): ' + ', '.join(e.employee_code for e in skipped[:15]) + '.'
    if request is not None:
        log_action(request, 'PROCESS_PAYROLL', cycle, details=f'Pay cycle "{cycle.name}" calculated: {created} employees, {len(skipped)} skipped')
    return True, f'"{cycle.name}" calculated for {created} employee(s). Review it, then validate and approve.{note}'


RUN_STEPS = {'validate': ('DRAFT', 'VALIDATED'), 'approve': ('VALIDATED', 'APPROVED'), 'release': ('APPROVED', 'RELEASED')}


def move_cycle(cycle, action, request):
    run = cycle.payroll_run
    if run is None:
        return False, 'Process the pay cycle first.'
    required, target = RUN_STEPS[action]
    if run.is_locked:
        return False, f'"{cycle.name}" is locked. Unlock it first.'
    if run.status != required:
        return False, f'Cannot {action}: the payroll is {run.get_status_display()}, expected {dict(PayrollRun.STATUS_CHOICES)[required]}.'
    if not run.lines.exists():
        return False, 'There are no payslip lines. Check salary structures and recalculate.'
    run.status = target
    run.save(update_fields=['status'])
    if target == 'RELEASED':
        for line in run.lines.select_related('employee').exclude(employee__user__isnull=True):
            Notification.objects.create(recipient_id=line.employee.user_id, message=f'Your payslip for {run.month} is available.',
                                        link=reverse('payslip_detail', args=[line.pk]))
    cycle.sync_status()
    log_action(request, 'PROCESS_PAYROLL', cycle, details=f'Pay cycle "{cycle.name}" status -> {target}')
    return True, f'"{cycle.name}" is now {run.get_status_display()}.'


# --------------------------------------------------------------------------- views
@company_owner_required
def pay_cycles(request):
    company = _company(request)
    qs = PayCycle.objects.filter(company=company).select_related('payroll_run')
    q, status = (request.GET.get('q') or '').strip(), request.GET.get('status') or ''
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(payroll_month__icontains=q))
    if status in dict(PayCycle.STATUS_CHOICES):
        qs = qs.filter(status=status)
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get('page'))
    for c in page:
        c.stats = cycle_stats(c)
    latest = PayCycle.objects.filter(company=company).select_related('payroll_run').order_by('-start_date').first()
    if latest:
        latest.stats = cycle_stats(latest)
    return render(request, 'paycycle/list.html', {
        'page': page, 'latest': latest, 'q': q, 'status': status, 'statuses': PayCycle.STATUS_CHOICES,
        'has_calendar': WorkingCalendar.objects.filter(company=company).exists()})


@company_owner_required
def pay_cycle_form(request, pk=None):
    company = _company(request)
    instance = _cycle(request, pk) if pk else None
    form = PayCycleForm(company, request.POST or None, instance=instance)
    if request.method == 'POST' and form.is_valid():
        cycle = form.save(commit=False)
        cycle.company = company
        if instance is None:
            cycle.created_by = request.user
        cycle.save()
        log_action(request, 'UPDATE' if instance else 'CREATE', cycle, details=f'Pay cycle "{cycle.name}" saved')
        messages.success(request, f'Pay cycle "{cycle.name}" saved.')
        return redirect('pay_cycle_detail', pk=cycle.pk)
    return render(request, 'paycycle/form.html', {'form': form, 'cycle': instance})


@company_owner_required
def pay_cycle_detail(request, pk):
    cycle = _cycle(request, pk)
    cycle.sync_status()
    stats = cycle_stats(cycle)
    run = cycle.payroll_run
    lines = run.lines.select_related('employee').order_by('employee__employee_code') if run else PayrollRunLine.objects.none()
    q = (request.GET.get('q') or '').strip()
    if q:
        lines = lines.filter(Q(employee__first_name__icontains=q) | Q(employee__last_name__icontains=q) | Q(employee__employee_code__icontains=q))
    page = Paginator(lines, PAGE_SIZE).get_page(request.GET.get('page'))
    done_ids = set(run.lines.values_list('employee_id', flat=True)) if run else set()
    pending = [e for e in eligible_employees(cycle).order_by('employee_code') if e.pk not in done_ids][:15]
    return render(request, 'paycycle/detail.html', {
        'cycle': cycle, 'run': run, 'stats': stats, 'page': page, 'q': q, 'pending': pending,
        'can_edit': run is None or not run.is_locked})


@company_owner_required
def pay_cycle_employee(request, pk, employee_pk):
    cycle = _cycle(request, pk)
    employee = get_object_or_404(Employee, pk=employee_pk, company=cycle.company)
    line = cycle.payroll_run.lines.filter(employee=employee).first() if cycle.payroll_run else None
    return render(request, 'paycycle/employee.html', {
        'cycle': cycle, 'employee': employee, 'summary': employee_cycle_summary(employee, cycle), 'line': line})


@company_owner_required
def pay_cycle_action(request, pk):
    cycle = _cycle(request, pk)
    if request.method != 'POST':
        raise PermissionDenied('Actions need a POST request.')
    action = request.POST.get('action')
    run = cycle.payroll_run
    if action in ('process', 'recalculate'):
        ok, msg = process_cycle(cycle, request.user, request)
    elif action in RUN_STEPS:
        ok, msg = move_cycle(cycle, action, request)
    elif action == 'send_back':
        if run is None or run.status == 'RELEASED' or run.is_locked:
            ok, msg = False, 'This pay cycle cannot be sent back to draft.'
        else:
            run.status = 'DRAFT'
            run.save(update_fields=['status'])
            cycle.sync_status()
            log_action(request, 'PROCESS_PAYROLL', cycle, details=f'Pay cycle "{cycle.name}" sent back to draft')
            ok, msg = True, f'"{cycle.name}" is back in draft.'
    elif action in ('lock', 'unlock'):
        if run is None:
            ok, msg = False, 'Process the pay cycle before locking it.'
        elif action == 'lock' and run.status not in ('APPROVED', 'RELEASED'):
            ok, msg = False, 'Approve the payroll before locking the pay cycle.'
        else:
            run.is_locked = action == 'lock'
            run.save(update_fields=['is_locked'])
            cycle.sync_status()
            log_action(request, 'PROCESS_PAYROLL', cycle, details=f'Pay cycle "{cycle.name}" {action}ed')
            ok, msg = True, f'"{cycle.name}" is now {cycle.get_status_display().lower()}.'
    elif action == 'delete':
        if run is not None:
            ok, msg = False, 'A pay cycle that has been processed cannot be deleted.'
        else:
            name = cycle.name
            log_action(request, 'DELETE', cycle, details=f'Pay cycle "{name}" deleted')
            cycle.delete()
            messages.success(request, f'Pay cycle "{name}" deleted.')
            return redirect('pay_cycles')
    else:
        ok, msg = False, 'Unknown action.'
    (messages.success if ok else messages.error)(request, msg)
    return redirect('pay_cycle_detail', pk=cycle.pk)


@company_owner_required
def pay_cycle_export(request, pk):
    cycle = _cycle(request, pk)
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="pay_cycle_{cycle.payroll_month.replace(" ", "_")}.csv"'
    w = csv.writer(resp)
    w.writerow(['Pay cycle', 'Start', 'End', 'Status', 'Employee ID', 'Name', 'Working days basis', 'LOP days', 'Gross', 'Deductions', 'Net pay'])
    if cycle.payroll_run:
        for l in cycle.payroll_run.lines.select_related('employee').order_by('employee__employee_code'):
            w.writerow([cycle.name, cycle.start_date, cycle.end_date, cycle.get_status_display(), l.employee.employee_code,
                        l.employee.full_name, l.days_in_month, l.lop_days, l.total_earnings, l.total_deductions, l.net_pay])
    log_action(request, 'EXPORT', cycle, details=f'Pay cycle "{cycle.name}" exported')
    return resp


@company_owner_required
def working_calendar(request):
    company = _company(request)
    calendar = WorkingCalendar.objects.filter(company=company, is_default=True).first()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'save_calendar':
            form = CalendarForm(request.POST, instance=calendar)
            if form.is_valid():
                obj = form.save(commit=False)
                obj.company, obj.is_default = company, True
                obj.save()
                log_action(request, 'UPDATE' if calendar else 'CREATE', obj, details='Working calendar saved')
                messages.success(request, 'Working calendar saved. Recalculate any pay cycle that is still being processed to use it.')
            else:
                messages.error(request, '; '.join(sum((list(v) for v in form.errors.values()), [])))
        elif calendar is None:
            messages.error(request, 'Save the working calendar first.')
        elif action == 'add_day':
            form = CalendarDayForm(calendar, request.POST)
            if form.is_valid():
                day = form.save(commit=False)
                day.calendar = calendar
                day.save()
                log_action(request, 'CREATE', day, details=f'Calendar entry {day.date} {day.day_type}')
                messages.success(request, 'Calendar entry added. Recalculate any pay cycle that is still being processed to use it.')
            else:
                messages.error(request, '; '.join(sum((list(v) for v in form.errors.values()), [])))
        elif action == 'delete_day':
            day = get_object_or_404(CalendarDay, pk=request.POST.get('day_id'), calendar=calendar)
            log_action(request, 'DELETE', day, details=f'Calendar entry {day.date} removed')
            day.delete()
            messages.success(request, 'Calendar entry removed.')
        return redirect('working_calendar')
    month = request.GET.get('month') or datetime.date.today().strftime('%Y-%m')
    try:
        first = datetime.datetime.strptime(month, '%Y-%m').date()
    except ValueError:
        first = datetime.date.today().replace(day=1)
    last = (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
    summary = CalendarRules.load(calendar).summary(first, last)
    return render(request, 'paycycle/calendar.html', {
        'calendar': calendar, 'form': CalendarForm(instance=calendar), 'day_form': CalendarDayForm(calendar),
        'days': calendar.days.all() if calendar else [], 'summary': summary, 'month': first.strftime('%Y-%m'),
        'month_label': first.strftime('%B %Y'), 'using_default': calendar is None})


urlpatterns = [
    path('payroll/pay-cycles/', pay_cycles, name='pay_cycles'),
    path('payroll/pay-cycles/new/', pay_cycle_form, name='pay_cycle_new'),
    path('payroll/pay-cycles/<int:pk>/', pay_cycle_detail, name='pay_cycle_detail'),
    path('payroll/pay-cycles/<int:pk>/edit/', pay_cycle_form, name='pay_cycle_edit'),
    path('payroll/pay-cycles/<int:pk>/action/', pay_cycle_action, name='pay_cycle_action'),
    path('payroll/pay-cycles/<int:pk>/export/', pay_cycle_export, name='pay_cycle_export'),
    path('payroll/pay-cycles/<int:pk>/employee/<int:employee_pk>/', pay_cycle_employee, name='pay_cycle_employee'),
    path('attendance/working-calendar/', working_calendar, name='working_calendar'),
]
