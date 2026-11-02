"""Payroll Reports & Analytics (company owners only). 21 read-only reports from one registry.

Nothing here writes to the database. Every report reads the owner's own company data only, shows totals,
and can be exported as CSV or Excel (use Print / Save as PDF in the browser for a PDF).
"""
import csv
import datetime
import io
from collections import OrderedDict, defaultdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.apps import apps
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import path
from django.utils import timezone

from .audit import log_action
from .models import BankPayment, Employee, PayrollRun, PayrollRunLine
from .permissions import company_owner_required

PAGE_SIZE = 50
CENT = Decimal('0.01')
ZERO = Decimal('0.00')
EXPORT_LIMIT = 50000


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


def _model(name):
    """Models that live in other modules of the project are looked up by name, so this file does not depend on where they are defined."""
    return apps.get_model('payroll_app', name)


def _safe(value):
    """A text cell that starts with = + - @ is read as a formula by Excel; make it plain text."""
    s = str(value)
    return "'" + s if s[:1] in ('=', '+', '-', '@', '\t', '\r') else s


def _mask(value):
    value = (value or '').strip()
    return ('X' * max(len(value) - 4, 0) + value[-4:]) if value else ''


def _name(emp):
    return f'{emp.employee_code} {emp.full_name}'.strip()


def _pct(part, whole):
    return (part * 100 / whole).quantize(CENT, rounding=ROUND_HALF_UP) if whole else ZERO


# --------------------------------------------------------------------------- shared data
def employer_rates(company):
    """(company PF %, company ESI %, employer PF %, employer ESI %, PF wage ceiling) so the employer share can be worked out from the employee amounts."""
    from .accounting_models import AccountingSettings
    cs = _model('CompanySettings').objects.filter(company=company).first()
    acc = AccountingSettings.objects.filter(company=company).first()
    return (_d(getattr(cs, 'pf_percentage', 0)) if cs else ZERO, _d(getattr(cs, 'esi_percentage', 0)) if cs else ZERO,
            _d(acc.employer_pf_percent) if acc else ZERO, _d(acc.employer_esi_percent) if acc else ZERO,
            _d(getattr(cs, 'pf_wage_ceiling', 0)) if cs else ZERO)


def employer_pf(line, rates):
    base, _e, pct, _p, _c = rates
    return (_d(line.pf) * pct / base).quantize(CENT, rounding=ROUND_HALF_UP) if base and pct else ZERO


def employer_esi(line, rates):
    _b, base, _p, pct, _c = rates
    return (_d(line.esi) * pct / base).quantize(CENT, rounding=ROUND_HALF_UP) if base and pct else ZERO


def run_lines(company, run):
    return (PayrollRunLine.objects.filter(payroll_run=run, employee__company=company)
            .select_related('employee').order_by('employee__employee_code')) if run is not None else PayrollRunLine.objects.none()


def totals_of(rows, keys):
    return {k: sum((r.get(k) or ZERO for r in rows), ZERO) for k in keys}


# --------------------------------------------------------------------------- the registry
REPORTS = OrderedDict()


def report(key, title, name, description, needs=('run',)):
    def deco(func):
        REPORTS[key] = {'key': key, 'title': title, 'name': name, 'description': description, 'needs': needs, 'func': func}
        return func
    return deco


M, T, I, P = 'money', 'text', 'int', 'pct'


