from django.db import models
from django.contrib.auth.models import AbstractUser
from django.conf import settings
from .concurrency import VersionedModel


# ---------------------------------------------------------------------------
# Multi-tenant / RBAC foundation
# ---------------------------------------------------------------------------

import os as _os
from django.core.exceptions import ValidationError as _ValidationError


def validate_proof_document(file):
    """Validator for investment proof uploads (used by migration 0002)."""
    allowed = {'.pdf', '.jpg', '.jpeg', '.png'}
    ext = _os.path.splitext(file.name)[1].lower()
    if ext not in allowed:
        raise _ValidationError('Only PDF, JPG, JPEG or PNG files are allowed.')
    if file.size > 5 * 1024 * 1024:
        raise _ValidationError('File size must be 5 MB or less.')


class Company(models.Model):
    """A client/tenant company using the payroll SaaS. All company-scoped
    data (employees, payroll, leave, etc.) is isolated by this FK."""

    STATUS_CHOICES = [
        ('PENDING_APPROVAL', 'Pending Approval'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
        ('SUSPENDED', 'Suspended'),
    ]

    name = models.CharField(max_length=200, unique=True)
    contact_email = models.EmailField()
    contact_phone = models.CharField(max_length=15, blank=True)
    address = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING_APPROVAL')
    rejection_reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'Companies'

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"

    @property
    def is_approved(self):
        return self.status == 'APPROVED'


class User(AbstractUser):
    """Custom user carrying the RBAC role and tenant (company) link.

    ADMIN        - platform/staff administrator (EdgePro side), sees everything.
    COMPANY_OWNER- the client's account owner, scoped to their own Company.
    EMPLOYEE     - an individual employee, scoped to their own Employee record.
    """

    ROLE_CHOICES = [
        ('ADMIN', 'Admin'),
        ('COMPANY_OWNER', 'Company Owner'),
        ('EMPLOYEE', 'Employee'),
    ]

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='EMPLOYEE')
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, null=True, blank=True, related_name='users'
    )
    # Existing accounts default to verified so nobody is locked out by this
    # migration; self-registration explicitly sets it to False until the OTP
    # is confirmed.
    email_verified = models.BooleanField(default=True)

    class Meta:
        pass

    def save(self, *args, **kwargs):
        # `createsuperuser` never sets `role`, which previously left every
        # superuser with role=EMPLOYEE (no admin notifications, blocked from
        # creating employees). A superuser is always a platform ADMIN.
        if self.is_superuser and self.role != 'ADMIN':
            self.role = 'ADMIN'
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.username} ({self.role})"

    @property
    def is_admin_role(self):
        return self.role == 'ADMIN' or self.is_superuser

    @property
    def is_company_owner(self):
        return self.role == 'COMPANY_OWNER'

    @property
    def is_employee_role(self):
        return self.role == 'EMPLOYEE'


