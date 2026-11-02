"""Advanced bank payment module (company owners only).

Features, all under "Bank Payment Management" in the payroll structure:
  Bank Detail Validation, Bank Payment File (batches), Payment Approval (maker-checker),
  Payment Instruction Export, Duplicate Payment Prevention, Bank Reconciliation, Payment Audit Trail.
The existing Salary Payment Register, Payment Status and Failed Payment Handling pages are unchanged.

Nothing here talks to a bank. It produces files that you upload to your bank's portal and reads the
statement / response file the bank gives back. As before, a payment is only marked Paid from a bank UTR.
"""
import csv
import datetime
import io
import re
import time
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.utils import timezone
from django.views.decorators.http import require_POST

from .audit import log_action
from .bank_batch_models import BankBatch, BankBatchItem
from .models import AuditLog, BankPayment, Company, Employee, PayrollRun, PayrollRunLine, User
from .permissions import company_owner_required

PAGE_SIZE = 25
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
MAX_NEW_BATCH_ROWS = 1000
IFSC_RE = re.compile(r'^[A-Z]{4}0[A-Z0-9]{6}$')
ACCOUNT_RE = re.compile(r'^[0-9]{9,18}$')
RTGS_MIN = Decimal('200000')          # RTGS is for large payments only
IMPS_MAX = Decimal('500000')          # IMPS upper limit per payment
CENT = Decimal('0.01')
RECON_MINUTES = 30                    # a reconciliation preview must be confirmed within this time

STATE_LABELS = {
    'new': 'Not prepared', 'ready': 'Prepared', 'failed': 'Failed - can retry', 'initiated': 'Marked initiated',
    'in_batch': 'In an open batch', 'paid': 'Paid',
}
ELIGIBLE_STATES = ('new', 'ready', 'failed')


# --------------------------------------------------------------------------- small helpers
def _company(request):
    company = getattr(request.user, 'company', None)
    if company is None:
        raise PermissionDenied('This page is for company owners.')
    return company


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _money(value):
    """Decimal with two places, or None when the text is not a number."""
    try:
        return Decimal(re.sub(r'[^0-9.\-]', '', str(value).replace(',', ''))).quantize(CENT)
    except (InvalidOperation, ValueError):
        return None


def _qs(request):
    q = request.GET.copy()
    q.pop('page', None)
    return q.urlencode()


def _page(request, queryset_or_list):
    return Paginator(queryset_or_list, PAGE_SIZE).get_page(request.GET.get('page'))


def clean_account(value):
    return re.sub(r'\s+', '', str(value or ''))


def bank_code(ifsc):
    return (ifsc or '').strip().upper()[:4]


def _safe(value):
    """A text cell that starts with = + - @ is read as a formula by Excel; make it plain text."""
    s = str(value)
    return "'" + s if s[:1] in ('=', '+', '-', '@', '\t', '\r') else s


def _released_runs(company):
    return PayrollRun.objects.filter(company=company, status='RELEASED').order_by('-created_at', '-id')


def _run_from_request(request, company, key='run'):
    run_id = _int(request.GET.get(key) or request.POST.get(key))
    return _released_runs(company).filter(pk=run_id).first() if run_id else None


# --------------------------------------------------------------------------- validation
def check_bank_details(employee, amount, mode='NEFT'):
    """Plain-English problems with paying this employee this amount: (errors, warnings).
    Errors stop a payment from being put in a batch. Warnings only tell the owner to look."""
    errors, warnings = [], []
    acc = clean_account(employee.bank_account_no)
    ifsc = (employee.ifsc_code or '').strip().upper()
    if not acc:
        errors.append('Account number missing')
    elif not ACCOUNT_RE.match(acc):
        errors.append('Account number must be 9 to 18 digits')
    if not ifsc:
        errors.append('IFSC missing')
    elif not IFSC_RE.match(ifsc):
        errors.append('IFSC is not valid (4 letters, then 0, then 6 letters or digits)')
    if employee.bank_status == 'INACTIVE':
        errors.append('Bank account is marked inactive')
    if amount is None or amount <= 0:
        errors.append('Net pay is zero or negative')
    elif mode == 'RTGS' and amount < RTGS_MIN:
        errors.append('RTGS needs at least 2,00,000 - use NEFT or IMPS')
    elif mode == 'IMPS' and amount > IMPS_MAX:
        errors.append('IMPS allows at most 5,00,000 - use NEFT or RTGS')
    if not (employee.account_holder_name or '').strip():
        warnings.append('Account holder name missing - the employee name will be used')
    if employee.employment_status in ('RESIGNED', 'TERMINATED'):
        warnings.append('Employee has left - pay through Full & Final Settlement if this is not a normal salary')
    return errors, warnings


def shared_accounts(company):
    """{(account, ifsc): [employee codes]} for accounts used by more than one employee of the company."""
    groups = defaultdict(list)
    rows = Employee.objects.filter(company=company).exclude(bank_account_no='').values_list('employee_code', 'bank_account_no', 'ifsc_code')
    for code, acc, ifsc in rows:
        groups[(clean_account(acc), (ifsc or '').strip().upper())].append(code)
    return {k: v for k, v in groups.items() if len(v) > 1}