@report('monthly-register', 'Monthly Payroll Register', 'rep_monthly_register', 'Every employee of the month: all earnings, all deductions and net pay.')
def r_monthly_register(c):
    cols = [('code', 'Code', T), ('name', 'Employee', T), ('dept', 'Department', T), ('days', 'Days', I), ('lop_days', 'LOP days', I),
            ('basic', 'Basic', M), ('hra', 'HRA', M), ('conveyance', 'Conveyance', M), ('special', 'Special allowance', M), ('arrears', 'Arrears', M),
            ('reimb', 'Reimbursements', M), ('other_earn', 'Other earnings', M), ('earnings', 'Total earnings', M), ('pf', 'PF', M), ('esi', 'ESI', M),
            ('tds', 'TDS', M), ('pt', 'Prof. tax', M), ('loan', 'Loan', M), ('insurance', 'Insurance', M), ('other_ded', 'Other deductions', M),
            ('deductions', 'Total deductions', M), ('net', 'Net pay', M)]
    rows = [{'code': l.employee.employee_code, 'name': l.employee.full_name, 'dept': l.employee.department, 'days': l.days_in_month, 'lop_days': l.lop_days,
             'basic': _d(l.basic), 'hra': _d(l.hra), 'conveyance': _d(l.conveyance), 'special': _d(l.special_allowance), 'arrears': _d(l.arrears),
             'reimb': _d(l.reimbursements), 'other_earn': _d(l.other_earnings), 'earnings': _d(l.total_earnings), 'pf': _d(l.pf), 'esi': _d(l.esi),
             'tds': _d(l.tds), 'pt': _d(l.professional_tax), 'loan': _d(l.loan_deduction), 'insurance': _d(l.insurance_deduction),
             'other_ded': _d(l.other_deductions), 'deductions': _d(l.total_deductions), 'net': _d(l.net_pay)} for l in run_lines(c['company'], c['run'])]
    return cols, rows, totals_of(rows, [k for k, _l, kind in cols if kind == M]), []


@report('employee-register', 'Employee Salary Register', 'rep_employee_register', 'One employee across all months: earnings, deductions and net pay.', needs=('employee',))
def r_employee_register(c):
    cols = [('month', 'Month', T), ('status', 'Run status', T), ('days', 'Days', I), ('lop_days', 'LOP days', I), ('earnings', 'Total earnings', M),
            ('deductions', 'Total deductions', M), ('net', 'Net pay', M)]
    rows = []
    if c['employee'] is not None:
        qs = (PayrollRunLine.objects.filter(employee=c['employee'], employee__company=c['company']).select_related('payroll_run').order_by('payroll_run__created_at', 'payroll_run__id'))
        rows = [{'month': l.payroll_run.month, 'status': l.payroll_run.get_status_display(), 'days': l.days_in_month, 'lop_days': l.lop_days, 'earnings': _d(l.total_earnings),
                 'deductions': _d(l.total_deductions), 'net': _d(l.net_pay)} for l in qs]
    notes = [] if c['employee'] is not None else ['Choose an employee.']
    return cols, rows, totals_of(rows, ['earnings', 'deductions', 'net']), notes


def _by_department(c):
    groups = defaultdict(lambda: {'count': 0, 'earnings': ZERO, 'deductions': ZERO, 'net': ZERO})
    for l in run_lines(c['company'], c['run']):
        g = groups[(l.employee.department or '').strip() or 'Unassigned']
        g['count'] += 1
        g['earnings'] += _d(l.total_earnings)
        g['deductions'] += _d(l.total_deductions)
        g['net'] += _d(l.net_pay)
    cols = [('group', 'Department', T), ('count', 'Employees', I), ('earnings', 'Total earnings', M), ('deductions', 'Total deductions', M), ('net', 'Net pay', M),
            ('avg', 'Average net pay', M), ('share', 'Share of earnings %', P)]
    total_earn = sum((g['earnings'] for g in groups.values()), ZERO)
    rows = [{'group': k, 'count': g['count'], 'earnings': g['earnings'], 'deductions': g['deductions'], 'net': g['net'],
             'avg': (g['net'] / g['count']).quantize(CENT, rounding=ROUND_HALF_UP) if g['count'] else ZERO, 'share': _pct(g['earnings'], total_earn)} for k, g in sorted(groups.items())]
    totals = totals_of(rows, ['earnings', 'deductions', 'net'])
    totals['count'] = sum(r['count'] for r in rows)
    return cols, rows, totals


@report('branch-wise', 'Branch-Wise Payroll', 'rep_branch_wise', 'Payroll cost split by branch.')
def r_branch_wise(c):
    cols, rows, totals = _by_department(c)
    cols = [('group', 'Branch (department for now)', T)] + cols[1:]
    return cols, rows, totals, ['Employees do not have a branch field yet, so this shows the department split. It will split by branch once a Branch Master exists.']


@report('department-wise', 'Department-Wise Payroll', 'rep_department_wise', 'Payroll cost split by department.')
def r_department_wise(c):
    cols, rows, totals = _by_department(c)
    return cols, rows, totals, []


