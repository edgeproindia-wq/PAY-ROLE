"""Salary history: filters, the all-employees list, and salary changes for many employees at once (always with a preview first).

The single-employee page is unchanged. Everything here uses the same rules as saving one salary: a change starts on the 1st of a month, an employee who
already has a change for that month is skipped (never overwritten), released payslips are never touched, and only the owner's own company is ever read
or changed. Saving is all-or-nothing and every change is written to the audit log.
"""
import datetime
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils import timezone

from .audit import log_action
from .models import PayrollRun, SalaryRevision
from .salary_history import _amount, _month_start, month_choices, sync_current_structure

MAX_BULK = 500
LIST_LIMIT = 500
MAX_VALUE = Decimal('99999999.99')
TWO = Decimal('0.01')
COMPONENTS = ('basic', 'hra', 'conveyance', 'special_allowance')
STATUS_LABELS = [('ACTIVE', 'Active'), ('ON_LEAVE', 'On leave'), ('RESIGNED', 'Resigned'), ('TERMINATED', 'Terminated')]
METHODS = [('same', 'Same salary for everyone'), ('percent', "Raise each person's salary by a percentage"), ('amount', "Add a fixed amount to each person's Basic")]


# ---- filters -------------------------------------------------------------------------------------------------------------
def read_filters(params):
    return {'q': (params.get('q') or '').strip()[:60], 'department': (params.get('department') or '').strip()[:200],
            'designation': (params.get('designation') or '').strip()[:200], 'status': (params.get('status') or '').strip()}


def filter_employees(employees, f):
    qs = employees
    for token in f['q'].split():
        qs = qs.filter(Q(employee_code__icontains=token) | Q(first_name__icontains=token) | Q(last_name__icontains=token))
    if f['department']:
        qs = qs.filter(department=f['department'])
    if f['designation']:
        qs = qs.filter(designation=f['designation'])
    if f['status'] in dict(STATUS_LABELS):
        qs = qs.filter(employment_status=f['status'])
    return qs


def filter_context(request, employees):
    """What the filter boxes need on every version of the page."""
    bulk_post = request.method == 'POST' and request.POST.get('action', '').startswith('bulk')
    f = read_filters(request.POST if bulk_post else request.GET)
    return {'f': f, 'filters_active': any(f.values()),
            'departments': sorted({d for d in employees.values_list('department', flat=True) if d}),
            'designations': sorted({d for d in employees.values_list('designation', flat=True) if d}), 'status_choices': STATUS_LABELS}


# ---- the timeline of an employee (same rules as the single page) ------------------------------------------------------------
def _revisions_for(emps):
    by = {}
    for r in SalaryRevision.objects.filter(employee__in=emps).select_related('created_by').order_by('employee_id', 'effective_from'):
        by.setdefault(r.employee_id, []).append(r)
    return by


def _timeline(revs, today):
    out = []
    for i, r in enumerate(revs):
        nxt = revs[i + 1].effective_from if i + 1 < len(revs) else None
        out.append({'rev': r, 'effective_to': (nxt - datetime.timedelta(days=1)) if nxt else None,
                    'in_force': r.effective_from <= today and (nxt is None or nxt > today)})
    return out


