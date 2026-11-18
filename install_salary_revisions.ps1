# install_salary_revisions.ps1 - Salary Revision Approvals, the Zoho Payroll way: a salary change is REQUESTED first, then approved, rejected, or moved to the next month ("Process later"); many can be approved or rejected at once and a problem one is skipped and listed.
# It adds ONE small database table (a migration). Run it from the payroll project folder. It installs, tests and commits on this PC; it pushes only if you type YES.
& {
$ErrorActionPreference = 'Continue'
$root = $PWD.Path
$enc = New-Object System.Text.UTF8Encoding($false)
if (-not (Test-Path "$root/manage.py") -or -not (Test-Path "$root/payroll_app/salary_history.py")) { "STOP: open PowerShell in the payroll project folder (the one with manage.py), then run this again."; return }
"Salary Revision Approvals"
git fetch origin 2>&1 | Out-Null
$dirty = @(git status --porcelain --untracked-files=no)
if ($dirty.Count -gt 0) { "NOTHING WAS CHANGED: you have uncommitted edits to tracked files. Commit or undo them first:"; $dirty | ForEach-Object { "    $_" }; return }
$behind = [int](git rev-list --count HEAD..origin/main)
if ($behind -gt 0) { "NOTHING WAS CHANGED: this PC is $behind commit(s) behind GitHub. Run: git pull --ff-only   then run this again."; return }
$answer = Read-Host "Type YES (capital letters) to also push to GitHub at the end, or just press Enter to stop before pushing"
$push = ($answer.Trim() -ceq 'YES')
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$tmp = [IO.Path]::GetTempPath()
$work = Join-Path $tmp "salrev_$stamp"
New-Item -ItemType Directory -Path $work -Force | Out-Null
$files = [ordered]@{}
$files['patch_rev.py'] = @'
"""Adds Salary Revision Approvals. Usage: python patch_rev.py <stamp> <workdir>   (run from the project folder)"""
import os, shutil, sys
stamp, work = sys.argv[1], sys.argv[2]
APP = 'payroll_app'
M, U, S = f'{APP}/models.py', f'{APP}/urls.py', f'{APP}/payroll_structure.py'
if os.path.exists(f'{APP}/salary_revision_approvals.py'):
    print('ALREADY APPLIED: salary_revision_approvals.py is already in the project.'); sys.exit(0)
def rd(p): return open(p, encoding='utf-8', newline='').read()
def fail(m): print('STOP: ' + m + ' Nothing was written.'); sys.exit(1)
for p in (M, U, S, f'{APP}/salary_history.py', f'{APP}/salary_history_models.py'):
    if not os.path.exists(p): fail(f'{p} is missing, so this is not the expected project.')
new = {}
def append(path, text, label):
    t = new.get(path) if path in new else rd(path)
    nl = '\r\n' if '\r\n' in t else '\n'
    if text.strip() in t: return
    last = [l for l in t.split('\n') if l.strip()][-1]
    if last.startswith((' ', '\t')): fail(f'{path} does not end at the top level.')
    new[path] = t.rstrip() + nl + nl + text.replace('\n', nl) + nl
append(M, 'from . import salary_revision_request_models  # noqa: E402,F401', 'models')
append(U, 'from . import salary_revision_approvals as _srv  # noqa: E402\nurlpatterns += _srv.urlpatterns', 'urls')
st = rd(S); nl = '\r\n' if '\r\n' in st else '\n'
old = '  Salary Revision|' + nl
if st.count(old) != 1: fail(f'payroll_structure.py: "Salary Revision" line found {st.count(old)} times (needed exactly 1).')
new[S] = st.replace(old, '  Salary Revision Approvals|salary_revision_approvals' + nl)
B = f'{APP}/templates/base.html'
import re
bt = rd(B)
if bt.count("{% url 'salary_history' as ") != 1: fail(f'base.html: the Salary history menu link was found {bt.count("{% url 'salary_history' as ")} times (needed exactly 1).')
m = re.search(r"^([ \t]*)\{% url 'salary_history' as (\w+) %\}[^\n]*\n", bt.replace('\r\n', '\n'), re.M)
if not m: fail('base.html: could not find the Salary history menu line.')
nlb = '\r\n' if '\r\n' in bt else '\n'
line = m.group(1) + "{% url 'salary_revision_approvals' as u_srv1 %}{% if u_srv1 %}<a href=\"{{ u_srv1 }}\"><span class=\"tx\">Salary Revision Approvals</span></a>{% endif %}"
new[B] = bt.replace(m.group(0).replace('\n', nlb), line + nlb + m.group(0).replace('\n', nlb), 1)
os.makedirs('backups', exist_ok=True)
bk = f'backups/salary_revision_approvals_{stamp}'
for p in new:
    os.makedirs(os.path.dirname(f'{bk}/{p}'), exist_ok=True); shutil.copy2(p, f'{bk}/{p}')
for p, t in new.items(): open(p, 'w', encoding='utf-8', newline='').write(t)
os.makedirs(f'{APP}/templates/salary_revision', exist_ok=True)
for src, dst in (('salary_revision_request_models.py', f'{APP}/salary_revision_request_models.py'), ('salary_revision_approvals.py', f'{APP}/salary_revision_approvals.py'),
                 ('approvals.html', f'{APP}/templates/salary_revision/approvals.html'), ('tests_salary_revision_approvals.py', f'{APP}/tests_salary_revision_approvals.py')):
    shutil.copy2(os.path.join(work, src), dst)
print('Added Salary Revision Approvals at /salary-revisions/approvals/ (request, approve, reject, process later, bulk with skip). It is listed in the Salary Structure section of the Next / Previous order.')
print('Backup: ' + bk)
print('CHANGED_FILES=' + ' '.join([M, U, S, B, f'{APP}/salary_revision_request_models.py', f'{APP}/salary_revision_approvals.py', f'{APP}/templates/salary_revision/approvals.html', f'{APP}/tests_salary_revision_approvals.py']))
'@
$files['salary_revision_request_models.py'] = @'
"""A salary revision that is waiting for approval. It becomes a real salary change (a SalaryRevision) only when it is approved."""
from django.conf import settings
from django.db import models
from django.db.models import Q


class SalaryRevisionRequest(models.Model):
    STATUS = [('PENDING', 'Pending'), ('APPROVED', 'Approved'), ('REJECTED', 'Rejected')]
    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='salary_revision_requests')
    employee = models.ForeignKey('payroll_app.Employee', on_delete=models.CASCADE, related_name='salary_revision_requests')
    effective_from = models.DateField(help_text='First day of the month the new salary starts')
    basic = models.DecimalField(max_digits=10, decimal_places=2)
    hra = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    conveyance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    special_allowance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    note = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default='PENDING')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['-requested_at']
        constraints = [models.UniqueConstraint(fields=['employee', 'effective_from'], condition=Q(status='PENDING'), name='one_pending_salary_revision_per_month')]

    @property
    def gross_salary(self):
        return self.basic + self.hra + self.conveyance + self.special_allowance

    def __str__(self):
        return f'{self.employee_id} from {self.effective_from} ({self.status})'
