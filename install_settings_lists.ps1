# install_settings_lists.ps1 - Settings pack 1: Work Locations, Departments and Designations as real lists (with "Add from my employees"), in Organisation & Payroll Configuration.
# It adds ONE small database table (a migration). Run it from the payroll project folder. It installs, tests and commits on this PC; it pushes only if you type YES.
& {
$ErrorActionPreference = 'Continue'
$root = $PWD.Path
$enc = New-Object System.Text.UTF8Encoding($false)
if (-not (Test-Path "$root/manage.py") -or -not (Test-Path "$root/payroll_app/payroll_structure.py")) { "STOP: open PowerShell in the payroll project folder (the one with manage.py), then run this again."; return }
"Settings pack 1: Work Locations, Departments, Designations"
git fetch origin 2>&1 | Out-Null
$dirty = @(git status --porcelain --untracked-files=no)
if ($dirty.Count -gt 0) { "NOTHING WAS CHANGED: you have uncommitted edits to tracked files. Commit or undo them first:"; $dirty | ForEach-Object { "    $_" }; return }
$behind = [int](git rev-list --count HEAD..origin/main)
if ($behind -gt 0) { "NOTHING WAS CHANGED: this PC is $behind commit(s) behind GitHub. Run: git pull --ff-only   then run this again."; return }
$answer = Read-Host "Type YES (capital letters) to also push to GitHub at the end, or just press Enter to stop before pushing"
$push = ($answer.Trim() -ceq 'YES')
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$tmp = [IO.Path]::GetTempPath()
$work = Join-Path $tmp "setl_$stamp"
New-Item -ItemType Directory -Path $work -Force | Out-Null
$files = [ordered]@{}
$files['patch_p1.py'] = @'
"""Adds Work Locations, Departments and Designations lists. Usage: python patch_p1.py <stamp> <workdir>   (run from the project folder)"""
import os, re, shutil, sys
stamp, work = sys.argv[1], sys.argv[2]
APP = 'payroll_app'
M, U, S, B = f'{APP}/models.py', f'{APP}/urls.py', f'{APP}/payroll_structure.py', f'{APP}/templates/base.html'
if os.path.exists(f'{APP}/master_lists.py'):
    print('ALREADY APPLIED: master_lists.py is already in the project.'); sys.exit(0)
def rd(p): return open(p, encoding='utf-8', newline='').read()
def fail(m): print('STOP: ' + m + ' Nothing was written.'); sys.exit(1)
for p in (M, U, S, B):
    if not os.path.exists(p): fail(f'{p} is missing, so this is not the expected project.')
new = {}
def append(path, text):
    t = new.get(path) if path in new else rd(path)
    nl = '\r\n' if '\r\n' in t else '\n'
    last = [l for l in t.split('\n') if l.strip()][-1]
    if last.startswith((' ', '\t')): fail(f'{path} does not end at the top level.')
    new[path] = t.rstrip() + nl + nl + text.replace('\n', nl) + nl
append(M, 'from . import master_list_models  # noqa: E402,F401')
append(U, 'from . import master_lists as _ml  # noqa: E402\nurlpatterns += _ml.urlpatterns')
st = rd(S); nl = '\r\n' if '\r\n' in st else '\n'
a, b = '  Branch Master|' + nl, '  Department & Cost Centre|' + nl
if st.count(a) != 1 or st.count(b) != 1: fail('payroll_structure.py: the Branch Master / Department lines were not found exactly once.')
st = st.replace(a, '  Work Locations|settings_work_locations' + nl).replace(b, '  Departments|settings_departments' + nl + '  Designations|settings_designations' + nl)
new[S] = st
bt = rd(B); nlb = '\r\n' if '\r\n' in bt else '\n'
m = [x for x in re.finditer(r"^([ \t]*)\{% url 'settings' as (\w+) %\}[^\n\r]*Company Master[^\n\r]*\r?\n", bt, re.M)]
if len(m) != 1: fail(f'base.html: the Company Master menu line was found {len(m)} times (needed exactly 1).')
ind = m[0].group(1)
add = ''.join(ind + "{% url '" + n + "' as u_ml" + str(i) + " %}{% if u_ml" + str(i) + " %}<a href=\"{{ u_ml" + str(i) + " }}\"><span class=\"tx\">" + lab + "</span></a>{% endif %}" + nlb
              for i, (n, lab) in enumerate((('settings_work_locations', 'Work Locations'), ('settings_departments', 'Departments'), ('settings_designations', 'Designations'))))
new[B] = bt[:m[0].end()] + add + bt[m[0].end():]
os.makedirs('backups', exist_ok=True)
bk = f'backups/master_lists_{stamp}'
for p in new:
    os.makedirs(os.path.dirname(f'{bk}/{p}'), exist_ok=True); shutil.copy2(p, f'{bk}/{p}')
for p, t in new.items(): open(p, 'w', encoding='utf-8', newline='').write(t)
os.makedirs(f'{APP}/templates/master_lists', exist_ok=True)
for src, dst in (('master_list_models.py', f'{APP}/master_list_models.py'), ('master_lists.py', f'{APP}/master_lists.py'), ('list.html', f'{APP}/templates/master_lists/list.html'), ('tests_master_lists.py', f'{APP}/tests_master_lists.py')):
    shutil.copy2(os.path.join(work, src), dst)
print('Added Work Locations, Departments and Designations (Organisation & Payroll Configuration). Each has Add, Rename, Switch off, Delete, and "Add from my employees".')
print('Backup: ' + bk)
print('CHANGED_FILES=' + ' '.join([M, U, S, B, f'{APP}/master_list_models.py', f'{APP}/master_lists.py', f'{APP}/templates/master_lists/list.html', f'{APP}/tests_master_lists.py']))
'@
$files['master_list_models.py'] = @'
"""Company master lists: work locations (branches), departments and designations. One row = one name in one list."""
from django.db import models


class MasterItem(models.Model):
    KINDS = [('LOCATION', 'Work location'), ('DEPARTMENT', 'Department'), ('DESIGNATION', 'Designation')]
    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='master_items')
    kind = models.CharField(max_length=12, choices=KINDS)
    name = models.CharField(max_length=120)
    detail = models.CharField(max_length=200, blank=True, help_text='Work location: city or address. Optional.')
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['name']
        constraints = [models.UniqueConstraint(fields=['company', 'kind', 'name'], name='unique_master_item_name')]

    def __str__(self):
        return f'{self.get_kind_display()}: {self.name}'