def active_payment_ids(company):
    return set(BankBatchItem.objects.filter(batch__company=company, batch__status__in=BankBatch.ACTIVE_STATUSES)
               .values_list('payment_id', flat=True))


def line_state(line, active_ids):
    payment = getattr(line, 'bank_payment', None)
    if payment is None:
        return 'new'
    if payment.status == 'PAID':
        return 'paid'
    if payment.pk in active_ids:
        return 'in_batch'
    if payment.status == 'FAILED':
        return 'failed'
    if payment.status == 'INITIATED':
        return 'initiated'
    return 'ready'


def _released_lines(company, run=None):
    qs = (PayrollRunLine.objects
          .filter(payroll_run__company=company, payroll_run__status='RELEASED', employee__company=company)
          .select_related('employee', 'payroll_run', 'bank_payment')
          .order_by('-payroll_run__created_at', 'employee__employee_code'))
    return qs.filter(payroll_run=run) if run is not None else qs


def build_rows(company, run, mode):
    """One dict per released payslip line with its payment state and bank-detail problems."""
    shared = shared_accounts(company)
    active = active_payment_ids(company)
    rows = []
    for line in _released_lines(company, run)[:MAX_NEW_BATCH_ROWS]:
        emp = line.employee
        errors, warnings = check_bank_details(emp, line.net_pay, mode)
        key = (clean_account(emp.bank_account_no), (emp.ifsc_code or '').strip().upper())
        if key in shared:
            others = [c for c in shared[key] if c != emp.employee_code]
            warnings.append('Same account as ' + ', '.join(others[:3]))
        state = line_state(line, active)
        rows.append({'line': line, 'emp': emp, 'payment': getattr(line, 'bank_payment', None), 'state': state,
                     'state_label': STATE_LABELS[state], 'errors': errors, 'warnings': warnings,
                     'eligible': state in ELIGIBLE_STATES and not errors, 'bank': bank_code(emp.ifsc_code)})
    return rows


# --------------------------------------------------------------------------- batch creation
def _next_reference(company):
    year = timezone.localdate().year
    n = BankBatch.objects.filter(company=company, reference__startswith=f'BB-{year}-').count() + 1
    while BankBatch.objects.filter(company=company, reference=f'BB-{year}-{n:04d}').exists():
        n += 1
    return f'BB-{year}-{n:04d}'


def create_batch(request, company, run, mode, line_ids, note=''):
    """Put the chosen released payslip lines in a new batch. Returns (batch or None, skipped list of (line, reason))."""
    with transaction.atomic():
        # Lock the company row: two people creating a batch at the same moment cannot both take the same payment.
        Company.objects.select_for_update().get(pk=company.pk)
        active = active_payment_ids(company)
        wanted = list(_released_lines(company, run).filter(pk__in=line_ids))
        skipped, chosen = [], []
        for line in wanted:
            state = line_state(line, active)
            errors, _warn = check_bank_details(line.employee, line.net_pay, mode)
            if state == 'paid':
                skipped.append((line, 'Already paid - a payment is never repeated'))
            elif state == 'in_batch':
                skipped.append((line, 'Already in another open batch'))
            elif state == 'initiated':
                skipped.append((line, 'Marked initiated outside a batch - mark it paid or failed first'))
            elif errors:
                skipped.append((line, '; '.join(errors)))
            else:
                chosen.append((line, state))
        if not chosen:
            return None, skipped
        batch = BankBatch.objects.create(
            company=company, reference=_next_reference(company), payroll_run=run, mode=mode,
            created_by=request.user, note=(note or '')[:255])
        total, retried = Decimal('0.00'), 0
        for line, state in chosen:
            emp = line.employee
            snapshot = dict(company=company, amount=line.net_pay, bank_name=emp.bank_name,
                            account_holder_name=emp.account_holder_name or emp.full_name,
                            account_no=clean_account(emp.bank_account_no), ifsc_code=emp.ifsc_code.strip().upper(), mode=mode)
            if state == 'new':
                payment = BankPayment.objects.create(payroll_line=line, **snapshot)
            else:
                payment = BankPayment.objects.select_for_update().get(pk=line.bank_payment.pk)
                if payment.status == 'FAILED':       # retry: same rule as the existing Retry button
                    payment.attempts += 1
                    payment.failure_reason = ''
                    payment.status = 'PENDING'
                    retried += 1
                    log_action(request, 'PAYMENT_STATUS_CHANGE', payment, company=company,
                               details=f'{line.payslip_number}: FAILED -> PENDING (retry in {batch.reference})')
                for field, value in snapshot.items():
                    setattr(payment, field, value)
                payment.save()
            BankBatchItem.objects.create(batch=batch, payment=payment, amount=payment.amount)
            total += payment.amount
        batch.total_amount, batch.item_count = total, len(chosen)
        batch.save(update_fields=['total_amount', 'item_count', 'updated_at'])
        log_action(request, 'CREATE', batch,
                   details=f'Bank batch {batch.reference} created: {len(chosen)} payments, total {total}, {mode}'
                           + (f', {retried} retried' if retried else '') + (f', {len(skipped)} skipped' if skipped else ''))
    return batch, skipped


