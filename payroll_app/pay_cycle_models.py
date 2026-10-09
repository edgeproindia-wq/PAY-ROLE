"""Working Calendar, holidays and Pay Cycles.

Flow:  Working Calendar -> Attendance -> Leave -> Pay Cycle -> Salary calculation -> Payroll run -> Payslip
A Pay Cycle is linked to ONE payroll run (PayrollRun.pay_cycle). Payroll runs that have no pay cycle are
calculated exactly as before."""
import datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from .models import Company, Employee, PayrollRun


class WorkingCalendar(models.Model):
    """Which days are working days for a company (or for some employees)."""
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='working_calendars')
    name = models.CharField(max_length=80, default='Standard calendar')
    weekly_off_days = models.CharField(
        max_length=20, default='6',
        help_text='Weekly offs as weekday numbers, comma separated: 0=Monday ... 5=Saturday, 6=Sunday')
    is_default = models.BooleanField(default=False, help_text="The company's default calendar")

    class Meta:
        app_label = 'payroll_app'
        ordering = ['-is_default', 'name']
        constraints = [
            models.UniqueConstraint(fields=['company', 'name'], name='unique_calendar_name_per_company'),
            models.UniqueConstraint(fields=['company'], condition=Q(is_default=True), name='one_default_calendar_per_company'),
        ]

    def __str__(self):
        return self.name

    def weekly_offs(self):
        out = set()
        for part in (self.weekly_off_days or '').split(','):
            part = part.strip()
            if part.isdigit() and 0 <= int(part) <= 6:
                out.add(int(part))
        return out


