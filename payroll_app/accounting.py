"""Accounting Integration (company owners only).

Features, all under "Accounting Integration" in the payroll structure:
  Salary Expense Journal, Salary Payable Entries, Employer Contribution Entries, Statutory Liability Entries,
  Loan Recovery Entries, Branch-Wise Cost Allocation (by department for now), General Ledger Mapping,
  Accounting Export, Reconciliation.

A journal is built from a RELEASED payroll run:
  Debit  salary expense (per department) + employer PF / ESI expense
  Credit salary payable (net pay) + PF, ESI, TDS, PT, loan recovery, insurance and other deductions payable
When bank payments are marked Paid, a payment entry moves them from Salary Payable to Bank.
A journal is only posted when it balances; the books never get a one-sided entry.
"""
import csv
import datetime
import io
from collections import OrderedDict, defaultdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.utils import timezone
from django.views.decorators.http import require_POST

from .accounting_models import AccountingSettings, JournalEntry, JournalLine, JournalPaymentLink, LedgerMapping
from .audit import log_action
from .models import BankPayment, Company, CompanySettings, PayrollRun, PayrollRunLine
from .permissions import company_owner_required

PAGE_SIZE = 25
CENT = Decimal('0.01')
ZERO = Decimal('0.00')

# head code -> (label, default ledger name, type)
HEADS = OrderedDict([
    ('SALARY_EXPENSE', ('Salary and wages expense', 'Salaries and Wages', 'Expense')),
    ('EMPLOYER_PF_EXPENSE', ('Employer PF contribution expense', 'Employer PF Expense', 'Expense')),
    ('EMPLOYER_ESI_EXPENSE', ('Employer ESI contribution expense', 'Employer ESI Expense', 'Expense')),
    ('SALARY_PAYABLE', ('Salary payable', 'Salary Payable', 'Liability')),
    ('PF_PAYABLE', ('PF payable', 'PF Payable', 'Liability')),
    ('ESI_PAYABLE', ('ESI payable', 'ESI Payable', 'Liability')),
    ('TDS_PAYABLE', ('TDS on salary payable', 'TDS Payable on Salary', 'Liability')),
    ('PT_PAYABLE', ('Professional tax payable', 'Professional Tax Payable', 'Liability')),
    ('LOAN_RECOVERY', ('Employee loan recovery', 'Employee Loans and Advances', 'Asset')),
    ('INSURANCE_PAYABLE', ('Insurance premium payable', 'Insurance Premium Payable', 'Liability')),
    ('OTHER_DEDUCTIONS', ('Other deductions payable', 'Other Deductions Payable', 'Liability')),
    ('BANK', ('Bank account (salary payments)', 'Bank Account', 'Asset')),
])


# --------------------------------------------------------------------------- helpers
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