'@
$files['salary_revision_approvals.py'] = @'
"""Salary Revision Approvals (the Zoho Payroll way): a salary change is first REQUESTED, then an owner approves, rejects, or moves it to a later month ("Process later").
Only an approved request becomes a real salary change, using the same rules as the Salary history page. Approve and reject work for one revision or for many ticked at
once; a revision with a problem is skipped and listed, the others still go through."""
import datetime
from decimal import Decimal

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import path
from django.utils import timezone

from .audit import log_action
from .models import Employee, PayrollRun, PayrollRunLine, SalaryRevision
from .permissions import company_owner_required
from .salary_history import FIELDS, _amount, _month_start, month_choices, sync_current_structure
from .salary_revision_request_models import SalaryRevisionRequest

MAX_BULK = 200


def _first_of_next_month(d):
    y, m = divmod(d.year * 12 + d.month, 12)
    return datetime.date(y, m + 1, 1)


def _check(req):
    """Why this request cannot be approved right now, or an empty string."""
    if req.status != 'PENDING':
        return f'it is already {req.get_status_display().lower()}'
    if req.effective_from.day != 1:
        return 'the month must start on the 1st'
    if SalaryRevision.objects.filter(employee=req.employee, effective_from=req.effective_from).exists():
        return f'the employee already has a salary starting {req.effective_from:%B %Y}'
    return ''


