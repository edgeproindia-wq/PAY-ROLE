# EdgePro Payroll — Audit, Repair & Production-Readiness Report (Sept 2026)

Verdict: **NOT PRODUCTION READY — ISSUES REMAINING** (only external configuration items remain; see §21).
All code-level workflows below were tested. `AUDIT_REPORT_PREVIOUS.md` is the earlier audit, kept for history.

## 1. Existing architecture
Django (runs on 5.2.15 and 6.1), one app `payroll_app`, custom `User` (role ADMIN / COMPANY_OWNER / EMPLOYEE + company FK),
multi-tenant `Company`, SQLite locally, `DATABASE_URL` (Aiven Postgres) on Render, WhiteNoise, Gunicorn, role decorators in
`permissions.py`, audit log, in-app notifications, ~45 templates on a shared `base.html` design system.

## 2–5. Feature matrix (status found BEFORE this work)
| Feature | Found | Now |
|---|---|---|
| Login / logout / reset / change password | Works | Kept; + email login, reasons for refusal, POST logout, 1h reset expiry |
| Email verification / OTP | **Missing** | Added |
| Superuser → admin role | **Broken** | Fixed + data migration |
| Client registration / approval | Partial | Completed (mobile, duplicate email, suspend/reactivate, no delete) |
| Admin dashboard | Partial | Completed with live DB figures |
| Demo request | Partial (no email/SMS/date/dup check) | Completed |
| Complaints / client requests / audit log | Works | Kept |
| Employee CRUD, Employee↔User link | Works (admin could not create) | Fixed admin create; login email synced |
| Bank details | Partial (A/C + IFSC only) | Completed |
| Attendance | Partial (no check-in/out) | Completed |
| Leave | Partial (no balance/overlap/decider) | Completed |
| Reimbursement | Partial (no receipts, not in payroll) | Completed |
| Payroll calculation | **Broken** (net = gross) | Fixed |
| Payroll tenant isolation / duplicates | **Broken** | Fixed |
| Payslip PDF / payslip number | **Missing** (stub) | Added |
| Bank transfer | **Missing** (stub pages) | Added |
| Reports / CSV / Excel / DOCX | Works | Kept; payslip exports now show full breakdown |
| `smoke_test.py` | **Dangerous**: deleted all users/companies in the real DB | Replaced with a safe runner |

