"""SAFE smoke test runner.

The previous version of this script deleted every User and Company in the
REAL database (db.sqlite3 / DATABASE_URL) before running. It has been
replaced: the smoke checks now live in payroll_app/tests_audit.py
(SmokeAllPagesTests) and run on Django's temporary test database, which is
created and destroyed automatically. Your real data is never touched.

Usage:  python smoke_test.py
"""
import subprocess
import sys

sys.exit(subprocess.call([sys.executable, 'manage.py', 'test',
                          'payroll_app.tests_audit.SmokeAllPagesTests', '-v', '2']))
