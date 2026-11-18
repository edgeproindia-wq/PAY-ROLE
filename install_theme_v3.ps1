# install_theme_v3.ps1 - gives the whole app the calm teal "glass" look of your NKP Work Tracker: floating light sidebar, soft rounded cards, teal buttons, top bar, tables, and a "Live" badge on Home with matching chart colours.
# Styling only: no page logic, data or payroll calculation is touched. It adds one clearly marked block to static/css/namma-theme.css (delete that block to go back).
# Run it from the payroll project folder. It installs, tests and commits on this PC; it pushes only if you type YES.
& {
$ErrorActionPreference = 'Continue'
$root = $PWD.Path
$enc = New-Object System.Text.UTF8Encoding($false)
if (-not (Test-Path "$root/manage.py") -or -not (Test-Path "$root/static/css/namma-theme.css")) { "STOP: open PowerShell in the payroll project folder (the one with manage.py), then run this again."; return }
"New look: teal glass theme for the whole app"
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
$work = Join-Path $tmp "themev3_$stamp"
New-Item -ItemType Directory -Path $work -Force | Out-Null
$files = [ordered]@{}
$files['patch_theme.py'] = @'
"""Applies the teal look: appends the theme block to static/css/namma-theme.css, recolours the Home charts, adds a "Live" badge to the Home heading.
Usage: python patch_theme.py <stamp> <workdir>   (run from the project folder)"""
import os, shutil, sys

stamp, work = sys.argv[1], sys.argv[2]
CSS, DASH = 'static/css/namma-theme.css', 'payroll_app/templates/Dashboard.html'
MARK = 'np-theme-v3 begin'
def rd(p): return open(p, encoding='utf-8', newline='').read()
def fail(m): print('STOP: ' + m + ' Nothing was written.'); sys.exit(1)
for p in (CSS, DASH):
    if not os.path.exists(p): fail(p + ' is missing.')
css, dash = rd(CSS), rd(DASH)
if MARK in css:
    print('ALREADY APPLIED: the teal theme is already in namma-theme.css.'); sys.exit(0)
block = open(os.path.join(work, 'np-theme-v3.css'), encoding='utf-8').read().strip('\n')
nl = '\r\n' if '\r\n' in css else '\n'
new_css = css.rstrip() + nl + nl + block.replace('\n', nl) + nl

dn = '\r\n' if '\r\n' in dash else '\n'
def once(t, old, new, label):
    if t.count(old) != 1: fail('%s: expected text found %d times in %s (needed exactly 1).' % (label, t.count(old), DASH))
    return t.replace(old, new)
t = dash
t = once(t, '<p class="page-subtitle">Overview of employees, payroll runs and pending approvals</p>',
         '<p class="page-subtitle">Overview of employees, payroll runs and pending approvals<span class="np-live">Live</span></p>', 'heading')
t = once(t, "const palette = ['#147d39','#0f9d6a','#f5a524','#ef6c5a','#0369a1','#189645','#14b8a6'];",
         "const palette = ['#0e9fb0','#2f6fe4','#f5a524','#ef6c5a','#12a37a','#8b5cf6','#64748b'];", 'chart colours')
n = t.count("'#147d39'")
if n < 2: fail('expected the green chart colour at least twice, found %d.' % n)
t = t.replace("'#147d39'", "'#0e9fb0'")
t = t.replace("rgba(20,125,57,.12)", "rgba(14,159,176,.14)")

os.makedirs('backups', exist_ok=True)
bk = 'backups/theme_v3_' + stamp
for p in (CSS, DASH):
    os.makedirs(os.path.dirname(bk + '/' + p), exist_ok=True); shutil.copy2(p, bk + '/' + p)
open(CSS, 'w', encoding='utf-8', newline='').write(new_css)
open(DASH, 'w', encoding='utf-8', newline='').write(t)
shutil.copy2(os.path.join(work, 'tests_theme_v3.py'), 'payroll_app/tests_theme_v3.py')
print('Teal theme added to %s; Home heading and %d chart colour(s) updated. Backup: %s' % (CSS, n + 1, bk))
print('CHANGED_FILES=%s %s payroll_app/tests_theme_v3.py' % (CSS, DASH))
'@
$files['np-theme-v3.css'] = @'
/* np-theme-v3 begin: calm teal "glass" look for the whole app (colours, cards, sidebar, top bar, tables, buttons). Only styling; no page logic.
   To go back to the old look, delete everything between "np-theme-v3 begin" and "np-theme-v3 end". */
:root{
  --accent:#0e9fb0; --accent-dark:#0b7f8d; --accent-soft:#e1f6f8;
  --success:#12a37a; --success-soft:#e1f7f0; --warn:#c2740c; --warn-soft:#fff3df; --danger:#dc3b3b; --danger-soft:#fdeceb; --info:#2f6fe4; --info-soft:#e8f0fe;
  --ink:#0d2b36; --body:#2f4b57; --muted:#6a8591; --faint:#9db3bc;
  --line:#dcebee; --line-soft:#ebf4f6; --card:#ffffff; --radius:18px; --radius-sm:12px;
  --shadow:0 1px 2px rgba(13,43,54,.04),0 8px 24px rgba(14,159,176,.07); --shadow-md:0 12px 34px rgba(13,43,54,.12);
}
html body{ background:linear-gradient(135deg,#e6f6f8 0%,#f3fafb 45%,#e9f4f9 100%) fixed !important; color:var(--body); }
html body, html body input, html body select, html body textarea, html body button{ font-family:'Inter',system-ui,-apple-system,'Segoe UI',Arial,sans-serif; }

/* ---- sidebar: floating white glass ---- */
html body .sidebar{ top:12px !important; left:12px !important; bottom:12px !important; height:auto !important; width:260px; border-radius:22px;
  background:rgba(255,255,255,.82) !important; backdrop-filter:blur(14px); -webkit-backdrop-filter:blur(14px); color:var(--body) !important;
  border:1px solid rgba(255,255,255,.9); box-shadow:0 10px 40px rgba(14,159,176,.14); }
html body .sidebar .sb-brand{ border-bottom:1px solid var(--line-soft) !important; padding:18px 20px 14px; }
html body .sidebar .sb-brand b, html body .sidebar .sb-user b{ color:var(--ink) !important; }
html body .sidebar .sb-brand small, html body .sidebar .sb-user small{ color:var(--muted) !important; }
html body .sidebar .sb-nav a, html body .sidebar .sb-nav button, html body .sidebar .sb-group-head, html body .sidebar .sb-sub a{
  color:var(--body) !important; border-left:0 !important; border-radius:12px; margin:2px 10px; font-weight:600; }
html body .sidebar .sb-nav a{ padding:9px 14px !important; width:auto; }
html body .sidebar .sb-sub a{ font-weight:500; padding:7px 12px !important; font-size:13px; color:var(--muted) !important; }
html body .sidebar .sb-nav a:hover, html body .sidebar .sb-group-head:hover{ background:var(--accent-soft) !important; color:var(--accent-dark) !important; }
html body .sidebar .sb-nav a.active{ background:var(--accent-soft) !important; color:var(--accent-dark) !important; box-shadow:inset 3px 0 0 var(--accent); }
html body .sidebar .sb-group-head{ background:transparent; border:0; text-align:left; }
html body .sidebar .sb-label, html body .sidebar .sb-section{ color:var(--faint) !important; opacity:1 !important; }
html body .sidebar .sb-sub{ border-left:1px dashed var(--line); margin-left:22px; padding-left:4px; }
html body .sidebar svg.svg-ic{ stroke:currentColor; }
html body .sidebar .sb-user{ border-top:1px solid var(--line-soft) !important; }
html body .sidebar .sb-collapse{ color:var(--muted) !important; }
html body .sidebar .sb-logo, html body .sidebar .sb-avatar{ background:linear-gradient(135deg,#19c3d3,#0e9fb0) !important; }
html body .main{ margin-left:284px; }
html body.sb-collapsed .sidebar{ width:76px; }
html body.sb-collapsed .main{ margin-left:100px; }
@media (max-width:900px){ html body .main, html body.sb-collapsed .main{ margin-left:0; } html body .sidebar{ top:0 !important; left:0 !important; bottom:0 !important; border-radius:0 22px 22px 0; width:270px; } }

/* ---- top bar ---- */
html body .topbar{ background:rgba(255,255,255,.72) !important; backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px); border-bottom:1px solid rgba(220,235,238,.8) !important; }
html body .tb-btn{ border-radius:14px; border:1px solid var(--line); background:rgba(255,255,255,.9); }
html body .tb-btn:hover{ border-color:var(--accent); background:var(--accent-soft); }

/* ---- cards, stat cards, tables ---- */
html body .card{ background:rgba(255,255,255,.86); border:1px solid rgba(220,235,238,.9); border-radius:var(--radius); box-shadow:var(--shadow); }
html body .card-header{ border-bottom:1px solid var(--line-soft); }
html body .card-header h3{ color:var(--ink); font-weight:800; }
html body .kpi-card{ background:rgba(255,255,255,.9) !important; border:1px solid rgba(220,235,238,.9); border-radius:var(--radius); box-shadow:var(--shadow); color:var(--ink) !important; }
html body .kpi-row .kpi-card .label{ color:var(--muted) !important; }
html body .kpi-row .kpi-card .value{ color:var(--ink) !important; font-size:28px; }
html body .kpi-row .kpi-card .text-muted, html body .kpi-row .kpi-card .text-sm{ color:var(--muted) !important; font-weight:500; }
html body .kpi-card .icon-badge{ border-radius:12px; }
html body .dx-stat{ border-radius:14px; background:rgba(255,255,255,.8); border-color:var(--line); }
html body .dx-hello{ border-radius:var(--radius); border-color:rgba(220,235,238,.9); background:linear-gradient(135deg,#dff5f8,rgba(255,255,255,.9)); }
html body table{ background:transparent; }
html body th{ background:#f1f9fa; color:var(--muted); }
html body tbody tr:hover{ background:#f4fbfc; }
html body .page-title{ font-size:26px; letter-spacing:-.02em; color:var(--ink); }
html body .badge-neutral{ background:#e9f1f3; }
html body .tab.active{ color:var(--accent-dark); border-color:var(--accent); }
html body .modal-box{ border-radius:20px; }

/* ---- buttons and fields ---- */
html body .btn{ border-radius:12px; font-weight:700; }
html body .btn-primary{ background:linear-gradient(135deg,#19b6c6,#0e9fb0); box-shadow:0 6px 16px rgba(14,159,176,.28); }
html body .btn-primary:hover{ background:linear-gradient(135deg,#14aab9,#0b8d9c); }
html body .btn-outline{ background:rgba(255,255,255,.9); border-color:var(--line); }
html body .btn-outline:hover{ border-color:var(--accent); color:var(--accent-dark); }
html body .btn-dark{ background:#12394a; }
html body input[type=text], html body input[type=email], html body input[type=number], html body input[type=date], html body input[type=password], html body input[type=tel], html body select, html body textarea{ border-radius:12px; border-color:var(--line); background:rgba(255,255,255,.95); }
html body input:focus, html body select:focus, html body textarea:focus{ border-color:var(--accent); box-shadow:0 0 0 3px rgba(14,159,176,.16); }
html body .pagination span.current{ background:var(--accent); }

/* ---- small additions used by the new Home header ---- */
.np-live{ display:inline-flex; align-items:center; gap:6px; margin-left:10px; padding:3px 10px; border-radius:999px; background:var(--success-soft); color:var(--success); font-size:11.5px; font-weight:700; vertical-align:middle; }
.np-live::before{ content:''; width:7px; height:7px; border-radius:50%; background:var(--success); box-shadow:0 0 0 3px rgba(18,163,122,.2); }
@media print{ html body{ background:#fff !important; } }
/* np-theme-v3 end */
'@
$files['tests_theme_v3.py'] = @'
"""The teal look is in place, the Home page still works, and the old green is gone from the Home charts."""
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from .tests import make_company, make_user


class ThemeV3Tests(TestCase):
    def test_the_theme_is_in_the_shared_stylesheet(self):
        text = open(finders.find('css/namma-theme.css'), encoding='utf-8').read()
        self.assertIn('np-theme-v3 begin', text)
        self.assertIn('np-theme-v3 end', text)
        self.assertIn('--accent:#0e9fb0', text)
        self.assertIn('.sidebar', text)

    def test_the_app_pages_still_link_the_stylesheet(self):
        owner = make_user('themeowner', 'COMPANY_OWNER', make_company('Theme Co'))
        self.client.force_login(owner)
        html = self.client.get(reverse('dashboard')).content.decode()
        self.assertIn('css/namma-theme.css', html)

    def test_home_has_the_live_badge_and_teal_charts(self):
        owner = make_user('themeowner2', 'COMPANY_OWNER', make_company('Theme Co 2'))
        self.client.force_login(owner)
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('np-live', html)
        self.assertIn("'#0e9fb0'", html)
        self.assertNotIn("'#147d39'", html)

    def test_the_login_page_is_not_touched(self):
        self.client.logout()
        self.assertEqual(self.client.get('/login/').status_code, 200)
'@
foreach ($k in $files.Keys) { [IO.File]::WriteAllText((Join-Path $work $k), ($files[$k] + "`n"), $enc) }
$out = @(python (Join-Path $work 'patch_theme.py') $stamp $work 2>&1 | ForEach-Object { "$_" })
$code = $LASTEXITCODE
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
$out | Where-Object { $_ -notmatch '^CHANGED_FILES=' } | ForEach-Object { Write-Host $_ }
if ($code -ne 0) { "Stopped. Nothing was written."; return }
if (($out -join "`n") -match '(?m)^ALREADY APPLIED') { "Already installed. Nothing to do."; return }
$changed = @(($out | Where-Object { $_ -match '^CHANGED_FILES=' }) -replace '^CHANGED_FILES=', '' -split ' ' | Where-Object { $_ })

# the tests, one file at a time: the new one, plus every test about the stylesheet, Home, the menu, the layout and the installed app
$modules = @('payroll_app.tests_theme_v3', 'payroll_app.tests_styles_css', 'payroll_app.tests_pwa', 'payroll_app.tests_home_entry')
foreach ($f in Get-ChildItem (Join-Path $root 'payroll_app') -Filter 'tests*.py') {
    if ($f.BaseName -match 'theme|style|css|dashboard|nav|brand|layout|landing') { $modules += ('payroll_app.' + $f.BaseName); continue }
    if (Select-String -Path $f.FullName -Pattern 'namma-theme|147d39|page-subtitle|Overview of employees|attTrendChart' -Quiet) { $modules += ('payroll_app.' + $f.BaseName) }
}
$modules = @($modules | Select-Object -Unique | Where-Object { Test-Path (Join-Path $root ('payroll_app/' + $_.Split('.')[-1] + '.py')) })
""; "Testing $($modules.Count) test files, one at a time. Please do not press Ctrl+C and do not close this window."
$allLog = Join-Path $tmp "themev3_all_$stamp.txt"
'' | Out-File $allLog -Encoding utf8
$bad = @()
$n = 0
foreach ($m in $modules) {
    $n++
    $t0 = Get-Date
    $ml = Join-Path $tmp ("themev3_mod_" + $m.Split('.')[-1] + "_$stamp.txt")
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
    "    git restore -- static/css/namma-theme.css payroll_app/templates/Dashboard.html"
    "    Remove-Item payroll_app/tests_theme_v3.py"
    return
}
""; "All $($modules.Count) test files passed."
git add -- @($changed)
git commit -m "New look: teal glass theme (sidebar, cards, top bar, tables, buttons), Live badge and teal charts on Home" 2>&1 | ForEach-Object { "$_" } | Where-Object { $_.Trim() } | ForEach-Object { "  $_" }
if ($LASTEXITCODE -ne 0) { "The commit did not work. Send me the lines above."; return }
if ($push) { git push origin HEAD 2>&1 | ForEach-Object { "$_" } | Where-Object { $_.Trim() -and $_ -notmatch 'RemoteException' } | ForEach-Object { "  $_" } } else { "Committed on this PC, not pushed. To publish it run:  git push origin HEAD" }
""; "DONE. After Render shows 'Deploy live': press Ctrl+F5 and open Home. You should see the floating light sidebar, rounded white cards, teal buttons and a green 'Live' badge next to the heading."
"To go back to the old look: delete the block between 'np-theme-v3 begin' and 'np-theme-v3 end' at the end of static/css/namma-theme.css."
}
