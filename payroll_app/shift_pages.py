"""Assign Shifts (give one shift to many employees at once) and Daily Working Hours (one day, everyone: in, out, hours, late, overtime).
Both build on the existing shift and attendance records; nothing in payroll changes."""
import csv
import datetime

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import path
from django.utils import timezone

from .attendance_hours import label as hours_label
from .audit import log_action
from .models import Attendance, Employee
from .models_phase4 import Shift, ShiftAssignment
from .permissions import company_owner_required
from .phase4 import _assign_shift, day_summary, shift_for


def _date(text, default):
    try:
        return datetime.date.fromisoformat((text or '').strip())
    except ValueError:
        return default


def _safe(v):
    v = '' if v is None else str(v)
    return "'" + v if v[:1] in ('=', '+', '-', '@') else v


def _mins(m):
    return '' if m is None else f'{m // 60}h {m % 60:02d}m'


@company_owner_required
def shift_assign(request):
    company = request.user.company
    today = timezone.localdate()
    employees = list(Employee.objects.filter(company=company).order_by('first_name', 'last_name'))
    shifts = list(Shift.objects.filter(company=company, active=True))
    if request.method == 'POST':
        shift = next((s for s in shifts if str(s.pk) == request.POST.get('shift', '')), None)
        start = _date(request.POST.get('effective_from'), None)
        end = _date(request.POST.get('effective_to'), None) if request.POST.get('effective_to') else None
        picked = set(request.POST.getlist('employee'))
        chosen = [e for e in employees if str(e.pk) in picked]          # only this company's people
        problem = None
        if shift is None:
            problem = 'Choose a shift. Create one first on the Shifts page if the list is empty.'
        elif start is None:
            problem = 'Choose the date the shift starts.'
        elif end is not None and end < start:
            problem = 'Effective to cannot be before Effective from.'
        elif not chosen:
            problem = 'Tick at least one employee.'
        if problem:
            messages.error(request, problem)
        else:
            for e in chosen:
                _assign_shift(company, e, shift, start, end)
            period = f'from {start:%d %b %Y}' + (f' to {end:%d %b %Y}' if end else ' onwards')
            log_action(request, 'UPDATE', chosen[0], details=f'Shift {shift.name} set for {len(chosen)} employee(s) {period}')
            messages.success(request, f'{len(chosen)} employee(s) assigned to {shift.name} {period}.')
            return redirect('shift_assign')
    rows = [{'e': e, 'shift': shift_for(e, today)} for e in employees]
    return render(request, 'shift_pages/shift_assign.html', {'rows': rows, 'shifts': shifts, 'today': today.isoformat()})


@company_owner_required
def hr_day_hours(request):
    company = request.user.company
    today = timezone.localdate()
    day = _date(request.GET.get('date'), today)
    records = {a.employee_id: a for a in Attendance.objects.filter(employee__company=company, date=day)}
    rows = []
    for e in Employee.objects.filter(company=company).order_by('first_name', 'last_name'):
        rec = records.get(e.pk)
        sh = shift_for(e, day)
        info = day_summary(rec, sh) if rec else {'worked': None, 'late': None, 'overtime': None}
        rows.append({'e': e, 'rec': rec, 'shift': sh, 'hours': hours_label(day, rec.check_in, rec.check_out) if rec else '\u2014',
                     'late': info['late'], 'overtime': _mins(info['overtime']) if info['overtime'] else ''})
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="daily-working-hours-{day.isoformat()}.csv"'
        w = csv.writer(resp)
        w.writerow(['Date', 'Employee code', 'Employee', 'Shift', 'Status', 'Check in', 'Check out', 'Hours worked', 'Late', 'Overtime'])
        for r in rows:
            rec = r['rec']
            w.writerow([day.isoformat(), _safe(r['e'].employee_code), _safe(f"{r['e'].first_name} {r['e'].last_name}".strip()), _safe(r['shift'] or ''),
                        rec.get_status_display() if rec else 'No record', rec.check_in.strftime('%H:%M') if rec and rec.check_in else '',
                        rec.check_out.strftime('%H:%M') if rec and rec.check_out else '', r['hours'], 'Yes' if r['late'] else '', r['overtime']])
        return resp
    counts = {'present': sum(1 for r in rows if r['rec'] and r['rec'].status in ('PRESENT', 'HALF_DAY')),
              'no_record': sum(1 for r in rows if not r['rec']), 'late': sum(1 for r in rows if r['late'])}
    return render(request, 'shift_pages/day_hours.html', {'rows': rows, 'day': day.isoformat(), 'counts': counts,
                  'prev': (day - datetime.timedelta(days=1)).isoformat(), 'next': (day + datetime.timedelta(days=1)).isoformat()})


urlpatterns = [
    path('hr/shifts/assign/', shift_assign, name='shift_assign'),
    path('hr/day-hours/', hr_day_hours, name='hr_day_hours'),
]