def _approve(request, req):
    """Approve one request. Returns an error text, or '' when done."""
    problem = _check(req)
    if problem:
        return problem
    try:
        with transaction.atomic():
            SalaryRevision.objects.create(employee=req.employee, effective_from=req.effective_from, note=(req.note or 'Approved revision')[:255],
                                          created_by=request.user, **{f: getattr(req, f) for f in FIELDS})
            sync_current_structure(req.employee)
            req.status, req.decided_by, req.decided_at = 'APPROVED', request.user, timezone.now()
            req.save(update_fields=['status', 'decided_by', 'decided_at'])
    except IntegrityError:
        return 'someone else just saved a salary for that month'
    log_action(request, 'UPDATE', req.employee, details=f'Salary revision approved from {req.effective_from:%B %Y}: gross {req.gross_salary}')
    return ''


def _reject(request, req, reason=''):
    if req.status != 'PENDING':
        return f'it is already {req.get_status_display().lower()}'
    req.status, req.decided_by, req.decided_at, req.decision_note = 'REJECTED', request.user, timezone.now(), (reason or '')[:255]
    req.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_note'])
    log_action(request, 'UPDATE', req.employee, details=f'Salary revision rejected (from {req.effective_from:%B %Y})')
    return ''


def _later(request, req):
    """Process later: push the start to the next month."""
    if req.status != 'PENDING':
        return f'it is already {req.get_status_display().lower()}'
    new = _first_of_next_month(req.effective_from)
    if SalaryRevisionRequest.objects.filter(employee=req.employee, effective_from=new, status='PENDING').exists() or \
            SalaryRevision.objects.filter(employee=req.employee, effective_from=new).exists():
        return f'the employee already has a salary or a pending revision for {new:%B %Y}'
    req.effective_from = new
    req.save(update_fields=['effective_from'])
    log_action(request, 'UPDATE', req.employee, details=f'Salary revision moved to {new:%B %Y} (process later)')
    return ''


def _new_request(request, company, employees):
    errors = []
    emp = employees.filter(pk=request.POST.get('employee') or 0).first()
    if emp is None:
        errors.append('Choose an employee.')
    try:
        eff = datetime.date.fromisoformat(request.POST.get('effective_from', ''))
    except ValueError:
        eff = None
        errors.append('Choose the month the new salary starts.')
    if eff and eff.day != 1:
        errors.append('A salary change must start on the first day of a month.')
    vals = {'basic': _amount(request.POST.get('basic'), 'Basic', errors, required=True)}
    for f, label in (('hra', 'HRA'), ('conveyance', 'Conveyance'), ('special_allowance', 'Special allowance')):
        vals[f] = _amount(request.POST.get(f), label, errors)
    if emp is not None and not PayrollRunLine.objects.filter(employee=emp).exists():
        errors.append(f'{emp.employee_code} has no completed pay run yet. A salary can be revised after the first pay run; until then change the salary on the Salary Structure page.')
    if emp is not None and eff:
        if SalaryRevision.objects.filter(employee=emp, effective_from=eff).exists():
            errors.append(f'{emp.employee_code} already has a salary starting {eff:%B %Y}.')
        if SalaryRevisionRequest.objects.filter(employee=emp, effective_from=eff, status='PENDING').exists():
            errors.append(f'{emp.employee_code} already has a revision waiting for {eff:%B %Y}.')
    if errors:
        for e in errors:
            messages.error(request, e)
        return False
    SalaryRevisionRequest.objects.create(company=company, employee=emp, effective_from=eff, note=(request.POST.get('note') or '')[:255], requested_by=request.user, **vals)
    log_action(request, 'CREATE', emp, details=f'Salary revision requested from {eff:%B %Y}: gross {sum(vals.values())}')
    messages.success(request, f'Revision for {emp.employee_code} sent for approval.')
    return True