'@
$files['master_lists.py'] = @'
"""Settings > Work Locations, Departments, Designations. Plain lists the company owner keeps; "Add from my employees" fills a list from what is already typed on the employees."""
from django.contrib import messages
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import path

from .audit import log_action
from .master_list_models import MasterItem
from .models import Employee
from .permissions import company_owner_required

PAGES = {
    'LOCATION': ('Work Locations', 'settings_work_locations', None, 'Branches and offices, for example Hosur, Hubli, Bangalore.'),
    'DEPARTMENT': ('Departments', 'settings_departments', 'department', 'The departments your employees belong to.'),
    'DESIGNATION': ('Designations', 'settings_designations', 'designation', 'Job titles used in your company.'),
}
MAX_NAME = 120


def _clean(text):
    return ' '.join((text or '').split())[:MAX_NAME]


def _used(company, field):
    """name (lower case) -> number of employees whose typed value matches."""
    if not field:
        return {}
    rows = Employee.objects.filter(company=company).exclude(**{field: ''}).values(field).annotate(n=Count('pk'))
    out = {}
    for r in rows:
        k = _clean(r[field]).lower()
        out[k] = out.get(k, 0) + r['n']
    return out


def _view(request, kind):
    title, url_name, emp_field, blurb = PAGES[kind]
    company = request.user.company
    items = MasterItem.objects.filter(company=company, kind=kind)
    if request.method == 'POST':
        act = request.POST.get('act', '')
        if act == 'add':
            name = _clean(request.POST.get('name'))
            if not name:
                messages.error(request, 'Type a name.')
            elif items.filter(name__iexact=name).exists():
                messages.error(request, f'"{name}" is already in the list.')
            else:
                it = MasterItem.objects.create(company=company, kind=kind, name=name, detail=_clean(request.POST.get('detail'))[:200])
                log_action(request, 'CREATE', it, details=f'{title}: added {name}')
                messages.success(request, f'"{name}" added.')
        elif act == 'import' and emp_field:
            have = {i.name.lower() for i in items}
            added = 0
            for name in sorted({_clean(v) for v in Employee.objects.filter(company=company).exclude(**{emp_field: ''}).values_list(emp_field, flat=True)}):
                if name and name.lower() not in have:
                    MasterItem.objects.create(company=company, kind=kind, name=name)
                    have.add(name.lower())
                    added += 1
            messages.success(request, f'{added} added from your employees.' if added else 'Everything your employees use is already in the list.')
        elif act in ('toggle', 'delete', 'rename'):
            it = items.filter(pk=request.POST.get('pk') or 0).first()
            if it is None:
                messages.error(request, 'That entry was not found.')
            elif act == 'toggle':
                it.active = not it.active
                it.save(update_fields=['active'])
            elif act == 'delete':
                if _used(company, emp_field).get(it.name.lower(), 0):
                    messages.error(request, f'"{it.name}" is used by employees. Switch it off instead of deleting.')
                else:
                    it.delete()
                    messages.success(request, 'Deleted.')
            else:
                name = _clean(request.POST.get('name'))
                if not name:
                    messages.error(request, 'Type a name.')
                elif items.filter(name__iexact=name).exclude(pk=it.pk).exists():
                    messages.error(request, f'"{name}" is already in the list.')
                else:
                    it.name, it.detail = name, _clean(request.POST.get('detail'))[:200]
                    it.save(update_fields=['name', 'detail'])
                    messages.success(request, 'Saved.')
        return redirect(url_name)
    used = _used(company, emp_field)
    rows = [{'i': i, 'n': used.get(i.name.lower(), 0) if emp_field else None} for i in items]
    return render(request, 'master_lists/list.html', {'title': title, 'blurb': blurb, 'rows': rows, 'kind': kind, 'can_import': bool(emp_field),
                  'tabs': [(PAGES[k][0], PAGES[k][1], k == kind) for k in PAGES], 'has_detail': kind == 'LOCATION'})


