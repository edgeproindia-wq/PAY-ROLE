"""Stage 2 automation: Bulk attendance (select -> review -> confirm) and Payroll preview (the real engine, nothing saved)."""
import datetime
from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.urls import path
from django.utils import timezone

from .audit import log_action
from .models import Attendance, Employee, LeaveRequest, PayrollRun
from .payroll_engine import build_run_lines, parse_month
from .permissions import company_owner_required

STATUSES = [('PRESENT', 'Present'), ('ABSENT', 'Absent'), ('HALF_DAY', 'Half day'), ('LEAVE', 'On leave')]
STATUS_KEYS = {k for k, _ in STATUSES}
PAGE_SIZE = 50


def _day(raw):
    try:
        return datetime.date.fromisoformat(raw or '')
    except ValueError:
        return timezone.localdate()


def eligible_employees(company, day):
    """Active employees of this company who had already joined on that day."""
    return (Employee.objects.filter(company=company, employment_status='ACTIVE')
            .filter(Q(date_of_joining__isnull=True) | Q(date_of_joining__lte=day)))


@company_owner_required
def attendance_bulk(request):
    company = request.user.company
    day = _day(request.POST.get('date') or request.GET.get('date'))
    emps = eligible_employees(company, day)
    back = f'{request.path}?date={day.isoformat()}' + (f'&q={request.POST.get("q") or request.GET.get("q") or ""}')
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'present_all':
            marked = set(Attendance.objects.filter(employee__in=emps, date=day).values_list('employee_id', flat=True))
            on_leave = set(LeaveRequest.objects.filter(employee__in=emps, status='APPROVED', from_date__lte=day, to_date__gte=day)
                           .values_list('employee_id', flat=True))
            new = [Attendance(employee_id=e, date=day, status='LEAVE' if e in on_leave else 'PRESENT')
                   for e in emps.exclude(pk__in=marked).values_list('pk', flat=True)]
            Attendance.objects.bulk_create(new)
            leave_n = sum(1 for a in new if a.status == 'LEAVE')
            if new:
                log_action(request, 'CREATE', company, details=f'Bulk attendance {day}: {len(new) - leave_n} present, {leave_n} on approved leave')
            messages.success(request, f'{day:%d %b %Y}: marked {len(new) - leave_n} employee(s) Present and {leave_n} On leave (approved leave). '
                                      f'{len(marked)} already had a record and were left unchanged.' if new else
                             f'{day:%d %b %Y}: every eligible employee is already marked. Nothing to do.')
        elif action == 'save_rows':
            ids = [int(i) for i in request.POST.getlist('emp') if i.isdigit()]
            valid = set(emps.filter(pk__in=ids).values_list('pk', flat=True))        # employees of another company are ignored
            existing = {a.employee_id: a for a in Attendance.objects.filter(employee_id__in=valid, date=day)}
            create, update, bad = [], [], 0
            for eid in valid:
                status = request.POST.get(f'status_{eid}', '')
                remarks = (request.POST.get(f'remarks_{eid}') or '')[:255]
                if status not in STATUS_KEYS:
                    bad += 1 if status else 0
                    continue
                rec = existing.get(eid)
                if rec is None:
                    create.append(Attendance(employee_id=eid, date=day, status=status, remarks=remarks))
                elif rec.status != status or rec.remarks != remarks:
                    rec.status, rec.remarks = status, remarks
                    update.append(rec)
            with transaction.atomic():
                Attendance.objects.bulk_create(create)
                if update:
                    Attendance.objects.bulk_update(update, ['status', 'remarks'])
            if create or update:
                log_action(request, 'UPDATE', company, details=f'Bulk attendance {day}: {len(create)} added, {len(update)} changed')
            messages.success(request, f'Saved {day:%d %b %Y}: {len(create)} added, {len(update)} changed.' + (f' {bad} invalid row(s) ignored.' if bad else ''))
        return redirect(back + (f'&page={request.POST.get("page")}' if request.POST.get('page') else ''))
    q = (request.GET.get('q') or '').strip()
    listing = emps.order_by('employee_code')
    if q:
        listing = listing.filter(Q(employee_code__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(department__icontains=q))
    page = Paginator(listing, PAGE_SIZE).get_page(request.GET.get('page'))
    records = {a.employee_id: a for a in Attendance.objects.filter(employee__in=[e.pk for e in page], date=day)}
    rows = [{'emp': e, 'rec': records.get(e.pk)} for e in page]
    counts = dict(Attendance.objects.filter(employee__in=emps, date=day).order_by().values_list('status').annotate(n=Count('id')))
    total = emps.count()
    marked = sum(counts.values())
    return render(request, 'attendance_bulk.html', {'day': day, 'rows': rows, 'page': page, 'q': q, 'statuses': STATUSES, 'total': total, 'marked': marked,
                                                    'unmarked': total - marked, 'counts': counts})


@company_owner_required
def payroll_preview(request):
    """Runs the REAL payroll engine inside a transaction that is always rolled back, so the preview can never differ from the real run."""
    company = request.user.company
    import datetime as _dt
    today = timezone.localdate()
    months = []
    for i in range(12, -61, -1):
        y, m = divmod(today.year * 12 + today.month - 1 + i, 12)
        months.append(_dt.date(y, m + 1, 1).strftime('%B %Y'))
    ctx = {'months': months, 'month': (request.GET.get('month') or '').strip(), 'lines': None, 'skipped': [], 'existing': None, 'error': ''}
    if ctx['month']:
        period = parse_month(ctx['month'])
        if not period or ctx['month'] not in months:
            ctx['error'] = 'Choose the payroll month from the list.'
        else:
            label = period[0].strftime('%B %Y')
            ctx['month'] = label
            ctx['existing'] = PayrollRun.objects.filter(company=company, month__iexact=label).first()
            if ctx['existing'] is None:
                emps = (Employee.objects.filter(company=company, employment_status='ACTIVE')
                        .filter(Q(date_of_joining__isnull=True) | Q(date_of_joining__lte=period[1])).order_by('employee_code'))
                try:
                    with transaction.atomic():
                        run = PayrollRun.objects.create(company=company, month=label, status='DRAFT', created_by=request.user)
                        created, skipped = build_run_lines(run, emps)
                        lines = list(run.lines.select_related('employee').order_by('employee__employee_code'))
                        transaction.set_rollback(True)          # nothing from the preview is kept
                except IntegrityError:
                    ctx['existing'] = PayrollRun.objects.filter(company=company, month__iexact=label).first()
                else:
                    ctx.update(lines=lines, skipped=skipped,
                               totals={'gross': sum((l.gross_salary for l in lines), Decimal('0')),
                                       'deductions': sum((l.total_deductions for l in lines), Decimal('0')),
                                       'net': sum((l.net_pay for l in lines), Decimal('0'))})
    return render(request, 'payroll_preview.html', ctx)


urlpatterns = [path('attendance/bulk/', attendance_bulk, name='attendance_bulk'),
               path('payroll/preview/', payroll_preview, name='payroll_preview')]
