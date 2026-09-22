from django import forms
from django.contrib.auth.forms import UserCreationForm

from .models import (
    Employee, SalaryStructure, Attendance, LeaveRequest, Reimbursement,
    PayrollRun, InvestmentDeclaration, ArrearsRecord, FullFinalSettlement,
    UserRoleAssignment, CompanySettings, Company, DemoRequest, User,
    ClientComplaint, ClientRequest,
)

TEXT_WIDGET_CLASS = 'form-control'


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)

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
        fields = ['employee', 'date', 'status', 'remarks']
        widgets = {'date': forms.DateInput(attrs={'type': 'date'})}

    def __init__(self, *args, employee_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if employee_queryset is not None:
            self.fields['employee'].queryset = employee_queryset
        _style(self.fields)


class LeaveRequestForm(forms.ModelForm):
    class Meta:
        model = LeaveRequest
        fields = ['employee', 'leave_type', 'from_date', 'to_date', 'reason']
        widgets = {
            'from_date': forms.DateInput(attrs={'type': 'date'}),
            'to_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, employee_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if employee_queryset is not None:
            self.fields['employee'].queryset = employee_queryset
        _style(self.fields)

    def clean(self):
        cleaned = super().clean()
        from_date = cleaned.get('from_date')
        to_date = cleaned.get('to_date')
        if from_date and to_date and to_date < from_date:
            raise forms.ValidationError('To date cannot be earlier than from date.')
        return cleaned


class LeaveDecisionForm(forms.Form):
    """Approve/reject a leave request — status is never taken from raw POST."""
    decision = forms.ChoiceField(choices=[('APPROVED', 'Approve'), ('REJECTED', 'Reject')])


class ReimbursementForm(forms.ModelForm):
    class Meta:
        model = Reimbursement
        fields = ['employee', 'category', 'amount', 'date', 'description']
        widgets = {'date': forms.DateInput(attrs={'type': 'date'})}

    def __init__(self, *args, employee_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if employee_queryset is not None:
            self.fields['employee'].queryset = employee_queryset
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
        _style(self.fields)


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

    def __init__(self, *args, employee_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if employee_queryset is not None:
            self.fields['employee'].queryset = employee_queryset
        _style(self.fields)


# ---------------------------------------------------------------------------
# Public / auth workflow forms
# ---------------------------------------------------------------------------

class DemoRequestForm(forms.ModelForm):
    class Meta:
        model = DemoRequest
        fields = ['full_name', 'company_name', 'email', 'phone', 'team_size', 'message']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class CompanyRegistrationForm(forms.Form):
    """Self-service company signup. Creates a Company (PENDING_APPROVAL) and
    an inactive COMPANY_OWNER user; the account is enabled only after an
    admin approves the company."""

    company_name = forms.CharField(max_length=200)
    contact_email = forms.EmailField()
    contact_phone = forms.CharField(max_length=15, required=False)
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

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError('This username is already taken.')
        return username

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get('password1'), cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError('Passwords do not match.')
        if p1:
            from django.contrib.auth.password_validation import validate_password
            validate_password(p1)
        return cleaned


class EmployeeSelfRegisterForm(forms.Form):
    """Step 2 of employee self-registration (after the email OTP is verified).

    The email itself is validated/verified separately (see views.send_registration_otp
    and views.employee_register) against an existing Employee-Master record.
    """

    full_name = forms.CharField(max_length=150)
    mobile_number = forms.CharField(max_length=10, min_length=10, label='Mobile number')
    username = forms.CharField(max_length=150, label='Choose username')
    password1 = forms.CharField(widget=forms.PasswordInput, label='Password')
    password2 = forms.CharField(widget=forms.PasswordInput, label='Confirm password')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)

    def clean_mobile_number(self):
        mobile = self.cleaned_data['mobile_number'].strip()
        if not mobile.isdigit():
            raise forms.ValidationError('Enter a valid 10-digit mobile number.')
        return mobile

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError('This username is already taken.')
        return username

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get('password1'), cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError('Passwords do not match.')
        if p1:
            from django.contrib.auth.password_validation import validate_password
            validate_password(p1)
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
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError('This username is already taken.')
        return username

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get('password1'), cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError('Passwords do not match.')
        if p1:
            from django.contrib.auth.password_validation import validate_password
            validate_password(p1)
        return cleaned
