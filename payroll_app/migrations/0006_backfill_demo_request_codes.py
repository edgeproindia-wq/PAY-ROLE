from django.db import migrations


def backfill(apps, schema_editor):
    """Give every existing demo request a request ID (DR-<year>-<id>)."""
    DemoRequest = apps.get_model('payroll_app', 'DemoRequest')
    for d in DemoRequest.objects.filter(request_code__isnull=True):
        d.request_code = f"DR-{d.created_at:%Y}-{d.pk:05d}"
        d.save(update_fields=['request_code'])


class Migration(migrations.Migration):
    dependencies = [('payroll_app', '0005_phase3_features')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
