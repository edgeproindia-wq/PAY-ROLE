# EdgePro Payroll — Multi-Tenant SaaS

## Updating an existing install (Windows)
Copy the new files over your project folder (keep your `db.sqlite3`), then:
```powershell
powershell -ExecutionPolicy Bypass -File .\APPLY_UPDATE.ps1
```
It backs up the database, installs packages, migrates, repairs missing columns and runs all tests.

## First-time setup (local)
```bash
python -m venv venv
source venv/bin/activate        # venv\Scripts\activate on Windows
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser   # then set role=ADMIN either in /admin/ or shell
python manage.py runserver
```
Visit `/welcome/` for the public landing page, `/login/` to sign in, `/admin/` for Django admin.

## Roles
- **ADMIN** — platform staff. Full access, `/admin-panel/` for demo requests, company approvals, complaints/requests, audit log.
- **COMPANY_OWNER** — a client's account owner. Scoped to their own `Company`; created via `/register/`, activated only after admin approval.
- **EMPLOYEE** — an individual employee. Scoped to their own `Employee` record; account created by a Company Owner/Admin from Employee Master → "Create Login".

## Tests
```bash
python manage.py test payroll_app
```
64 tests cover auth/OTP, RBAC, company isolation/IDOR, payroll maths, payslip PDFs, bank transfer, demo alerts and every page for every role.

## Deploying to Render
1. Push this repo to GitHub.
2. Create a Render Web Service from the repo — it will use `build.sh` and the `Procfile`.
3. Set environment variables from `.env.example` (`DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `DJANGO_SECURE_SSL_REDIRECT=true`, `DJANGO_SESSION_COOKIE_SECURE=true`, `DJANGO_CSRF_COOKIE_SECURE=true`, and `DATABASE_URL` if using a managed MySQL/Postgres instance instead of SQLite).
4. Render runs `release: python manage.py migrate` automatically before each deploy (see `Procfile`).

See `AUDIT_REPORT.md` for the full technical audit.