def sync_batch(batch):
    """A sent batch becomes Completed once every payment in it is Paid."""
    if batch.status in (BankBatch.FILE_GENERATED, BankBatch.SENT) and batch.item_count \
            and not batch.items.exclude(payment__status='PAID').exists():
        batch.status, batch.completed_at = BankBatch.COMPLETED, timezone.now()
        batch.save(update_fields=['status', 'completed_at', 'updated_at'])
    return batch


def can_approve(user, batch):
    """(allowed, self_approval). The creator may not approve their own batch unless the company has only one owner login."""
    if batch.created_by_id != user.id:
        return True, False
    owners = User.objects.filter(company=batch.company, role='COMPANY_OWNER', is_active=True).count()
    if owners >= 2:
        return False, False
    return True, True


# --------------------------------------------------------------------------- files
FILE_HEADERS = ['Sr No', 'Payment ID', 'Employee Code', 'Beneficiary Name', 'Account Number', 'IFSC', 'Bank', 'Amount', 'Mode', 'Narration']


def batch_file_rows(batch, bank=None):
    items = batch.items.select_related('payment__payroll_line__employee', 'payment__payroll_line__payroll_run').order_by('id')
    rows = []
    for item in items:
        p = item.payment
        if bank and bank_code(p.ifsc_code) != bank:
            continue
        rows.append([len(rows) + 1, p.pk, p.payroll_line.employee.employee_code, p.account_holder_name, p.account_no,
                     p.ifsc_code, p.bank_name, p.amount, p.mode, f'Salary {p.payroll_line.payroll_run.month}'[:30]])
    return rows


def batch_banks(batch):
    """[(bank code, bank name, count, total)] so the owner can download one file per bank."""
    groups = defaultdict(lambda: {'names': Counter(), 'count': 0, 'total': Decimal('0.00')})
    for item in batch.items.select_related('payment'):
        g = groups[bank_code(item.payment.ifsc_code) or '----']
        g['names'][item.payment.bank_name or ''] += 1
        g['count'] += 1
        g['total'] += item.payment.amount
    out = []
    for code in sorted(groups):
        g = groups[code]
        name = next((n for n, _c in g['names'].most_common() if n), code)
        out.append({'code': code, 'name': name, 'count': g['count'], 'total': g['total']})
    return out


def table_response(fmt, stem, headers, rows):
    """CSV or Excel download. Text cells are made safe against spreadsheet formulas."""
    if fmt == 'xlsx':
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(headers)
        for r in rows:
            ws.append([float(v) if isinstance(v, Decimal) else (_safe(v) if isinstance(v, str) else v) for v in r])
        buf = io.BytesIO()
        wb.save(buf)
        resp = HttpResponse(buf.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        resp['Content-Disposition'] = f'attachment; filename="{stem}.xlsx"'
        return resp
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="{stem}.csv"'
    w = csv.writer(resp)
    w.writerow(headers)
    for r in rows:
        w.writerow([f'{v:.2f}' if isinstance(v, Decimal) else (_safe(v) if isinstance(v, str) else v) for v in r])
    return resp


# --------------------------------------------------------------------------- pages: validation
@company_owner_required
def bank_validation(request):
    company = _company(request)
    run = _run_from_request(request, company)
    mode = request.GET.get('mode') if request.GET.get('mode') in dict(BankBatch.MODE_CHOICES) else 'NEFT'
    only_issues = request.GET.get('issues') == '1'
    q = (request.GET.get('q') or '').strip().lower()
    rows = [r for r in build_rows(company, run, mode) if r['state'] != 'paid']
    if only_issues:
        rows = [r for r in rows if r['errors'] or r['warnings']]
    if q:
        rows = [r for r in rows if q in f"{r['emp'].employee_code} {r['emp'].full_name} {r['emp'].ifsc_code} {r['emp'].bank_name}".lower()]
    kpi = {'checked': len(rows), 'errors': sum(1 for r in rows if r['errors']), 'warnings': sum(1 for r in rows if r['warnings'] and not r['errors'])}
    return render(request, 'bank/validation.html', {
        'page': _page(request, rows), 'qs': _qs(request), 'kpi': kpi, 'runs': _released_runs(company), 'run': run,
        'mode': mode, 'modes': BankBatch.MODE_CHOICES, 'only_issues': only_issues, 'q': request.GET.get('q', '')})


# --------------------------------------------------------------------------- pages: batches
@company_owner_required
def bank_batches(request):
    company = _company(request)
    status = request.GET.get('status', '')
    q = (request.GET.get('q') or '').strip()
    qs = BankBatch.objects.filter(company=company).select_related('payroll_run', 'created_by', 'approved_by')
    for b in qs.filter(status__in=(BankBatch.FILE_GENERATED, BankBatch.SENT)):
        sync_batch(b)
    if status in dict(BankBatch.STATUS_CHOICES):
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(reference__icontains=q) | Q(payroll_run__month__icontains=q))
    counts = Counter(BankBatch.objects.filter(company=company).values_list('status', flat=True))
    return render(request, 'bank/batches.html', {
        'page': _page(request, qs), 'qs': _qs(request), 'status': status, 'q': q, 'status_choices': BankBatch.STATUS_CHOICES,
        'kpi': {'waiting': counts[BankBatch.PENDING_APPROVAL], 'approved': counts[BankBatch.APPROVED] + counts[BankBatch.FILE_GENERATED],
                'sent': counts[BankBatch.SENT], 'completed': counts[BankBatch.COMPLETED]}})