@company_owner_required
def salary_revision_approvals(request):
    company = request.user.company
    employees = Employee.objects.filter(company=company).order_by('employee_code')
    base = SalaryRevisionRequest.objects.filter(company=company).select_related('employee')
    if request.method == 'POST':
        act = request.POST.get('act', '')
        if act == 'request':
            _new_request(request, company, employees)
            return redirect('salary_revision_approvals')
        ids = [int(x) for x in request.POST.getlist('ids') if x.isdigit()][:MAX_BULK]
        if act in ('approve', 'reject', 'later') and request.POST.get('one'):
            ids = [int(request.POST['one'])] if request.POST['one'].isdigit() else []
        chosen = list(base.filter(pk__in=ids)) if ids else []
        if act not in ('approve', 'reject', 'later') or not chosen:
            messages.error(request, 'Tick at least one revision.')
            return redirect('salary_revision_approvals')
        done, skipped = 0, []
        for req in chosen:
            if act == 'approve':
                err = _approve(request, req)
            elif act == 'reject':
                err = _reject(request, req, request.POST.get('reason', ''))
            else:
                err = _later(request, req)
            if err:
                skipped.append(f'{req.employee.employee_code}: {err}')
            else:
                done += 1
        verb = {'approve': 'approved', 'reject': 'rejected', 'later': 'moved to the next month'}[act]
        if done:
            messages.success(request, f'{done} revision(s) {verb}.')
        if skipped:
            messages.warning(request, 'Skipped (still waiting, fix and try again): ' + '; '.join(skipped))
        return redirect('salary_revision_approvals')
    status = request.GET.get('status', 'PENDING')
    status = status if status in dict(SalaryRevisionRequest.STATUS) else 'PENDING'
    q = (request.GET.get('q') or '').strip()[:60]
    qs = base.filter(status=status)
    for token in q.split():
        qs = qs.filter(Q(employee__employee_code__icontains=token) | Q(employee__first_name__icontains=token) | Q(employee__last_name__icontains=token))
    rows = []
    for r in qs[:300]:
        cur = SalaryRevision.objects.filter(employee=r.employee, effective_from__lte=r.effective_from).order_by('-effective_from').first()
        old = cur.gross_salary if cur else None
        rows.append({'r': r, 'old': old, 'change': (r.gross_salary - old) if old is not None else None, 'problem': _check(r) if status == 'PENDING' else ''})
    tabs = [(k, label, base.filter(status=k).count()) for k, label in SalaryRevisionRequest.STATUS]
    return render(request, 'salary_revision/approvals.html', {'rows': rows, 'status': status, 'q': q, 'employees': employees,
                  'months': month_choices(back=0, ahead=12), 'tabs': tabs})