## 6–7. Issues: ISSUE → ROOT CAUSE → FIX → TEST → RESULT
1. **Net pay = gross, no deductions** → run creation copied gross into net → `payroll_engine.py`: prorated LOP (ABSENT=1, HALF_DAY=0.5), arrears, approved reimbursements, PF 12% earned basic (company setting), ESI 0.75% if gross ≤ 21,000, TDS using the slab already in the Income Tax page → `test_line_includes_lop_arrears_reimbursement_and_deductions` → PASS.
2. **Arrears/reimbursements could be paid twice** → no "paid" marker → `paid_in_run` FK claimed per run, released on reprocess → `test_arrears_and_reimbursement_not_paid_twice`, `test_reprocess_recalculates_without_losing_claims` → PASS.
3. **Payroll runs not owned by a company; empty runs visible/approvable by every tenant** → `PayrollRun` had no company FK and views used `lines__isnull=True` → `PayrollRun.company` + `scope_runs()`; legacy runs back-filled from their lines → `test_other_company_cannot_see_or_act_on_run` → PASS.
4. **Duplicate payroll for same month; status could jump DRAFT→RELEASED** → no checks → one run per company+month, strict DRAFT→VALIDATED→APPROVED→RELEASED, month format validated → `test_duplicate_month_and_bad_month_rejected`, `test_status_cannot_skip_steps` → PASS.
5. **Superusers had role EMPLOYEE** (no admin notifications, blocked from adding employees) → `createsuperuser` never sets role → `User.save()` forces ADMIN; migration 0003 fixes existing rows → `SuperuserRoleTests` + legacy-DB migration test → PASS.
6. **No email verification** → not built → hashed 6-digit OTP, 10-min expiry, 5 attempts, resend throttle; admin approval needs verified email (explicit override available) → `RegistrationOtpApprovalLoginTests` → PASS.
7. **Suspended/rejected company users kept access** → only `is_active` checked → auth backend + role decorator check company status, active sessions end immediately → `SuspensionTests` → PASS.
8. **Local "CSRF verification failed"** → DEBUG defaulted to False locally ⇒ Secure-only cookies over http → DEBUG defaults to True locally, False on Render (`RENDER` env), SECRET_KEY mandatory in production → manual prod-mode run + `test_csrf_enforced` → PASS.
9. **Demo request: no alerts, no date, duplicates** → not built → email to `DEMO_NOTIFY_EMAIL` (<admin email>), SMS to `DEMO_NOTIFY_MOBILE` (<admin mobile>) via Fast2SMS/Twilio, delivery status stored on the request, 30-min duplicate guard, Indian mobile validation, double-click guard → `DemoRequestTests` (incl. SMTP down, SMS provider down) → PASS.
10. **Payslip PDF was a stub; "View" linked to a page with no payslip** → not built → ReportLab PDF, payslip detail page, payslip number; scoped queryset ⇒ other users' IDs return 404 → `test_payslip_pdf_access_control` → PASS.
11. **Bank transfer pages were stubs** → not built → `BankPayment` (OneToOne per payslip = no duplicates), bank snapshot, CSV transfer file, INITIATED/PAID/FAILED/RETRY; PAID only with a unique UTR; PAID is final; every change audited → `test_bank_transfer_lifecycle` → PASS.
12. **Employee forms required picking "employee"** (employees had to select themselves; failed if left blank) → field shown to employees → removed for EMPLOYEE role and fixed server-side (leave, reimbursement, investment declaration) → leave/reimbursement tests → PASS.
13. **Admin could not create employees** → view refused admins with no company → admin chooses company; owners are always forced to their own → `EmployeeManagementTests` → PASS.
14. **Receipts/documents: none; media would have been public** → no upload field → receipt upload (pdf/png/jpg ≤5 MB) served only via permission-checked view; public `/media/` route removed → `test_reimbursement_receipt_upload_and_isolation` → PASS.
15. **`no such column: payroll_app_employee.user_id`** on old DBs → DB built by an older copy with migration faked → `manage.py schema_audit [--fix]` (adds missing nullable columns with Django's schema editor, refuses while migrations are pending) → reproduced the exact error and repaired it → PASS.
16. **Audit log errors silently swallowed** → `except: pass` → logged; login/logout/failed-login recorded via auth signals (never passwords).
17. **Leave/claims could be re-decided or self-approved** → no guards → PENDING-only decisions, self-approval 403, `decided_by/decided_at`, employee notified → PASS.
18. **Edit-employee page showed blank counts** → queryset passed instead of page → fixed.
19. **Client list pagination unordered** → aggregate ignores Meta.ordering → explicit `order_by`.

## 8. Files modified / added
Modified: `config/settings.py`, `config/urls.py`, `payroll_app/{models,views,forms,urls,admin,permissions,audit,signals,tests}.py`, `requirements.txt`, `.env.example`, `.gitignore`, `smoke_test.py`, templates: `base.html`, `public_base.html`, `auth/login.html`, `public/{company_register,request_demo}.html`, `admin_panel/{dashboard,company_list,company_approvals,demo_requests}.html`, `Attendance.html`, `Leave Management.html`, `Reimbursement.html`, `Payslips.html`, `Bank Transfer.html`, `Employee Master.html`, `Payroll/{Payroll Combined,Payroll Run Detail}.html`, `payslip management/*.html`, `Income Tax Management/Investment declartion.html`.
Added: `payroll_app/{backends,notifications,payroll_engine,pdf,leave}.py`, `payroll_app/management/commands/schema_audit.py`, `payroll_app/tests_audit.py`, `templates/auth/verify_email.html`, `APPLY_UPDATE.ps1`.

## 9–10. Migrations / database changes
`0002_audit_enhancements` — only **adds** nullable/defaulted columns and 2 new tables (EmailOTP, BankPayment).
`0003_backfill_existing_data` — superusers → ADMIN; legacy runs get their company; legacy payslip lines get a payslip number and totals derived from their stored gross/net (**amounts already paid are never recalculated**).
Verified on a database created by the previous version: all rows kept (users, employees, payslip lines, leave), back-fill correct, schema audit clean.

## 11–15. Verification by area
Authentication, admin, client, employee, payroll: 64 automated tests (success + failure paths), including every page for every role (200 where allowed, 403 where not), direct-URL/POST/ID-manipulation attempts across tenants and employees.

## 16. Security
Backend RBAC on every view; tenant + employee isolation on every object; CSRF on; POST-only state changes; hashed passwords & OTPs; single-use 1-hour reset tokens; secure cookies, HSTS, SSL redirect, X-Frame DENY in production; secrets only via env vars.

## 17. UI/UX
Existing design kept. Added: check-in/out buttons, leave balance cards, receipt links, payslip view/PDF, bank-transfer tabs with inline actions, admin search/filter, visible form errors in all pop-up forms, clearer login messages.

## 18. Notifications
In-app: new registration/demo (admins incl. superusers), leave/claim submitted (owner), decision (employee), payslip released (employee), approval (owner). Email: OTP, approval, demo alert, password reset, payslip with PDF attachment. SMS: demo alert. All fail gracefully and are logged.

## 19. Test results
`check` clean · `makemigrations --check` no changes · `migrate` OK · `test payroll_app`: **64/64 pass** on Django 5.2.15 and 6.1.1.

## 20. Deployment verification (production mode, RENDER=true)
Refuses to start without `DJANGO_SECRET_KEY`; `check --deploy` → only the intentional HSTS-preload notice; `collectstatic` OK; Gunicorn serves `/login/` 200 over https, http → 301 https, Secure cookies, HSTS, custom 404.

## 21. Remaining issues (why it is not yet "production ready")
1. Real SMTP credentials must be set on Render and a test email confirmed (OTP, reset, demo alert depend on it).
2. SMS needs `SMS_PROVIDER` + key (Fast2SMS needs a DLT-approved sender for transactional SMS in India).
3. Uploaded receipts need a Render persistent disk (`MEDIA_ROOT`) — the default container disk is wiped on deploy.
4. PF/ESI/TDS rates are the ones already in the code (old-regime-style slab, no PT, no standard deduction, no 80C/regime choice). Have your accountant confirm before real payroll.
5. Run `migrate` once against the live Aiven database (build.sh does this) and spot-check row counts.
6. No login rate-limiting yet (recommend `django-axes`).

## 22. Recommended improvements
New-regime TDS with declarations; professional tax by state; employer PF/ESI cost report; bank-specific transfer file formats (HDFC/ICICI bulk upload); login throttling; scheduled Postgres backups on Aiven.
