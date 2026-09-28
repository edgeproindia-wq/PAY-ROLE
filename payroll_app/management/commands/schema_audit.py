"""Compare the real database schema with the Django models.

    python manage.py schema_audit          # report only (safe, read-only)
    python manage.py schema_audit --fix    # add missing *nullable/defaulted* columns

This exists for databases that were created by an older copy of the project
and then had migration 0001 "faked" or edited — the classic symptom being
`no such column: payroll_app_employee.user_id`. It never drops tables,
columns or rows, and it uses Django's own schema editor (no raw SQL).
"""
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import NOT_PROVIDED


class Command(BaseCommand):
    help = 'Report (and optionally repair) columns missing from the database compared with the models.'

    def add_arguments(self, parser):
        parser.add_argument('--fix', action='store_true', help='Add missing columns that are nullable or have a default.')

    def handle(self, *args, **options):
        fix = options['fix']
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if pending:
            self.stdout.write(self.style.WARNING(
                f'{len(pending)} migration(s) not applied yet — this is normal after an update. '
                'Run `python manage.py migrate` and then run schema_audit again:'))
            for migration, _ in pending:
                self.stdout.write(f'   - {migration.app_label}.{migration.name}')
            if fix:
                self.stdout.write(self.style.ERROR(
                    '--fix refused: apply pending migrations first (otherwise migrate would try to add the same columns twice).'))
                return
            return

        existing_tables = set(connection.introspection.table_names())
        problems, fixed, manual = 0, 0, 0
        for model in apps.get_app_config('payroll_app').get_models():
            table = model._meta.db_table
            if table not in existing_tables:
                problems += 1
                self.stdout.write(self.style.ERROR(f'MISSING TABLE  {table}  (run migrate)'))
                continue
            with connection.cursor() as cursor:
                cols = {c.name for c in connection.introspection.get_table_description(cursor, table)}
            for field in model._meta.local_concrete_fields:
                if field.column in cols:
                    continue
                problems += 1
                safe = field.null or field.default is not NOT_PROVIDED
                self.stdout.write(self.style.ERROR(f'MISSING COLUMN {table}.{field.column}'))
                if fix and safe:
                    with connection.schema_editor() as editor:
                        editor.add_field(model, field)
                    fixed += 1
                    self.stdout.write(self.style.SUCCESS(f'   added {table}.{field.column}'))
                elif fix:
                    manual += 1
                    self.stdout.write(self.style.WARNING(
                        f'   not added automatically: NOT NULL without default — needs a data decision'))

        if problems == 0:
            self.stdout.write(self.style.SUCCESS('Schema matches the models. No missing tables or columns.'))
        elif fix:
            self.stdout.write(f'Fixed {fixed} column(s); {manual} need manual attention.')
        else:
            self.stdout.write(self.style.WARNING(f'{problems} problem(s). Back up db.sqlite3, then run with --fix.'))