@company_owner_required
def work_locations(request):
    return _view(request, 'LOCATION')


@company_owner_required
def departments(request):
    return _view(request, 'DEPARTMENT')


@company_owner_required
def designations(request):
    return _view(request, 'DESIGNATION')


urlpatterns = [
    path('settings/work-locations/', work_locations, name='settings_work_locations'),
    path('settings/departments/', departments, name='settings_departments'),
    path('settings/designations/', designations, name='settings_designations'),
]
'@
$files['list.html'] = @'
{% extends "base.html" %}
{% block title %}{{ title }}{% endblock %}
{% block content %}{% include "features/_ui.html" %}
<div class="fx">
  <div class="fx-card"><h1>{{ title }}</h1><div class="muted">{{ blurb }}</div></div>
  <div class="fx-filters">{% for label, url_name, on in tabs %}{% url url_name as u %}<a class="fx-btn{% if not on %} ghost{% endif %}" href="{{ u }}">{{ label }}</a>{% endfor %}</div>
  <div class="fx-card">
    <form method="post" class="fx-filters">{% csrf_token %}<input type="hidden" name="act" value="add">
      <label>Name <input name="name" maxlength="120" required></label>
      {% if has_detail %}<label>City / address (optional) <input name="detail" maxlength="200"></label>{% endif %}
      <button class="fx-btn" type="submit">Add</button>
    </form>
    {% if can_import %}<form method="post" style="margin-top:10px">{% csrf_token %}<input type="hidden" name="act" value="import"><button class="fx-btn ghost" type="submit">Add from my employees</button> <span class="muted">Adds every {{ title|lower }} name already typed on an employee.</span></form>{% endif %}
  </div>
  <div class="fx-card fx-table"><table><tr><th>Name</th>{% if has_detail %}<th>City / address</th>{% endif %}{% if can_import %}<th>Employees</th>{% endif %}<th>Status</th><th></th></tr>
    {% for r in rows %}<tr>
      <td colspan="1"><form method="post" id="f{{ r.i.pk }}">{% csrf_token %}<input type="hidden" name="pk" value="{{ r.i.pk }}"><input name="name" value="{{ r.i.name }}" maxlength="120"></form></td>
      {% if has_detail %}<td><input form="f{{ r.i.pk }}" name="detail" value="{{ r.i.detail }}" maxlength="200"></td>{% endif %}
      {% if can_import %}<td>{{ r.n }}</td>{% endif %}
      <td>{% if r.i.active %}Active{% else %}Off{% endif %}</td>
      <td style="white-space:nowrap"><button form="f{{ r.i.pk }}" class="fx-btn ghost" name="act" value="rename" type="submit">Save</button>
        <button form="f{{ r.i.pk }}" class="fx-btn ghost" name="act" value="toggle" type="submit">{% if r.i.active %}Switch off{% else %}Switch on{% endif %}</button>
        <button form="f{{ r.i.pk }}" class="fx-btn danger" name="act" value="delete" type="submit">Delete</button></td></tr>
    {% empty %}<tr><td colspan="5" class="muted">Nothing here yet. Add the first one above.</td></tr>{% endfor %}</table></div>
