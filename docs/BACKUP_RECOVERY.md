# EdgePro Payroll - Backup & Recovery

## What must be backed up
| Data | Where | How |
|---|---|---|
| Database (all payroll, employees, documents stored in DB) | Aiven PostgreSQL (`DATABASE_URL`) | Aiven automatic backups + weekly manual `pg_dump` |
| Uploaded files (receipts, proofs, grievance attachments) | Private bucket (`STORAGE_BUCKET`, R2 or S3) | Bucket versioning / lifecycle + periodic copy |
| Code | GitHub (private) | Git history, tags `before-*` |
| Secrets | Render Environment tab | Keep an offline copy in your password manager |

## 1. Database backups
1. Aiven console -> your PostgreSQL service -> **Backups**: confirm daily backups are enabled and note the retention.
2. Weekly manual dump from your PC (PostgreSQL client tools installed):
   ```powershell
   $env:PGURL = (Read-Host "Paste DATABASE_URL (not saved)")
   pg_dump --format=custom --no-owner --file ("payroll_" + (Get-Date -Format yyyyMMdd) + ".dump") $env:PGURL
   Remove-Item Env:PGURL
   ```
   Store the file encrypted (e.g. BitLocker drive / encrypted cloud folder). It contains personal data.

## 2. File backups
- Cloudflare R2 / AWS S3: enable **object versioning** (S3) or keep a second bucket and copy weekly
  (`rclone sync r2:<bucket> backup:<bucket>`). Keep the bucket private.

## 3. Restore test (do this before go-live and every quarter)
1. Create a **separate** empty database (a new Aiven service or local PostgreSQL). Never restore over production.
2. `pg_restore --no-owner --dbname <TEST_DATABASE_URL> payroll_YYYYMMDD.dump`
3. Point a local copy of the app at it: `$env:DATABASE_URL = "<TEST_DATABASE_URL>"`, then
   `python manage.py migrate --plan` (must show no unapplied migrations) and `python manage.py schema_audit`.
4. Log in, open Employee Master, a released payroll run and a payslip PDF. Record the date and result.

## 4. Recovery in production
1. Put the site in maintenance (Render -> suspend service) to stop new writes.
2. Restore the chosen Aiven backup to a **new** service (Aiven "Fork" / point-in-time), verify it as in step 3.
3. Update `DATABASE_URL` in Render to the restored service and resume.
4. Code rollback if needed: Render -> Events -> **Rollback**, or `git revert` + push.

## 5. Before every deploy that includes migrations
- Take a manual Aiven backup (or `pg_dump`), then deploy. Migrations in this project are additive.