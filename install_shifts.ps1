# install_shifts.ps1 - adds the two missing Attendance pages: Assign Shifts (give one shift to many employees at once) and Daily Working Hours (one day, everyone: in, out, hours, late, overtime).
# No database change and no payroll change. It installs, tests and commits on this PC; it pushes only if you type YES. Run it from the payroll project folder.
& {
$ErrorActionPreference = 'Continue'
$root = $PWD.Path
$enc = New-Object System.Text.UTF8Encoding($false)
if (-not (Test-Path "$root/manage.py") -or -not (Test-Path "$root/payroll_app/phase4.py")) { "STOP: open PowerShell in the payroll project folder (the one with manage.py), then run this again."; return }
"Adding Assign Shifts and Daily Working Hours pages"
git fetch origin 2>&1 | Out-Null
$dirty = @(git status --porcelain --untracked-files=no)
# the owner-menu edit that is waiting in base.html (made by install_menu.ps1) is allowed; anything else is not
$other = @($dirty | Where-Object { $_ -notmatch 'payroll_app/templates/base\.html$' })
$baseDirty = @($dirty | Where-Object { $_ -match 'payroll_app/templates/base\.html$' })
if ($baseDirty.Count -gt 0) { $bd = (git diff -- payroll_app/templates/base.html | Out-String); if ($bd -notmatch 'np-menu-v2') { $other += $baseDirty } }
if ($other.Count -gt 0) { "NOTHING WAS CHANGED: you have uncommitted edits to tracked files. Commit or undo them first:"; $other | ForEach-Object { "    $_" }; return }
if ($baseDirty.Count -gt 0) { "  (the new owner menu in base.html is still waiting to be committed; this installer leaves it alone)" }
$behind = [int](git rev-list --count HEAD..origin/main)
if ($behind -gt 0) { "NOTHING WAS CHANGED: this PC is $behind commit(s) behind GitHub. Run: git pull --ff-only   then run this again."; return }
$answer = Read-Host "Type YES (capital letters) to also push to GitHub at the end, or just press Enter to stop before pushing"
$push = ($answer.Trim() -ceq 'YES')
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$tmp = [IO.Path]::GetTempPath()
$work = Join-Path $tmp "shifts_$stamp"
New-Item -ItemType Directory -Path $work -Force | Out-Null
$files = [ordered]@{}
$files['patch_shifts.py'] = @'
"""Adds the Assign Shifts and Daily Working Hours pages. Usage: python patch_shifts.py <stamp> <workdir>  (run from the project folder)"""
import os, shutil, sys
stamp, work = sys.argv[1], sys.argv[2]
APP = 'payroll_app'
U = f'{APP}/urls.py'
if os.path.exists(f'{APP}/shift_pages.py'):
    print('ALREADY APPLIED: shift_pages.py is already in the project.'); sys.exit(0)
def fail(m): print('STOP: ' + m + ' Nothing was written.'); sys.exit(1)
for p in (U, f'{APP}/phase4.py', f'{APP}/attendance_hours.py', f'{APP}/models_phase4.py'):
    if not os.path.exists(p): fail(f'{p} is missing, so this is not the expected project.')
t = open(U, encoding='utf-8', newline='').read()
for name in ("name='shift_assign'", "name='hr_day_hours'"):
    if name in t: fail(f'urls.py already has {name}.')
last = [l for l in t.split('\n') if l.strip()][-1]
if last.startswith((' ', '\t')): fail('urls.py does not end at the top level.')
nl = '\r\n' if '\r\n' in t else '\n'
os.makedirs('backups', exist_ok=True)
bk = f'backups/shift_pages_{stamp}'
os.makedirs(f'{bk}/{APP}', exist_ok=True); shutil.copy2(U, f'{bk}/{U}') if os.path.isdir(f'{bk}/{APP}') else None
new = t.rstrip() + nl + nl + nl.join(['# ---- Assign Shifts and Daily Working Hours ----', 'from . import shift_pages as _shift_pages  # noqa: E402', 'urlpatterns += _shift_pages.urlpatterns']) + nl
open(U, 'w', encoding='utf-8', newline='').write(new)
os.makedirs(f'{APP}/templates/shift_pages', exist_ok=True)
for src, dst in (('shift_pages.py', f'{APP}/shift_pages.py'), ('tests_shift_pages.py', f'{APP}/tests_shift_pages.py'),
                 ('shift_assign.html', f'{APP}/templates/shift_pages/shift_assign.html'), ('day_hours.html', f'{APP}/templates/shift_pages/day_hours.html')):
    shutil.copy2(os.path.join(work, src), dst)
print('Added the Assign Shifts page (/hr/shifts/assign/) and the Daily Working Hours page (/hr/day-hours/). The menu already has links to them; they appear now.')
print('Backup: ' + bk)
print('CHANGED_FILES=' + ' '.join([U, f'{APP}/shift_pages.py', f'{APP}/tests_shift_pages.py', f'{APP}/templates/shift_pages/shift_assign.html', f'{APP}/templates/shift_pages/day_hours.html']))
'@
$files['shift_pages.py'] = @'
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
'@
$files['tests_shift_pages.py'] = @'
import datetime
from django.test import TestCase
from django.urls import reverse
from .models import Attendance
from .models_phase4 import Shift, ShiftAssignment
from .phase4 import shift_for
from .tests import make_company, make_employee, make_user

T = datetime.time


class ShiftPageTests(TestCase):
    def setUp(self):
        self.c = make_company('Acme')
        self.owner = make_user('own', 'OWNER' if False else self._role(), self.c)
        self.e1, self.e2 = make_employee(self.c, 'E1', 'a@x.com'), make_employee(self.c, 'E2', 'b@x.com')
        self.other_co = make_company('Other')
        self.stranger = make_employee(self.other_co, 'X1', 'x@x.com')
        self.day = make_shift = Shift.objects.create(company=self.c, name='Day', start_time=T(9, 0), end_time=T(18, 0), break_minutes=60, grace_minutes=10)
        self.client.force_login(self.owner)

    @staticmethod
    def _role():
        from .models import User
        return [r[0] for r in User._meta.get_field('role').choices if 'OWNER' in r[0].upper() or 'COMPANY' in r[0].upper()][0]

    def test_pages_open(self):
        self.assertEqual(self.client.get(reverse('shift_assign')).status_code, 200)
        self.assertEqual(self.client.get(reverse('hr_day_hours')).status_code, 200)

    def test_bulk_assign_gives_the_shift_to_everyone_ticked(self):
        r = self.client.post(reverse('shift_assign'), {'shift': self.day.pk, 'effective_from': '2026-11-01', 'employee': [self.e1.pk, self.e2.pk]})
        self.assertEqual(r.status_code, 302)
        for e in (self.e1, self.e2):
            self.assertEqual(shift_for(e, datetime.date(2026, 11, 5)), self.day)

    def test_a_new_assignment_ends_the_earlier_one(self):
        night = Shift.objects.create(company=self.c, name='Night', start_time=T(21, 0), end_time=T(6, 0))
        self.client.post(reverse('shift_assign'), {'shift': self.day.pk, 'effective_from': '2026-11-01', 'employee': [self.e1.pk]})
        self.client.post(reverse('shift_assign'), {'shift': night.pk, 'effective_from': '2026-11-16', 'employee': [self.e1.pk]})
        self.assertEqual(shift_for(self.e1, datetime.date(2026, 11, 10)), self.day)
        self.assertEqual(shift_for(self.e1, datetime.date(2026, 11, 20)), night)

    def test_another_companys_employee_is_ignored(self):
        self.client.post(reverse('shift_assign'), {'shift': self.day.pk, 'effective_from': '2026-11-01', 'employee': [self.stranger.pk]})
        self.assertFalse(ShiftAssignment.objects.filter(employee=self.stranger).exists())

    def test_bad_input_changes_nothing(self):
        for data in ({'effective_from': '2026-11-01', 'employee': [self.e1.pk]},
                     {'shift': self.day.pk, 'employee': [self.e1.pk]},
                     {'shift': self.day.pk, 'effective_from': '2026-11-01'},
                     {'shift': self.day.pk, 'effective_from': '2026-11-10', 'effective_to': '2026-11-01', 'employee': [self.e1.pk]}):
            self.client.post(reverse('shift_assign'), data)
        self.assertEqual(ShiftAssignment.objects.count(), 0)

    def test_day_hours_shows_hours_and_late(self):
        ShiftAssignment.objects.create(company=self.c, employee=self.e1, shift=self.day, effective_from=datetime.date(2026, 11, 1))
        d = datetime.date(2026, 11, 10)
        Attendance.objects.create(employee=self.e1, date=d, status='PRESENT', check_in=T(9, 40), check_out=T(19, 0))
        r = self.client.get(reverse('hr_day_hours'), {'date': '2026-11-10'})
        self.assertContains(r, '8h 20m')
        self.assertContains(r, 'Late')
        self.assertContains(r, 'No record')                      # E2 has none
        self.assertEqual(r.context['counts']['late'], 1)

    def test_bad_date_falls_back_to_today_and_csv_downloads(self):
        self.assertEqual(self.client.get(reverse('hr_day_hours'), {'date': 'nonsense'}).status_code, 200)
        r = self.client.get(reverse('hr_day_hours'), {'date': '2026-11-10', 'export': 'csv'})
        self.assertEqual(r['Content-Type'], 'text/csv')
        self.assertIn('Employee code', r.content.decode())

    def test_day_hours_never_lists_another_company(self):
        r = self.client.get(reverse('hr_day_hours'))
        self.assertNotContains(r, 'X1')

    def test_login_is_required(self):
        self.client.logout()
        self.assertNotEqual(self.client.get(reverse('shift_assign')).status_code, 200)
        self.assertNotEqual(self.client.get(reverse('hr_day_hours')).status_code, 200)
'@
$files['shift_assign.html'] = @'
{% extends "base.html" %}
{% block title %}Assign shifts{% endblock %}
{% block content %}{% include "features/_ui.html" %}
<div class="fx">
  <div class="fx-card"><h1>Assign shifts</h1><div class="muted">Pick a shift and a start date, tick the employees, and save. A new assignment ends the earlier one the day before it starts. Create or change shifts on the Shifts page.</div></div>
  {% if not shifts %}<div class="fx-card">There is no active shift yet. Create one on the Shifts page first.</div>{% endif %}
  <form method="post" class="fx-card">{% csrf_token %}
    <div class="fx-filters">
      <label>Shift <select name="shift" required>{% for s in shifts %}<option value="{{ s.pk }}">{{ s }}</option>{% endfor %}</select></label>
      <label>Effective from <input type="date" name="effective_from" value="{{ today }}" required></label>
      <label>Effective to (optional) <input type="date" name="effective_to"></label>
      <button class="fx-btn" type="submit">Assign to ticked employees</button>
    </div>
    <div class="fx-table"><table>
      <tr><th style="width:60px"><input type="checkbox" onclick="document.querySelectorAll('.pick').forEach(function(c){c.checked=this.checked}.bind(this))" aria-label="Tick all"></th><th>Employee</th><th>Current shift (today)</th></tr>
      {% for r in rows %}<tr><td><input class="pick" type="checkbox" name="employee" value="{{ r.e.pk }}"></td><td>{{ r.e.employee_code }} - {{ r.e.first_name }} {{ r.e.last_name }}</td><td>{{ r.shift|default:"No shift" }}</td></tr>
      {% empty %}<tr><td colspan="3" class="muted">No employees.</td></tr>{% endfor %}
    </table></div>
  </form>
</div>{% endblock %}
'@
$files['day_hours.html'] = @'
{% extends "base.html" %}
{% block title %}Daily working hours{% endblock %}
{% block content %}{% include "features/_ui.html" %}
<div class="fx">
  <div class="fx-card"><h1>Daily working hours</h1><div class="muted">Everyone's check-in, check-out and hours for one day. Hours are taken after the shift break. Overtime is shown for information and is not paid automatically.</div></div>
  <form method="get" class="fx-filters"><a class="fx-btn ghost" href="?date={{ prev }}">&larr; Previous day</a><input type="date" name="date" value="{{ day }}"><button class="fx-btn ghost" type="submit">Show</button><a class="fx-btn ghost" href="?date={{ next }}">Next day &rarr;</a><a class="fx-btn" href="?date={{ day }}&export=csv">Download CSV</a></form>
  <div class="fx-card">Present or half day: <b>{{ counts.present }}</b> &nbsp;|&nbsp; No attendance record: <b>{{ counts.no_record }}</b> &nbsp;|&nbsp; Late: <b>{{ counts.late }}</b></div>
  <div class="fx-card fx-table"><table><tr><th>Employee</th><th>Shift</th><th>Status</th><th>Check in</th><th>Check out</th><th>Hours</th><th>Late</th><th>Overtime</th></tr>
    {% for r in rows %}<tr><td>{{ r.e.employee_code }} - {{ r.e.first_name }} {{ r.e.last_name }}</td><td>{{ r.shift|default:"-" }}</td><td>{% if r.rec %}{{ r.rec.get_status_display }}{% else %}No record{% endif %}</td><td>{{ r.rec.check_in|time:"H:i"|default:"-" }}</td><td>{{ r.rec.check_out|time:"H:i"|default:"-" }}</td><td>{{ r.hours }}</td><td>{% if r.late %}Late{% endif %}</td><td>{{ r.overtime }}</td></tr>
    {% empty %}<tr><td colspan="8" class="muted">No employees.</td></tr>{% endfor %}</table></div>
</div>{% endblock %}
'@
foreach ($k in $files.Keys) { [IO.File]::WriteAllText((Join-Path $work $k), ($files[$k] + "`n"), $enc) }
$out = @(python (Join-Path $work 'patch_shifts.py') $stamp $work 2>&1 | ForEach-Object { "$_" })
$code = $LASTEXITCODE
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
$out | Where-Object { $_ -notmatch '^CHANGED_FILES=' } | ForEach-Object { Write-Host $_ }
if ($code -ne 0) { "Stopped. Nothing was written."; return }
if (($out -join "`n") -match '(?m)^ALREADY APPLIED') { "Already installed. Nothing to do."; return }
$changed = @(($out | Where-Object { $_ -match '^CHANGED_FILES=' }) -replace '^CHANGED_FILES=', '' -split ' ' | Where-Object { $_ })

# the tests, one file at a time: the new one, plus every test about the stylesheet, Home, the menu, the layout and the installed app
$modules = @('payroll_app.tests_shift_pages', 'payroll_app.tests_structure', 'payroll_app.tests_phase4')
foreach ($f in Get-ChildItem (Join-Path $root 'payroll_app') -Filter 'tests*.py') { if ($f.BaseName -match 'nav|menu|shift|attendance_hours|work_hours') { $modules += ('payroll_app.' + $f.BaseName) } }
$modules = @($modules | Select-Object -Unique | Where-Object { Test-Path (Join-Path $root ('payroll_app/' + $_.Split('.')[-1] + '.py')) })
""; "Testing $($modules.Count) test files, one at a time. Please do not press Ctrl+C and do not close this window."
$allLog = Join-Path $tmp "shifts_all_$stamp.txt"
'' | Out-File $allLog -Encoding utf8
$bad = @()
$n = 0
foreach ($m in $modules) {
    $n++
    $t0 = Get-Date
    $ml = Join-Path $tmp ("shifts_mod_" + $m.Split('.')[-1] + "_$stamp.txt")
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
if ($bad.Count -gt 0) {
    ""; "NOT COMMITTED: $($bad.Count) of $($modules.Count) test files had a failure: $($bad -join ', ')"
    "Send me the lines above that say FAILED. To undo this change completely, run:"
    "    git restore -- payroll_app/urls.py"
    "    Remove-Item payroll_app/shift_pages.py, payroll_app/tests_shift_pages.py, payroll_app/templates/shift_pages -Recurse"
    return
}
""; "All $($modules.Count) test files passed."
git add -- @($changed)
git commit -m "Add Assign Shifts and Daily Working Hours pages" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_012DNxBSw8YiiRcGGDCeu8rH" 2>&1 | ForEach-Object { "$_" } | Where-Object { $_.Trim() } | ForEach-Object { "  $_" }
if ($LASTEXITCODE -ne 0) { "The commit did not work. Send me the lines above."; return }
if ($push) { git push origin HEAD 2>&1 | ForEach-Object { "$_" } | Where-Object { $_.Trim() -and $_ -notmatch 'RemoteException' } | ForEach-Object { "  $_" } } else { "Committed on this PC, not pushed. To publish it run:  git push origin HEAD" }
""; "DONE. After Render shows 'Deploy live': open Attendance & Leave in the menu. Assign Shifts and Daily Working Hours are there now."
}