urlpatterns = [path('salary-revisions/approvals/', salary_revision_approvals, name='salary_revision_approvals')]
'@
$files['approvals.html'] = @'
{% extends "base.html" %}
{% block title %}Salary revision approvals{% endblock %}
{% block content %}{% include "features/_ui.html" %}
<div class="fx">
  <div class="fx-card"><h1>Salary revision approvals</h1><div class="muted">A salary change is sent here first. It becomes the employee's salary only after you approve it. Approve, reject, or press Process later to move it to the next month. Approving or rejecting many at once is fine: one with a problem is skipped and listed, the rest go through.</div></div>

  <div class="fx-card">
    <b>Revise a salary</b>
    <form method="post" class="fx-filters" style="margin-top:10px">{% csrf_token %}<input type="hidden" name="act" value="request">
      <label>Employee <select name="employee" required><option value="">Choose</option>{% for e in employees %}<option value="{{ e.pk }}">{{ e.employee_code }} - {{ e.first_name }} {{ e.last_name }}</option>{% endfor %}</select></label>
      <label>Starts <select name="effective_from" required>{% for v, l in months %}<option value="{{ v }}">{{ l }}</option>{% endfor %}</select></label>
      <label>Basic <input name="basic" inputmode="decimal" required></label>
      <label>HRA <input name="hra" inputmode="decimal"></label>
      <label>Conveyance <input name="conveyance" inputmode="decimal"></label>
      <label>Special allowance <input name="special_allowance" inputmode="decimal"></label>
      <label>Note <input name="note" maxlength="255"></label>
      <button class="fx-btn" type="submit">Send for approval</button>
    </form>
  </div>

  <div class="fx-filters">{% for k, label, n in tabs %}<a class="fx-btn{% if status != k %} ghost{% endif %}" href="?status={{ k }}">{{ label }} ({{ n }})</a>{% endfor %}
    <form method="get" style="display:inline-flex;gap:8px"><input type="hidden" name="status" value="{{ status }}"><input name="q" value="{{ q }}" placeholder="Search employee"><button class="fx-btn ghost" type="submit">Search</button></form></div>

  <form method="post" class="fx-card fx-table">{% csrf_token %}
    {% if status == 'PENDING' and rows %}<div class="fx-filters"><button class="fx-btn" name="act" value="approve" type="submit">Approve ticked</button><button class="fx-btn ghost" name="act" value="reject" type="submit">Reject ticked</button><button class="fx-btn ghost" name="act" value="later" type="submit">Process later</button><input name="reason" placeholder="Reason (for reject, optional)" maxlength="255"></div>{% endif %}
    <table><tr>{% if status == 'PENDING' %}<th style="width:40px"></th>{% endif %}<th>Employee</th><th>Starts</th><th>Current gross</th><th>New gross</th><th>Change</th><th>Note</th>{% if status == 'PENDING' %}<th>Check</th>{% else %}<th>Decision</th>{% endif %}</tr>
    {% for x in rows %}<tr>{% if status == 'PENDING' %}<td><input type="checkbox" name="ids" value="{{ x.r.pk }}"></td>{% endif %}
      <td>{{ x.r.employee.employee_code }} - {{ x.r.employee.first_name }} {{ x.r.employee.last_name }}</td><td>{{ x.r.effective_from|date:"M Y" }}</td>
      <td>{% if x.old is not None %}&#8377;{{ x.old|floatformat:"2g" }}{% else %}-{% endif %}</td><td>&#8377;{{ x.r.gross_salary|floatformat:"2g" }}</td>
      <td>{% if x.change is not None %}{% if x.change > 0 %}+{% endif %}&#8377;{{ x.change|floatformat:"2g" }}{% else %}-{% endif %}</td><td>{{ x.r.note }}</td>
      {% if status == 'PENDING' %}<td>{% if x.problem %}<span class="muted">{{ x.problem }}</span>{% else %}Ready{% endif %}</td>
      {% else %}<td>{{ x.r.get_status_display }} {{ x.r.decided_at|date:"d M Y" }}{% if x.r.decision_note %} - {{ x.r.decision_note }}{% endif %}</td>{% endif %}</tr>
    {% empty %}<tr><td colspan="8" class="muted">Nothing here.</td></tr>{% endfor %}</table>
  </form>
</div>{% endblock %}
'@
$files['tests_salary_revision_approvals.py'] = @'
import datetime
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from .models import PayrollRun, PayrollRunLine, SalaryRevision, SalaryStructure
from .salary_history import salary_for_period
from .salary_revision_request_models import SalaryRevisionRequest as R
from .tests import make_company, make_employee, make_user
from .tests_reports import line

D = Decimal