@report('ctc', 'CTC Report', 'rep_ctc', 'Monthly and annual cost to company: gross salary plus the employer PF and ESI share.')
def r_ctc(c):
    rates = employer_rates(c['company'])
    cols = [('code', 'Code', T), ('name', 'Employee', T), ('dept', 'Department', T), ('gross', 'Gross salary', M), ('er_pf', 'Employer PF', M), ('er_esi', 'Employer ESI', M),
            ('monthly', 'Monthly CTC', M), ('annual', 'Annual CTC (x12)', M)]
    rows = []
    for l in run_lines(c['company'], c['run']):
        gross, pf, esi = _d(l.gross_salary), employer_pf(l, rates), employer_esi(l, rates)
        rows.append({'code': l.employee.employee_code, 'name': l.employee.full_name, 'dept': l.employee.department, 'gross': gross, 'er_pf': pf, 'er_esi': esi,
                     'monthly': gross + pf + esi, 'annual': (gross + pf + esi) * 12})
    notes = ['CTC = gross salary (before loss of pay) + employer PF + employer ESI. Arrears, reimbursements and employer insurance are not included.']
    if not rates[2] and not rates[3]:
        notes.append('Employer PF / ESI percentages are not set (Accounting Integration > Employer Contribution Entries), so only the gross salary is counted.')
    return cols, rows, totals_of(rows, ['gross', 'er_pf', 'er_esi', 'monthly', 'annual']), notes


@report('gross', 'Gross Salary Report', 'rep_gross', 'Gross salary by component, loss of pay and the gross actually payable.')
def r_gross(c):
    cols = [('code', 'Code', T), ('name', 'Employee', T), ('basic', 'Basic', M), ('hra', 'HRA', M), ('conveyance', 'Conveyance', M), ('special', 'Special allowance', M),
            ('gross', 'Gross salary', M), ('lop', 'Loss of pay', M), ('earnings', 'Total earnings payable', M)]
    rows = [{'code': l.employee.employee_code, 'name': l.employee.full_name, 'basic': _d(l.basic), 'hra': _d(l.hra), 'conveyance': _d(l.conveyance),
             'special': _d(l.special_allowance), 'gross': _d(l.gross_salary), 'lop': _d(l.lop_amount), 'earnings': _d(l.total_earnings)} for l in run_lines(c['company'], c['run'])]
    return cols, rows, totals_of(rows, ['basic', 'hra', 'conveyance', 'special', 'gross', 'lop', 'earnings']), []


@report('net', 'Net Salary Report', 'rep_net', 'Net pay of each employee and whether the bank has paid it.')
def r_net(c):
    cols = [('code', 'Code', T), ('name', 'Employee', T), ('earnings', 'Total earnings', M), ('deductions', 'Total deductions', M), ('net', 'Net pay', M), ('payment', 'Bank payment', T)]
    rows = []
    for l in run_lines(c['company'], c['run']).select_related('bank_payment'):
        p = getattr(l, 'bank_payment', None)
        rows.append({'code': l.employee.employee_code, 'name': l.employee.full_name, 'earnings': _d(l.total_earnings), 'deductions': _d(l.total_deductions),
                     'net': _d(l.net_pay), 'payment': p.get_status_display() if p else 'Not prepared'})
    return cols, rows, totals_of(rows, ['earnings', 'deductions', 'net']), []