class Employee(VersionedModel):
    EMPLOYMENT_STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('ON_LEAVE', 'On Leave'),
        ('RESIGNED', 'Resigned'),
        ('TERMINATED', 'Terminated'),
    ]
    GENDER_CHOICES = [
        ('M', 'Male'),
        ('F', 'Female'),
        ('O', 'Other'),
    ]

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name='employees', null=True, blank=True,
        help_text='The tenant company this employee belongs to.'
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='employee_profile', help_text='Login account for this employee (ESS access).'
    )
    employee_code = models.CharField(max_length=20, help_text='e.g. EMP0001')
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=15, blank=True)
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    date_of_joining = models.DateField()
    department = models.CharField(max_length=200)
    designation = models.CharField(max_length=200)
    employment_status = models.CharField(max_length=20, choices=EMPLOYMENT_STATUS_CHOICES, default='ACTIVE')
    pan_number = models.CharField(max_length=10, blank=True)
    aadhar_number = models.CharField(max_length=12, blank=True)
    ACCOUNT_TYPE_CHOICES = [
        ('SAVINGS', 'Savings'),
        ('CURRENT', 'Current'),
        ('SALARY', 'Salary'),
    ]
    BANK_STATUS_CHOICES = [
        ('UNVERIFIED', 'Unverified'),
        ('VERIFIED', 'Verified'),
        ('INACTIVE', 'Inactive'),
    ]

    bank_name = models.CharField(max_length=120, blank=True)
    account_holder_name = models.CharField(max_length=150, blank=True)
    bank_account_no = models.CharField(max_length=30, blank=True)
    ifsc_code = models.CharField(max_length=11, blank=True)
    bank_branch = models.CharField(max_length=150, blank=True)
    account_type = models.CharField(max_length=10, choices=ACCOUNT_TYPE_CHOICES, blank=True)
    bank_status = models.CharField(max_length=10, choices=BANK_STATUS_CHOICES, default='UNVERIFIED')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['company', 'employee_code'], name='unique_employee_code_per_company'),
        ]
        ordering = ['employee_code']

    @property
    def has_bank_details(self):
        """Minimum data needed to pay someone: account number + IFSC, and
        the account not marked inactive."""
        return bool(self.bank_account_no and self.ifsc_code and self.bank_status != 'INACTIVE')

    @property
    def masked_account_no(self):
        acc = self.bank_account_no or ''
        return ('X' * max(len(acc) - 4, 0)) + acc[-4:] if acc else ''

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return f"{self.employee_code} - {self.full_name}"


class SalaryStructure(models.Model):
    employee = models.OneToOneField(Employee, on_delete=models.CASCADE, related_name='salary_structure')
    basic = models.DecimalField(max_digits=10, decimal_places=2)
    hra = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    conveyance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    special_allowance = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    @property
    def gross_salary(self):
        return self.basic + self.hra + self.conveyance + self.special_allowance

    def __str__(self):
        return f"Salary structure for {self.employee}"


class Attendance(models.Model):
    STATUS_CHOICES = [
        ('PRESENT', 'Present'),
        ('ABSENT', 'Absent'),
        ('HALF_DAY', 'Half Day'),
        ('LEAVE', 'On Leave'),
    ]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='attendance_records')
    date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PRESENT')
    check_in = models.TimeField(null=True, blank=True)
    check_out = models.TimeField(null=True, blank=True)
    remarks = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-date']
        unique_together = ('employee', 'date')

    @property
    def hours_worked(self):
        if self.check_in and self.check_out:
            import datetime as _dt
            delta = _dt.datetime.combine(self.date, self.check_out) - _dt.datetime.combine(self.date, self.check_in)
            return round(delta.total_seconds() / 3600, 2) if delta.total_seconds() > 0 else 0
        return None

    def __str__(self):
        return f"{self.employee} - {self.date} - {self.status}"


class LeaveRequest(VersionedModel):
    LEAVE_TYPE_CHOICES = [
        ('CASUAL', 'Casual Leave'),
        ('SICK', 'Sick Leave'),
        ('EARNED', 'Earned Leave'),
        ('UNPAID', 'Unpaid Leave (loss of pay)'),
        ('MATERNITY', 'Maternity Leave'),
        ('PATERNITY', 'Paternity Leave'),
        ('COMP_OFF', 'Compensatory Off'),
    ]
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
        ('CANCELLED', 'Cancelled'),
    ]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='leave_requests')
    leave_type = models.CharField(max_length=10, choices=LEAVE_TYPE_CHOICES)
    from_date = models.DateField()
    to_date = models.DateField()
    reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='leave_decisions'
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    approver_comment = models.CharField(max_length=255, blank=True)      # reason given when approving/rejecting
    cancellation_reason = models.CharField(max_length=255, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-from_date']

    @property
    def days(self):
        return (self.to_date - self.from_date).days + 1

    def __str__(self):
        return f"{self.employee} - {self.leave_type} - {self.status}"


class Reimbursement(VersionedModel):
    CATEGORY_CHOICES = [
        ('TRAVEL', 'Travel'),
        ('MEDICAL', 'Medical'),
        ('FOOD', 'Food'),
        ('OTHER', 'Other'),
    ]
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    ]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='reimbursements')
    category = models.CharField(max_length=10, choices=CATEGORY_CHOICES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    date = models.DateField()
    description = models.CharField(max_length=255, blank=True)
    receipt = models.FileField(upload_to='reimbursement_receipts/%Y/%m/', null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='reimbursement_decisions'
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    approver_comment = models.CharField(max_length=255, blank=True)
    # Set when an approved claim is included in a payroll run, so the same
    # claim can never be paid twice.
    paid_in_run = models.ForeignKey(
        'PayrollRun', on_delete=models.SET_NULL, null=True, blank=True, related_name='reimbursements_paid'
    )

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return f"{self.employee} - {self.category} - {self.amount}"


class PayrollRun(VersionedModel):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('VALIDATED', 'Validated'),
        ('APPROVED', 'Approved'),
        ('RELEASED', 'Released'),
    ]

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, null=True, blank=True, related_name='payroll_runs',
        help_text='Tenant that owns this run. Legacy runs are back-filled from their lines.'
    )
    month = models.CharField(max_length=20, help_text='e.g. August 2026')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='DRAFT')
    is_locked = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='payroll_runs_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.month} ({self.status})"


