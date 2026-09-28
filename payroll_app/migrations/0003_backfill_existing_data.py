"""Back-fill new columns for rows that existed before 0002.

Nothing is deleted or recalculated here:
- superusers created by `createsuperuser` get role=ADMIN (they were EMPLOYEE),
- each legacy PayrollRun gets the company of its employees (only when all of
  its lines belong to one company; mixed legacy runs stay NULL = admin-only),
- legacy payslip lines get a payslip number and their earnings/deductions
  totals derived from the stored gross/net, so old released payslips keep
  showing exactly the amounts that were paid.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    User = apps.get_model('payroll_app', 'User')
    PayrollRun = apps.get_model('payroll_app', 'PayrollRun')
    PayrollRunLine = apps.get_model('payroll_app', 'PayrollRunLine')

    User.objects.filter(is_superuser=True).exclude(role='ADMIN').update(role='ADMIN')

    for run in PayrollRun.objects.filter(company__isnull=True):
        company_ids = set(
            PayrollRunLine.objects.filter(payroll_run=run)
            .values_list('employee__company_id', flat=True)
        )
        company_ids.discard(None)
        if len(company_ids) == 1:
            run.company_id = company_ids.pop()
            run.save(update_fields=['company'])

    for line in PayrollRunLine.objects.select_related('employee').filter(payslip_number=''):
        line.payslip_number = f"PS-{line.payroll_run_id:05d}-{line.employee.employee_code}"
        if not line.total_earnings:
            line.total_earnings = line.gross_salary
        if not line.total_deductions and line.gross_salary > line.net_pay:
            line.total_deductions = line.gross_salary - line.net_pay
        line.save(update_fields=['payslip_number', 'total_earnings', 'total_deductions'])


class Migration(migrations.Migration):

    dependencies = [
        ('payroll_app', '0002_audit_enhancements'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