# ---- the all-employees list -------------------------------------------------------------------------------------------------
def list_page(request, company, employees, bulk_errors=None):
    params = request.POST if bulk_errors is not None else request.GET
    ctx = filter_context(request, employees)
    f = ctx['f']
    qs = filter_employees(employees, f)
    total = qs.count()
    emps = list(qs[:LIST_LIMIT])
    revs = _revisions_for(emps)
    today = timezone.localdate()
    view = 'changes' if params.get('view') == 'changes' else 'current'
    rows, changes = [], []
    for e in emps:
        tl = _timeline(revs.get(e.pk, []), today)
        rows.append({'emp': e, 'current': next((t for t in tl if t['in_force']), None), 'has_any': bool(tl)})
        if view == 'changes':
            changes += [{'emp': e, **t} for t in tl]
    changes.sort(key=lambda c: (c['rev'].effective_from, c['emp'].employee_code), reverse=True)
    picked = {int(x) for x in params.getlist('ids') if x.isdigit()} if bulk_errors is not None else set()
    ctx.update({'employees': employees, 'chosen': None, 'listing': True, 'rows': rows, 'changes': changes, 'view': view, 'total': total, 'shown': len(emps),
                'capped': total > LIST_LIMIT, 'months': month_choices(), 'methods': METHODS, 'picked': picked,
                'bulk_errors': bulk_errors or [], 'bulk_post': params if bulk_errors is not None else {}, 'errors': [], 'history': [], 'post': {}})
    return render(request, 'salary_history.html', ctx)


# ---- many employees at once -------------------------------------------------------------------------------------------------
def _plan(request, company):
    P = request.POST
    errors, ids = [], []
    for x in P.getlist('ids'):
        if x.isdigit() and int(x) not in ids:
            ids.append(int(x))
    if not ids:
        errors.append('Tick at least one employee.')
    if len(ids) > MAX_BULK:
        errors.append('You ticked %d employees. Please change at most %d at a time (use the filters).' % (len(ids), MAX_BULK))
    try:
        eff = datetime.date.fromisoformat(P.get('effective_from', ''))
    except ValueError:
        eff = None
        errors.append('Choose the month the new salary starts.')
    if eff and eff.day != 1:
        errors.append('A salary change must start on the first day of a month. For a mid-month change use Arrears.')
    method, params = P.get('method', ''), {}
    if method not in dict(METHODS):
        errors.append('Choose how the new salary is worked out.')
    elif method == 'same':
        params['basic'] = _amount(P.get('basic'), 'Basic', errors, required=True)
        for key, label in (('hra', 'HRA'), ('conveyance', 'Conveyance'), ('special_allowance', 'Special allowance')):
            params[key] = _amount(P.get(key), label, errors)
    elif method == 'percent':
        params['percent'] = _amount(P.get('percent'), 'Percentage', errors, required=True)
        if params['percent'] > 100:
            errors.append('The percentage cannot be more than 100.')
    else:
        params['amount'] = _amount(P.get('amount'), 'Amount', errors, required=True)
    emps = []
    if ids and len(ids) <= MAX_BULK:
        emps = list(company.employees.filter(pk__in=ids).order_by('employee_code'))
        if not emps and not errors:
            errors.append('None of the ticked employees belong to your company.')
    return {'errors': errors, 'ids': [e.pk for e in emps], 'emps': emps, 'eff': eff, 'method': method, 'params': params, 'note': (P.get('note') or '')[:255]}


def _rows(plan):
    eff, method, params = plan['eff'], plan['method'], plan['params']
    revs = _revisions_for(plan['emps'])
    rows = []
    for e in plan['emps']:
        mine = revs.get(e.pk, [])
        base = None
        for r in mine:
            if r.effective_from <= eff:
                base = r
        row = {'emp': e, 'before': base.gross_salary if base else None, 'new': None, 'gross': None, 'skip': ''}
        vals = None
        if any(r.effective_from == eff for r in mine):
            row['skip'] = 'already has a salary starting %s' % eff.strftime('%B %Y')
        elif method == 'same':
            vals = {k: params[k] for k in COMPONENTS}
        elif base is None:
            row['skip'] = 'no salary in force before that month'
        elif method == 'percent':
            factor = 1 + params['percent'] / 100
            vals = {k: (getattr(base, k) * factor).quantize(TWO, ROUND_HALF_UP) for k in COMPONENTS}
        else:
            vals = {k: getattr(base, k) for k in COMPONENTS}
            vals['basic'] = (base.basic + params['amount']).quantize(TWO, ROUND_HALF_UP)
        if vals is not None:
            if any(v > MAX_VALUE for v in vals.values()):
                row['skip'] = 'the new amount is too large'
            else:
                row['new'], row['gross'] = vals, sum(vals.values())
        rows.append(row)
    return rows


