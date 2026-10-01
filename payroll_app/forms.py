import os
import re

from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.db.models import Q
from django.utils import timezone

from .models import (
    Employee, SalaryStructure, Attendance, LeaveRequest, Reimbursement,
    PayrollRun, InvestmentDeclaration, ArrearsRecord, FullFinalSettlement,
    UserRoleAssignment, CompanySettings, Company, DemoRequest, User,
    ClientComplaint, ClientRequest,
)

TEXT_WIDGET_CLASS = 'form-control'


def _apply_employee_scope(form, employee_queryset, forced_employee):
    """Employees file requests only for themselves: the employee field is
    removed from the form entirely and the owner is fixed server-side."""
    if forced_employee is not None:
        form.fields.pop('employee', None)
        form.instance.employee = forced_employee
    elif employee_queryset is not None:
        form.fields['employee'].queryset = employee_queryset


def validate_indian_mobile(value, required=True):
    raw = (value or '').strip()
    if not raw:
        if required:
            raise forms.ValidationError('Mobile number is required.')
        return ''
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[2:]
    if not re.fullmatch(r'[6-9]\d{9}', digits):
        raise forms.ValidationError('Enter a valid 10-digit Indian mobile number.')
    return digits


def _style(fields):
    for f in fields.values():
        existing = f.widget.attrs.get('class', '')
        f.widget.attrs['class'] = (existing + ' ' + TEXT_WIDGET_CLASS).strip()