@report('earnings-deductions', 'Earnings & Deductions Report', 'rep_earnings_deductions', 'Each pay head for the month: how many employees have it and how much.')
def r_earnings_deductions(c):
    lines = list(run_lines(c['company'], c['run']))
    heads = [('Earning', 'Basic', 'basic'), ('Earning', 'HRA', 'hra'), ('Earning', 'Conveyance', 'conveyance'), ('Earning', 'Special allowance', 'special_allowance'),
             ('Earning', 'Arrears', 'arrears'), ('Earning', 'Reimbursements', 'reimbursements'), ('Earning', 'Other earnings', 'other_earnings'),
             ('Deduction', 'PF', 'pf'), ('Deduction', 'ESI', 'esi'), ('Deduction', 'TDS', 'tds'), ('Deduction', 'Professional tax', 'professional_tax'),
             ('Deduction', 'Loan', 'loan_deduction'), ('Deduction', 'Insurance', 'insurance_deduction'), ('Deduction', 'Other deductions', 'other_deductions')]
    total_earn = sum((_d(l.total_earnings) for l in lines), ZERO)
    total_ded = sum((_d(l.total_deductions) for l in lines), ZERO)
    rows = []
    for kind, label, field in heads:
        amounts = [_d(getattr(l, field)) for l in lines]
        total = sum(amounts, ZERO)
        rows.append({'kind': kind, 'head': label, 'people': sum(1 for a in amounts if a), 'total': total,
                     'share': _pct(total, total_earn if kind == 'Earning' else total_ded)})
    rows.append({'kind': 'Total', 'head': 'Total earnings (as per payslips)', 'people': len(lines), 'total': total_earn, 'share': ZERO})
    rows.append({'kind': 'Total', 'head': 'Total deductions (as per payslips)', 'people': len(lines), 'total': total_ded, 'share': ZERO})
    cols = [('kind', 'Type', T), ('head', 'Pay head', T), ('people', 'Employees', I), ('total', 'Amount', M), ('share', '% of earnings / deductions', P)]
    return cols, rows, None, ['Earning heads can add up to more than total earnings when loss of pay is taken off the earnings; the two total rows are the payslip totals.']


@report('pf', 'PF Report', 'rep_pf', 'Provident fund of each employee: employee share, employer share and the total to pay.')
def r_pf(c):
    rates = employer_rates(c['company'])
    ceiling = rates[4]
    cols = [('code', 'Code', T), ('name', 'Employee', T), ('wage', 'PF wage (basic, capped)', M), ('ee', 'Employee PF', M), ('er', 'Employer PF', M), ('total', 'Total PF', M)]
    rows = []
    for l in run_lines(c['company'], c['run']):
        if not _d(l.pf):
            continue
        basic = _d(l.basic)
        wage = min(basic, ceiling) if ceiling else basic
        er = employer_pf(l, rates)
        rows.append({'code': l.employee.employee_code, 'name': l.employee.full_name, 'wage': wage, 'ee': _d(l.pf), 'er': er, 'total': _d(l.pf) + er})
    notes = ['UAN numbers are not stored yet, so they are not shown.']
    if not rates[2]:
        notes.append('Employer PF percentage is not set (Accounting Integration > Employer Contribution Entries), so the employer share shows 0.')
    return cols, rows, totals_of(rows, ['wage', 'ee', 'er', 'total']), notes


@report('esi', 'ESI Report', 'rep_esi', 'ESI of each employee who has it: employee share, employer share and the total.')
def r_esi(c):
    rates = employer_rates(c['company'])
    cols = [('code', 'Code', T), ('name', 'Employee', T), ('earnings', 'Total earnings', M), ('ee', 'Employee ESI', M), ('er', 'Employer ESI', M), ('total', 'Total ESI', M)]
    rows = []
    for l in run_lines(c['company'], c['run']):
        if not _d(l.esi):
            continue
        er = employer_esi(l, rates)
        rows.append({'code': l.employee.employee_code, 'name': l.employee.full_name, 'earnings': _d(l.total_earnings), 'ee': _d(l.esi), 'er': er, 'total': _d(l.esi) + er})
    notes = [] if rates[3] else ['Employer ESI percentage is not set (Accounting Integration > Employer Contribution Entries), so the employer share shows 0.']
    return cols, rows, totals_of(rows, ['earnings', 'ee', 'er', 'total']), notes


@report('pt', 'PT Report', 'rep_pt', 'Professional tax deducted from each employee.')
def r_pt(c):
    cols = [('code', 'Code', T), ('name', 'Employee', T), ('dept', 'Department', T), ('earnings', 'Total earnings', M), ('pt', 'Professional tax', M)]
    rows = [{'code': l.employee.employee_code, 'name': l.employee.full_name, 'dept': l.employee.department, 'earnings': _d(l.total_earnings), 'pt': _d(l.professional_tax)}
            for l in run_lines(c['company'], c['run']) if _d(l.professional_tax)]
    return cols, rows, totals_of(rows, ['earnings', 'pt']), []


