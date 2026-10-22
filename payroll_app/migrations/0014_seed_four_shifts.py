"""Adds the company's four shifts (First, Second, General, Third) to the one company named below, when they are missing.
It only ADDS: a shift that already exists is never changed, no other company is touched, and going backwards removes nothing."""
import datetime

from django.db import migrations

COMPANY_NAME = 'EPRO'
FOUR = [('First Shift', (6, 30), (14, 30)),
        ('Second Shift', (14, 30), (0, 30)),         # ends 12:30 AM the next day
        ('General Shift', (10, 0), (20, 0)),
        ('Third Shift', (0, 30), (6, 0))]


def add_shifts(apps, schema_editor):
    Company = apps.get_model('payroll_app', 'Company')
    Shift = apps.get_model('payroll_app', 'Shift')
    companies = list(Company.objects.filter(name__iexact=COMPANY_NAME.strip()))
    if not companies:
        print(f'[four shifts] no company named "{COMPANY_NAME}" was found, so nothing was added.')
    for company in companies:
        have = {n.lower() for n in Shift.objects.filter(company=company).values_list('name', flat=True)}
        added = []
        for name, (sh, sm), (eh, em) in FOUR:
            if name.lower() in have:
                continue
            Shift.objects.create(company=company, name=name, start_time=datetime.time(sh, sm), end_time=datetime.time(eh, em),
                                 break_minutes=0, grace_minutes=10, active=True)
            added.append(name)
        print(f'[four shifts] {company.name}: added {added if added else "nothing (all four already exist)"}')


class Migration(migrations.Migration):

    dependencies = [('payroll_app', '0013_salary_history')]

    operations = [migrations.RunPython(add_shifts, migrations.RunPython.noop)]
