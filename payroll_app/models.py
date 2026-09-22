from django.db import models
from django.contrib.auth.models import AbstractUser
from django.conf import settings
from django.core.exceptions import ValidationError

ALLOWED_PROOF_EXTENSIONS = ('.pdf', '.jpg', '.jpeg', '.png')
MAX_PROOF_UPLOAD_MB = 5


def validate_proof_document(f):
    ext = ('.' + f.name.rsplit('.', 1)[-1].lower()) if '.' in f.name else ''
    if ext not in ALLOWED_PROOF_EXTENSIONS:
        raise ValidationError(f'Unsupported file type "{ext}". Allowed: {", ".join(ALLOWED_PROOF_EXTENSIONS)}.')
    if f.size > MAX_PROOF_UPLOAD_MB * 1024 * 1024:
        raise ValidationError(f'File too large ({f.size / (1024*1024):.1f} MB). Max {MAX_PROOF_UPLOAD_MB} MB.')


# ---------------------------------------------------------------------------
# Multi-tenant / RBAC foundation
# ---------------------------------------------------------------------------

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

    class Meta:
        pass

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


class Employee(models.Model):
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
    bank_account_no = models.CharField(max_length=30, blank=True)
    ifsc_code = models.CharField(max_length=11, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['employee_code']
        constraints = [
            models.UniqueConstraint(fields=['company', 'employee_code'], name='unique_employee_code_per_company'),
        ]

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
    remarks = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-date']
        unique_together = ('employee', 'date')

    def __str__(self):
        return f"{self.employee} - {self.date} - {self.status}"


class LeaveRequest(models.Model):
    LEAVE_TYPE_CHOICES = [
        ('CASUAL', 'Casual Leave'),
        ('SICK', 'Sick Leave'),
        ('EARNED', 'Earned Leave'),
    ]
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    ]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='leave_requests')
    leave_type = models.CharField(max_length=10, choices=LEAVE_TYPE_CHOICES)
    from_date = models.DateField()
    to_date = models.DateField()
    reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')

    class Meta:
        ordering = ['-from_date']

    def __str__(self):
        return f"{self.employee} - {self.leave_type} - {self.status}"


class Reimbursement(models.Model):
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
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return f"{self.employee} - {self.category} - {self.amount}"


class PayrollRun(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('VALIDATED', 'Validated'),
        ('APPROVED', 'Approved'),
        ('RELEASED', 'Released'),
    ]

    month = models.CharField(max_length=20, help_text='e.g. August 2026')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='DRAFT')
    is_locked = models.BooleanField(default=False)
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

    payroll_run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name='lines')
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE)
    basic = models.DecimalField(max_digits=10, decimal_places=2)
    gross_salary = models.DecimalField(max_digits=10, decimal_places=2)
    net_pay = models.DecimalField(max_digits=10, decimal_places=2)
    payment_status = models.CharField(max_length=10, choices=PAYMENT_STATUS_CHOICES, default='PENDING')
    payment_reference = models.CharField(max_length=50, blank=True)
    payment_attempted_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.payroll_run} - {self.employee}"


class CompanySettings(models.Model):
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
        upload_to='investment_proofs/%Y/%m/', blank=True, null=True, validators=[validate_proof_document],
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
        ('PENDING', 'Pending'),
        ('CONTACTED', 'Contacted'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    ]

    full_name = models.CharField(max_length=150)
    company_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=15, blank=True)
    team_size = models.CharField(max_length=50, blank=True)
    message = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')
    admin_notes = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

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
