import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.test import Client
from django.urls import reverse
import datetime

from payroll_app.models import Company, User, Employee, SalaryStructure, PayrollRun, PayrollRunLine

# Clean slate
User.objects.all().delete()
Company.objects.all().delete()

company = Company.objects.create(name='SmokeCo', contact_email='smoke@co.com', status='APPROVED')
admin = User.objects.create_superuser('smoke_admin', 'a@a.com', 'StrongPass123')
admin.role = 'ADMIN'
admin.save()
owner = User.objects.create_user('smoke_owner', password='StrongPass123', role='COMPANY_OWNER', company=company)
emp = Employee.objects.create(
    employee_code='SMK001', first_name='Smoke', last_name='Test', email='smk@example.com',
    date_of_joining=datetime.date(2024, 1, 1), department='Ops', designation='Analyst', company=company,
)
emp_user = User.objects.create_user('smoke_emp', password='StrongPass123', role='EMPLOYEE', company=company)
emp.user = emp_user
emp.save()
SalaryStructure.objects.create(employee=emp, basic=20000, hra=5000, conveyance=1000, special_allowance=500)
run = PayrollRun.objects.create(month='Smoke Month', status='RELEASED')
PayrollRunLine.objects.create(payroll_run=run, employee=emp, basic=20000, gross_salary=26500, net_pay=25000)

PUBLIC_GET_URLS = ['landing', 'request_demo', 'company_register', 'login', 'password_reset']

ADMIN_GET_URLS = [
    'admin_dashboard', 'admin_demo_requests', 'admin_company_approvals', 'admin_company_list',
    'admin_client_complaints', 'admin_client_requests', 'admin_audit_log',
]

OWNER_EMPLOYEE_GET_URLS = [
    'dashboard', 'employee_master', 'salary_structure', 'attendance', 'leave_management',
    'reimbursement', 'payslips', 'investment_declaration', 'reports_analytics', 'ess',
    'notifications', 'payslip_history', 'email_payslip', 'generate_payslip',
]

OWNER_ONLY_GET_URLS = [
    'statutory_compliance', 'income_tax', 'compliance_reports', 'total_employees_report',
    'new_joiners_report', 'payroll_cost_report', 'pending_payroll_report', 'employees_on_leave_report',
    'payroll_combined', 'arrears', 'full_final_settlement', 'bank_transfer', 'user_roles_permissions',
    'settings', 'client_complaints', 'client_requests', 'download_pdf',
]

results = []


def hit(client, name, args=None):
    url = reverse(name, args=args or [])
    resp = client.get(url)
    status = 'OK' if resp.status_code in (200, 302) else f'FAIL({resp.status_code})'
    results.append((name, resp.status_code, status))


c = Client()
for name in PUBLIC_GET_URLS:
    hit(c, name)

c2 = Client()
c2.login(username='smoke_admin', password='StrongPass123')
for name in ADMIN_GET_URLS + OWNER_EMPLOYEE_GET_URLS + OWNER_ONLY_GET_URLS:
    hit(c2, name)
hit(c2, 'payroll_run_detail', args=[run.pk])
hit(c2, 'employee_edit', args=[emp.pk])

c3 = Client()
c3.login(username='smoke_owner', password='StrongPass123')
for name in OWNER_EMPLOYEE_GET_URLS + OWNER_ONLY_GET_URLS:
    hit(c3, name)

c4 = Client()
c4.login(username='smoke_emp', password='StrongPass123')
for name in OWNER_EMPLOYEE_GET_URLS:
    hit(c4, name)

failures = [r for r in results if r[2] != 'OK']
print(f"Total checks: {len(results)}   Failures: {len(failures)}")
for r in results:
    print(r)
if failures:
    raise SystemExit(1)
