"""Create sample clients and users for testing many people at the same time.

    python manage.py seed_users
    python manage.py seed_users --clients 3 --employees 4 --password "Sample-pass-123"
    python manage.py seed_users --link-existing SHANKAR

Safe to run again: anything that already exists is kept (passwords are never changed).
Refuses to run when DEBUG is off (production) unless you add --force.
"""
import secrets
import string
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from payroll_app.models import Company, Employee, SalaryStructure, User


def random_password(length=12):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


class Command(BaseCommand):
    help = 'Creates sample client companies with two owner logins and employee logins, and checks your existing login.'

    def add_arguments(self, parser):
        parser.add_argument('--clients', type=int, default=2, help='How many sample clients to create (default 2).')
        parser.add_argument('--employees', type=int, default=3, help='Employees per client (default 3).')
        parser.add_argument('--password', default=None,
                            help='Use this password for every NEW sample user. Default: a random one per user, shown once.')
        parser.add_argument('--link-existing', metavar='USERNAME', default=None,
                            help='Your existing login. A platform ADMIN / superuser is kept exactly as it is. '
                                 'Any other existing login is attached to the first sample client.')
        parser.add_argument('--force', action='store_true', help='Allow running when DEBUG is off.')

    @transaction.atomic
    def handle(self, *args, **opts):
        if not settings.DEBUG and not opts['force']:
            raise CommandError('DEBUG is off, so this looks like production. Sample users are for testing only. '
                               'Add --force if you really want them here.')
        fixed = opts['password']
        credentials = []          # (username, role, client, password or None if it already existed)

        def make_user(username, email, role, company):
            user = User.objects.filter(username=username).first()
            if user:
                return user, None
            password = fixed or random_password()
            user = User.objects.create_user(username=username, email=email, password=password, role=role,
                                            company=company, is_active=True)
            user.email_verified = True
            user.save()
            return user, password

        companies = []
        for i in range(1, opts['clients'] + 1):
            company, _ = Company.objects.get_or_create(
                name=f'Sample Client {i}',
                defaults={'contact_email': f'owner{i}@sample-client{i}.example', 'status': 'APPROVED'},
            )
            if company.status != 'APPROVED':
                company.status = 'APPROVED'
                company.save()
            companies.append(company)

            # two owners per client, so you can test two people of the same client working together
            for suffix in ('', 'b'):
                owner, pw = make_user(f'owner{i}{suffix}', f'owner{i}{suffix}@sample-client{i}.example',
                                      'COMPANY_OWNER', company)
                credentials.append((owner.username, 'COMPANY_OWNER', company.name, pw))

            for j in range(1, opts['employees'] + 1):
                code = f'EMP{j:04d}'
                username = f'c{i}_emp{j}'
                email = f'{username}@sample-client{i}.example'
                emp = Employee.objects.filter(company=company, employee_code=code).first()
                if emp is None:
                    emp = Employee.objects.create(
                        company=company, employee_code=code, first_name=f'Sample{j}', last_name=f'Client{i}',
                        email=email, date_of_joining=date(2024, 4, 1), department='Operations', designation='Executive',
                    )
                    SalaryStructure.objects.create(employee=emp, basic=Decimal('25000') + j * 1000,
                                                   hra=Decimal('10000'), conveyance=Decimal('1600'),
                                                   special_allowance=Decimal('2000'))
                user, pw = make_user(username, email, 'EMPLOYEE', company)
                if emp.user_id is None:
                    emp.user = user
                    emp.save()
                credentials.append((username, 'EMPLOYEE', company.name, pw))

        # ---- your existing login ----
        link = opts['link_existing']
        if link:
            existing = User.objects.filter(username=link).first()
            if existing is None:
                raise CommandError(f'No existing login called "{link}".')
            if existing.is_superuser or existing.role == 'ADMIN':
                self.stdout.write(self.style.WARNING(
                    f'Existing login {link}: platform ADMIN. It is kept exactly as it is (same password). '
                    'By design a platform ADMIN manages clients but never opens a client\'s payroll data, '
                    'so use the owner logins below to work inside a client.'))
            elif companies:
                existing.company = companies[0]
                if existing.role == 'EMPLOYEE':
                    existing.role = 'COMPANY_OWNER'
                existing.save()
                self.stdout.write(self.style.SUCCESS(f'Existing login {link} now belongs to {companies[0].name} as {existing.role}.'))

        self.stdout.write('')
        self.stdout.write(f'{"username":<14}{"role":<15}{"client":<18}password')
        for username, role, client, pw in credentials:
            self.stdout.write(f'{username:<14}{role:<15}{client:<18}{pw if pw else "(already existed - unchanged)"}')
        if any(pw for *_, pw in credentials):
            self.stdout.write(self.style.WARNING('Passwords are shown only now. Copy them if you need them.'))