class CalendarDay(models.Model):
    """A date that differs from the normal weekly pattern."""
    PUBLIC_HOLIDAY, COMPANY_HOLIDAY, OPTIONAL_HOLIDAY = 'PUBLIC_HOLIDAY', 'COMPANY_HOLIDAY', 'OPTIONAL_HOLIDAY'
    SPECIAL_WORKING, HALF_DAY = 'SPECIAL_WORKING', 'HALF_DAY'
    TYPE_CHOICES = [
        (PUBLIC_HOLIDAY, 'Public holiday'), (COMPANY_HOLIDAY, 'Company holiday'),
        (OPTIONAL_HOLIDAY, 'Optional holiday (working day)'),
        (SPECIAL_WORKING, 'Special working day'), (HALF_DAY, 'Half working day (counts as a working day)'),
    ]
    calendar = models.ForeignKey(WorkingCalendar, on_delete=models.CASCADE, related_name='days')
    date = models.DateField()
    day_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default=PUBLIC_HOLIDAY)
    name = models.CharField(max_length=80, blank=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['date']
        constraints = [models.UniqueConstraint(fields=['calendar', 'date'], name='one_entry_per_calendar_date')]

    def __str__(self):
        return f'{self.date:%d %b %Y} {self.get_day_type_display()}'


class EmployeeCalendar(models.Model):
    """Optional: give one employee a different calendar from the company default."""
    employee = models.OneToOneField(Employee, on_delete=models.CASCADE, related_name='calendar_assignment')
    calendar = models.ForeignKey(WorkingCalendar, on_delete=models.PROTECT, related_name='employee_assignments')

    class Meta:
        app_label = 'payroll_app'


class PayCycle(models.Model):
    MONTHLY, WEEKLY, BIWEEKLY, CUSTOM = 'MONTHLY', 'WEEKLY', 'BIWEEKLY', 'CUSTOM'
    CYCLE_CHOICES = [(MONTHLY, 'Monthly'), (WEEKLY, 'Weekly'), (BIWEEKLY, 'Bi-weekly'), (CUSTOM, 'Custom')]
    DRAFT, PROCESSING, PROCESSED, LOCKED = 'DRAFT', 'PROCESSING', 'PROCESSED', 'LOCKED'
    STATUS_CHOICES = [(DRAFT, 'Draft'), (PROCESSING, 'Processing'), (PROCESSED, 'Processed'), (LOCKED, 'Locked')]
    CALENDAR_DAYS, WORKING_DAYS = 'CALENDAR_DAYS', 'WORKING_DAYS'
    POLICY_CHOICES = [
        (WORKING_DAYS, 'Working days from the Working Calendar'),
        (CALENDAR_DAYS, 'Calendar days of the period (the original method)'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='pay_cycles')
    name = models.CharField(max_length=100)
    cycle_type = models.CharField(max_length=10, choices=CYCLE_CHOICES, default=MONTHLY)
    payroll_month = models.CharField(max_length=20, help_text='e.g. October 2026')
    start_date = models.DateField()
    end_date = models.DateField()
    processing_date = models.DateField(null=True, blank=True)
    pay_date = models.DateField(null=True, blank=True, help_text='Salary pay date')
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=DRAFT)
    salary_policy = models.CharField(max_length=14, choices=POLICY_CHOICES, default=WORKING_DAYS)
    unmarked_as_lop = models.BooleanField(
        default=False, help_text='Count working days with no attendance record (and no approved leave) as loss of pay')
    calendar = models.ForeignKey(WorkingCalendar, on_delete=models.SET_NULL, null=True, blank=True, related_name='pay_cycles',
                                 help_text='Leave empty to use each employee\'s calendar or the company default')
    payroll_run = models.OneToOneField(PayrollRun, on_delete=models.SET_NULL, null=True, blank=True, related_name='pay_cycle')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='pay_cycles_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['-start_date']
        constraints = [
            models.UniqueConstraint(fields=['company', 'start_date', 'end_date'], name='unique_pay_period_per_company'),
            models.UniqueConstraint(fields=['company', 'payroll_month'], condition=Q(cycle_type='MONTHLY'),
                                    name='one_monthly_cycle_per_month'),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        errors = {}
        if self.start_date and self.end_date and self.end_date < self.start_date:
            errors['end_date'] = 'The end date cannot be before the start date.'
        if self.cycle_type == self.MONTHLY:
            from .payroll_engine import parse_month
            parsed = parse_month(self.payroll_month)
            if not parsed:
                errors['payroll_month'] = 'Enter the month like "October 2026".'
            else:
                self.payroll_month = parsed[0].strftime('%B %Y')          # one spelling everywhere: "October 2026"
            if parsed and self.start_date and self.end_date and (self.end_date - self.start_date).days > 35:
                errors['end_date'] = 'A monthly pay cycle cannot be longer than about one month.'
        if self.processing_date and self.start_date and self.processing_date < self.start_date:
            errors['processing_date'] = 'The processing date cannot be before the start date.'
        if self.pay_date and self.processing_date and self.pay_date < self.processing_date:
            errors['pay_date'] = 'The salary pay date cannot be before the processing date.'
        if self.company_id and self.start_date and self.end_date and not errors.get('end_date'):
            clash = (PayCycle.objects.filter(company_id=self.company_id, start_date__lte=self.end_date,
                                             end_date__gte=self.start_date).exclude(pk=self.pk).first())
            if clash:
                errors['start_date'] = (f'This period overlaps the pay cycle "{clash.name}" '
                                        f'({clash.start_date:%d-%b-%Y} to {clash.end_date:%d-%b-%Y}).')
        if (self.company_id and self.cycle_type == self.MONTHLY and self.payroll_month and not errors.get('payroll_month')
                and PayCycle.objects.filter(company_id=self.company_id, cycle_type=self.MONTHLY,
                                            payroll_month__iexact=self.payroll_month.strip()).exclude(pk=self.pk).exists()):
            errors['payroll_month'] = f'A monthly pay cycle for {self.payroll_month} already exists.'
        if errors:
            raise ValidationError(errors)

    def derived_status(self):
        run = self.payroll_run
        if run is None:
            return self.DRAFT
        if run.is_locked:
            return self.LOCKED
        if run.status in ('APPROVED', 'RELEASED'):
            return self.PROCESSED
        return self.PROCESSING

    def sync_status(self):
        new = self.derived_status()
        if new != self.status:
            self.status = new
            self.save(update_fields=['status', 'updated_at'])
        return new