@company_owner_required
def bank_batch_new(request):
    company = _company(request)
    run = _run_from_request(request, company)
    mode = (request.POST.get('mode') or request.GET.get('mode') or 'NEFT')
    mode = mode if mode in dict(BankBatch.MODE_CHOICES) else 'NEFT'
    if request.method == 'POST':
        if run is None:
            messages.error(request, 'Choose the released payroll month first.')
            return redirect('bank_batch_new')
        line_ids = [i for i in (_int(v) for v in request.POST.getlist('line')) if i]
        if not line_ids:
            messages.error(request, 'Tick at least one employee.')
            return redirect(f"{request.path}?run={run.pk}&mode={mode}")
        batch, skipped = create_batch(request, company, run, mode, line_ids, request.POST.get('note'))
        for line, reason in skipped[:15]:
            messages.warning(request, f'{line.employee.employee_code} {line.employee.full_name} left out: {reason}')
        if batch is None:
            messages.error(request, 'No batch was created: none of the ticked employees can be paid right now.')
            return redirect(f"{request.path}?run={run.pk}&mode={mode}")
        messages.success(request, f'Batch {batch.reference} created with {batch.item_count} payment(s). It now needs approval.')
        return redirect('bank_batch_detail', pk=batch.pk)
    rows = build_rows(company, run, mode) if run is not None else []
    banks = sorted({r['bank'] for r in rows if r['bank']})
    return render(request, 'bank/batch_new.html', {
        'rows': rows, 'runs': _released_runs(company), 'run': run, 'mode': mode, 'modes': BankBatch.MODE_CHOICES, 'banks': banks,
        'eligible': sum(1 for r in rows if r['eligible']), 'blocked': sum(1 for r in rows if r['errors'])})


@company_owner_required
def bank_batch_detail(request, pk):
    company = _company(request)
    batch = get_object_or_404(BankBatch.objects.select_related('payroll_run', 'created_by', 'approved_by', 'sent_by'), pk=pk, company=company)
    sync_batch(batch)
    items = list(batch.items.select_related('payment__payroll_line__employee', 'payment__verified_by').order_by('id'))
    status_count = Counter(i.payment.status for i in items)
    allowed, self_approval = can_approve(request.user, batch)
    return render(request, 'bank/batch_detail.html', {
        'batch': batch, 'items': items, 'banks': batch_banks(batch), 'status_count': status_count,
        'can_approve': allowed, 'self_approval': self_approval,
        'waiting': status_count['PENDING'] + status_count['INITIATED'],
        'paid_total': sum((i.payment.amount for i in items if i.payment.status == 'PAID'), Decimal('0.00'))})


BATCH_ACTIONS = {
    'approve': ((BankBatch.PENDING_APPROVAL,), BankBatch.APPROVED),
    'reject': ((BankBatch.PENDING_APPROVAL,), BankBatch.REJECTED),
    'mark_sent': ((BankBatch.FILE_GENERATED,), BankBatch.SENT),
    'cancel': ((BankBatch.PENDING_APPROVAL, BankBatch.APPROVED, BankBatch.FILE_GENERATED), BankBatch.CANCELLED),
}