</div>{% endblock %}
'@
$files['tests_master_lists.py'] = @'
from django.test import TestCase
from django.urls import reverse
from .master_list_models import MasterItem
from .tests import make_company, make_employee, make_user


class MasterListTests(TestCase):
    def setUp(self):
        from .models import User
        role = [r[0] for r in User._meta.get_field('role').choices if 'OWNER' in r[0].upper() or 'COMPANY' in r[0].upper()][0]
        self.c = make_company('Acme')
        self.owner = make_user('own', role, self.c)
        self.other = make_company('Other')
        self.other_owner = make_user('own2', role, self.other)
        e = make_employee(self.c, 'E1', 'a@x.com')
        e.department, e.designation = 'Detailing', 'Detailer'
        e.save()
        e2 = make_employee(self.c, 'E2', 'b@x.com')
        e2.department, e2.designation = 'detailing ', 'Checker'
        e2.save()
        self.client.force_login(self.owner)

    def post(self, url_name, **data):
        return self.client.post(reverse(url_name), data, follow=True)

    def test_all_three_pages_open(self):
        for n in ('settings_work_locations', 'settings_departments', 'settings_designations'):
            self.assertEqual(self.client.get(reverse(n)).status_code, 200)

    def test_add_and_duplicate_and_empty(self):
        self.post('settings_work_locations', act='add', name='Hosur', detail='Tamil Nadu')
        self.post('settings_work_locations', act='add', name=' hosur ')
        self.post('settings_work_locations', act='add', name='   ')
        self.assertEqual(MasterItem.objects.filter(company=self.c, kind='LOCATION').count(), 1)

    def test_import_from_employees_makes_one_entry_per_name(self):
        self.post('settings_departments', act='import')
        self.assertEqual(list(MasterItem.objects.filter(kind='DEPARTMENT').values_list('name', flat=True)), ['Detailing'])
        r = self.post('settings_departments', act='import')
        self.assertContains(r, 'already in the list')

    def test_employee_count_is_shown_and_used_item_cannot_be_deleted(self):
        self.post('settings_departments', act='import')
        it = MasterItem.objects.get(kind='DEPARTMENT')
        r = self.client.get(reverse('settings_departments'))
        self.assertEqual(r.context['rows'][0]['n'], 2)
        self.post('settings_departments', act='delete', pk=it.pk)
        self.assertTrue(MasterItem.objects.filter(pk=it.pk).exists())

    def test_unused_item_can_be_renamed_switched_off_and_deleted(self):
        self.post('settings_designations', act='add', name='Trainee')
        it = MasterItem.objects.get(name='Trainee')
        self.post('settings_designations', act='rename', pk=it.pk, name='Junior Trainee')
        self.post('settings_designations', act='toggle', pk=it.pk)
        it.refresh_from_db()
        self.assertEqual((it.name, it.active), ('Junior Trainee', False))
        self.post('settings_designations', act='delete', pk=it.pk)
        self.assertFalse(MasterItem.objects.filter(pk=it.pk).exists())

    def test_another_company_cannot_touch_my_items(self):
        self.post('settings_work_locations', act='add', name='Hosur')
        it = MasterItem.objects.get(name='Hosur')
        self.client.force_login(self.other_owner)
        self.post('settings_work_locations', act='delete', pk=it.pk)
        self.assertTrue(MasterItem.objects.filter(pk=it.pk).exists())
        self.assertEqual(self.client.get(reverse('settings_work_locations')).context['rows'], [])

    def test_login_is_required(self):
        self.client.logout()
        self.assertNotEqual(self.client.get(reverse('settings_departments')).status_code, 200)
