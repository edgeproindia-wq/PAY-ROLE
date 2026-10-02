"""Owner page: create sign-in accounts for ALL employees in one click (they sign in
with their Employee ID), and reset one employee's password. Company-scoped."""
from django import forms
from django.contrib import messages
from django.contrib.auth.password_validation import validate_password
from django.shortcuts import redirect, render

from .audit import log_action
from .models import Employee, User
from .permissions import company_owner_required


class PasswordPairForm(forms.Form):
    password1 = forms.CharField(widget=forms.PasswordInput, label='Password')
    password2 = forms.CharField(widget=forms.PasswordInput, label='Confirm password')

    def clean(self):
        c = super().clean()
        p1, p2 = c.get('password1'), c.get('password2')
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError('The two passwords do not match.')
        if p1:
            try:
                validate_password(p1)
            except forms.ValidationError as e:
                raise forms.ValidationError(e.messages)
        return c


class ResetForm(PasswordPairForm):
    employee_code = forms.CharField(max_length=30, label='Employee ID')


def _new_username(employee):
    base = f"emp{employee.company_id}-{employee.employee_code}".lower().replace(' ', '')[:140]
    username, n = base, 1
    while User.objects.filter(username__iexact=username).exists():
        n += 1
        username = f"{base}-{n}"
    return username


@company_owner_required
def hr_employee_logins(request):
    company = request.user.company
    action = request.POST.get('action') if request.method == 'POST' else ''
    bulk = PasswordPairForm(request.POST if action == 'bulk' else None, prefix='bulk')
    reset = ResetForm(request.POST if action == 'reset' else None, prefix='reset')
    if action == 'bulk' and bulk.is_valid():
        created = 0
        for e in Employee.objects.filter(company=company, user__isnull=True):
            u = User.objects.create_user(username=_new_username(e), password=bulk.cleaned_data['password1'],
                                         role='EMPLOYEE', company=company, email=e.email or '',
                                         first_name=e.first_name, last_name=e.last_name)
            e.user = u
            e.save(update_fields=['user'])
            created += 1
        log_action(request, 'CREATE', company, details=f'Bulk employee logins created: {created}')
        messages.success(request, f'{created} employee login(s) created. Employees sign in with their Employee ID.')
        return redirect('hr_employee_logins')
    if action == 'reset' and reset.is_valid():
        code = reset.cleaned_data['employee_code'].strip()
        e = Employee.objects.filter(company=company, employee_code__iexact=code).select_related('user').first()
        if e is None:
            reset.add_error('employee_code', 'No employee with this ID in your company.')
        else:
            if e.user is None:
                e.user = User.objects.create_user(username=_new_username(e), password=reset.cleaned_data['password1'],
                                                  role='EMPLOYEE', company=company, email=e.email or '',
                                                  first_name=e.first_name, last_name=e.last_name)
                e.save(update_fields=['user'])
                messages.success(request, f'Login created for {e.employee_code}.')
            else:
                u = e.user
                u.set_password(reset.cleaned_data['password1'])
                u.is_active = True
                if hasattr(u, 'email_verified'):
                    u.email_verified = True
                u.save()
                messages.success(request, f'Password reset for {e.employee_code}.')
            log_action(request, 'UPDATE', e, details='Employee login created/reset by owner')
            return redirect('hr_employee_logins')
    pending = Employee.objects.filter(company=company, user__isnull=True).order_by('employee_code')
    active = Employee.objects.filter(company=company, user__isnull=False).count()
    for f in list(bulk.fields.values()) + list(reset.fields.values()):
        f.widget.attrs.setdefault('class', 'form-control')
    return render(request, 'features/hr_employee_logins.html',
                  {'bulk': bulk, 'reset': reset, 'pending': pending, 'active': active})