@company_owner_required
@require_POST
def bank_batch_action(request, pk):
    company = _company(request)
    get_object_or_404(BankBatch, pk=pk, company=company)
    action = request.POST.get('action')
    back = 'bank_approvals' if request.POST.get('next') == 'bank_approvals' else None
    target_page = lambda b: redirect(back) if back else redirect('bank_batch_detail', pk=b.pk)   # noqa: E731
    with transaction.atomic():
        batch = BankBatch.objects.select_for_update().get(pk=pk, company=company)
        if action not in BATCH_ACTIONS:
            messages.error(request, 'Unknown action.')
            return target_page(batch)
        allowed_from, target = BATCH_ACTIONS[action]
        if batch.status not in allowed_from:
            messages.error(request, f'{batch.reference} is "{batch.get_status_display()}", so it cannot be changed this way. Someone may have just changed it.')
            return target_page(batch)
        now = timezone.now()
        if action == 'approve':
            ok, self_approval = can_approve(request.user, batch)
            if not ok:
                messages.error(request, 'A different company owner must approve this batch. The person who created it cannot approve it.')
                return target_page(batch)
            bad = batch.items.exclude(payment__status__in=('PENDING', 'INITIATED')).select_related('payment__payroll_line__employee')
            if bad.exists():
                names = ', '.join(i.payment.payroll_line.employee.employee_code for i in bad[:10])
                messages.error(request, f'Cannot approve: some payments are no longer waiting to be paid ({names}). Cancel this batch and create a new one.')
                return target_page(batch)
            batch.approved_by, batch.approved_at, batch.self_approved = request.user, now, self_approval
            detail = f'{batch.reference} approved' + (' (self-approved: only one owner login)' if self_approval else '')
            log_kind = 'APPROVE'
        elif action == 'reject':
            reason = (request.POST.get('reason') or '').strip()
            if not reason:
                messages.error(request, 'Please write why the batch is rejected.')
                return target_page(batch)
            batch.rejection_reason = reason[:255]
            detail = f'{batch.reference} rejected: {batch.rejection_reason}'
            log_kind = 'REJECT'
        elif action == 'mark_sent':
            batch.sent_by, batch.sent_at = request.user, now
            detail, log_kind = f'{batch.reference} marked as sent to the bank', 'UPDATE'
        else:   # cancel
            if batch.status == BankBatch.FILE_GENERATED and request.POST.get('confirm') != '1':
                messages.error(request, 'Tick the box to confirm the bank file was NOT uploaded to the bank.')
                return target_page(batch)
            detail, log_kind = f'{batch.reference} cancelled', 'UPDATE'
        batch.status = target
        batch.save()
        log_action(request, log_kind, batch, details=detail)
    messages.success(request, detail[0].upper() + detail[1:] + '.')
    return target_page(batch)


@company_owner_required
def bank_batch_file(request, pk):
    company = _company(request)
    fmt = 'xlsx' if request.GET.get('format') == 'xlsx' else 'csv'
    bank = (request.GET.get('bank') or '').strip().upper()[:4]
    with transaction.atomic():
        batch = get_object_or_404(BankBatch.objects.select_for_update(), pk=pk, company=company)
        if not batch.file_allowed:
            messages.error(request, 'The bank file can only be created after the batch is approved.')
            return redirect('bank_batch_detail', pk=batch.pk)
        rows = batch_file_rows(batch, bank or None)
        if batch.status == BankBatch.APPROVED:
            batch.status, batch.file_generated_at = BankBatch.FILE_GENERATED, timezone.now()
            batch.save(update_fields=['status', 'file_generated_at', 'updated_at'])
        log_action(request, 'EXPORT', batch, details=f'Bank file for {batch.reference} downloaded ({fmt}, {bank or "all banks"}, {len(rows)} payments)')
    return table_response(fmt, f'{batch.reference}_{bank or "ALL"}', FILE_HEADERS, rows)


@company_owner_required
def bank_approvals(request):
    company = _company(request)
    qs = BankBatch.objects.filter(company=company).select_related('payroll_run', 'created_by', 'approved_by')
    waiting = list(qs.filter(status=BankBatch.PENDING_APPROVAL).order_by('created_at'))
    for b in waiting:
        b.can_approve, b.self_approval = can_approve(request.user, b)
    recent = qs.filter(status__in=(BankBatch.APPROVED, BankBatch.REJECTED, BankBatch.FILE_GENERATED, BankBatch.SENT, BankBatch.COMPLETED))[:15]
    owners = User.objects.filter(company=company, role='COMPANY_OWNER', is_active=True).count()
    return render(request, 'bank/approvals.html', {'waiting': waiting, 'recent': recent, 'owners': owners})


# --------------------------------------------------------------------------- pages: instruction export
@company_owner_required
def bank_export(request):
    company = _company(request)
    run = _run_from_request(request, company)
    status = request.GET.get('status', '')
    bank = (request.GET.get('bank') or '').strip().upper()[:4]
    payments = (BankPayment.objects.filter(company=company)
                .select_related('payroll_line__employee', 'payroll_line__payroll_run').order_by('-created_at', '-id'))
    if run is not None:
        payments = payments.filter(payroll_line__payroll_run=run)
    if status in dict(BankPayment.STATUS_CHOICES):
        payments = payments.filter(status=status)
    if bank:
        payments = payments.filter(ifsc_code__istartswith=bank)
    fmt = request.GET.get('export')
    if fmt in ('csv', 'xlsx'):
        headers = ['Payment ID', 'Month', 'Employee Code', 'Beneficiary Name', 'Account Number', 'IFSC', 'Bank', 'Amount', 'Mode', 'Status', 'UTR', 'Failure Reason']
        rows = [[p.pk, p.payroll_line.payroll_run.month, p.payroll_line.employee.employee_code, p.account_holder_name, p.account_no,
                 p.ifsc_code, p.bank_name, p.amount, p.mode, p.get_status_display(), p.reference_number, p.failure_reason] for p in payments[:20000]]
        log_action(request, 'EXPORT', company=company, details=f'Bank payment instructions exported ({fmt}, {len(rows)} payments)')
        return table_response(fmt, f'payment_instructions_{timezone.localdate():%Y%m%d}', headers, rows)
    batches = BankBatch.objects.filter(company=company, status__in=(BankBatch.APPROVED, BankBatch.FILE_GENERATED, BankBatch.SENT, BankBatch.COMPLETED))[:10]
    return render(request, 'bank/export.html', {
        'page': _page(request, payments), 'qs': _qs(request), 'runs': _released_runs(company), 'run': run, 'status': status, 'bank': bank,
        'status_choices': BankPayment.STATUS_CHOICES, 'batches': batches})