def _back(request):
    f = read_filters(request.POST)
    return request.path + ('?' + urlencode({k: v for k, v in f.items() if v}) if any(f.values()) else '')


def _preview(request, company, employees, plan, rows):
    ok = [r for r in rows if not r['skip']]
    ctx = filter_context(request, employees)
    ctx.update({'employees': employees, 'chosen': None, 'listing': False, 'errors': [], 'history': [], 'post': {}, 'months': month_choices(),
                'bulk': {'rows': rows, 'ok': len(ok), 'skipped': len(rows) - len(ok), 'eff': plan['eff'], 'method': plan['method'],
                         'method_label': dict(METHODS)[plan['method']], 'params': plan['params'], 'note': plan['note'], 'ids': plan['ids'],
                         'total_before': sum((r['before'] for r in ok if r['before'] is not None), Decimal('0')), 'total_after': sum((r['gross'] for r in ok), Decimal('0')),
                         'back': _back(request)}})
    return render(request, 'salary_history.html', ctx)


def _save(request, company, employees, plan, rows):
    eff, note = plan['eff'], plan['note']
    ok = [r for r in rows if not r['skip']]
    if not ok:
        messages.error(request, 'Nothing was saved: every ticked employee was skipped.')
        return redirect(_back(request))
    try:
        with transaction.atomic():                                    # all or nothing
            for r in ok:
                SalaryRevision.objects.create(employee=r['emp'], effective_from=eff, note=note, created_by=request.user, **r['new'])
                sync_current_structure(r['emp'])
    except IntegrityError:
        messages.error(request, 'Someone else changed a salary at the same moment, so nothing was saved. Please preview again.')
        return redirect(_back(request))
    for r in ok:
        log_action(request, 'UPDATE', r['emp'], details=f"Salary revised from {eff:%B %Y}: gross {r['gross']} (bulk)")
    skipped = [r for r in rows if r['skip']]
    messages.success(request, 'New salary saved for %d employee%s from %s.' % (len(ok), '' if len(ok) == 1 else 's', eff.strftime('%B %Y')))
    if skipped:
        shown = '; '.join('%s %s' % (r['emp'].employee_code, r['skip']) for r in skipped[:8])
        messages.warning(request, 'Skipped %d: %s%s' % (len(skipped), shown, ' ...' if len(skipped) > 8 else ''))
    released, drafts = [], []
    for run in PayrollRun.objects.filter(company=company):
        start = _month_start(run.month)
        if start and start >= eff:
            if run.status == 'RELEASED':
                released.append(run.month)
            elif run.status in ('DRAFT', 'VALIDATED', 'APPROVED'):
                drafts.append(run.month)
    if released:
        messages.warning(request, 'Already released and paid at the old salary (unchanged): ' + ', '.join(released) + '. Use Arrears to pay any difference.')
    if drafts:
        messages.info(request, 'Not released yet - reprocess to use the new salary: ' + ', '.join(drafts))
    return redirect(_back(request))


def handle_extras(request, company, employees, chosen):
    """Called first by the salary history view. Returns a response for the list, the preview and the bulk save, or None to let the single-employee page do its normal work."""
    action = request.POST.get('action', '') if request.method == 'POST' else ''
    if action in ('bulk_preview', 'bulk_save'):
        plan = _plan(request, company)
        if plan['errors']:
            return list_page(request, company, employees, bulk_errors=plan['errors'])
        rows = _rows(plan)
        return _preview(request, company, employees, plan, rows) if action == 'bulk_preview' else _save(request, company, employees, plan, rows)
    if chosen is None and request.method == 'GET':
        return list_page(request, company, employees)
    return None
