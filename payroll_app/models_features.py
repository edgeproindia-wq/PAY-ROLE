"""Phase-3 models: grievances, announcements, demo-request activity, loans,
insurance and professional-tax slabs. All company-scoped; files use the
default (private) storage configured in settings."""
import os

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

ALLOWED_ATTACHMENT_EXT = {'.pdf', '.jpg', '.jpeg', '.png'}
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


def validate_attachment(f):
    if os.path.splitext(f.name)[1].lower() not in ALLOWED_ATTACHMENT_EXT:
        raise ValidationError('Only PDF, JPG or PNG files are allowed.')
    if f.size > MAX_ATTACHMENT_BYTES:
        raise ValidationError('File must be 5 MB or smaller.')


# ------------------------------------------------------------------ grievances
class Grievance(models.Model):
    CATEGORY_CHOICES = [('PAYROLL', 'Salary / Payroll'), ('ATTENDANCE', 'Attendance / Leave'),
                        ('WORKPLACE', 'Workplace'), ('HARASSMENT', 'Harassment'), ('IT', 'IT / Systems'),
                        ('OTHER', 'Other')]
    STATUS_CHOICES = [('OPEN', 'Open'), ('IN_PROGRESS', 'In progress'), ('RESOLVED', 'Resolved'),
                      ('CLOSED', 'Closed'), ('REJECTED', 'Rejected')]

    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='grievances')
    employee = models.ForeignKey('payroll_app.Employee', on_delete=models.CASCADE, related_name='grievances')
    tracking_id = models.CharField(max_length=20, unique=True, null=True, blank=True, editable=False)
    category = models.CharField(max_length=12, choices=CATEGORY_CHOICES)
    subject = models.CharField(max_length=150)
    description = models.TextField(max_length=3000)
    department = models.CharField(max_length=100, blank=True)
    attachment = models.FileField(upload_to='grievances/%Y/%m/', blank=True, null=True, validators=[validate_attachment])
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='OPEN')
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='assigned_grievances')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.tracking_id:
            self.tracking_id = f'GRV-{self.created_at:%Y}-{self.pk:05d}'
            type(self).objects.filter(pk=self.pk).update(tracking_id=self.tracking_id)

    def __str__(self):
        return f'{self.tracking_id} {self.subject}'


class GrievanceUpdate(models.Model):
    """Audit trail: every comment and status change on a grievance."""
    grievance = models.ForeignKey(Grievance, on_delete=models.CASCADE, related_name='updates')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    message = models.TextField(max_length=2000, blank=True)
    old_status = models.CharField(max_length=12, blank=True)
    new_status = models.CharField(max_length=12, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['created_at']


# ------------------------------------------------------------------ announcements
class Announcement(models.Model):
    AUDIENCE_CHOICES = [('ALL', 'Everyone in the company'), ('EMPLOYEES', 'Employees only')]
    STATUS_CHOICES = [('PUBLISHED', 'Published'), ('ARCHIVED', 'Archived')]

    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='announcements')
    title = models.CharField(max_length=150)
    message = models.TextField(max_length=3000)
    audience = models.CharField(max_length=10, choices=AUDIENCE_CHOICES, default='ALL')
    publish_date = models.DateField(default=timezone.localdate)
    expiry_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PUBLISHED')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['-publish_date', '-created_at']

    @classmethod
    def active_for(cls, company):
        today = timezone.localdate()
        return cls.objects.filter(company=company, status='PUBLISHED', publish_date__lte=today).filter(
            models.Q(expiry_date__isnull=True) | models.Q(expiry_date__gte=today))


# ------------------------------------------------------------------ demo request history
class DemoRequestActivity(models.Model):
    demo = models.ForeignKey('payroll_app.DemoRequest', on_delete=models.CASCADE, related_name='activities')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    action = models.CharField(max_length=200)
    note = models.TextField(max_length=2000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['-created_at']


# ------------------------------------------------------------------ loans
class Loan(models.Model):
    """ASSUMPTION (confirm with HR/finance): loan_amount is the TOTAL amount to be
    recovered (principal + any interest). The fixed monthly EMI is deducted from
    net pay until it is recovered. interest_rate is informational only."""
    STATUS_CHOICES = [('ACTIVE', 'Active'), ('CLOSED', 'Closed')]

    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='loans')
    employee = models.ForeignKey('payroll_app.Employee', on_delete=models.CASCADE, related_name='loans')
    reference_no = models.CharField(max_length=40)
    loan_type = models.CharField(max_length=20, default='PERSONAL', choices=[
        ('PERSONAL', 'Personal loan'), ('SALARY_ADVANCE', 'Salary advance'), ('VEHICLE', 'Vehicle loan'),
        ('HOUSING', 'Housing loan'), ('OTHER', 'Other')])
    loan_amount = models.DecimalField(max_digits=12, decimal_places=2)
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text='% per year (information only)')
    tenure_months = models.PositiveSmallIntegerField()
    emi_amount = models.DecimalField(max_digits=10, decimal_places=2)
    start_date = models.DateField(help_text='First month the EMI is deducted')
    status = models.CharField(max_length=6, choices=STATUS_CHOICES, default='ACTIVE')
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['-start_date']
        constraints = [models.UniqueConstraint(fields=['company', 'reference_no'], name='unique_loan_ref_per_company')]

    def recovered(self, exclude_run=None):
        qs = self.repayments.all()
        if exclude_run is not None:
            qs = qs.exclude(line__payroll_run=exclude_run)
        return qs.aggregate(s=models.Sum('amount'))['s'] or 0

    @property
    def balance(self):
        return max(self.loan_amount - self.recovered(), 0)


class LoanRepayment(models.Model):
    """One EMI deducted in one payroll line (deleted with the line if a draft run is reprocessed)."""
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name='repayments')
    line = models.ForeignKey('payroll_app.PayrollRunLine', on_delete=models.CASCADE, related_name='loan_repayments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        app_label = 'payroll_app'
        constraints = [models.UniqueConstraint(fields=['loan', 'line'], name='one_repayment_per_loan_per_line')]


# ------------------------------------------------------------------ insurance
class InsurancePolicy(models.Model):
    """ASSUMPTION: employee_premium_monthly is deducted from salary every month the
    policy is effective; employer_contribution_monthly is shown for information."""
    STATUS_CHOICES = [('ACTIVE', 'Active'), ('INACTIVE', 'Inactive')]

    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='insurance_policies')
    employee = models.ForeignKey('payroll_app.Employee', on_delete=models.CASCADE, related_name='insurance_policies')
    provider = models.CharField(max_length=120)
    policy_number = models.CharField(max_length=60)
    policy_type = models.CharField(max_length=60, blank=True, help_text='e.g. Group health, Term life')
    sum_insured = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    employee_premium_monthly = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    employer_contribution_monthly = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default='ACTIVE')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['-effective_from']


# ------------------------------------------------------------------ professional tax
class ProfessionalTaxSlab(models.Model):
    """Company-configurable PT slabs on monthly gross. No slabs = no PT.
    State rules differ and change - enter them as advised by your CA."""
    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='pt_slabs')
    min_monthly_gross = models.DecimalField(max_digits=10, decimal_places=2)
    max_monthly_gross = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                            help_text='Leave blank for "and above"')
    state = models.CharField(max_length=40, blank=True, help_text='State / region these slabs apply to (for reference)')
    monthly_amount = models.DecimalField(max_digits=8, decimal_places=2)
    february_amount = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True,
                                          help_text='Optional different amount for February')

    class Meta:
        app_label = 'payroll_app'
        ordering = ['min_monthly_gross']