class PayrollRunLine(models.Model):
    PAYMENT_STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('SUCCESS', 'Success'),
        ('FAILED', 'Failed'),
    ]
    payment_status = models.CharField(max_length=10, choices=PAYMENT_STATUS_CHOICES, default='PENDING')
    payment_reference = models.CharField(max_length=50, blank=True)
    payment_attempted_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    payroll_run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name='lines')
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE)
    payslip_number = models.CharField(max_length=40, blank=True, db_index=True)
    basic = models.DecimalField(max_digits=10, decimal_places=2)
    hra = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    conveyance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    special_allowance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gross_salary = models.DecimalField(max_digits=10, decimal_places=2)
    days_in_month = models.PositiveSmallIntegerField(default=0)
    lop_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    lop_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    arrears = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    reimbursements = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_earnings = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    pf = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    esi = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tds = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    professional_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    loan_deduction = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    insurance_deduction = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    other_earnings = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    other_deductions = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_deductions = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    net_pay = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.payroll_run} - {self.employee}"


class CompanySettings(VersionedModel):
    company = models.OneToOneField(
        Company, on_delete=models.CASCADE, null=True, blank=True, related_name='settings'
    )
    company_name = models.CharField(max_length=200, default='My Company')
    address = models.CharField(max_length=500, blank=True)
    pan_number = models.CharField(max_length=10, blank=True)
    gst_number = models.CharField(max_length=15, blank=True)
    pf_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=12.0)
    esi_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.75)
    pf_wage_ceiling = models.DecimalField(max_digits=10, decimal_places=2, default=21000.0)
    casual_leave_days = models.IntegerField(default=12)
    sick_leave_days = models.IntegerField(default=12)
    earned_leave_days = models.IntegerField(default=15)

    def __str__(self):
        return self.company_name


class ArrearsRecord(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='arrears_records')
    effective_from = models.DateField()
    old_basic = models.DecimalField(max_digits=10, decimal_places=2)
    new_basic = models.DecimalField(max_digits=10, decimal_places=2)
    months = models.IntegerField(default=1)
    paid_in_run = models.ForeignKey(
        'PayrollRun', on_delete=models.SET_NULL, null=True, blank=True, related_name='arrears_paid'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def difference(self):
        return self.new_basic - self.old_basic

    @property
    def total_arrears(self):
        return self.difference * self.months

    def __str__(self):
        return f"Arrears - {self.employee} - {self.effective_from}"


class FullFinalSettlement(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='settlements')
    last_working_day = models.DateField()
    pending_salary = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    leave_encashment = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    deductions = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def net_settlement(self):
        return self.pending_salary + self.leave_encashment - self.deductions

    def __str__(self):
        return f"F&F - {self.employee}"


class UserRoleAssignment(models.Model):
    ROLE_CHOICES = [
        ('ADMIN', 'Admin'),
        ('HR', 'HR'),
        ('FINANCE', 'Finance'),
        ('PAYROLL_EXECUTIVE', 'Payroll Executive'),
        ('REPORTING_MANAGER', 'Reporting Manager'),
        ('EMPLOYEE', 'Employee'),
    ]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='role_assignments')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    assigned_on = models.DateField(auto_now_add=True)

    def __str__(self):
        return f"{self.employee} - {self.role}"