@report('tds', 'TDS Report', 'rep_tds', 'Tax deducted at source from each employee.')
def r_tds(c):
    cols = [('code', 'Code', T), ('name', 'Employee', T), ('pan', 'PAN (masked)', T), ('earnings', 'Total earnings', M), ('tds', 'TDS', M)]
    rows = [{'code': l.employee.employee_code, 'name': l.employee.full_name, 'pan': _mask(l.employee.pan_number), 'earnings': _d(l.total_earnings), 'tds': _d(l.tds)}
            for l in run_lines(c['company'], c['run']) if _d(l.tds)]
    return cols, rows, totals_of(rows, ['earnings', 'tds']), ['PAN numbers are masked on screen and in exports.']


@report('lop', 'LOP Report', 'rep_lop', 'Loss of pay: employees with loss-of-pay days and the amount cut.')
def r_lop(c):
    cols = [('code', 'Code', T), ('name', 'Employee', T), ('days', 'Days in month', I), ('lop_days', 'LOP days', I), ('lop', 'Loss of pay', M)]
    rows = [{'code': l.employee.employee_code, 'name': l.employee.full_name, 'days': l.days_in_month, 'lop_days': l.lop_days, 'lop': _d(l.lop_amount)}
            for l in run_lines(c['company'], c['run']) if l.lop_days or _d(l.lop_amount)]
    totals = totals_of(rows, ['lop'])
    totals['lop_days'] = sum((Decimal(str(r['lop_days'])) for r in rows), Decimal('0'))
    return cols, rows, totals, []


@report('overtime', 'Overtime Report', 'rep_overtime', 'Estimated overtime hours from attendance check-in and check-out against the assigned shift.')
def r_overtime(c):
    cols = [('code', 'Code', T), ('name', 'Employee', T), ('days', 'Days with overtime', I), ('hours', 'Overtime hours', M)]
    rows, notes = [], []
    run = c['run']
    period = None
    if run is not None:
        try:
            from .payroll_engine import parse_month
            period = parse_month(run.month)
        except Exception:
            period = None
    if period:
        start, end = period[0], period[1]
        assignments = defaultdict(list)
        for a in _model('ShiftAssignment').objects.filter(company=c['company']).select_related('shift'):
            assignments[a.employee_id].append(a)
        agg = defaultdict(lambda: {'minutes': 0, 'days': 0})
        no_shift = 0
        att = _model('Attendance').objects.filter(employee__company=c['company'], date__gte=start, date__lte=end, check_in__isnull=False, check_out__isnull=False).select_related('employee')
        for rec in att:
            shift = next((a.shift for a in assignments.get(rec.employee_id, []) if a.effective_from <= rec.date and (a.effective_to is None or a.effective_to >= rec.date)), None)
            if shift is None:
                no_shift += 1
                continue
            worked = (datetime.datetime.combine(rec.date, rec.check_out) - datetime.datetime.combine(rec.date, rec.check_in)).total_seconds() / 60
            extra = worked - shift.scheduled_minutes
            if worked > 0 and extra > 0:
                g = agg[rec.employee_id]
                g['minutes'] += extra
                g['days'] += 1
        emps = {e.pk: e for e in Employee.objects.filter(pk__in=agg.keys(), company=c['company'])}
        for pk, g in sorted(agg.items(), key=lambda kv: emps[kv[0]].employee_code):
            e = emps[pk]
            rows.append({'code': e.employee_code, 'name': e.full_name, 'days': g['days'], 'hours': (Decimal(g['minutes']) / 60).quantize(CENT, rounding=ROUND_HALF_UP)})
        if no_shift:
            notes.append(f'{no_shift} attendance day(s) were skipped because the employee had no shift assigned.')
    notes.append('Estimate only: hours worked beyond the scheduled shift minutes, on days with both check-in and check-out. Overnight shifts are not counted.')
    totals = totals_of(rows, ['hours'])
    totals['days'] = sum(r['days'] for r in rows)
    return cols, rows, totals, notes


