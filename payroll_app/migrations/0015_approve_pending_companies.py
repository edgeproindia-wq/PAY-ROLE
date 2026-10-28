"""Removes the admin-approval wait for companies that registered before approval was switched off.

Every company still waiting (PENDING_APPROVAL) becomes APPROVED and its owner account(s) become active, so nobody
sees "Your company registration is pending admin approval" any more. Rejected and suspended companies are not touched.
An owner whose email was never verified stays unverified (they still have to confirm the code before signing in).
Going backwards changes nothing."""
from django.db import migrations
from django.utils import timezone


def approve_waiting(apps, schema_editor):
    Company = apps.get_model('payroll_app', 'Company')
    User = apps.get_model('payroll_app', 'User')
    waiting = list(Company.objects.filter(status='PENDING_APPROVAL'))
    for company in waiting:
        Company.objects.filter(pk=company.pk).update(status='APPROVED', approved_at=timezone.now(), rejection_reason='')
        User.objects.filter(company_id=company.pk, role='COMPANY_OWNER', is_active=False).update(is_active=True)
    if waiting:
        print(f'[approval removed] {len(waiting)} company registration(s) that were waiting are now approved.')


class Migration(migrations.Migration):

    dependencies = [
        ('payroll_app', '0014_seed_four_shifts'),
    ]

    operations = [
        migrations.RunPython(approve_waiting, migrations.RunPython.noop),
    ]