# --------------------------------------------------------------------------- pages: duplicates
@company_owner_required
def bank_duplicates(request):
    company = _company(request)
    shared = shared_accounts(company)
    names = {e.employee_code: e.full_name for e in Employee.objects.filter(company=company)}
    shared_rows = [{'account': f'...{acc[-4:]}', 'ifsc': ifsc, 'people': [f'{c} {names.get(c, "")}'.strip() for c in codes]}
                   for (acc, ifsc), codes in sorted(shared.items(), key=lambda kv: kv[1])]
    per_month = defaultdict(list)
    for p in BankPayment.objects.filter(company=company).exclude(status='FAILED').select_related('payroll_line__employee', 'payroll_line__payroll_run'):
        per_month[(p.payroll_line.employee_id, p.payroll_line.payroll_run.month.strip().lower())].append(p)
    repeat_rows = [{'employee': ps[0].payroll_line.employee, 'month': ps[0].payroll_line.payroll_run.month,
                    'payments': ps} for ps in per_month.values() if len(ps) > 1]
    in_two = (BankBatchItem.objects.filter(batch__company=company, batch__status__in=BankBatch.ACTIVE_STATUSES)
              .order_by().values('payment_id').annotate(n=Count('id')).filter(n__gt=1))
    two_batch_ids = [r['payment_id'] for r in in_two]
    two_batch = list(BankPayment.objects.filter(pk__in=two_batch_ids, company=company).select_related('payroll_line__employee'))
    return render(request, 'bank/duplicates.html', {
        'shared_rows': shared_rows, 'repeat_rows': repeat_rows, 'two_batch': two_batch,
        'clean': not (shared_rows or repeat_rows or two_batch)})


# --------------------------------------------------------------------------- pages: reconciliation
ALIASES = {
    'payment_id': {'payment id', 'payment ref', 'payment reference', 'customer reference', 'customer ref', 'client reference', 'client ref', 'unique ref'},
    'utr': {'utr', 'utr no', 'utr number', 'utr ref', 'bank reference', 'bank ref', 'bank reference number', 'transaction id',
            'transaction reference', 'txn id', 'txn ref', 'txn reference', 'reference number', 'reference no'},
    'account': {'account number', 'account no', 'beneficiary account', 'beneficiary account number', 'beneficiary account no', 'a c no', 'ac no', 'credit account'},
    'amount': {'amount', 'txn amount', 'transaction amount', 'amount inr', 'credit amount', 'paid amount'},
    'status': {'status', 'txn status', 'transaction status', 'payment status'},
    'reason': {'failure reason', 'reason', 'remarks', 'message', 'reject reason', 'status description'},
}
SUCCESS_WORDS = ('success', 'processed', 'paid', 'completed', 'credited', 'settled', 'executed')
FAIL_WORDS = ('fail', 'reject', 'return', 'revers', 'declin', 'unsuccess', 'cancel')


def _norm_header(value):
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower()).strip()


def _column_map(cells):
    found = {}
    for idx, cell in enumerate(cells):
        h = _norm_header(cell)
        for key, names in ALIASES.items():
            if h in names and key not in found:
                found[key] = idx
    return found