@report('arrears', 'Arrears Report', 'rep_arrears', 'Salary revision arrears: what was paid in the chosen month and what is still pending.')
def r_arrears(c):
    cols = [('code', 'Code', T), ('name', 'Employee', T), ('from', 'Effective from', T), ('old', 'Old basic', M), ('new', 'New basic', M), ('months', 'Months', I),
            ('amount', 'Arrears amount', M), ('state', 'State', T)]
    rows = []
    qs = _model('ArrearsRecord').objects.filter(employee__company=c['company']).select_related('employee', 'paid_in_run').order_by('employee__employee_code', 'effective_from')
    for a in qs:
        paid_here = c['run'] is not None and a.paid_in_run_id == c['run'].pk
        pending = a.paid_in_run_id is None
        if not (paid_here or pending):
            continue
        rows.append({'code': a.employee.employee_code, 'name': a.employee.full_name, 'from': a.effective_from.isoformat() if a.effective_from else '', 'old': _d(a.old_basic),
                     'new': _d(a.new_basic), 'months': a.months, 'amount': (_d(a.new_basic) - _d(a.old_basic)) * Decimal(str(a.months or 0)),
                     'state': 'Paid in this month' if paid_here else 'Pending'})
    return cols, rows, totals_of(rows, ['amount']), ['Arrears amount = (new basic - old basic) x months.']


@report('loan-recovery', 'Loan Recovery Report', 'rep_loan_recovery', 'Loan instalments recovered from salary in the month and the balance left.')
def r_loan_recovery(c):
    cols = [('code', 'Code', T), ('name', 'Employee', T), ('ref', 'Loan reference', T), ('type', 'Loan type', T), ('emi', 'Instalment', M), ('recovered', 'Recovered this month', M),
            ('balance', 'Balance (loan less all recoveries)', M)]
    rows = []
    Repay = _model('LoanRepayment')
    if c['run'] is not None:
        for r in Repay.objects.filter(line__payroll_run=c['run'], loan__company=c['company']).select_related('loan', 'loan__employee').order_by('loan__employee__employee_code'):
            loan = r.loan
            done = Repay.objects.filter(loan=loan).aggregate(s=Sum('amount'))['s'] or ZERO
            rows.append({'code': loan.employee.employee_code, 'name': loan.employee.full_name, 'ref': loan.reference_no, 'type': str(getattr(loan, 'loan_type', '')),
                         'emi': _d(loan.emi_amount), 'recovered': _d(r.amount), 'balance': _d(loan.loan_amount) - _d(done)})
    return cols, rows, totals_of(rows, ['emi', 'recovered', 'balance']), ['The balance ignores interest: it is the loan amount less everything recovered so far.']


@report('ff', 'F&F Settlement Report', 'rep_ff', 'Full and final settlements: pending salary, leave encashment, deductions and the net amount.', needs=())
def r_ff(c):
    cols = [('code', 'Code', T), ('name', 'Employee', T), ('last_day', 'Last working day', T), ('pending', 'Pending salary', M), ('leave', 'Leave encashment', M),
            ('deductions', 'Deductions', M), ('net', 'Net payable', M)]
    rows = []
    for s in _model('FullFinalSettlement').objects.filter(employee__company=c['company']).select_related('employee').order_by('-created_at'):
        rows.append({'code': s.employee.employee_code, 'name': s.employee.full_name, 'last_day': s.last_working_day.isoformat() if s.last_working_day else '',
                     'pending': _d(s.pending_salary), 'leave': _d(s.leave_encashment), 'deductions': _d(s.deductions),
                     'net': _d(s.pending_salary) + _d(s.leave_encashment) - _d(s.deductions)})
    return cols, rows, totals_of(rows, ['pending', 'leave', 'deductions', 'net']), []


