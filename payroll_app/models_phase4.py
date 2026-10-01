"""Phase-4 models: shifts / working hours and company-configurable pay components.
Both are opt-in: nothing changes in payroll until HR adds them."""
from django.db import models


class Shift(models.Model):
    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='shifts')
    name = models.CharField(max_length=60)
    start_time = models.TimeField()
    end_time = models.TimeField(help_text='If earlier than the start time, the shift ends the next day')
    break_minutes = models.PositiveSmallIntegerField(default=60)
    grace_minutes = models.PositiveSmallIntegerField(default=10, help_text='Check-in later than start + grace is "late"')
    active = models.BooleanField(default=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['start_time']
        constraints = [models.UniqueConstraint(fields=['company', 'name'], name='unique_shift_name_per_company')]

    def __str__(self):
        return f'{self.name} ({self.start_time:%H:%M}-{self.end_time:%H:%M})'

    @property
    def scheduled_minutes(self):
        start = self.start_time.hour * 60 + self.start_time.minute
        end = self.end_time.hour * 60 + self.end_time.minute
        if end <= start:
            end += 24 * 60                       # overnight shift
        return max(end - start - self.break_minutes, 0)


class ShiftAssignment(models.Model):
    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='shift_assignments')
    employee = models.ForeignKey('payroll_app.Employee', on_delete=models.CASCADE, related_name='shift_assignments')
    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name='assignments')
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['-effective_from']


class PayComponent(models.Model):
    """A recurring monthly earning or deduction for one employee, on top of the
    salary structure. ASSUMPTIONS (confirm with HR/CA): earnings are pro-rated by
    days paid (like basic/HRA); deductions are fixed; 'taxable' earnings are added
    to the TDS base."""
    KIND_CHOICES = [('EARNING', 'Earning / allowance'), ('DEDUCTION', 'Deduction')]

    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='pay_components')
    employee = models.ForeignKey('payroll_app.Employee', on_delete=models.CASCADE, related_name='pay_components')
    name = models.CharField(max_length=60, help_text='e.g. Shift allowance, Canteen deduction')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    CALC_CHOICES = [('FIXED', 'Fixed amount'), ('PERCENT_BASIC', '% of basic')]
    calc_type = models.CharField(max_length=14, choices=CALC_CHOICES, default='FIXED')
    percent = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text='Used when calculated as % of basic')
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    taxable = models.BooleanField(default=True, help_text='Earnings only: include in the TDS calculation')
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['employee', 'kind', 'name']