<#
  EdgePro Payroll - safe update script (Windows PowerShell)

  Run from inside the project folder (the one containing manage.py):
      powershell -ExecutionPolicy Bypass -File .\APPLY_UPDATE.ps1

  What it does, in order - it stops at the first failure:
    1. Backs up db.sqlite3 (and media\) with a timestamp. Nothing is deleted.
    2. Creates/uses venv and installs requirements.
    3. schema_audit  -> reports tables/columns missing vs. the models
    4. migrate       -> applies 0002 (new columns/tables) + 0003 (back-fill)
    5. schema_audit --fix -> repairs columns missing on an old/faked DB
                            (e.g. "no such column: payroll_app_employee.user_id")
    6. check, makemigrations --check, full test suite (on a temporary test DB)
#>
$ErrorActionPreference = 'Stop'
function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Run($exe, [string[]]$argv) {
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $exe $($argv -join ' ')" -ForegroundColor Red; exit $LASTEXITCODE }
}

if (-not (Test-Path .\manage.py)) { Write-Host 'Run this script from the folder that contains manage.py' -ForegroundColor Red; exit 1 }

Step '1. Backup'
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
New-Item -ItemType Directory -Force -Path .\backups | Out-Null
if (Test-Path .\db.sqlite3) {
    Copy-Item .\db.sqlite3 ".\backups\db_$stamp.sqlite3"
    Write-Host "Database backed up to backups\db_$stamp.sqlite3" -ForegroundColor Green
} else { Write-Host 'No db.sqlite3 yet - a new one will be created.' -ForegroundColor Yellow }
if (Test-Path .\media) { Copy-Item .\media ".\backups\media_$stamp" -Recurse; Write-Host 'media\ backed up' -ForegroundColor Green }

Step '2. Virtual environment + packages'
if (-not (Test-Path .\venv\Scripts\python.exe)) { Run 'python' @('-m', 'venv', 'venv') }
$py = '.\venv\Scripts\python.exe'
Run $py @('-m', 'pip', 'install', '--upgrade', 'pip', '-q')
Run $py @('-m', 'pip', 'install', '-r', 'requirements.txt', '-q')

# Local development: DEBUG on, console email (OTP codes print in this window)
$env:DJANGO_DEBUG = 'true'
Remove-Item Env:RENDER -ErrorAction SilentlyContinue

Step '3. Schema audit (before migrate)'
Run $py @('manage.py', 'schema_audit')

Step '4. Apply migrations'
Run $py @('manage.py', 'showmigrations', 'payroll_app')
Run $py @('manage.py', 'migrate')

Step '5. Schema audit + repair (adds only missing nullable columns)'
Run $py @('manage.py', 'schema_audit', '--fix')

Step '6. Checks and tests'
Run $py @('manage.py', 'check')
Run $py @('manage.py', 'makemigrations', '--check', '--dry-run')
Run $py @('manage.py', 'test', 'payroll_app')

Write-Host "`nALL STEPS PASSED." -ForegroundColor Green
Write-Host "Start the server with:  .\venv\Scripts\python.exe manage.py runserver" -ForegroundColor Green
Write-Host "Need an admin login?    .\venv\Scripts\python.exe manage.py createsuperuser" -ForegroundColor Green