@report('bank-payment', 'Bank Payment Report', 'rep_bank_payment', 'Bank payments of the month: who, how much, to which bank, the status and the UTR.')
def r_bank_payment(c):
    cols = [('code', 'Code', T), ('name', 'Employee', T), ('bank', 'Bank', T), ('account', 'Account', T), ('ifsc', 'IFSC', T), ('amount', 'Amount', M), ('mode', 'Mode', T),
            ('status', 'Status', T), ('utr', 'UTR', T), ('batch', 'Batch', T)]
    rows = []
    if c['run'] is not None:
        qs = (BankPayment.objects.filter(company=c['company'], payroll_line__payroll_run=c['run'])
              .select_related('payroll_line__employee').prefetch_related('batch_items__batch').order_by('payroll_line__employee__employee_code'))
        for p in qs:
            items = list(p.batch_items.all())
            rows.append({'code': p.payroll_line.employee.employee_code, 'name': p.payroll_line.employee.full_name, 'bank': p.bank_name, 'account': _mask(p.account_no),
                         'ifsc': p.ifsc_code, 'amount': _d(p.amount), 'mode': p.mode, 'status': p.get_status_display(), 'utr': p.reference_number,
                         'batch': items[-1].batch.reference if items else ''})
    return cols, rows, totals_of(rows, ['amount']), []


@report('variance', 'Payroll Variance Report', 'rep_variance', 'The month against the month before: who joined, who left and who changed.')
def r_variance(c):
    cols = [('code', 'Code', T), ('name', 'Employee', T), ('prev', 'Previous net pay', M), ('curr', 'This month net pay', M), ('diff', 'Change', M), ('pct', 'Change %', P), ('flag', 'Note', T)]
    rows = []
    notes = []
    if c['run'] is not None and c['prev_run'] is not None:
        prev = {l.employee_id: l for l in run_lines(c['company'], c['prev_run'])}
        curr = {l.employee_id: l for l in run_lines(c['company'], c['run'])}
        for pk in sorted(set(prev) | set(curr), key=lambda k: (curr.get(k) or prev.get(k)).employee.employee_code):
            l = curr.get(pk) or prev.get(pk)
            a, b = (_d(prev[pk].net_pay) if pk in prev else ZERO), (_d(curr[pk].net_pay) if pk in curr else ZERO)
            flag = 'New this month' if pk not in prev else ('Not paid this month' if pk not in curr else ('Changed' if a != b else ''))
            rows.append({'code': l.employee.employee_code, 'name': l.employee.full_name, 'prev': a, 'curr': b, 'diff': b - a, 'pct': _pct(b - a, a) if a else ZERO, 'flag': flag})
        notes.append(f'Compared with {c["prev_run"].month}.')
    elif c['run'] is not None:
        notes.append('There is no earlier month to compare with.')
    return cols, rows, totals_of(rows, ['prev', 'curr', 'diff']), notes


@report('cost-trend', 'Payroll Cost Trend', 'rep_cost_trend', 'Month by month: headcount, earnings, deductions and net pay, with the change from the month before.', needs=())
def r_cost_trend(c):
    cols = [('month', 'Month', T), ('status', 'Run status', T), ('count', 'Employees', I), ('earnings', 'Total earnings', M), ('deductions', 'Total deductions', M),
            ('net', 'Net pay', M), ('change', 'Net pay change %', P)]
    runs = list(PayrollRun.objects.filter(company=c['company']).exclude(status='DRAFT').order_by('-created_at', '-id')[:24])[::-1]
    agg = {r['payroll_run']: r for r in PayrollRunLine.objects.filter(payroll_run__in=runs, employee__company=c['company']).order_by().values('payroll_run')
           .annotate(n=Count('id'), e=Sum('total_earnings'), d=Sum('total_deductions'), net=Sum('net_pay'))}
    rows, prev = [], None
    for run in runs:
        a = agg.get(run.pk, {})
        net = _d(a.get('net'))
        rows.append({'month': run.month, 'status': run.get_status_display(), 'count': a.get('n', 0), 'earnings': _d(a.get('e')), 'deductions': _d(a.get('d')), 'net': net,
                     'change': _pct(net - prev, prev) if prev else ZERO})
        prev = net
    return cols, rows, None, ['Up to the last 24 months that are not drafts.']


# --------------------------------------------------------------------------- views
def _selectors(request, company, spec):
    runs = list(PayrollRun.objects.filter(company=company).exclude(status='DRAFT').order_by('-created_at', '-id')[:60])
    run = None
    if 'run' in spec['needs'] or spec['key'] == 'arrears':
        rid = _int(request.GET.get('run'))
        run = next((r for r in runs if r.pk == rid), runs[0] if runs else None)
    employee = None
    if 'employee' in spec['needs']:
        eid = _int(request.GET.get('employee'))
        employee = Employee.objects.filter(pk=eid, company=company).first() if eid else None
    prev_run = None
    if run is not None and spec['key'] == 'variance':
        later = [r for r in runs if (r.created_at, r.pk) < (run.created_at, run.pk)]
        prev_run = later[0] if later else None
    return runs, run, employee, prev_run


