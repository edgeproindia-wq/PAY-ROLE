"""Phase-4 helpers and pages: shifts, working hours (overtime-ready) and pay components."""
import datetime
from decimal import ROUND_HALF_UP, Decimal

from django import forms
from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .audit import log_action
from .models import Attendance, Employee
from .models_phase4 import PayComponent, Shift, ShiftAssignment
from .permissions import company_owner_required


def q(v):
    return Decimal(v).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------ shift / hours logic
def shift_for(employee, day):
    a = (ShiftAssignment.objects.filter(employee=employee, effective_from__lte=day)
         .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=day)).select_related('shift').first())
    return a.shift if a else None


def day_summary(record, shift):
    """Worked minutes, late flag and overtime minutes for one attendance record.
    Overtime is only CALCULATED here (overtime-ready); it is not paid automatically."""
    worked = late = overtime = None
    if record.check_in and record.check_out:
        start = record.check_in.hour * 60 + record.check_in.minute
        end = record.check_out.hour * 60 + record.check_out.minute
        if end < start:
            end += 24 * 60
        worked = end - start - (shift.break_minutes if shift else 0)
        worked = max(worked, 0)
    if shift and record.check_in:
        s = shift.start_time.hour * 60 + shift.start_time.minute
        ci = record.check_in.hour * 60 + record.check_in.minute
        late = ci > s + shift.grace_minutes
    if shift and worked is not None:
        overtime = max(worked - shift.scheduled_minutes, 0)
    return {'worked': worked, 'late': late, 'overtime': overtime}


def month_summary(employee, year, month):
    start = datetime.date(year, month, 1)
    end = (start + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)
    worked = overtime = late = days = 0
    for rec in Attendance.objects.filter(employee=employee, date__range=(start, end)):
        s = day_summary(rec, shift_for(employee, rec.date))
        if s['worked'] is not None:
            worked += s['worked']
            days += 1
        overtime += s['overtime'] or 0
        late += 1 if s['late'] else 0
    return {'employee': employee, 'shift': shift_for(employee, end), 'days': days,
            'hours': round(worked / 60, 2), 'overtime_hours': round(overtime / 60, 2), 'late_days': late}


# ------------------------------------------------------------------ pay components in payroll
def component_totals(employee, period, pay_factor):
    """(earnings, taxable_earnings, deductions, breakdown) for the payroll month."""
    if not period:
        return Decimal('0.00'), Decimal('0.00'), Decimal('0.00'), []
    start, end = period
    comps = (PayComponent.objects.filter(employee=employee, active=True, effective_from__lte=end)
             .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=start)))
    earn = taxable = ded = Decimal('0')
    breakdown = []
    basic = getattr(getattr(employee, 'salary_structure', None), 'basic', 0) or 0
    for c in comps:
        monthly = q(Decimal(basic) * c.percent / 100) if c.calc_type == 'PERCENT_BASIC' else c.amount
        if c.kind == 'EARNING':
            amt = q(monthly * pay_factor)
            earn += amt
            taxable += monthly if c.taxable else 0
        else:
            amt = q(monthly)
            ded += amt
        breakdown.append((c.name, c.kind, amt))
    return q(earn), q(taxable), q(ded), breakdown


# ------------------------------------------------------------------ HR pages
def _style(form):
    for f in form.fields.values():
        f.widget.attrs.setdefault('class', 'form-control')
    return form


class ShiftForm(forms.ModelForm):
    class Meta:
        model = Shift
        fields = ['name', 'start_time', 'end_time', 'break_minutes', 'grace_minutes']
        widgets = {'start_time': forms.TimeInput(attrs={'type': 'time'}), 'end_time': forms.TimeInput(attrs={'type': 'time'})}

    def __init__(self, company, *a, **kw):
        super().__init__(*a, **kw)
        self.company = company

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if Shift.objects.filter(company=self.company, name__iexact=name).exists():
            raise forms.ValidationError('A shift with this name already exists.')
        return name


class AssignForm(forms.Form):
    employee = forms.ModelChoiceField(queryset=Employee.objects.none())
    shift = forms.ModelChoiceField(queryset=Shift.objects.none())
    effective_from = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))

    def __init__(self, company, *a, **kw):
        super().__init__(*a, **kw)
        self.fields['employee'].queryset = Employee.objects.filter(company=company)
        self.fields['shift'].queryset = Shift.objects.filter(company=company, active=True)