class ApprovalTests(TestCase):
    def setUp(self):
        from .models import User
        role = [r[0] for r in User._meta.get_field('role').choices if 'OWNER' in r[0].upper() or 'COMPANY' in r[0].upper()][0]
        self.c = make_company('Acme')
        self.owner = make_user('own', role, self.c)
        self.e1, self.e2 = make_employee(self.c, 'E1', 'a@x.com'), make_employee(self.c, 'E2', 'b@x.com')
        self.stranger = make_employee(make_company('Other'), 'X1', 'x@x.com')
        run = PayrollRun.objects.create(company=self.c, month='September 2026', status='RELEASED')
        for e in (self.e1, self.e2):
            line(run, e, 30000)
            SalaryStructure.objects.create(employee=e, basic=D('20000'), hra=D('5000'), conveyance=D('1000'), special_allowance=D('4000'))
        self.client.force_login(self.owner)
        self.url = reverse('salary_revision_approvals')

    def ask(self, emp, month='2027-01-01', basic='25000', **kw):
        data = {'act': 'request', 'employee': emp.pk, 'effective_from': month, 'basic': basic, 'hra': '5000', 'conveyance': '1000', 'special_allowance': '4000'}
        data.update(kw)
        return self.client.post(self.url, data)

    def test_page_opens(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_request_changes_nothing_until_approved(self):
        self.ask(self.e1)
        self.assertEqual(R.objects.filter(status='PENDING').count(), 1)
        self.assertFalse(SalaryRevision.objects.filter(employee=self.e1, effective_from=datetime.date(2027, 1, 1)).exists())

    def test_approve_creates_the_salary_change(self):
        self.ask(self.e1)
        r = R.objects.get()
        self.client.post(self.url, {'act': 'approve', 'one': r.pk})
        r.refresh_from_db()
        self.assertEqual(r.status, 'APPROVED')
        sal = salary_for_period(self.e1, (datetime.date(2027, 1, 1), datetime.date(2027, 1, 31)))
        self.assertEqual(sal.basic, D('25000'))
        before = salary_for_period(self.e1, (datetime.date(2026, 12, 1), datetime.date(2026, 12, 31)))
        self.assertEqual(before.basic, D('20000'))

    def test_reject_changes_nothing(self):
        self.ask(self.e1)
        r = R.objects.get()
        self.client.post(self.url, {'act': 'reject', 'one': r.pk, 'reason': 'Not agreed'})
        r.refresh_from_db()
        self.assertEqual((r.status, r.decision_note), ('REJECTED', 'Not agreed'))
        self.assertFalse(SalaryRevision.objects.filter(effective_from=datetime.date(2027, 1, 1)).exists())

    def test_process_later_moves_to_next_month(self):
        self.ask(self.e1, month='2027-12-01')
        r = R.objects.get()
        self.client.post(self.url, {'act': 'later', 'one': r.pk})
        r.refresh_from_db()
        self.assertEqual((r.effective_from, r.status), (datetime.date(2028, 1, 1), 'PENDING'))

    def test_bulk_approve_skips_the_one_with_a_problem(self):
        self.ask(self.e1)
        self.ask(self.e2)
        SalaryRevision.objects.create(employee=self.e2, effective_from=datetime.date(2027, 1, 1), basic=D('1'))   # a clash created after the request
        ids = list(R.objects.values_list('pk', flat=True))
        r = self.client.post(self.url, {'act': 'approve', 'ids': ids}, follow=True)
        self.assertEqual(R.objects.get(employee=self.e1).status, 'APPROVED')
        self.assertEqual(R.objects.get(employee=self.e2).status, 'PENDING')
        self.assertContains(r, 'Skipped')
        self.assertContains(r, 'E2')

    def test_nothing_ticked_is_refused(self):
        r = self.client.post(self.url, {'act': 'approve'}, follow=True)
        self.assertContains(r, 'Tick at least one')

    def test_bad_requests_are_refused(self):
        self.ask(self.e1, month='2027-01-15')                       # not the 1st
        self.ask(self.e1, basic='abc')
        self.ask(self.e1, basic='0')
        self.ask(self.stranger)                                     # another company
        self.assertEqual(R.objects.count(), 0)

    def test_an_employee_with_no_pay_run_cannot_be_revised(self):
        e3 = make_employee(self.c, 'E3', 'c@x.com')
        self.ask(e3)
        self.assertEqual(R.objects.count(), 0)

    def test_a_second_request_for_the_same_month_is_refused(self):
        self.ask(self.e1)
        self.ask(self.e1, basic='26000')
        self.assertEqual(R.objects.count(), 1)

    def test_another_companys_request_cannot_be_decided(self):
        other_owner = make_user('own2', self.owner.role, self.stranger.company)
        r = R.objects.create(company=self.c, employee=self.e1, effective_from=datetime.date(2027, 1, 1), basic=D('1'))
        self.client.force_login(other_owner)
        self.client.post(self.url, {'act': 'approve', 'one': r.pk})
        r.refresh_from_db()
        self.assertEqual(r.status, 'PENDING')

    def test_login_is_required(self):
        self.client.logout()
        self.assertNotEqual(self.client.get(self.url).status_code, 200)
'@
foreach ($k in $files.Keys) { [IO.File]::WriteAllText((Join-Path $work $k), ($files[$k] + "`n"), $enc) }
$beforeMig = @(Get-ChildItem (Join-Path $root 'payroll_app/migrations') -Filter '*.py' | ForEach-Object { $_.Name })
$out = @(python (Join-Path $work 'patch_rev.py') $stamp $work 2>&1 | ForEach-Object { "$_" })
$code = $LASTEXITCODE
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
$out | Where-Object { $_ -notmatch '^CHANGED_FILES=' } | ForEach-Object { Write-Host $_ }
if ($code -ne 0) { "Stopped. Nothing was written."; return }
if (($out -join "`n") -match '(?m)^ALREADY APPLIED') { "Already installed. Nothing to do."; return }
$changed = @(($out | Where-Object { $_ -match '^CHANGED_FILES=' }) -replace '^CHANGED_FILES=', '' -split ' ' | Where-Object { $_ })
$trackedChanged = @($changed | Where-Object { git ls-files --error-unmatch -- $_ 2>$null })
$newFiles = @($changed | Where-Object { $trackedChanged -notcontains $_ })
function Undo-All { param($why)
    ""; "NOT COMMITTED: $why"
    git restore -- @($trackedChanged) 2>&1 | Out-Null
    foreach ($f in $newFiles) { Remove-Item (Join-Path $root $f) -Force -ErrorAction SilentlyContinue }
    $now = @(Get-ChildItem (Join-Path $root 'payroll_app/migrations') -Filter '*.py' | ForEach-Object { $_.Name })
    foreach ($m in $now) { if ($beforeMig -notcontains $m) { Remove-Item (Join-Path $root "payroll_app/migrations/$m") -Force -ErrorAction SilentlyContinue } }
    Remove-Item (Join-Path $root 'payroll_app/templates/salary_revision') -Recurse -Force -ErrorAction SilentlyContinue
    "Everything this installer added has been removed again. Your project is as it was before."
}

# the one new database table
""; "Creating the database migration..."
$mk = @(python manage.py makemigrations payroll_app -n salary_revision_requests 2>&1 | ForEach-Object { "$_" })
$mk | ForEach-Object { "  $_" }
$newMig = @(Get-ChildItem (Join-Path $root 'payroll_app/migrations') -Filter '*.py' | Where-Object { $beforeMig -notcontains $_.Name })
if ($newMig.Count -ne 1) { Undo-All "expected exactly one new migration, found $($newMig.Count)."; return }
$migText = Get-Content $newMig[0].FullName -Raw
$ops = @([regex]::Matches($migText, 'migrations\.([A-Za-z]+)\('))
$opNames = @($ops | ForEach-Object { $_.Groups[1].Value })
$onlyOk = ($opNames.Count -gt 0) -and (@($opNames | Where-Object { $_ -ne 'CreateModel' -and $_ -ne 'AddConstraint' }).Count -eq 0) -and (@($opNames | Where-Object { $_ -eq 'CreateModel' }).Count -eq 1) -and ($migText -match 'name=[''"]SalaryRevisionRequest[''"]')
if (-not $onlyOk) { Undo-All "the migration would change more than the one new table ($($opNames -join ', ')). Send me this line."; return }
"  Migration $($newMig[0].Name): creates only the SalaryRevisionRequest table."
$changed += ("payroll_app/migrations/" + $newMig[0].Name)

# the tests, one file at a time
$modules = @('payroll_app.tests_salary_revision_approvals', 'payroll_app.tests_structure', 'payroll_app.tests_np_nav')
foreach ($f in Get-ChildItem (Join-Path $root 'payroll_app') -Filter 'tests*.py') { if ($f.BaseName -match 'salary_history|salary_revision|salary_struct|menu') { $modules += ('payroll_app.' + $f.BaseName) } }
$modules = @($modules | Select-Object -Unique | Where-Object { Test-Path (Join-Path $root ('payroll_app/' + $_.Split('.')[-1] + '.py')) })
""; "Testing $($modules.Count) test files, one at a time. Please do not press Ctrl+C and do not close this window."
$allLog = Join-Path $tmp "salrev_all_$stamp.txt"
'' | Out-File $allLog -Encoding utf8
$bad = @()
$n = 0
foreach ($m in $modules) {
    $n++
    $t0 = Get-Date
    $ml = Join-Path $tmp ("salrev_mod_" + $m.Split('.')[-1] + "_$stamp.txt")
    python manage.py test $m 2>&1 | Out-File $ml -Encoding utf8
    $secs = [int]((Get-Date) - $t0).TotalSeconds
    $res = @(Select-String -Path $ml -Pattern '^(Ran |OK|FAILED)|^(FAIL|ERROR): ' | ForEach-Object { $_.Line })
    $ran = ($res | Where-Object { $_ -match '^Ran (\d+) test' } | Select-Object -First 1)
    $count = if ($ran -match '^Ran (\d+) test') { $Matches[1] } else { '?' }
    $okRun = (@($res | Where-Object { $_ -match '^OK' }).Count -gt 0) -and (@($res | Where-Object { $_ -match '^FAILED' }).Count -eq 0)
    if ($okRun) { "[{0,2}/{1}] OK     {2}   ({3} tests, {4}s)" -f $n, $modules.Count, $m, $count, $secs }
    else {
        $bad += $m
        $names = @($res | Where-Object { $_ -match '^(FAIL|ERROR): ' } | ForEach-Object { ($_ -replace '^(FAIL|ERROR): (\w+).*', '$2') })
        $why = if ($names.Count -eq 0) { 'the test file did not run (it could not be loaded)' } else { ($names -join ', ') }
        "[{0,2}/{1}] FAILED {2}   -> {3}" -f $n, $modules.Count, $m, $why
    }
    Get-Content $ml | Add-Content $allLog
}
if ($bad.Count -gt 0) { Undo-All "$($bad.Count) of $($modules.Count) test files had a failure: $($bad -join ', '). Send me the lines above that say FAILED, and the file $allLog"; return }
$drift = @(python manage.py makemigrations --check --dry-run 2>&1 | ForEach-Object { "$_" })
if ($LASTEXITCODE -ne 0) { Undo-All "the database model check found other unsaved model changes: $($drift -join ' ')"; return }
""; "All $($modules.Count) test files passed."
git add -- @($changed)
git commit -m "Salary Revision Approvals: request, approve, reject, process later, bulk with skip" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_012DNxBSw8YiiRcGGDCeu8rH" 2>&1 | ForEach-Object { "$_" } | Where-Object { $_.Trim() } | ForEach-Object { "  $_" }
if ($LASTEXITCODE -ne 0) { "The commit did not work. Send me the lines above."; return }
if ($push) { git push origin HEAD 2>&1 | ForEach-Object { "$_" } | Where-Object { $_.Trim() -and $_ -notmatch 'RemoteException' } | ForEach-Object { "  $_" } } else { "Committed on this PC, not pushed. To publish it run:  git push origin HEAD" }
""; "DONE. After Render shows 'Deploy live': open Salary Structure in the menu, then Salary Revision Approvals (address /salary-revisions/approvals/)."
}