class EmployeeForm(forms.ModelForm):
    class Meta:
        model = Employee
        # company & user are set server-side (never trusted from client input)
        exclude = ['company', 'user', 'created_at', 'updated_at']
        widgets = {
            'date_of_birth': forms.DateInput(attrs={'type': 'date'}),
            'date_of_joining': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, company_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Platform admins must choose which client company the employee belongs to.
        if company_queryset is not None:
            self.fields['company'] = forms.ModelChoiceField(
                queryset=company_queryset, required=True, label='Company',
                initial=getattr(self.instance, 'company_id', None),
            )
        _style(self.fields)

    def clean_email(self):
        return self.cleaned_data['email'].strip().lower()

    def clean_ifsc_code(self):
        value = (self.cleaned_data.get('ifsc_code') or '').strip().upper()
        if value and not re.fullmatch(r'[A-Z]{4}0[A-Z0-9]{6}', value):
            raise forms.ValidationError('Enter a valid 11-character IFSC (e.g. HDFC0001234).')
        return value

    def clean_bank_account_no(self):
        value = (self.cleaned_data.get('bank_account_no') or '').replace(' ', '')
        if value and not re.fullmatch(r'\d{9,18}', value):
            raise forms.ValidationError('Bank account number must be 9 to 18 digits.')
        return value

    def clean_pan_number(self):
        value = self.cleaned_data.get('pan_number', '')
        return value.upper().strip()

    def clean_aadhar_number(self):
        value = self.cleaned_data.get('aadhar_number', '').strip()
        if value and (not value.isdigit() or len(value) != 12):
            raise forms.ValidationError('Aadhaar number must be exactly 12 digits.')
        return value


class SalaryStructureForm(forms.ModelForm):
    class Meta:
        model = SalaryStructure
        fields = ['employee', 'basic', 'hra', 'conveyance', 'special_allowance']

    def __init__(self, *args, employee_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if employee_queryset is not None:
            self.fields['employee'].queryset = employee_queryset
        _style(self.fields)

    def clean_basic(self):
        value = self.cleaned_data['basic']
        if value <= 0:
            raise forms.ValidationError('Basic pay must be greater than zero.')
        return value


class AttendanceForm(forms.ModelForm):
    class Meta:
        model = Attendance
        fields = ['employee', 'date', 'status', 'check_in', 'check_out', 'remarks']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'check_in': forms.TimeInput(attrs={'type': 'time'}),
            'check_out': forms.TimeInput(attrs={'type': 'time'}),
        }

    def clean(self):
        cleaned = super().clean()
        cin, cout = cleaned.get('check_in'), cleaned.get('check_out')
        if cin and cout and cout <= cin:
            raise forms.ValidationError('Check-out time must be after check-in time.')
        return cleaned

    def __init__(self, *args, employee_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if employee_queryset is not None:
            self.fields['employee'].queryset = employee_queryset
        _style(self.fields)
        self.fields['check_in'].required = False
        self.fields['check_out'].required = False


class LeaveRequestForm(forms.ModelForm):
    class Meta:
        model = LeaveRequest
        fields = ['employee', 'leave_type', 'from_date', 'to_date', 'reason']
        widgets = {
            'from_date': forms.DateInput(attrs={'type': 'date'}),
            'to_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, employee_queryset=None, forced_employee=None, **kwargs):
        self.forced_employee = forced_employee
        super().__init__(*args, **kwargs)
        _apply_employee_scope(self, employee_queryset, forced_employee)
        _style(self.fields)

    def clean(self):
        cleaned = super().clean()
        from_date = cleaned.get('from_date')
        to_date = cleaned.get('to_date')
        if from_date and to_date and to_date < from_date:
            raise forms.ValidationError('To date cannot be earlier than from date.')
        employee = self.forced_employee or cleaned.get('employee')
        leave_type = cleaned.get('leave_type')
        if employee and from_date and to_date and to_date >= from_date:
            overlap = LeaveRequest.objects.filter(
                employee=employee, status__in=['PENDING', 'APPROVED'],
                from_date__lte=to_date, to_date__gte=from_date,
            )
            if overlap.exists():
                raise forms.ValidationError('These dates overlap an existing pending/approved leave request.')
            if leave_type:
                from .leave import leave_balance
                remaining = leave_balance(employee, from_date.year).get(leave_type, {}).get('remaining')
                requested = (to_date - from_date).days + 1
                if remaining is not None and requested > remaining:
                    raise forms.ValidationError(
                        f'Insufficient leave balance: {requested} day(s) requested, {remaining} remaining.'
                    )
        return cleaned


class LeaveDecisionForm(forms.Form):
    """Approve/reject a leave request — status is never taken from raw POST."""
    decision = forms.ChoiceField(choices=[('APPROVED', 'Approve'), ('REJECTED', 'Reject')])
    comment = forms.CharField(max_length=255, required=False)


class ReimbursementForm(forms.ModelForm):
    class Meta:
        model = Reimbursement
        fields = ['employee', 'category', 'amount', 'date', 'description', 'receipt']
        widgets = {'date': forms.DateInput(attrs={'type': 'date'})}

    def clean_receipt(self):
        f = self.cleaned_data.get('receipt')
        if not f or not hasattr(f, 'size'):
            return f
        ext = os.path.splitext(f.name)[1].lower()
        allowed = getattr(settings, 'ALLOWED_UPLOAD_EXTENSIONS', ['.pdf', '.png', '.jpg', '.jpeg'])
        if ext not in allowed:
            raise forms.ValidationError(f"Unsupported file type. Allowed: {', '.join(allowed)}")
        if f.size > getattr(settings, 'FILE_UPLOAD_MAX_MEMORY_SIZE', 5 * 1024 * 1024):
            raise forms.ValidationError('File too large (max 5 MB).')
        return f

    def clean_date(self):
        value = self.cleaned_data['date']
        if value > timezone.localdate():
            raise forms.ValidationError('Expense date cannot be in the future.')
        return value

    def __init__(self, *args, employee_queryset=None, forced_employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        _apply_employee_scope(self, employee_queryset, forced_employee)
        _style(self.fields)

    def clean_amount(self):
        value = self.cleaned_data['amount']
        if value <= 0:
            raise forms.ValidationError('Amount must be greater than zero.')
        return value


class PayrollRunForm(forms.ModelForm):
    class Meta:
        model = PayrollRun
        fields = ['month']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['month'].widget = forms.TextInput(attrs={'placeholder': 'e.g. August 2026'})
        _style(self.fields)

    def clean_month(self):
        from .payroll_engine import parse_month
        value = self.cleaned_data['month'].strip()
        period = parse_month(value)
        if not period:
            raise forms.ValidationError('Enter the payroll month as "Month YYYY", e.g. "August 2026".')
        return period[0].strftime('%B %Y')


class ArrearsForm(forms.ModelForm):
    class Meta:
        model = ArrearsRecord
        fields = ['employee', 'effective_from', 'old_basic', 'new_basic', 'months']
        widgets = {'effective_from': forms.DateInput(attrs={'type': 'date'})}

    def __init__(self, *args, employee_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if employee_queryset is not None:
            self.fields['employee'].queryset = employee_queryset
        _style(self.fields)


class FullFinalSettlementForm(forms.ModelForm):
    class Meta:
        model = FullFinalSettlement
        fields = ['employee', 'last_working_day', 'pending_salary', 'leave_encashment', 'deductions']
        widgets = {'last_working_day': forms.DateInput(attrs={'type': 'date'})}

    def __init__(self, *args, employee_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if employee_queryset is not None:
            self.fields['employee'].queryset = employee_queryset
        _style(self.fields)


class UserRoleAssignmentForm(forms.ModelForm):
    class Meta:
        model = UserRoleAssignment
        fields = ['employee', 'role']

    def __init__(self, *args, employee_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if employee_queryset is not None:
            self.fields['employee'].queryset = employee_queryset
        _style(self.fields)


class CompanySettingsForm(forms.ModelForm):
    class Meta:
        model = CompanySettings
        exclude = ['company']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class InvestmentDeclarationForm(forms.ModelForm):
    class Meta:
        model = InvestmentDeclaration
        fields = ['employee', 'financial_year', 'section', 'investment_type', 'declared_amount', 'proof_document']

    def __init__(self, *args, employee_queryset=None, forced_employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        _apply_employee_scope(self, employee_queryset, forced_employee)
        _style(self.fields)


# ---------------------------------------------------------------------------
# Public / auth workflow forms
# ---------------------------------------------------------------------------

DEMO_INDUSTRIES = [('', 'Select industry'), ('IT', 'IT / Software'), ('MANUFACTURING', 'Manufacturing'),
                   ('ENGINEERING', 'Engineering / Construction'), ('HEALTHCARE', 'Healthcare'), ('RETAIL', 'Retail'),
                   ('EDUCATION', 'Education'), ('FINANCE', 'Finance'), ('OTHER', 'Other')]
DEMO_MODULES = [('PAYROLL', 'Payroll'), ('ATTENDANCE', 'Attendance & leave'), ('ESS', 'Employee self-service'),
                ('COMPLIANCE', 'PF / ESI / TDS compliance'), ('REPORTS', 'Reports'), ('DOCUMENTS', 'Form 16 & documents')]


class DemoRequestForm(forms.ModelForm):
    industry = forms.ChoiceField(choices=DEMO_INDUSTRIES, required=False)
    modules_choice = forms.MultipleChoiceField(choices=DEMO_MODULES, required=False, label='Modules you need',
                                               widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = DemoRequest
        fields = ['full_name', 'company_name', 'email', 'phone', 'team_size', 'industry', 'preferred_datetime', 'message']
        labels = {
            'phone': 'Mobile number', 'team_size': 'Number of employees / users',
            'preferred_datetime': 'Preferred demo date & time', 'message': 'Message / requirements',
        }
        widgets = {'preferred_datetime': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['phone'].required = True
        self.fields['preferred_datetime'].input_formats = ['%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S']
        _style(self.fields)

    def clean_email(self):
        return self.cleaned_data['email'].strip().lower()

    def clean_phone(self):
        return validate_indian_mobile(self.cleaned_data.get('phone'))

    def save(self, commit=True):
        obj = super().save(commit=False)
        labels = dict(DEMO_MODULES)
        obj.modules = ', '.join(labels[c] for c in self.cleaned_data.get('modules_choice') or [])
        if commit:
            obj.save()
        return obj

    def clean_preferred_datetime(self):
        value = self.cleaned_data.get('preferred_datetime')
        if value and value < timezone.now():
            raise forms.ValidationError('Please choose a future date and time.')
        return value

    def clean(self):
        cleaned = super().clean()
        email, company = cleaned.get('email'), cleaned.get('company_name')
        if email and company:
            recent = timezone.now() - timezone.timedelta(minutes=30)
            if DemoRequest.objects.filter(email__iexact=email, company_name__iexact=company.strip(),
                                          created_at__gte=recent).exists():
                raise forms.ValidationError(
                    'We already received a demo request from you in the last 30 minutes — our team will be in touch.'
                )
        return cleaned


MAX_ACCOUNTS_PER_EMAIL = 20  # one email address may be used by up to 20 accounts


class RegistrationEmailForm(forms.Form):
    """Step 1 of company sign-up: the email that will receive the OTP."""
    email = forms.EmailField(label='Email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).count() >= MAX_ACCOUNTS_PER_EMAIL:
            raise forms.ValidationError(
                f'This email already exists on {MAX_ACCOUNTS_PER_EMAIL} accounts, the maximum allowed for one email.')
        return email


class CompanyRegistrationForm(forms.Form):
    """Self-service company signup. Creates a Company (PENDING_APPROVAL) and
    an inactive COMPANY_OWNER user; the account is enabled only after an
    admin approves the company."""

    company_name = forms.CharField(max_length=200)
    owner_full_name = forms.CharField(max_length=150, label='Your full name')
    contact_email = forms.EmailField(label='Email')
    contact_phone = forms.CharField(max_length=15, label='Mobile number')
    address = forms.CharField(max_length=500, required=False, widget=forms.Textarea(attrs={'rows': 2}))
    username = forms.CharField(max_length=150)
    password1 = forms.CharField(widget=forms.PasswordInput, label='Password')
    password2 = forms.CharField(widget=forms.PasswordInput, label='Confirm password')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)

    def clean_company_name(self):
        name = self.cleaned_data['company_name'].strip()
        if Company.objects.filter(name__iexact=name).exists():
            raise forms.ValidationError('A company with this name is already registered.')
        return name

    def clean_contact_email(self):
        email = self.cleaned_data['contact_email'].strip().lower()
        if User.objects.filter(email__iexact=email).count() >= MAX_ACCOUNTS_PER_EMAIL:
            raise forms.ValidationError(
                f'This email already exists on {MAX_ACCOUNTS_PER_EMAIL} accounts, the maximum allowed for one email.')
        return email

    def clean_contact_phone(self):
        return validate_indian_mobile(self.cleaned_data.get('contact_phone'))

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if '@' in username:
            raise forms.ValidationError('Username cannot contain "@" (you can still sign in with your email).')
        if User.objects.filter(Q(username__iexact=username) | Q(email__iexact=username)).exists():
            raise forms.ValidationError('This username is already taken.')
        return username

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get('password1'), cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError('Passwords do not match.')
        if p1:
            from django.contrib.auth.password_validation import validate_password
            try:
                validate_password(p1, user=User(username=cleaned.get('username', ''), email=cleaned.get('contact_email', '')))
            except forms.ValidationError as exc:
                self.add_error('password1', exc)
        return cleaned


class OTPVerifyForm(forms.Form):
    code = forms.CharField(max_length=6, min_length=6, label='6-digit code',
                           widget=forms.TextInput(attrs={'inputmode': 'numeric', 'autocomplete': 'one-time-code'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)

    def clean_code(self):
        code = self.cleaned_data['code'].strip()
        if not code.isdigit():
            raise forms.ValidationError('The code must be 6 digits.')
        return code


class LoginForm(AuthenticationForm):
    """Accepts username or email and explains *why* a correct-password login
    was refused (pending approval / email not verified / suspended) instead of
    a misleading 'wrong password'."""

    username = forms.CharField(label='Username, email or Employee ID', max_length=254,
                               widget=forms.TextInput(attrs={'autofocus': True, 'autocomplete': 'username'}))

    def clean(self):
        from .login_throttle import is_locked
        if is_locked(self.request, self.cleaned_data.get('username')):
            raise forms.ValidationError('Too many failed sign-in attempts. Please wait 15 minutes and try again, '
                                        'or reset your password.', code='locked')
        try:
            return super().clean()
        except forms.ValidationError:
            ident = (self.cleaned_data.get('username') or '').strip()
            password = self.cleaned_data.get('password')
            from .backends import find_login_users
            users = find_login_users(ident)
            if ident and password and len(users) == 1 and users[0].check_password(password):
                user = users[0]
                if not user.email_verified:
                    raise forms.ValidationError('Your email is not verified yet. Please enter the code we emailed you.', code='unverified')
                company = user.company
                if company and company.status == 'PENDING_APPROVAL':
                    raise forms.ValidationError('Your company registration is pending admin approval.', code='pending')
                if company and company.status == 'REJECTED':
                    raise forms.ValidationError('Your company registration was not approved. Please contact support.', code='rejected')
                if company and company.status == 'SUSPENDED':
                    raise forms.ValidationError('Your company account is suspended. Please contact support.', code='suspended')
                if not user.is_active:
                    raise forms.ValidationError('This account is inactive. Please contact your administrator.', code='inactive')
            if ident and '@' in ident and len(users) > 1:
                raise forms.ValidationError(
                    'This email is linked to more than one account. Please sign in with your username.',
                    code='ambiguous_email')
            if ident and '@' not in ident and len(users) > 1:
                raise forms.ValidationError(
                    'This Employee ID is used in more than one company. Please sign in with your username or email.',
                    code='ambiguous_employee_id')
            raise forms.ValidationError('Invalid username/email or password.', code='invalid_login')


class BankPaymentUpdateForm(forms.Form):
    ACTION_CHOICES = [('INITIATE', 'Mark initiated'), ('PAID', 'Mark paid'), ('FAILED', 'Mark failed'), ('RETRY', 'Retry')]
    action = forms.ChoiceField(choices=ACTION_CHOICES)
    reference_number = forms.CharField(max_length=60, required=False)
    failure_reason = forms.CharField(max_length=255, required=False)

    def clean(self):
        cleaned = super().clean()
        action = cleaned.get('action')
        ref = (cleaned.get('reference_number') or '').strip()
        if action == 'PAID' and not ref:
            raise forms.ValidationError('A bank UTR / reference number is required to mark a payment as paid.')
        if action == 'FAILED' and not (cleaned.get('failure_reason') or '').strip():
            raise forms.ValidationError('Please record why the payment failed.')
        cleaned['reference_number'] = ref
        return cleaned


class ClientComplaintForm(forms.ModelForm):
    class Meta:
        model = ClientComplaint
        fields = ['subject', 'description']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class ClientRequestForm(forms.ModelForm):
    class Meta:
        model = ClientRequest
        fields = ['request_type', 'description']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class EmployeeAccountForm(forms.Form):
    """Used by a Company Owner/Admin to create an employee's ESS login."""
    username = forms.CharField(max_length=150)
    password1 = forms.CharField(widget=forms.PasswordInput, label='Password')
    password2 = forms.CharField(widget=forms.PasswordInput, label='Confirm password')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if '@' in username:
            raise forms.ValidationError('Username cannot contain "@".')
        if User.objects.filter(Q(username__iexact=username) | Q(email__iexact=username)).exists():
            raise forms.ValidationError('This username is already taken.')
        return username

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get('password1'), cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError('Passwords do not match.')
        if p1:
            from django.contrib.auth.password_validation import validate_password
            try:
                validate_password(p1)
            except forms.ValidationError as exc:
                self.add_error('password1', exc)
        return cleaned
