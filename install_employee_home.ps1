# install_employee_home.ps1 - gives the EMPLOYEE home page the Zoho-style card layout: Welcome, Profile Details, Your Payslips (month tabs, donut chart, hide/show amounts), Payment Information, Contributions & Tax. Every older card stays under "More from your account".
# No database change. It installs, tests and commits on this PC; it pushes only if you type YES. Run it from the payroll project folder.
& {
$ErrorActionPreference = 'Continue'
$root = $PWD.Path
$enc = New-Object System.Text.UTF8Encoding($false)
if (-not (Test-Path "$root/manage.py") -or -not (Test-Path "$root/payroll_app/employee_portal.py")) { "STOP: open PowerShell in the payroll project folder (the one with manage.py), then run this again."; return }
"Adding the new employee home page"
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
$work = Join-Path $tmp "emphome_$stamp"
New-Item -ItemType Directory -Path $work -Force | Out-Null
$files = [ordered]@{}
$files['patch_emp.py'] = @'
"""Gives the employee home the Zoho-style card layout. Usage: python patch_emp.py <stamp> <workdir>   (run from the project folder)"""
import os, shutil, sys
stamp, work = sys.argv[1], sys.argv[2]
APP = 'payroll_app'
H, P, T = f'{APP}/templates/employee/home.html', f'{APP}/employee_portal.py', f'{APP}/tests_employee_portal.py'
for p in (H, P, T):
    if not os.path.exists(p): print(f'STOP: {p} is missing, so this is not the expected project. Nothing was written.'); sys.exit(1)
def rd(p): return open(p, encoding='utf-8', newline='').read()
def fail(m): print('STOP: ' + m + ' Nothing was written.'); sys.exit(1)
h = rd(H)
if 'np-ep-v2' in h: print('ALREADY APPLIED: the employee home already has the new layout.'); sys.exit(0)
nl = '\r\n' if '\r\n' in h else '\n'
def N(s): return s.replace('\n', nl)
def once(t, old, new, label):
    old, new = N(old), N(new)
    if t.count(old) != 1: fail(f'{label}: the expected text was found {t.count(old)} times (needed exactly 1).')
    return t.replace(old, new)
# 1. template: replace the dark hero and the four number boxes by the new top, keep everything below
a, b = h.find(N('  <div class="ep-hero">')), h.find(N('  <div class="ep-grid">'))
if a < 0 or b < 0 or b < a: fail('home.html: could not find the old top part (hero and numbers).')
top = open(os.path.join(work, 'new_top.html'), encoding='utf-8').read()
h2 = h[:a] + N(top) + h[b:]
h2 = once(h2, '  @media (max-width:900px){.ep-kpis{grid-template-columns:1fr 1fr}.ep-grid{grid-template-columns:1fr}}\n',
          '  @media (max-width:900px){.ep-kpis{grid-template-columns:1fr 1fr}.ep-grid{grid-template-columns:1fr}}\n' + open(os.path.join(work, 'new.css'), encoding='utf-8').read(), 'home.html style')
# 2. view: add the totals
p = rd(P)
p2 = once(p, "        'pan_masked': _mask(getattr(emp, 'pan_number', ''), keep=3),\n    }\n    return render(request, 'employee/home.html', ctx)",
          "        'pan_masked': _mask(getattr(emp, 'pan_number', ''), keep=3),\n        'ytd': ytd_totals(payslips),\n    }\n    return render(request, 'employee/home.html', ctx)", 'employee_portal.py view')
pn = '\r\n' if '\r\n' in p2 else '\n'
p2 = p2.rstrip() + pn + pn + 'from .employee_home_extra import ytd_totals  # noqa: E402' + pn
# 3. the old test looked for the old heading
t = rd(T)
t2 = once(t, "self.assertContains(resp, 'Welcome, Anu')", "self.assertContains(resp, 'Welcome Anu!')", 'tests_employee_portal.py')
os.makedirs('backups', exist_ok=True)
bk = f'backups/employee_home_{stamp}'
for f in (H, P, T):
    os.makedirs(os.path.dirname(f'{bk}/{f}'), exist_ok=True); shutil.copy2(f, f'{bk}/{f}')
for f, txt in ((H, h2), (P, p2), (T, t2)): open(f, 'w', encoding='utf-8', newline='').write(txt)
shutil.copy2(os.path.join(work, 'employee_home_extra.py'), f'{APP}/employee_home_extra.py')
shutil.copy2(os.path.join(work, 'tests_employee_home_v2.py'), f'{APP}/tests_employee_home_v2.py')
print('Employee home now has: Welcome, Profile Details, Your Payslips (month tabs, donut, hidden amounts), Payment Information, Contributions & Tax. All the older cards are kept under "More from your account".')
print('Backup: ' + bk)
print('CHANGED_FILES=' + ' '.join([H, P, T, f'{APP}/employee_home_extra.py', f'{APP}/tests_employee_home_v2.py']))
'@
$files['new_top.html'] = @'
  <!-- np-ep-v2 -->
  <div class="ep2-welcome">
    <h1>Welcome {{ emp.first_name }}!</h1>
    <p>{{ emp.designation }}{% if emp.company %} at {{ emp.company.name }}{% endif %}.</p>
  </div>

  <section class="ep2-card">
    <div class="ep2-head"><h2><span class="ep2-ic">&#128100;</span> Profile Details</h2>{% url 'ess' as u_prof %}{% if u_prof %}<a href="{{ u_prof }}">View Profile &rarr;</a>{% endif %}</div>
    <div class="ep2-who">
      <span class="ep2-avatar">{{ emp.first_name|slice:":1" }}{{ emp.last_name|slice:":1" }}</span>
      <div><b>{{ emp.first_name }} {{ emp.last_name }}</b>
        <div class="ep2-sub"><span>{{ emp.designation }} ({{ emp.employee_code }})</span><span>{{ emp.email }}</span>{% if emp.phone %}<span>{{ emp.phone }}</span>{% endif %}</div></div>
    </div>
    <div class="ep2-facts">
      <div><small>Employment Status</small><b>{{ emp.get_employment_status_display }}</b></div>
      <div><small>Date of Joining</small><b>{{ emp.date_of_joining|date:"d/m/Y" }}</b></div>
      <div><small>Department</small><b>{{ emp.department|default:"-" }}</b></div>
      <div><small>Company</small><b>{{ emp.company.name|default:"-" }}</b></div>
      {% if shift %}<div><small>Shift</small><b>{{ shift }}</b></div>{% endif %}
    </div>
  </section>

  <div class="ep2-two">
    <section class="ep2-card">
      <div class="ep2-head"><h2><span class="ep2-ic">&#129534;</span> Your Payslips</h2>
        <button type="button" class="ep2-eye" id="ep2Eye" aria-pressed="false">Show amounts</button></div>
      {% if payslips %}
      <div class="ep2-tabs" role="tablist">{% for p in payslips|slice:":4" %}<button type="button" role="tab" class="ep2-tab{% if forloop.first %} on{% endif %}" data-i="{{ forloop.counter0 }}">{{ p.period }}</button>{% endfor %}</div>
      {% for p in payslips|slice:":4" %}
      <div class="ep2-pane{% if forloop.first %} on{% endif %}" data-i="{{ forloop.counter0 }}" data-net="{{ p.line.net_pay|default:0|stringformat:'s' }}" data-ded="{{ p.line.total_deductions|default:0|stringformat:'s' }}">
        <div class="ep2-donut" aria-hidden="true"><span>{{ p.period }}</span></div>
        <div class="ep2-legend">
          <div class="l lg"><small>Take Home</small><b class="amt" data-v="&#8377;{{ p.line.net_pay|floatformat:'2g' }}">************</b></div>
          <div class="l lr"><small>Deductions</small><b class="amt" data-v="&#8377;{{ p.line.total_deductions|floatformat:'2g' }}">************</b></div>
          <div class="l ln"><small>Gross Pay</small><b class="amt" data-v="&#8377;{{ p.line.gross_salary|floatformat:'2g' }}">************</b></div>
        </div>
        <a class="ep2-link" href="{% url 'payslip_pdf' p.line.pk %}">Download payslip PDF &rarr;</a>
      </div>
      {% endfor %}
      <div class="ep2-foot">{% url 'payslips' as u_ps %}{% if u_ps %}<a href="{{ u_ps }}">View Payslips &rarr;</a>{% endif %}</div>
      {% else %}<p class="ep2-empty">No payslips released yet.</p>{% endif %}
    </section>

    <section class="ep2-card">
      <div class="ep2-head"><h2><span class="ep2-ic">&#127974;</span> Payment Information</h2></div>
      <div class="ep2-pay">
        <small>Payment Mode</small><b>{% if bank_masked %}Bank Transfer{% else %}Not set up{% endif %}</b>
        {% if emp.bank_name %}<small>Bank</small><b>{{ emp.bank_name }}{% if emp.bank_branch %}, {{ emp.bank_branch }}{% endif %}</b>{% endif %}
        {% if bank_masked %}<small>Account Number</small><b>{{ bank_masked }}</b>{% endif %}
        {% if emp.ifsc_code %}<small>IFSC</small><b>{{ emp.ifsc_code }}</b>{% endif %}
        {% if not bank_masked %}<p class="ep2-empty">Ask HR to add your bank details so salary can be paid.</p>{% endif %}
      </div>
    </section>
  </div>

  <section class="ep2-card">
    <div class="ep2-head"><h2><span class="ep2-ic">&#128181;</span> Contributions &amp; Tax <span class="ep2-sub2">last {{ ytd.months }} payslip{{ ytd.months|pluralize }}</span></h2></div>
    <div class="ep2-facts">
      <div><small>Provident Fund (you)</small><b class="amt" data-v="&#8377;{{ ytd.pf|floatformat:'2g' }}">************</b></div>
      <div><small>ESI (you)</small><b class="amt" data-v="&#8377;{{ ytd.esi|floatformat:'2g' }}">************</b></div>
      <div><small>Income Tax (TDS)</small><b class="amt" data-v="&#8377;{{ ytd.tds|floatformat:'2g' }}">************</b></div>
      <div><small>Take home total</small><b class="amt" data-v="&#8377;{{ ytd.net|floatformat:'2g' }}">************</b></div>
    </div>
  </section>

  <div class="ep2-quick">{% url 'leave_management' as q1 %}{% if q1 %}<a href="{{ q1 }}">Apply leave</a>{% endif %}{% url 'attendance' as q2 %}{% if q2 %}<a href="{{ q2 }}">My attendance</a>{% endif %}{% url 'reimbursement' as q3 %}{% if q3 %}<a href="{{ q3 }}">Submit a claim</a>{% endif %}{% url 'investment_declaration' as q4 %}{% if q4 %}<a href="{{ q4 }}">Investment declaration</a>{% endif %}{% url 'grievances' as q5 %}{% if q5 %}<a href="{{ q5 }}">Raise a grievance</a>{% endif %}{% url 'my_document_upload' as q6 %}{% if q6 %}<a href="{{ q6 }}">Upload a document</a>{% endif %}</div>
  <script data-ep2>
  (function(){
    var eye=document.getElementById('ep2Eye'); if(!eye) return;
    function setAmt(show){document.querySelectorAll('.ep2-card .amt').forEach(function(e){e.textContent=show?e.getAttribute('data-v'):'************';});eye.setAttribute('aria-pressed',show?'true':'false');eye.textContent=show?'Hide amounts':'Show amounts';}
    eye.addEventListener('click',function(){setAmt(eye.getAttribute('aria-pressed')!=='true');});
    function donut(p){var n=parseFloat(p.getAttribute('data-net'))||0,d=parseFloat(p.getAttribute('data-ded'))||0,t=n+d,deg=t>0?Math.round(n/t*360):360;p.querySelector('.ep2-donut').style.background='conic-gradient(#12a37a 0 '+deg+'deg,#ef6c5a '+deg+'deg 360deg)';}
    document.querySelectorAll('.ep2-pane').forEach(donut);
    document.querySelectorAll('.ep2-tab').forEach(function(t){t.addEventListener('click',function(){
      document.querySelectorAll('.ep2-tab').forEach(function(x){x.classList.toggle('on',x===t);});
      document.querySelectorAll('.ep2-pane').forEach(function(p){p.classList.toggle('on',p.getAttribute('data-i')===t.getAttribute('data-i'));});});});
  })();
  </script>
  <h2 class="ep2-more">More from your account</h2>
'@
$files['new.css'] = @'
  /* np-ep-v2: employee home in the Zoho-style card layout */
  .ep2-welcome h1{font-size:28px;font-weight:800;margin:0 0 4px;color:#0f172a}
  .ep2-welcome p{margin:0 0 18px;color:#475569;font-size:15px}
  .ep2-card{background:#fff;border:1px solid #e6ebf0;border-radius:14px;padding:20px 24px;margin-bottom:18px;box-shadow:0 2px 14px rgba(15,23,42,.05)}
  .ep2-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}
  .ep2-head h2{font-size:17px;font-weight:700;margin:0;display:flex;align-items:center;gap:8px;color:#0f172a}
  .ep2-head a,.ep2-foot a,.ep2-link{color:#2f6fe4;font-weight:600;font-size:14px;text-decoration:none}
  .ep2-sub2{font-size:12.5px;font-weight:500;color:#64748b}
  .ep2-who{display:flex;align-items:center;gap:14px;background:#f4f7fa;border-radius:12px;padding:14px 16px;margin-bottom:16px}
  .ep2-avatar{width:48px;height:48px;border-radius:12px;background:#0e9fb0;color:#fff;font-weight:800;display:flex;align-items:center;justify-content:center;font-size:17px;flex:0 0 auto}
  .ep2-who b{font-size:18px}
  .ep2-sub{display:flex;gap:18px;flex-wrap:wrap;color:#475569;font-size:14px;margin-top:2px}
  .ep2-facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:16px 28px}
  .ep2-facts small,.ep2-pay small{display:block;color:#64748b;font-size:13px}
  .ep2-facts b,.ep2-pay b{display:block;font-size:15px;color:#0f172a;margin-top:3px;font-weight:600}
  .ep2-two{display:grid;grid-template-columns:1.5fr 1fr;gap:18px}
  .ep2-two>section{min-width:0}
  .ep2-tabs{display:flex;gap:6px;flex-wrap:wrap;border-bottom:1px solid #e6ebf0;margin-bottom:16px}
  .ep2-tab{background:none;border:0;border-bottom:2px solid transparent;padding:8px 10px;color:#64748b;font:inherit;font-weight:600;cursor:pointer}
  .ep2-tab.on{color:#0f172a;border-bottom-color:#2f6fe4}
  .ep2-pane{display:none;grid-template-columns:200px 1fr;gap:24px;align-items:center}
  .ep2-pane.on{display:grid}
  .ep2-donut{width:170px;height:170px;border-radius:50%;background:#12a37a;position:relative;margin:auto}
  .ep2-donut:after{content:"";position:absolute;inset:26px;background:#fff;border-radius:50%}
  .ep2-donut span{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;z-index:1;font-size:12.5px;font-weight:700;text-align:center;padding:0 40px;color:#0f172a}
  .ep2-legend .l{border-left:3px solid;padding:2px 0 2px 14px;margin-bottom:14px}
  .ep2-legend .lg{border-color:#12a37a}.ep2-legend .lr{border-color:#ef6c5a}.ep2-legend .ln{border-color:#cbd5e1}
  .ep2-legend small{color:#475569;font-size:14px;display:block}
  .ep2-legend b{font-size:17px;letter-spacing:.5px}
  .ep2-link{grid-column:1/-1}
  .ep2-foot{border-top:1px solid #e6ebf0;margin-top:16px;padding-top:14px;text-align:center}
  .ep2-eye{background:#fff;border:1px solid #cbd5e1;border-radius:8px;padding:6px 12px;font:inherit;font-size:13px;font-weight:600;color:#334155;cursor:pointer}
  .ep2-pay b{margin-bottom:12px}
  .ep2-empty{color:#64748b;font-size:14px;margin:6px 0}
  .ep2-quick{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px}
  .ep2-quick a{padding:9px 16px;border:1px solid #cfe9ed;background:#f2fbfc;color:#0e7c8a;border-radius:999px;text-decoration:none;font-weight:600;font-size:14px}
  .ep2-quick a:hover{background:#0e9fb0;color:#fff}
  .ep2-more{font-size:18px;font-weight:800;margin:26px 0 12px;color:#0f172a}
  @media (max-width:900px){.ep2-two{grid-template-columns:1fr}.ep2-pane{grid-template-columns:1fr}}
'@
$files['employee_home_extra.py'] = @'
"""Totals for the employee home: what the employee contributed and took home over the latest payslips (up to 12)."""
from decimal import Decimal


def ytd_totals(payslips):
    """`payslips` is the list of payroll lines the home page already loaded. Missing numbers count as zero."""
    def total(name):
        return sum((Decimal(str(getattr(p, name, 0) or 0)) for p in payslips), Decimal('0'))
    return {'months': len(payslips), 'pf': total('pf'), 'esi': total('esi'), 'tds': total('tds'), 'net': total('net_pay')}
'@
$files['tests_employee_home_v2.py'] = @'
from decimal import Decimal
from types import SimpleNamespace
from django.test import SimpleTestCase
from .employee_home_extra import ytd_totals


class YtdTests(SimpleTestCase):
    def test_sums_the_lines_and_counts_them(self):
        a, b = SimpleNamespace(pf=1800, esi=0, tds=500, net_pay=Decimal('25000.50')), SimpleNamespace(pf=Decimal('1800'), esi=None, tds=0, net_pay=1000)
        t = ytd_totals([a, b])
        self.assertEqual((t['months'], t['pf'], t['esi'], t['tds'], t['net']), (2, Decimal('3600'), Decimal('0'), Decimal('500'), Decimal('26000.50')))

    def test_nothing_gives_zeros(self):
        self.assertEqual(ytd_totals([]), {'months': 0, 'pf': 0, 'esi': 0, 'tds': 0, 'net': 0})


from django.db import models
from django.test import TestCase
from django.urls import reverse
from .models import Company, Employee, PayrollRunLine, User
from .tests_employee_portal import make, released_status


class HomeV2PageTests(TestCase):
    def setUp(self):
        c = make(Company, name='Acme', status='APPROVED')
        extra = {'email_verified': True} if 'email_verified' in {f.name for f in User._meta.get_fields()} else {}
        self.u = make(User, username='emp1', role='EMPLOYEE', company=c, email='e1@acme.com', **extra)
        self.e = make(Employee, company=c, user=self.u, first_name='Anu', last_name='Rao', email='e1@acme.com')
        self.run_model = PayrollRunLine._meta.get_field('payroll_run').related_model
        self.money = {f.name: Decimal('1000') for f in PayrollRunLine._meta.concrete_fields if isinstance(f, models.DecimalField) and not f.null}

    def test_home_without_payslips_still_opens(self):
        self.client.force_login(self.u)
        r = self.client.get(reverse('employee_home'))
        self.assertContains(r, 'Welcome Anu!')
        self.assertContains(r, 'Profile Details')
        self.assertContains(r, 'No payslips released yet.')
        self.assertEqual(r.context['ytd']['months'], 0)

    def test_home_shows_payslip_tabs_masked_amounts_and_totals(self):
        run = make(self.run_model, status=released_status())
        make(PayrollRunLine, payroll_run=run, employee=self.e, **self.money)
        self.client.force_login(self.u)
        r = self.client.get(reverse('employee_home'))
        self.assertContains(r, 'Your Payslips')
        self.assertContains(r, '************')                 # amounts start hidden
        self.assertContains(r, 'Payment Information')
        self.assertEqual(r.context['ytd']['months'], 1)
        self.assertEqual(r.context['ytd']['pf'], Decimal('1000'))

    def test_the_other_cards_are_still_there(self):
        self.client.force_login(self.u)
        r = self.client.get(reverse('employee_home'))
        for text in ('Tax &amp; Documents', 'Announcements', 'Quick actions', 'My profile'):
            self.assertContains(r, text)
'@
foreach ($k in $files.Keys) { [IO.File]::WriteAllText((Join-Path $work $k), ($files[$k] + "`n"), $enc) }
$out = @(python (Join-Path $work 'patch_emp.py') $stamp $work 2>&1 | ForEach-Object { "$_" })
$code = $LASTEXITCODE
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
$out | Where-Object { $_ -notmatch '^CHANGED_FILES=' } | ForEach-Object { Write-Host $_ }
if ($code -ne 0) { "Stopped. Nothing was written."; return }
if (($out -join "`n") -match '(?m)^ALREADY APPLIED') { "Already installed. Nothing to do."; return }
$changed = @(($out | Where-Object { $_ -match '^CHANGED_FILES=' }) -replace '^CHANGED_FILES=', '' -split ' ' | Where-Object { $_ })

# the tests, one file at a time: the new one, plus every test about the stylesheet, Home, the menu, the layout and the installed app
$modules = @('payroll_app.tests_employee_home_v2', 'payroll_app.tests_employee_portal')
foreach ($f in Get-ChildItem (Join-Path $root 'payroll_app') -Filter 'tests*.py') { if ($f.BaseName -match 'nav|menu|employee|ess|payslip|dashboard') { $modules += ('payroll_app.' + $f.BaseName) } }
$modules = @($modules | Select-Object -Unique | Where-Object { Test-Path (Join-Path $root ('payroll_app/' + $_.Split('.')[-1] + '.py')) })
""; "Testing $($modules.Count) test files, one at a time. Please do not press Ctrl+C and do not close this window."
$allLog = Join-Path $tmp "emphome_all_$stamp.txt"
'' | Out-File $allLog -Encoding utf8
$bad = @()
$n = 0
foreach ($m in $modules) {
    $n++
    $t0 = Get-Date
    $ml = Join-Path $tmp ("emphome_mod_" + $m.Split('.')[-1] + "_$stamp.txt")
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
    "    git restore -- payroll_app/templates/employee/home.html payroll_app/employee_portal.py payroll_app/tests_employee_portal.py"
    "    Remove-Item payroll_app/employee_home_extra.py, payroll_app/tests_employee_home_v2.py"
    return
}
""; "All $($modules.Count) test files passed."
git add -- @($changed)
git commit -m "New employee home page in the card layout" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_012DNxBSw8YiiRcGGDCeu8rH" 2>&1 | ForEach-Object { "$_" } | Where-Object { $_.Trim() } | ForEach-Object { "  $_" }
if ($LASTEXITCODE -ne 0) { "The commit did not work. Send me the lines above."; return }
if ($push) { git push origin HEAD 2>&1 | ForEach-Object { "$_" } | Where-Object { $_.Trim() -and $_ -notmatch 'RemoteException' } | ForEach-Object { "  $_" } } else { "Committed on this PC, not pushed. To publish it run:  git push origin HEAD" }
""; "DONE. After Render shows 'Deploy live': log in as an employee and open the home page. Press Ctrl+F5 once."
}