@company_owner_required
def hr_shifts(request):
    company = request.user.company
    shift_form = _style(ShiftForm(company, request.POST if request.POST.get('form') == 'shift' else None))
    assign_form = _style(AssignForm(company, request.POST if request.POST.get('form') == 'assign' else None))
    if request.method == 'POST':
        if request.POST.get('deactivate'):
            s = get_object_or_404(Shift, pk=request.POST['deactivate'], company=company)
            s.active = False
            s.save(update_fields=['active'])
            messages.success(request, f'Shift {s.name} deactivated.')
            return redirect('hr_shifts')
        if request.POST.get('form') == 'shift' and shift_form.is_valid():
            s = shift_form.save(commit=False)
            s.company = company
            s.save()
            log_action(request, 'CREATE', s, details='Shift created')
            messages.success(request, 'Shift created.')
            return redirect('hr_shifts')
        if request.POST.get('form') == 'assign' and assign_form.is_valid():
            c = assign_form.cleaned_data
            # close the previous assignment the day before the new one starts
            ShiftAssignment.objects.filter(employee=c['employee'], effective_to__isnull=True,
                                           effective_from__lt=c['effective_from']).update(
                effective_to=c['effective_from'] - datetime.timedelta(days=1))
            ShiftAssignment.objects.filter(employee=c['employee'], effective_from__gte=c['effective_from']).delete()
            ShiftAssignment.objects.create(company=company, employee=c['employee'], shift=c['shift'],
                                           effective_from=c['effective_from'])
            log_action(request, 'UPDATE', c['employee'], details=f'Shift set to {c["shift"].name}')
            messages.success(request, f'{c["employee"]} assigned to {c["shift"].name}.')
            return redirect('hr_shifts')
    return render(request, 'features/hr_shifts.html', {
        'shift_form': shift_form, 'assign_form': assign_form,
        'shifts': Shift.objects.filter(company=company),
        'assignments': ShiftAssignment.objects.filter(company=company).select_related('employee', 'shift')[:200]})


@company_owner_required
def hr_work_hours(request):
    company = request.user.company
    today = timezone.localdate()
    try:
        year, month = [int(x) for x in request.GET.get('month', f'{today:%Y-%m}').split('-')]
        datetime.date(year, month, 1)
    except (ValueError, TypeError):
        year, month = today.year, today.month
    rows = [month_summary(e, year, month) for e in Employee.objects.filter(company=company).order_by('first_name')]
    return render(request, 'features/hr_work_hours.html', {'rows': rows, 'month': f'{year:04d}-{month:02d}'})


class ComponentForm(forms.ModelForm):
    class Meta:
        model = PayComponent
        fields = ['employee', 'name', 'kind', 'calc_type', 'amount', 'percent', 'taxable', 'effective_from', 'effective_to']
        widgets = {'effective_from': forms.DateInput(attrs={'type': 'date'}), 'effective_to': forms.DateInput(attrs={'type': 'date'})}

    def __init__(self, company, *a, **kw):
        super().__init__(*a, **kw)
        self.fields['employee'].queryset = Employee.objects.filter(company=company)
        for name in ('calc_type', 'percent', 'amount'):
            self.fields[name].required = False

    def clean(self):
        c = super().clean()
        if not c.get('calc_type'):
            c['calc_type'] = 'FIXED'
        if c.get('percent') is None:
            c['percent'] = 0
        if c.get('calc_type') == 'PERCENT_BASIC':
            if not c.get('percent') or c['percent'] <= 0 or c['percent'] > 100:
                self.add_error('percent', 'Enter a percentage between 0 and 100.')
        elif not c.get('amount') or c['amount'] <= 0:
            self.add_error('amount', 'Amount must be more than zero.')
        return c


@company_owner_required
def hr_pay_components(request):
    company = request.user.company
    form = _style(ComponentForm(company, request.POST or None))
    if request.method == 'POST' and request.POST.get('end'):
        c = get_object_or_404(PayComponent, pk=request.POST['end'], company=company)
        c.active = False
        c.effective_to = c.effective_to or timezone.localdate()
        c.save(update_fields=['active', 'effective_to'])
        messages.success(request, f'{c.name} ended.')
        return redirect('hr_pay_components')
    if request.method == 'POST' and form.is_valid():
        c = form.save(commit=False)
        c.company = company
        c.save()
        log_action(request, 'CREATE', c, details=f'Pay component {c.name} added')
        messages.success(request, 'Pay component added.')
        return redirect('hr_pay_components')
    return render(request, 'features/hr_pay_components.html', {
        'form': form, 'items': PayComponent.objects.filter(company=company).select_related('employee')})