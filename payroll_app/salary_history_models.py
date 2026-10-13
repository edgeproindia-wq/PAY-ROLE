"""Salary history: every salary change is kept with the date it starts. Payroll uses the salary in force for the payroll month."""
from django.conf import settings
from django.db import models


class SalaryRevision(models.Model):
    employee = models.ForeignKey('payroll_app.Employee', on_delete=models.CASCADE, related_name='salary_revisions')
    effective_from = models.DateField(help_text='First day of the month this salary starts')
    basic = models.DecimalField(max_digits=10, decimal_places=2)
    hra = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    conveyance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    special_allowance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['-effective_from']
        constraints = [models.UniqueConstraint(fields=['employee', 'effective_from'], name='one_salary_revision_per_employee_per_date')]

    @property
    def gross_salary(self):
        return self.basic + self.hra + self.conveyance + self.special_allowance

    def __str__(self):
        return f'{self.employee_id} from {self.effective_from}'