class InvestmentDeclaration(models.Model):
    SECTION_CHOICES = [
        ('80C', 'Section 80C'),
        ('80D', 'Section 80D'),
        ('80CCD', 'Section 80CCD (NPS)'),
        ('HRA', 'HRA Exemption'),
        ('OTHER', 'Other'),
    ]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='investment_declarations')
    financial_year = models.CharField(max_length=9, help_text='e.g. 2026-2027')
    section = models.CharField(max_length=10, choices=SECTION_CHOICES)
    investment_type = models.CharField(max_length=100)
    declared_amount = models.DecimalField(max_digits=10, decimal_places=2)
    proof_document = models.FileField(
        upload_to='investment_proofs/%Y/%m/', blank=True, null=True,
        validators=[validate_proof_document],
    )
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.employee} - {self.financial_year} - {self.section}"


# ---------------------------------------------------------------------------
# Public-facing workflows: demo requests, client complaints/requests
# ---------------------------------------------------------------------------

class DemoRequest(models.Model):
    """Public 'Request a Demo' submission — no login required to create."""

    STATUS_CHOICES = [
        ('PENDING', 'New'),
        ('CONTACTED', 'Contacted'),
        ('DEMO_SCHEDULED', 'Demo scheduled'),
        ('DEMO_COMPLETED', 'Demo completed'),
        ('FOLLOW_UP', 'Follow-up required'),
        ('CONVERTED', 'Converted'),
        ('CLOSED', 'Closed'),
        ('APPROVED', 'Approved (legacy)'),
        ('REJECTED', 'Rejected (legacy)'),
    ]

    full_name = models.CharField(max_length=150)
    company_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=15, blank=True)
    team_size = models.CharField(max_length=50, blank=True)
    preferred_datetime = models.DateTimeField(null=True, blank=True)
    message = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    admin_notes = models.CharField(max_length=500, blank=True)
    request_code = models.CharField(max_length=20, unique=True, null=True, blank=True, editable=False)
    industry = models.CharField(max_length=60, blank=True)
    modules = models.CharField(max_length=300, blank=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='assigned_demo_requests')
    follow_up_date = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)
    email_notified = models.BooleanField(default=False)
    sms_notified = models.BooleanField(default=False)
    notification_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.request_code:      # human-friendly ID, e.g. DR-2026-00042
            self.request_code = f"DR-{self.created_at:%Y}-{self.pk:05d}"
            type(self).objects.filter(pk=self.pk).update(request_code=self.request_code)

    def __str__(self):
        return f"Demo request - {self.company_name} ({self.get_status_display()})"


class ClientComplaint(models.Model):
    STATUS_CHOICES = [
        ('OPEN', 'Open'),
        ('IN_PROGRESS', 'In Progress'),
        ('RESOLVED', 'Resolved'),
        ('CLOSED', 'Closed'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='complaints')
    raised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='complaints_raised'
    )
    subject = models.CharField(max_length=200)
    description = models.TextField()
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='OPEN')
    admin_response = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Complaint - {self.company} - {self.subject}"


class ClientRequest(models.Model):
    """Generic requests a client/company owner can raise to the admin
    (e.g. feature request, extra seats, support request)."""

    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
        ('COMPLETED', 'Completed'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='requests')
    raised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='client_requests_raised'
    )
    request_type = models.CharField(max_length=100)
    description = models.TextField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')
    admin_response = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Request - {self.company} - {self.request_type}"