def _result(spec, ctx):
    out = spec['func'](ctx)
    cols, rows, totals, notes = out if len(out) == 4 else (out[0], out[1], out[2], [])
    return cols, rows, totals, notes


def _fmt(kind, value):
    if value is None or value == '':
        return ''
    if kind == M:
        return f'{Decimal(value):,.2f}'
    if kind == P:
        return f'{Decimal(value):.2f}%'
    return str(value)


def export_response(fmt, stem, cols, rows, totals):
    headers = [label for _k, label, _t in cols]
    body = [[r.get(k, '') for k, _l, _t in cols] for r in rows]
    if totals:
        body.append(['Total' if i == 0 else totals.get(k, '') for i, (k, _l, _t) in enumerate(cols)])
    if fmt == 'xlsx':
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(headers)
        for r in body:
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
    for r in body:
        w.writerow([f'{v:.2f}' if isinstance(v, Decimal) else (_safe(v) if isinstance(v, str) else v) for v in r])
    return resp


def make_view(key):
    spec = REPORTS[key]

    @company_owner_required
    def view(request):
        company = _company(request)
        runs, run, employee, prev_run = _selectors(request, company, spec)
        ctx = {'company': company, 'run': run, 'employee': employee, 'prev_run': prev_run}
        cols, rows, totals, notes = _result(spec, ctx)
        fmt = request.GET.get('export')
        if fmt in ('csv', 'xlsx'):
            log_action(request, 'EXPORT', company=company, details=f'Report exported: {spec["title"]} ({fmt}, {len(rows)} rows)')
            stem = spec['key'].replace('-', '_') + ('_' + run.month.replace(' ', '_') if run is not None and 'run' in spec['needs'] else '')
            return export_response(fmt, stem, cols, rows[:EXPORT_LIMIT], totals)
        page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get('page'))
        table = [[(_fmt(kind, r.get(k)), kind in (M, I, P)) for k, _l, kind in cols] for r in page.object_list]
        total_cells = [(('Total' if i == 0 else _fmt(kind, totals.get(k))), kind in (M, I, P)) for i, (k, _l, kind) in enumerate(cols)] if totals else None
        q = request.GET.copy()
        q.pop('page', None)
        return render(request, 'reports/report.html', {
            'spec': spec, 'cols': [(l, kind in (M, I, P)) for _k, l, kind in cols], 'table': table, 'totals': total_cells,
            'page': page, 'qs': q.urlencode(), 'notes': notes, 'runs': runs, 'run': run, 'employee': employee, 'row_count': len(rows),
            'employees': Employee.objects.filter(company=company).order_by('employee_code')[:1000] if 'employee' in spec['needs'] else [],
            'show_run': 'run' in spec['needs'] or key == 'arrears'})

    view.__name__ = 'report_' + key.replace('-', '_')
    return view


@company_owner_required
def rep_export_center(request):
    """Feature: Excel / PDF / CSV Export. Every report, one click each."""
    company = _company(request)
    runs = list(PayrollRun.objects.filter(company=company).exclude(status='DRAFT').order_by('-created_at', '-id')[:60])
    rid = _int(request.GET.get('run'))
    run = next((r for r in runs if r.pk == rid), runs[0] if runs else None)
    items = [{'title': s['title'], 'description': s['description'], 'name': s['name'], 'run_based': 'run' in s['needs'] or s['key'] == 'arrears',
              'employee_based': 'employee' in s['needs']} for s in REPORTS.values()]
    return render(request, 'reports/center.html', {'items': items, 'runs': runs, 'run': run})


urlpatterns = [path('reports/' + s['key'] + '/', make_view(k), name=s['name']) for k, s in REPORTS.items()]
urlpatterns.append(path('reports/export-center/', rep_export_center, name='rep_export_center'))