def _d(value):
    try:
        return Decimal(str(value if value is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return ZERO


def _qs(request):
    q = request.GET.copy()
    q.pop('page', None)
    return q.urlencode()


def _safe(value):
    """A text cell that starts with = + - @ is read as a formula by Excel; make it plain text."""
    s = str(value)
    return "'" + s if s[:1] in ('=', '+', '-', '@', '\t', '\r') else s


def table_response(fmt, stem, headers, rows):
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


def _released_runs(company):
    return PayrollRun.objects.filter(company=company, status='RELEASED').order_by('-created_at', '-id')


def _run_from_request(request, company):
    rid = _int(request.GET.get('run') or request.POST.get('run_id'))
    return _released_runs(company).filter(pk=rid).first() if rid else None


def ledger_map(company):
    """{head: (ledger name, ledger code, is_custom)}: the owner's mapping, or the default name."""
    out = {h: (info[1], '', False) for h, info in HEADS.items()}
    for m in LedgerMapping.objects.filter(company=company):
        if m.head in out:
            out[m.head] = (m.ledger_name, m.ledger_code, True)
    return out


def _entry_date(run):
    try:
        from .payroll_engine import parse_month
        period = parse_month(run.month)
        if period:
            return period[1]
    except Exception:                                    # an odd month text must never stop a journal
        pass
    return timezone.localdate()


# --------------------------------------------------------------------------- figures
def run_figures(company, run):
    """Everything the journals need from one payroll run. Nothing is written."""
    cs = CompanySettings.objects.filter(company=company).first()
    acc = AccountingSettings.objects.filter(company=company).first()
    base_pf = _d(getattr(cs, 'pf_percentage', 0)) if cs else ZERO
    base_esi = _d(getattr(cs, 'esi_percentage', 0)) if cs else ZERO
    emp_pf_pct = _d(acc.employer_pf_percent) if acc else ZERO
    emp_esi_pct = _d(acc.employer_esi_percent) if acc else ZERO
    t = {k: ZERO for k in ('earnings', 'pf', 'esi', 'tds', 'pt', 'loan', 'insurance', 'other', 'diff', 'net', 'deductions')}
    dept = defaultdict(lambda: {'count': 0, 'earnings': ZERO})
    employer_pf = employer_esi = ZERO
    problems, diff_people, count = [], 0, 0
    for line in PayrollRunLine.objects.filter(payroll_run=run).select_related('employee').order_by('employee__employee_code'):
        emp = line.employee
        count += 1
        if emp.company_id != company.pk:
            problems.append(f'{emp.employee_code}: employee belongs to another company')
            continue
        earnings, deductions, net = _d(line.total_earnings), _d(line.total_deductions), _d(line.net_pay)
        if earnings - deductions != net:
            problems.append(f'{emp.employee_code} {emp.full_name}: earnings {earnings} minus deductions {deductions} is not net pay {net}')
            continue
        heads = _d(line.pf) + _d(line.esi) + _d(line.tds) + _d(line.professional_tax) + _d(line.loan_deduction) + _d(line.insurance_deduction) + _d(line.other_deductions)
        diff = deductions - heads
        if diff != ZERO:
            diff_people += 1
        for key, val in (('earnings', earnings), ('pf', _d(line.pf)), ('esi', _d(line.esi)), ('tds', _d(line.tds)), ('pt', _d(line.professional_tax)),
                         ('loan', _d(line.loan_deduction)), ('insurance', _d(line.insurance_deduction)), ('other', _d(line.other_deductions)),
                         ('diff', diff), ('net', net), ('deductions', deductions)):
            t[key] += val
        name = (emp.department or '').strip() or 'Unassigned'
        dept[name]['count'] += 1
        dept[name]['earnings'] += earnings
        if base_pf and emp_pf_pct:
            employer_pf += (_d(line.pf) * emp_pf_pct / base_pf).quantize(CENT, rounding=ROUND_HALF_UP)
        if base_esi and emp_esi_pct:
            employer_esi += (_d(line.esi) * emp_esi_pct / base_esi).quantize(CENT, rounding=ROUND_HALF_UP)
    notes = []
    if diff_people:
        notes.append(f'{diff_people} employee(s): total deductions differ from the sum of the deduction heads. The difference ({t["diff"]}) is posted to Other deductions payable.')
    if not emp_pf_pct and not emp_esi_pct:
        notes.append('Employer PF / ESI percentages are not set, so no employer contribution entries are made. Set them on the Employer Contribution Entries page.')
    elif (emp_pf_pct and not base_pf) or (emp_esi_pct and not base_esi):
        notes.append('The company PF / ESI percentage is missing in Company Settings, so the employer share cannot be worked out.')
    return {'t': t, 'dept': OrderedDict(sorted(dept.items())), 'employer_pf': employer_pf, 'employer_esi': employer_esi,
            'problems': problems, 'notes': notes, 'count': count}


def build_lines(company, run, fig):
    """[(head, department, debit, credit, narration)] for the salary journal."""
    t, month = fig['t'], run.month
    out = []
    for name, d in fig['dept'].items():
        if d['earnings']:
            out.append(('SALARY_EXPENSE', name, d['earnings'], ZERO, f'Salary {month} - {name}'))
    if fig['employer_pf']:
        out.append(('EMPLOYER_PF_EXPENSE', '', fig['employer_pf'], ZERO, f'Employer PF {month}'))
    if fig['employer_esi']:
        out.append(('EMPLOYER_ESI_EXPENSE', '', fig['employer_esi'], ZERO, f'Employer ESI {month}'))
    credits = [('SALARY_PAYABLE', t['net'], f'Net salary payable {month}'), ('PF_PAYABLE', t['pf'] + fig['employer_pf'], f'PF payable {month}'),
               ('ESI_PAYABLE', t['esi'] + fig['employer_esi'], f'ESI payable {month}'), ('TDS_PAYABLE', t['tds'], f'TDS payable {month}'),
               ('PT_PAYABLE', t['pt'], f'Professional tax payable {month}'), ('LOAN_RECOVERY', t['loan'], f'Loan recovery {month}'),
               ('INSURANCE_PAYABLE', t['insurance'], f'Insurance payable {month}'),
               ('OTHER_DEDUCTIONS', t['other'] + t['diff'], f'Other deductions {month}')]
    for head, amount, narr in credits:
        if amount:
            out.append((head, '', ZERO, amount, narr))
    return out


def _next_voucher(company, kind):
    prefix = 'SJ' if kind == JournalEntry.SALARY else 'PJ'
    year = timezone.localdate().year
    n = JournalEntry.objects.filter(company=company, voucher_no__startswith=f'{prefix}-{year}-').count() + 1
    while JournalEntry.objects.filter(company=company, voucher_no=f'{prefix}-{year}-{n:04d}').exists():
        n += 1
    return f'{prefix}-{year}-{n:04d}'


def _make_entry(request, company, run, kind, lines, narration):
    mapping = ledger_map(company)
    entry = JournalEntry.objects.create(
        company=company, payroll_run=run, kind=kind, voucher_no=_next_voucher(company, kind), entry_date=_entry_date(run),
        narration=narration[:255], created_by=request.user,
        total_debit=sum((l[2] for l in lines), ZERO), total_credit=sum((l[3] for l in lines), ZERO))
    JournalLine.objects.bulk_create([JournalLine(entry=entry, head=h, ledger_name=mapping[h][0], ledger_code=mapping[h][1], department=dept,
                                                 debit=dr, credit=cr, narration=narr[:255]) for h, dept, dr, cr, narr in lines])
    return entry


def post_salary_journal(request, company, run):
    """(entry, error message). Posts the salary journal of a released run once, and only if it balances."""
    try:
        with transaction.atomic():
            Company.objects.select_for_update().get(pk=company.pk)          # two people posting at once queue up
            run = PayrollRun.objects.get(pk=run.pk, company=company)
            if run.status != 'RELEASED':
                return None, 'Only a released payroll run can be posted.'
            if JournalEntry.objects.filter(payroll_run=run, kind=JournalEntry.SALARY, status=JournalEntry.POSTED).exists():
                return None, f'{run.month} already has a posted salary journal.'
            fig = run_figures(company, run)
            if not fig['count']:
                return None, f'{run.month} has no payslips to post.'
            if fig['problems']:
                return None, 'Not posted, the payroll figures do not add up: ' + '; '.join(fig['problems'][:5])
            lines = build_lines(company, run, fig)
            debit, credit = sum((l[2] for l in lines), ZERO), sum((l[3] for l in lines), ZERO)
            if debit != credit:
                return None, f'Not posted: debits {debit} and credits {credit} differ.'
            entry = _make_entry(request, company, run, JournalEntry.SALARY, lines, f'Salary journal {run.month}')
            log_action(request, 'CREATE', entry, details=f'Salary journal {entry.voucher_no} posted for {run.month}: {debit}')
    except IntegrityError:
        return None, f'{run.month} was just posted by someone else.'
    return entry, ''


def post_payment_journal(request, company, run):
    """(entry, error message). Moves newly paid bank payments of a run from Salary Payable to Bank, each payment once."""
    try:
        with transaction.atomic():
            Company.objects.select_for_update().get(pk=company.pk)
            run = PayrollRun.objects.get(pk=run.pk, company=company)
            if not JournalEntry.objects.filter(payroll_run=run, kind=JournalEntry.SALARY, status=JournalEntry.POSTED).exists():
                return None, 'Post the salary journal for this month first.'
            already = set(JournalPaymentLink.objects.filter(payment__company=company).values_list('payment_id', flat=True))
            payments = [p for p in BankPayment.objects.filter(company=company, payroll_line__payroll_run=run, status='PAID') if p.pk not in already]
            if not payments:
                return None, 'No newly paid bank payments to post.'
            total = sum((_d(p.amount) for p in payments), ZERO)
            lines = [('SALARY_PAYABLE', '', total, ZERO, f'Salary paid {run.month}'), ('BANK', '', ZERO, total, f'Salary paid {run.month}')]
            entry = _make_entry(request, company, run, JournalEntry.PAYMENT, lines, f'Salary payment {run.month}: {len(payments)} payment(s)')
            JournalPaymentLink.objects.bulk_create([JournalPaymentLink(entry=entry, payment=p) for p in payments])
            log_action(request, 'CREATE', entry, details=f'Payment entry {entry.voucher_no} posted for {run.month}: {len(payments)} payments, {total}')
    except IntegrityError:
        return None, 'These payments were just posted by someone else.'
    return entry, ''


def reverse_journal(request, company, entry_id, reason):
    """Error message, or '' when reversed. A reversed salary journal lets the month be posted again."""
    with transaction.atomic():
        Company.objects.select_for_update().get(pk=company.pk)
        entry = get_object_or_404(JournalEntry, pk=entry_id, company=company)
        if entry.status != JournalEntry.POSTED:
            return f'{entry.voucher_no} is already reversed.'
        if not reason:
            return 'Please write why the entry is reversed.'
        if entry.kind == JournalEntry.SALARY and JournalEntry.objects.filter(payroll_run=entry.payroll_run, kind=JournalEntry.PAYMENT, status=JournalEntry.POSTED).exists():
            return 'Reverse the payment entries of this month first.'
        if entry.kind == JournalEntry.PAYMENT:
            entry.payment_links.all().delete()               # those payments can be posted again
        entry.status, entry.reversed_by, entry.reversed_at, entry.reverse_reason = JournalEntry.REVERSED, request.user, timezone.now(), reason[:255]
        entry.save()
        log_action(request, 'UPDATE', entry, details=f'{entry.voucher_no} reversed: {entry.reverse_reason}')
    return ''


# --------------------------------------------------------------------------- pages
@company_owner_required
def acc_journals(request):
    """Feature: Salary Expense Journal."""
    company = _company(request)
    if request.method == 'POST' and request.POST.get('action') == 'post':
        run = _run_from_request(request, company)
        if run is None:
            messages.error(request, 'Choose a released payroll month.')
            return redirect('acc_journals')
        entry, error = post_salary_journal(request, company, run)
        if error:
            messages.error(request, error)
            return redirect('acc_journals')
        messages.success(request, f'Journal {entry.voucher_no} posted: debit and credit {entry.total_debit}.')
        return redirect('acc_journal_detail', pk=entry.pk)
    runs = list(_released_runs(company)[:60])
    posted = {e.payroll_run_id: e for e in JournalEntry.objects.filter(company=company, kind=JournalEntry.SALARY, status=JournalEntry.POSTED, payroll_run__in=runs)}
    agg = {r['payroll_run']: r for r in PayrollRunLine.objects.filter(payroll_run__in=runs).order_by().values('payroll_run')
           .annotate(n=Count('id'), earn=Sum('total_earnings'), net=Sum('net_pay'))}
    rows = [{'run': r, 'entry': posted.get(r.pk), 'n': agg.get(r.pk, {}).get('n', 0), 'earn': agg.get(r.pk, {}).get('earn') or ZERO,
             'net': agg.get(r.pk, {}).get('net') or ZERO} for r in runs]
    entries = JournalEntry.objects.filter(company=company).select_related('payroll_run', 'created_by')
    q = (request.GET.get('q') or '').strip()
    status = request.GET.get('status', '')
    if q:
        entries = entries.filter(Q(voucher_no__icontains=q) | Q(payroll_run__month__icontains=q))
    if status in dict(JournalEntry.STATUS_CHOICES):
        entries = entries.filter(status=status)
    return render(request, 'accounting/journals.html', {
        'rows': rows, 'page': Paginator(entries, PAGE_SIZE).get_page(request.GET.get('page')), 'qs': _qs(request), 'q': q, 'status': status,
        'status_choices': JournalEntry.STATUS_CHOICES})


@company_owner_required
def acc_journal_detail(request, pk):
    company = _company(request)
    entry = get_object_or_404(JournalEntry.objects.select_related('payroll_run', 'created_by', 'reversed_by'), pk=pk, company=company)
    if request.method == 'POST':
        error = reverse_journal(request, company, entry.pk, (request.POST.get('reason') or '').strip())
        messages.error(request, error) if error else messages.success(request, f'{entry.voucher_no} reversed.')
        return redirect('acc_journal_detail', pk=entry.pk)
    return render(request, 'accounting/journal_detail.html', {'entry': entry, 'lines': entry.lines.all()})


@company_owner_required
def acc_payable(request):
    """Feature: Salary Payable Entries."""
    company = _company(request)
    if request.method == 'POST' and request.POST.get('action') == 'post_payment':
        run = _run_from_request(request, company)
        if run is None:
            messages.error(request, 'Choose a released payroll month.')
            return redirect('acc_payable')
        entry, error = post_payment_journal(request, company, run)
        if error:
            messages.error(request, error)
            return redirect('acc_payable')
        messages.success(request, f'Payment entry {entry.voucher_no} posted: {entry.total_debit}.')
        return redirect('acc_journal_detail', pk=entry.pk)
    return render(request, 'accounting/payable.html', {'rows': _reconcile_rows(company)})


@company_owner_required
def acc_employer(request):
    """Feature: Employer Contribution Entries."""
    company = _company(request)
    acc = AccountingSettings.objects.filter(company=company).first()
    if request.method == 'POST':
        try:
            pf, esi = Decimal(request.POST.get('employer_pf_percent', '0') or '0'), Decimal(request.POST.get('employer_esi_percent', '0') or '0')
        except InvalidOperation:
            pf = esi = Decimal('-1')
        if not (pf.is_finite() and esi.is_finite() and Decimal('0') <= pf <= Decimal('100') and Decimal('0') <= esi <= Decimal('100')):
            messages.error(request, 'Enter percentages between 0 and 100.')
        else:
            obj, _created = AccountingSettings.objects.update_or_create(company=company, defaults={'employer_pf_percent': pf, 'employer_esi_percent': esi})
            log_action(request, 'UPDATE', obj, company=company, details=f'Employer contribution percentages set: PF {pf}, ESI {esi}')
            messages.success(request, 'Employer contribution percentages saved. They apply to journals posted from now on.')
        return redirect('acc_employer')
    rows = []
    for run in _released_runs(company)[:24]:
        fig = run_figures(company, run)
        rows.append({'run': run, 'pf': fig['employer_pf'], 'esi': fig['employer_esi'], 'count': fig['count']})
    return render(request, 'accounting/employer.html', {'rows': rows, 'acc': acc})


@company_owner_required
def acc_statutory(request):
    """Feature: Statutory Liability Entries."""
    company = _company(request)
    rows = []
    for run in _released_runs(company)[:24]:
        fig = run_figures(company, run)
        t = fig['t']
        rows.append({'run': run, 'pf': t['pf'] + fig['employer_pf'], 'esi': t['esi'] + fig['employer_esi'], 'tds': t['tds'], 'pt': t['pt'],
                     'total': t['pf'] + fig['employer_pf'] + t['esi'] + fig['employer_esi'] + t['tds'] + t['pt'],
                     'posted': JournalEntry.objects.filter(payroll_run=run, kind=JournalEntry.SALARY, status=JournalEntry.POSTED).first()})
    return render(request, 'accounting/statutory.html', {'rows': rows})


@company_owner_required
def acc_loans(request):
    """Feature: Loan Recovery Entries."""
    company = _company(request)
    run = _run_from_request(request, company)
    people = []
    if run is not None:
        for line in PayrollRunLine.objects.filter(payroll_run=run, employee__company=company).exclude(loan_deduction=0).select_related('employee').order_by('employee__employee_code'):
            people.append({'emp': line.employee, 'loan': line.loan_deduction, 'insurance': line.insurance_deduction})
    rows = []
    for r in _released_runs(company)[:24]:
        a = PayrollRunLine.objects.filter(payroll_run=r, employee__company=company).aggregate(loan=Sum('loan_deduction'), ins=Sum('insurance_deduction'),
                                                                                              n=Count('id', filter=~Q(loan_deduction=0)))
        rows.append({'run': r, 'loan': a['loan'] or ZERO, 'insurance': a['ins'] or ZERO, 'people': a['n']})
    return render(request, 'accounting/loans.html', {'rows': rows, 'people': people, 'run': run, 'runs': _released_runs(company)[:24]})


@company_owner_required
def acc_allocation(request):
    """Feature: Branch-Wise Cost Allocation. Employees have a department but no branch yet, so cost is split by department."""
    company = _company(request)
    run = _run_from_request(request, company)
    rows, total = [], ZERO
    if run is not None:
        fig = run_figures(company, run)
        total = fig['t']['earnings']
        rows = [{'dept': name, 'count': d['count'], 'earnings': d['earnings'],
                 'share': (d['earnings'] * 100 / total).quantize(CENT) if total else ZERO} for name, d in fig['dept'].items()]
    return render(request, 'accounting/allocation.html', {'rows': rows, 'total': total, 'run': run, 'runs': _released_runs(company)[:24]})


@company_owner_required
def acc_mapping(request):
    """Feature: General Ledger Mapping."""
    company = _company(request)
    if request.method == 'POST':
        errors, changes = [], []
        for head in HEADS:
            name, code = (request.POST.get(f'name_{head}') or '').strip(), (request.POST.get(f'code_{head}') or '').strip()
            if len(name) > 100 or len(code) > 30:
                errors.append(f'{HEADS[head][0]}: the name is limited to 100 characters and the code to 30.')
            elif code and not name:
                errors.append(f'{HEADS[head][0]}: enter a ledger name for the code.')
            else:
                changes.append((head, name, code))
        if errors:
            for e in errors[:6]:
                messages.error(request, e)
            return redirect('acc_mapping')
        with transaction.atomic():
            for head, name, code in changes:
                if not name:
                    LedgerMapping.objects.filter(company=company, head=head).delete()
                else:
                    LedgerMapping.objects.update_or_create(company=company, head=head, defaults={'ledger_name': name, 'ledger_code': code})
        log_action(request, 'UPDATE', company, details='Ledger mapping saved')
        messages.success(request, 'Ledger mapping saved. It applies to journals posted from now on; posted journals keep the names they were posted with.')
        return redirect('acc_mapping')
    mapping = ledger_map(company)
    rows = [{'head': h, 'label': info[0], 'type': info[2], 'default': info[1], 'name': mapping[h][0] if mapping[h][2] else '', 'code': mapping[h][1], 'custom': mapping[h][2]}
            for h, info in HEADS.items()]
    return render(request, 'accounting/mapping.html', {'rows': rows})


@company_owner_required
def acc_export(request):
    """Feature: Accounting Export. Journal lines as CSV or Excel for your accounting software."""
    company = _company(request)
    run = _run_from_request(request, company)
    status = request.GET.get('status') if request.GET.get('status') in dict(JournalEntry.STATUS_CHOICES) else JournalEntry.POSTED
    kind = request.GET.get('kind') if request.GET.get('kind') in dict(JournalEntry.KIND_CHOICES) else ''
    lines = JournalLine.objects.filter(entry__company=company, entry__status=status).select_related('entry', 'entry__payroll_run').order_by('entry__entry_date', 'entry_id', 'id')
    if run is not None:
        lines = lines.filter(entry__payroll_run=run)
    if kind:
        lines = lines.filter(entry__kind=kind)
    fmt = request.GET.get('export')
    if fmt in ('csv', 'xlsx'):
        headers = ['Voucher No', 'Date', 'Type', 'Month', 'Ledger Name', 'Ledger Code', 'Department', 'Debit', 'Credit', 'Narration']
        rows = [[l.entry.voucher_no, l.entry.entry_date.isoformat(), l.entry.get_kind_display(), l.entry.payroll_run.month, l.ledger_name, l.ledger_code,
                 l.department, l.debit, l.credit, l.narration] for l in lines[:50000]]
        log_action(request, 'EXPORT', company=company, details=f'Accounting export ({fmt}, {len(rows)} lines)')
        return table_response(fmt, f'accounting_export_{timezone.localdate():%Y%m%d}', headers, rows)
    totals = lines.aggregate(d=Sum('debit'), c=Sum('credit'))
    return render(request, 'accounting/export.html', {
        'page': Paginator(lines, 50).get_page(request.GET.get('page')), 'qs': _qs(request), 'runs': _released_runs(company)[:24], 'run': run,
        'status': status, 'kind': kind, 'status_choices': JournalEntry.STATUS_CHOICES, 'kind_choices': JournalEntry.KIND_CHOICES,
        'debit': totals['d'] or ZERO, 'credit': totals['c'] or ZERO})


def _reconcile_rows(company):
    rows = []
    for run in _released_runs(company)[:24]:
        net = PayrollRunLine.objects.filter(payroll_run=run, employee__company=company).aggregate(s=Sum('net_pay'))['s'] or ZERO
        salary = JournalEntry.objects.filter(payroll_run=run, kind=JournalEntry.SALARY, status=JournalEntry.POSTED).first()
        payable = (salary.lines.filter(head='SALARY_PAYABLE').aggregate(s=Sum('credit'))['s'] or ZERO) if salary else None
        paid_bank = BankPayment.objects.filter(company=company, payroll_line__payroll_run=run, status='PAID').aggregate(s=Sum('amount'))['s'] or ZERO
        paid_posted = JournalLine.objects.filter(entry__payroll_run=run, entry__company=company, entry__kind=JournalEntry.PAYMENT, entry__status=JournalEntry.POSTED,
                                                 head='SALARY_PAYABLE').aggregate(s=Sum('debit'))['s'] or ZERO
        if salary is None:
            state, tone = 'Salary journal not posted', 'badge-neutral'
        elif payable != net:
            state, tone = 'Payroll changed after posting', 'badge-danger'
        elif paid_bank != paid_posted:
            state, tone = 'Paid payments not posted', 'badge-warn'
        elif paid_posted == payable:
            state, tone = 'Fully paid and posted', 'badge-success'
        else:
            state, tone = 'Part paid, matches', 'badge-info'
        rows.append({'run': run, 'net': net, 'payable': payable, 'paid_bank': paid_bank, 'paid_posted': paid_posted,
                     'outstanding': (payable - paid_posted) if payable is not None else None, 'state': state, 'tone': tone, 'salary': salary,
                     'can_post_payment': salary is not None and paid_bank > paid_posted})
    return rows


@company_owner_required
def acc_reconcile(request):
    """Feature: Reconciliation. Payroll vs journal vs bank, month by month."""
    company = _company(request)
    rows = _reconcile_rows(company)
    return render(request, 'accounting/reconcile.html', {'rows': rows, 'attention': sum(1 for r in rows if r['tone'] in ('badge-danger', 'badge-warn'))})


urlpatterns = [
    path('accounting/journals/', acc_journals, name='acc_journals'),
    path('accounting/journals/<int:pk>/', acc_journal_detail, name='acc_journal_detail'),
    path('accounting/salary-payable/', acc_payable, name='acc_payable'),
    path('accounting/employer-contributions/', acc_employer, name='acc_employer'),
    path('accounting/statutory-liabilities/', acc_statutory, name='acc_statutory'),
    path('accounting/loan-recovery/', acc_loans, name='acc_loans'),
    path('accounting/cost-allocation/', acc_allocation, name='acc_allocation'),
    path('accounting/ledger-mapping/', acc_mapping, name='acc_mapping'),
    path('accounting/export/', acc_export, name='acc_export'),
    path('accounting/reconciliation/', acc_reconcile, name='acc_reconcile'),
]
