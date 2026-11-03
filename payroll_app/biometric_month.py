"""Month-end biometric summary (company owners only).

Every punch the sync program sends is stored permanently in the punch log, and attendance is worked out from it. This page is for
the end of the month: for each employee, the days present, half days, absent and on leave, the days with a missing check-out,
the hours worked, and how many punches arrived. It reads attendance and punches only; it changes nothing.
"""
import csv
import datetime
import io
import re
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.http import HttpResponse
from django.shortcuts import render
from django.urls import path
from django.utils import timezone

from .audit import log_action
from .biometric import IST, _company, local_day_bounds
from .biometric_models import BiometricPunch
from .models import Attendance, Employee
from .permissions import company_owner_required

CENT = Decimal('0.01')
COLUMNS = [('code', 'Code'), ('name', 'Employee'), ('dept', 'Department'), ('present', 'Present'), ('half', 'Half day'), ('absent', 'Absent'), ('leave', 'On leave'),
           ('recorded', 'Days recorded'), ('punch_days', 'Days with a punch'), ('missing', 'Missing check-out'), ('hours', 'Hours worked'), ('avg', 'Average hours per day'),
           ('punches', 'Punches')]
NUMERIC = {'present', 'half', 'absent', 'leave', 'recorded', 'punch_days', 'missing', 'hours', 'avg', 'punches'}


def _safe(value):
    """A text cell that starts with = + - @ is read as a formula by Excel; make it plain text."""
    s = str(value)
    return "'" + s if s[:1] in ('=', '+', '-', '@', '\t', '\r') else s


def parse_month(text):
    """(first day, last day) of a YYYY-MM month; this month when the text is missing or wrong."""
    today = timezone.localdate()
    m = re.fullmatch(r'(\d{4})-(\d{2})', (text or '').strip())
    year, month = (int(m.group(1)), int(m.group(2))) if m else (today.year, today.month)
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        year, month = today.year, today.month
    first = datetime.date(year, month, 1)
    nxt = datetime.date(year + (month == 12), month % 12 + 1, 1)
    return first, nxt - datetime.timedelta(days=1)


def _hours(a):
    """Hours between check-in and check-out on the same day, or None."""
    if not (a.check_in and a.check_out):
        return None
    seconds = (datetime.datetime.combine(a.date, a.check_out) - datetime.datetime.combine(a.date, a.check_in)).total_seconds()
    return Decimal(seconds) / 3600 if seconds > 0 else None


def summary_rows(company, first, last):
    """One row per employee who has data in the month, or who is still on the payroll, plus the totals row."""
    counts = defaultdict(lambda: defaultdict(int))
    hours = defaultdict(lambda: Decimal('0'))
    both = defaultdict(int)
    for a in Attendance.objects.filter(employee__company=company, date__gte=first, date__lte=last):
        c = counts[a.employee_id]
        c['recorded'] += 1
        c[{'PRESENT': 'present', 'HALF_DAY': 'half', 'ABSENT': 'absent', 'LEAVE': 'leave'}.get(a.status, 'other')] += 1
        h = _hours(a)
        if h is not None:
            hours[a.employee_id] += h
            both[a.employee_id] += 1
        elif a.check_in and not a.check_out:
            c['missing'] += 1
    start, _ = local_day_bounds(first)
    _, end = local_day_bounds(last)
    punch_n, punch_days = defaultdict(int), defaultdict(set)
    for emp_id, when in BiometricPunch.objects.filter(company=company, employee__isnull=False, punched_at__gte=start, punched_at__lt=end).values_list('employee_id', 'punched_at'):
        punch_n[emp_id] += 1
        punch_days[emp_id].add(when.astimezone(IST).date())
    rows = []
    for e in Employee.objects.filter(company=company).order_by('employee_code'):
        has_data = e.pk in counts or e.pk in punch_n
        if not has_data and e.employment_status not in ('ACTIVE', 'ON_LEAVE'):
            continue
        c = counts.get(e.pk, {})
        h = hours.get(e.pk, Decimal('0')).quantize(CENT, rounding=ROUND_HALF_UP)
        n = both.get(e.pk, 0)
        rows.append({'code': e.employee_code, 'name': e.full_name, 'dept': e.department, 'present': c.get('present', 0), 'half': c.get('half', 0),
                     'absent': c.get('absent', 0), 'leave': c.get('leave', 0), 'recorded': c.get('recorded', 0), 'punch_days': len(punch_days.get(e.pk, ())),
                     'missing': c.get('missing', 0), 'hours': h, 'avg': (h / n).quantize(CENT, rounding=ROUND_HALF_UP) if n else Decimal('0.00'), 'punches': punch_n.get(e.pk, 0)})
    totals = {k: sum((r[k] for r in rows), Decimal('0') if k in ('hours',) else 0) for k in ('present', 'half', 'absent', 'leave', 'recorded', 'punch_days', 'missing', 'hours', 'punches')}
    return rows, totals


def _cell(key, value):
    return f'{value:.2f}' if key in ('hours', 'avg') else value


@company_owner_required
def bio_month(request):
    company = _company(request)
    first, last = parse_month(request.GET.get('month'))
    rows, totals = summary_rows(company, first, last)
    q = (request.GET.get('q') or '').strip().lower()
    if q:
        rows = [r for r in rows if q in f"{r['code']} {r['name']} {r['dept']}".lower()]
    fmt = request.GET.get('export')
    if fmt in ('csv', 'xlsx'):
        headers = [label for _k, label in COLUMNS]
        body = [[_safe(r[k]) if k in ('code', 'name', 'dept') else r[k] for k, _l in COLUMNS] for r in rows]
        stem = f'biometric_summary_{first:%Y_%m}'
        log_action(request, 'EXPORT', company=company, details=f'Biometric month summary exported ({first:%Y-%m}, {fmt}, {len(rows)} employees)')
        if fmt == 'xlsx':
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.append(headers)
            for r in body:
                ws.append([float(v) if isinstance(v, Decimal) else v for v in r])
            buf = io.BytesIO()
            wb.save(buf)
            resp = HttpResponse(buf.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            resp['Content-Disposition'] = f'attachment; filename="{stem}.xlsx"'
            return resp
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="{stem}.csv"'
        w = csv.writer(resp)
        w.writerow(headers)
        for r in body:
            w.writerow([f'{v:.2f}' if isinstance(v, Decimal) else v for v in r])
        return resp
    prev_month = (first - datetime.timedelta(days=1)).strftime('%Y-%m')
    next_month = (last + datetime.timedelta(days=1)).strftime('%Y-%m')
    start, _ = local_day_bounds(first)
    _, end = local_day_bounds(last)
    waiting = BiometricPunch.objects.filter(company=company, employee__isnull=True, punched_at__gte=start, punched_at__lt=end).count()
    shown = [[(_cell(k, r[k]), k in NUMERIC) for k, _l in COLUMNS] for r in rows]
    total_cells = [('Total' if i == 0 else (_cell(k, totals[k]) if k in totals else ''), k in NUMERIC) for i, (k, _l) in enumerate(COLUMNS)]
    return render(request, 'bio/month.html', {
        'month': first.strftime('%Y-%m'), 'month_label': first.strftime('%B %Y'), 'prev_month': prev_month, 'next_month': next_month, 'q': request.GET.get('q', ''),
        'cols': [(l, k in NUMERIC) for k, l in COLUMNS], 'rows': shown, 'totals': total_cells, 'count': len(rows), 'waiting': waiting,
        'has_data': any(r['recorded'] or r['punches'] for r in rows)})


urlpatterns = [path('biometric/month-end/', bio_month, name='bio_month')]