class Notification(models.Model):
    """In-app notification for a specific user."""

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications'
    )
    message = models.CharField(max_length=255)
    link = models.CharField(max_length=255, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"To {self.recipient}: {self.message[:40]}"


class AuditLog(models.Model):
    """Traceability for sensitive actions. Never store passwords/secrets here."""

    ACTION_CHOICES = [
        ('LOGIN', 'Login'),
        ('LOGOUT', 'Logout'),
        ('REGISTER', 'Registration'),
        ('APPROVE', 'Approve'),
        ('REJECT', 'Reject'),
        ('CREATE', 'Create'),
        ('UPDATE', 'Update'),
        ('DELETE', 'Delete'),
        ('PROCESS_PAYROLL', 'Payroll Processing'),
        ('PAYMENT_STATUS_CHANGE', 'Payment Status Change'),
        ('EXPORT', 'Export'),
        ('OTHER', 'Other'),
    ]

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs'
    )
    action = models.CharField(max_length=25, choices=ACTION_CHOICES)
    model_name = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=50, blank=True)
    company = models.ForeignKey(
        Company, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs'
    )
    details = models.CharField(max_length=500, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"[{self.timestamp:%Y-%m-%d %H:%M}] {self.actor} {self.action} {self.model_name}#{self.object_id}"


# ---------------------------------------------------------------------------
# Email verification (registration OTP)
# ---------------------------------------------------------------------------

class EmailOTP(models.Model):
    """One-time code emailed at registration. Only a hash of the code is
    stored; codes expire and are locked after too many wrong attempts."""

    PURPOSE_CHOICES = [('REGISTRATION', 'Registration')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='email_otps')
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES, default='REGISTRATION')
    code_hash = models.CharField(max_length=128)
    attempts = models.PositiveSmallIntegerField(default=0)
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"OTP for {self.user} ({'used' if self.is_used else 'active'})"


# ---------------------------------------------------------------------------
# Bank transfer / salary payment tracking
# ---------------------------------------------------------------------------

class BankPayment(VersionedModel):
    """One payment instruction per released payslip line. The OneToOne makes
    a duplicate payment for the same payslip impossible at the DB level.

    A payment is only ever marked PAID when a bank reference/UTR number is
    recorded by an authorised user — nothing is auto-marked as completed."""

    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('INITIATED', 'Initiated'),
        ('PAID', 'Paid'),
        ('FAILED', 'Failed'),
    ]
    MODE_CHOICES = [('NEFT', 'NEFT'), ('IMPS', 'IMPS'), ('RTGS', 'RTGS'), ('OTHER', 'Other')]

    payroll_line = models.OneToOneField(PayrollRunLine, on_delete=models.PROTECT, related_name='bank_payment')
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name='bank_payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    # Snapshot of bank details at the time the payment was prepared, so later
    # edits to the employee record cannot silently change where money went.
    bank_name = models.CharField(max_length=120, blank=True)
    account_holder_name = models.CharField(max_length=150, blank=True)
    account_no = models.CharField(max_length=30)
    ifsc_code = models.CharField(max_length=11)
    mode = models.CharField(max_length=5, choices=MODE_CHOICES, default='NEFT')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')
    reference_number = models.CharField(max_length=60, blank=True, help_text='Bank UTR / transaction reference')
    failure_reason = models.CharField(max_length=255, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    initiated_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments_verified'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['reference_number'], condition=~models.Q(reference_number=''),
                name='unique_bank_payment_reference',
            ),
        ]

    def __str__(self):
        return f"{self.payroll_line.employee} - {self.amount} - {self.status}"


# Phase-3 models live in their own modules; importing them here registers them with the app.
from .models_documents import EmployeeDocument  # noqa: E402,F401
from .models_features import (Announcement, DemoRequestActivity, Grievance, GrievanceUpdate,  # noqa: E402,F401
                              InsurancePolicy, Loan, LoanRepayment, ProfessionalTaxSlab)
from .models_phase4 import PayComponent, Shift, ShiftAssignment  # noqa: E402,F401
from .models_phase5 import AnnouncementRead  # noqa: E402,F401

from .pay_cycle_models import WorkingCalendar, CalendarDay, EmployeeCalendar, PayCycle  # noqa: E402,F401
from .profile_photo_models import ProfilePhoto  # noqa: E402,F401