'@
foreach ($k in $files.Keys) { [IO.File]::WriteAllText((Join-Path $work $k), ($files[$k] + "`n"), $enc) }
$beforeMig = @(Get-ChildItem (Join-Path $root 'payroll_app/migrations') -Filter '*.py' | ForEach-Object { $_.Name })
$out = @(python (Join-Path $work 'patch_p1.py') $stamp $work 2>&1 | ForEach-Object { "$_" })
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
    Remove-Item (Join-Path $root 'payroll_app/templates/master_lists') -Recurse -Force -ErrorAction SilentlyContinue
    "Everything this installer added has been removed again. Your project is as it was before."
}

# the one new database table
""; "Creating the database migration..."
$mk = @(python manage.py makemigrations payroll_app -n master_items 2>&1 | ForEach-Object { "$_" })
$mk | ForEach-Object { "  $_" }
$newMig = @(Get-ChildItem (Join-Path $root 'payroll_app/migrations') -Filter '*.py' | Where-Object { $beforeMig -notcontains $_.Name })
if ($newMig.Count -ne 1) { Undo-All "expected exactly one new migration, found $($newMig.Count)."; return }
$migText = Get-Content $newMig[0].FullName -Raw
$ops = @([regex]::Matches($migText, 'migrations\.([A-Za-z]+)\('))
$opNames = @($ops | ForEach-Object { $_.Groups[1].Value })
$onlyOk = ($opNames.Count -gt 0) -and (@($opNames | Where-Object { $_ -ne 'CreateModel' -and $_ -ne 'AddConstraint' }).Count -eq 0) -and (@($opNames | Where-Object { $_ -eq 'CreateModel' }).Count -eq 1) -and ($migText -match 'name=[''"]MasterItem[''"]')
if (-not $onlyOk) { Undo-All "the migration would change more than the one new table ($($opNames -join ', ')). Send me this line."; return }
"  Migration $($newMig[0].Name): creates only the MasterItem table."
$changed += ("payroll_app/migrations/" + $newMig[0].Name)

# the tests, one file at a time
$modules = @('payroll_app.tests_master_lists', 'payroll_app.tests_structure', 'payroll_app.tests_np_nav')
foreach ($f in Get-ChildItem (Join-Path $root 'payroll_app') -Filter 'tests*.py') { if ($f.BaseName -match 'menu|employee_import|settings') { $modules += ('payroll_app.' + $f.BaseName) } }
$modules = @($modules | Select-Object -Unique | Where-Object { Test-Path (Join-Path $root ('payroll_app/' + $_.Split('.')[-1] + '.py')) })
""; "Testing $($modules.Count) test files, one at a time. Please do not press Ctrl+C and do not close this window."
$allLog = Join-Path $tmp "setl_all_$stamp.txt"
'' | Out-File $allLog -Encoding utf8
$bad = @()
$n = 0
foreach ($m in $modules) {
    $n++
    $t0 = Get-Date
    $ml = Join-Path $tmp ("setl_mod_" + $m.Split('.')[-1] + "_$stamp.txt")
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
git commit -m "Settings: Work Locations, Departments and Designations lists" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_012DNxBSw8YiiRcGGDCeu8rH" 2>&1 | ForEach-Object { "$_" } | Where-Object { $_.Trim() } | ForEach-Object { "  $_" }
if ($LASTEXITCODE -ne 0) { "The commit did not work. Send me the lines above."; return }
if ($push) { git push origin HEAD 2>&1 | ForEach-Object { "$_" } | Where-Object { $_.Trim() -and $_ -notmatch 'RemoteException' } | ForEach-Object { "  $_" } } else { "Committed on this PC, not pushed. To publish it run:  git push origin HEAD" }
""; "DONE. After Render shows 'Deploy live': open Organisation & Payroll Configuration in the menu. Work Locations, Departments and Designations are there. Press Add from my employees to fill them from your staff."
}