def _cell_text(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return '' if value is None else str(value).strip()


def read_statement(upload):
    """Rows of the uploaded bank file as a list of lists, or (None, error message)."""
    name = (upload.name or '').lower()
    if upload.size > MAX_UPLOAD_BYTES:
        return None, 'The file is larger than 2 MB.'
    data = upload.read()
    if name.endswith('.csv'):
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = data.decode('latin-1')
        rows = list(csv.reader(io.StringIO(text)))
    elif name.endswith('.xlsx'):
        try:
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            rows = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
        except Exception:
            return None, 'This Excel file could not be read. Save it again as .xlsx or .csv.'
    else:
        return None, 'Upload a .csv or .xlsx file.'
    if len(rows) > MAX_ROWS + 50:
        return None, f'The file has more than {MAX_ROWS} rows. Split it and upload it in parts.'
    return rows, ''


def analyse_statement(company, rows):
    """Match each bank row to a payment. Nothing is written to the database here."""
    head_i, cols = None, {}
    for i, r in enumerate(rows[:30]):
        m = _column_map(r)
        if ('utr' in m or 'status' in m) and ('payment_id' in m or 'account' in m):
            head_i, cols = i, m
            break
    if head_i is None:
        return [], 'Could not find the columns. The file needs a UTR or Status column, and a Payment ID or Account Number column.'
    pool = (BankPayment.objects.filter(company=company, batch_items__batch__status__in=(BankBatch.FILE_GENERATED, BankBatch.SENT))
            .select_related('payroll_line__employee').distinct())
    by_id = {p.pk: p for p in pool}
    by_acc = defaultdict(list)
    for p in by_id.values():
        by_acc[(clean_account(p.account_no).lstrip('0'), p.amount)].append(p)
    seen_utr, seen_pay, out = set(), set(), []
    # One query for every UTR in the file (instead of one per row): which ones are already used, and by which payment.
    used_utr = {}
    if 'utr' in cols:
        wanted = {_cell_text(r[cols['utr']]) for r in rows[head_i + 1:] if cols['utr'] < len(r)} - {''}
        used_utr = dict(BankPayment.objects.filter(reference_number__in=wanted).values_list('reference_number', 'pk'))
    for n, raw in enumerate(rows[head_i + 1:], start=head_i + 2):
        if not any(_cell_text(c) for c in raw):
            continue
        get = lambda k: _cell_text(raw[cols[k]]) if k in cols and cols[k] < len(raw) else ''   # noqa: E731
        utr, status_text, reason = get('utr'), get('status').lower(), get('reason')
        amount = _money(get('amount')) if get('amount') else None
        row = {'n': n, 'payment_id': None, 'who': '', 'amount': '' if amount is None else str(amount), 'utr': utr,
               'action': 'SKIP', 'note': '', 'outcome': 'skip'}
        pay_id, payment = _int(get('payment_id')), None
        if pay_id is not None:
            payment = by_id.get(pay_id)
            if payment is None:
                row['note'] = 'Payment ID is not in a generated or sent batch of your company'
        elif get('account') and amount is not None:
            hits = by_acc.get((clean_account(get('account')).lstrip('0'), amount), [])
            if len(hits) == 1:
                payment = hits[0]
            else:
                row['note'] = 'No payment with this account and amount' if not hits else 'More than one payment has this account and amount - add the Payment ID column'
        else:
            row['note'] = 'Row needs a Payment ID, or an account number and amount'
        if payment is not None:
            row['payment_id'] = payment.pk
            row['who'] = f'{payment.payroll_line.employee.employee_code} {payment.payroll_line.employee.full_name}'
            is_fail = any(w in status_text for w in FAIL_WORDS)
            is_ok = (not is_fail) and (any(w in status_text for w in SUCCESS_WORDS) or (not status_text and bool(utr)))
            if payment.pk in seen_pay:
                row['note'] = 'This payment appears twice in the file'
            elif payment.status == 'PAID':
                row['note'] = 'Already marked paid'
            elif payment.status == 'FAILED':
                row['note'] = 'Already marked failed - retry it in a new batch first'
            elif amount is not None and amount != payment.amount:
                row['action'], row['outcome'] = 'REVIEW', 'review'
                row['note'] = f'Amount in file {amount} is not the payment amount {payment.amount}'
            elif is_fail:
                row['action'], row['outcome'], row['note'] = 'FAILED', 'ok', (reason or 'Rejected by bank')[:255]
            elif is_ok:
                if not utr or len(utr) > 60:
                    row['action'], row['outcome'], row['note'] = 'REVIEW', 'review', 'UTR is missing or longer than 60 characters'
                elif utr in seen_utr or used_utr.get(utr, payment.pk) != payment.pk:
                    row['action'], row['outcome'], row['note'] = 'REVIEW', 'review', f'UTR {utr} is already used by another payment'
                else:
                    row['action'], row['outcome'], row['note'] = 'PAID', 'ok', ''
                    seen_utr.add(utr)
            else:
                row['action'], row['outcome'], row['note'] = 'REVIEW', 'review', f'Status "{status_text or "empty"}" is not recognised'
            seen_pay.add(payment.pk)
        out.append(row)
    return out, ''


def apply_reconciliation(request, company, rows):
    """Write the confirmed rows. Every payment is locked and checked again first."""
    done = Counter()
    touched = set()
    for r in rows:
        if r.get('outcome') != 'ok':
            continue
        try:
            with transaction.atomic():
                payment = BankPayment.objects.select_for_update().get(pk=r['payment_id'], company=company)
                if payment.status not in ('PENDING', 'INITIATED'):
                    done['changed'] += 1
                    continue
                old = payment.status
                if r['action'] == 'PAID':
                    payment.status, payment.reference_number = 'PAID', r['utr']
                    payment.paid_at, payment.verified_by, payment.failure_reason = timezone.now(), request.user, ''
                else:
                    payment.status, payment.failure_reason = 'FAILED', r['note'][:255]
                payment.save()
                log_action(request, 'PAYMENT_STATUS_CHANGE', payment, company=company,
                           details=f'{payment.payroll_line.payslip_number}: {old} -> {payment.status} (bank reconciliation)'
                                   + (f' ref={payment.reference_number}' if payment.status == 'PAID' else ''))
                done[payment.status] += 1
                touched.update(BankBatchItem.objects.filter(payment=payment).values_list('batch_id', flat=True))
        except IntegrityError:
            done['utr_clash'] += 1
        except BankPayment.DoesNotExist:
            done['changed'] += 1
    for batch in BankBatch.objects.filter(pk__in=touched, company=company):
        sync_batch(batch)
    return done


@company_owner_required
def bank_reconcile(request):
    company = _company(request)
    session_key = 'bank_recon'
    if request.method == 'POST':
        step = request.POST.get('step')
        if step == 'upload':
            upload = request.FILES.get('statement')
            if upload is None:
                messages.error(request, 'Choose the bank file first.')
                return redirect('bank_reconcile')
            rows, error = read_statement(upload)
            result, error2 = ([], error) if rows is None else analyse_statement(company, rows)
            if error2:
                messages.error(request, error2)
                return redirect('bank_reconcile')
            request.session[session_key] = {'company': company.pk, 'rows': result, 'at': time.time(), 'name': upload.name[:80]}
            return redirect('bank_reconcile')
        if step == 'confirm':
            saved = request.session.get(session_key)
            fresh = saved and saved.get('company') == company.pk and time.time() - saved.get('at', 0) < RECON_MINUTES * 60
            if not fresh:
                request.session.pop(session_key, None)
                messages.error(request, 'The preview has expired. Upload the file again.')
                return redirect('bank_reconcile')
            done = apply_reconciliation(request, company, saved['rows'])
            request.session.pop(session_key, None)
            messages.success(request, f"Reconciliation saved: {done['PAID']} marked Paid, {done['FAILED']} marked Failed.")
            if done['changed'] or done['utr_clash']:
                messages.warning(request, f"{done['changed'] + done['utr_clash']} row(s) were skipped because the payment changed after the preview.")
            return redirect('bank_reconcile')
        if step == 'cancel':
            request.session.pop(session_key, None)
            return redirect('bank_reconcile')
    saved = request.session.get(session_key)
    preview = None
    if saved and saved.get('company') == company.pk and time.time() - saved.get('at', 0) < RECON_MINUTES * 60:
        rows = saved['rows']
        preview = {'rows': rows, 'name': saved.get('name', ''), 'ok': sum(1 for r in rows if r['outcome'] == 'ok'),
                   'review': sum(1 for r in rows if r['outcome'] == 'review'), 'skip': sum(1 for r in rows if r['outcome'] == 'skip'),
                   'paid': sum(1 for r in rows if r['outcome'] == 'ok' and r['action'] == 'PAID'),
                   'failed': sum(1 for r in rows if r['outcome'] == 'ok' and r['action'] == 'FAILED')}
    waiting = BankPayment.objects.filter(company=company, status__in=('PENDING', 'INITIATED'),
                                         batch_items__batch__status__in=(BankBatch.FILE_GENERATED, BankBatch.SENT)).distinct().count()
    return render(request, 'bank/reconcile.html', {'preview': preview, 'waiting': waiting, 'minutes': RECON_MINUTES})


# --------------------------------------------------------------------------- pages: audit trail
@company_owner_required
def bank_audit(request):
    company = _company(request)
    q = (request.GET.get('q') or '').strip()
    action = request.GET.get('action', '')
    logs = (AuditLog.objects.filter(company=company)
            .filter(Q(model_name__in=('BankBatch', 'BankPayment')) | Q(action='EXPORT', details__startswith='Bank '))
            .select_related('actor'))
    if action in dict(AuditLog.ACTION_CHOICES):
        logs = logs.filter(action=action)
    if q:
        logs = logs.filter(Q(details__icontains=q) | Q(actor__username__icontains=q))
    for key, lookup in (('from', 'timestamp__date__gte'), ('to', 'timestamp__date__lte')):
        try:
            logs = logs.filter(**{lookup: datetime.date.fromisoformat(request.GET.get(key, ''))})
        except ValueError:
            pass
    return render(request, 'bank/audit.html', {
        'page': Paginator(logs, 50).get_page(request.GET.get('page')), 'qs': _qs(request), 'q': q, 'action': action,
        'actions': [a for a in AuditLog.ACTION_CHOICES if a[0] in ('CREATE', 'APPROVE', 'REJECT', 'UPDATE', 'EXPORT', 'PAYMENT_STATUS_CHANGE')],
        'date_from': request.GET.get('from', ''), 'date_to': request.GET.get('to', '')})


urlpatterns = [
    path('bank-payments/validation/', bank_validation, name='bank_validation'),
    path('bank-payments/batches/', bank_batches, name='bank_batches'),
    path('bank-payments/batches/new/', bank_batch_new, name='bank_batch_new'),
    path('bank-payments/batches/<int:pk>/', bank_batch_detail, name='bank_batch_detail'),
    path('bank-payments/batches/<int:pk>/action/', bank_batch_action, name='bank_batch_action'),
    path('bank-payments/batches/<int:pk>/file/', bank_batch_file, name='bank_batch_file'),
    path('bank-payments/approvals/', bank_approvals, name='bank_approvals'),
    path('bank-payments/export/', bank_export, name='bank_export'),
    path('bank-payments/duplicates/', bank_duplicates, name='bank_duplicates'),
    path('bank-payments/reconciliation/', bank_reconcile, name='bank_reconcile'),
    path('bank-payments/audit-trail/', bank_audit, name='bank_audit'),
]
