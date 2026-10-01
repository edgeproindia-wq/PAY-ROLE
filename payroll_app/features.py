"""Phase-3 pages: leave cancellation, grievances, announcements, loans, insurance,
professional-tax slabs, investment-proof downloads and the admin demo-request CRM.
Every view is role-checked by decorator AND scoped by company/employee in its query,
so changing an id in the URL never reaches another company's or employee's data."""
import os

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .audit import log_action
from .models import DemoRequest, InvestmentDeclaration, LeaveRequest, Notification, User
from .models_features import (Announcement, DemoRequestActivity, Grievance, GrievanceUpdate, InsurancePolicy,
                              Loan, ProfessionalTaxSlab)
from .permissions import (admin_required, company_owner_required, effective_role, get_object_scoped,
                          owner_or_employee_required)

TEXT = 'form-control'


def _style(form):
    for f in form.fields.values():
        f.widget.attrs.setdefault('class', TEXT)
    return form


def _employee(request):
    return getattr(request.user, 'employee_profile', None) if effective_role(request.user) == 'EMPLOYEE' else None


def _owners(company):
    return User.objects.filter(company=company, role='COMPANY_OWNER', is_active=True)


def _notify(users, message, link=''):
    for u in users:
        if u is not None:
            Notification.objects.create(recipient=u, message=message[:255], link=link)


def _file(fieldfile):
    if not fieldfile:
        raise Http404
    name = os.path.basename(fieldfile.name)
    return FileResponse(fieldfile.open('rb'), as_attachment=True, filename=name)


# ================================================================ leave cancellation
@require_POST
@owner_or_employee_required
def leave_cancel(request, pk):
    emp = _employee(request)
    if emp is None:
        raise PermissionDenied('Only the employee can cancel their own leave.')
    leave = get_object_or_404(LeaveRequest, pk=pk, employee=emp)
    today = timezone.localdate()
    if leave.status == 'PENDING' or (leave.status == 'APPROVED' and leave.from_date > today):
        leave.status = 'CANCELLED'
        leave.cancelled_at = timezone.now()
        leave.cancellation_reason = (request.POST.get('reason') or '').strip()[:255]
        leave.save()
        log_action(request, 'UPDATE', leave, details='Leave cancelled by employee')
        _notify(_owners(emp.company), f'{emp} cancelled a {leave.get_leave_type_display()} request '
                                      f'({leave.from_date:%d %b} - {leave.to_date:%d %b}).', '/leave_management/')
        messages.success(request, 'Leave request cancelled.')
    else:
        messages.error(request, 'Only pending leave, or approved leave that has not started, can be cancelled.')
    return redirect(request.POST.get('next') or 'employee_home')


# ================================================================ grievances
class GrievanceForm(forms.ModelForm):
    class Meta:
        model = Grievance
        fields = ['category', 'subject', 'department', 'description', 'attachment']
        widgets = {'description': forms.Textarea(attrs={'rows': 4})}


class GrievanceUpdateForm(forms.Form):
    message = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), max_length=2000, required=False, label='Comment / response')
    status = forms.ChoiceField(choices=[('', '-- keep current --')] + Grievance.STATUS_CHOICES, required=False)
    assigned_to = forms.ModelChoiceField(queryset=User.objects.none(), required=False, label='Assign to')


def _grievance_for(request, pk):
    role = effective_role(request.user)
    if role == 'EMPLOYEE':
        emp = _employee(request)
        return get_object_or_404(Grievance, pk=pk, employee=emp) if emp else None
    if role == 'COMPANY_OWNER':
        return get_object_or_404(Grievance, pk=pk, company=request.user.company)
    raise PermissionDenied


@owner_or_employee_required
def grievances(request):
    role = effective_role(request.user)
    if role == 'EMPLOYEE':
        emp = _employee(request)
        if emp is None:
            raise PermissionDenied('No employee profile linked to this account.')
        form = _style(GrievanceForm(request.POST or None, request.FILES or None))
        if request.method == 'POST' and form.is_valid():
            g = form.save(commit=False)
            g.employee, g.company = emp, emp.company
            g.save()
            GrievanceUpdate.objects.create(grievance=g, author=request.user, new_status='OPEN', message='Grievance raised')
            log_action(request, 'CREATE', g, details=f'Grievance {g.tracking_id} raised')
            _notify(_owners(emp.company), f'New grievance {g.tracking_id}: {g.subject}', f'/grievances/{g.pk}/')
            messages.success(request, f'Grievance submitted. Your tracking ID is {g.tracking_id}.')
            return redirect('grievance_detail', pk=g.pk)
        items = Grievance.objects.filter(employee=emp)
        return render(request, 'features/grievances.html', {'form': form, 'items': items, 'is_owner': False})
    items = Grievance.objects.filter(company=request.user.company).select_related('employee', 'assigned_to')
    status = request.GET.get('status', '')
    if status:
        items = items.filter(status=status)
    return render(request, 'features/grievances.html', {'items': items, 'is_owner': True, 'status': status,
                                                        'statuses': Grievance.STATUS_CHOICES})


@owner_or_employee_required
def grievance_detail(request, pk):
    g = _grievance_for(request, pk)
    if g is None:
        raise Http404
    is_owner = effective_role(request.user) == 'COMPANY_OWNER'
    form = GrievanceUpdateForm(request.POST or None)
    form.fields['assigned_to'].queryset = User.objects.filter(company=g.company, role='COMPANY_OWNER')
    if not is_owner:
        del form.fields['status'], form.fields['assigned_to']
    _style(form)
    if request.method == 'POST' and form.is_valid():
        msg = form.cleaned_data.get('message', '').strip()
        new_status = form.cleaned_data.get('status') or ''
        if not is_owner and g.status in ('CLOSED', 'REJECTED'):
            messages.error(request, 'This grievance is closed.')
            return redirect('grievance_detail', pk=g.pk)
        old = g.status
        if is_owner:
            if new_status and new_status != old:
                g.status = new_status
            if form.cleaned_data.get('assigned_to') is not None:
                g.assigned_to = form.cleaned_data['assigned_to']
            g.save()
        if msg or g.status != old:
            GrievanceUpdate.objects.create(grievance=g, author=request.user, message=msg,
                                           old_status=old if g.status != old else '',
                                           new_status=g.status if g.status != old else '')
            log_action(request, 'UPDATE', g, details=f'Grievance {g.tracking_id} updated')
            if is_owner and g.employee.user_id:
                _notify([g.employee.user], f'Update on your grievance {g.tracking_id}: {g.get_status_display()}',
                        f'/grievances/{g.pk}/')
            elif not is_owner:
                _notify(_owners(g.company), f'New comment on grievance {g.tracking_id}', f'/grievances/{g.pk}/')
            messages.success(request, 'Grievance updated.')
        return redirect('grievance_detail', pk=g.pk)
    return render(request, 'features/grievance_detail.html', {'g': g, 'form': form, 'is_owner': is_owner,
                                                              'updates': g.updates.select_related('author')})


@owner_or_employee_required
def grievance_attachment(request, pk):
    return _file(_grievance_for(request, pk).attachment)


# ================================================================ announcements
class AnnouncementForm(forms.ModelForm):
    class Meta:
        model = Announcement
        fields = ['title', 'message', 'audience', 'publish_date', 'expiry_date']
        widgets = {'message': forms.Textarea(attrs={'rows': 4}),
                   'publish_date': forms.DateInput(attrs={'type': 'date'}),
                   'expiry_date': forms.DateInput(attrs={'type': 'date'})}

    def clean(self):
        c = super().clean()
        if c.get('expiry_date') and c.get('publish_date') and c['expiry_date'] < c['publish_date']:
            self.add_error('expiry_date', 'Expiry date must be on or after the publish date.')
        return c


@owner_or_employee_required
def announcements(request):
    company = request.user.company
    if effective_role(request.user) == 'EMPLOYEE':
        items = list(Announcement.active_for(company))
        from .models_phase5 import AnnouncementRead
        for item in items:                      # opening the page marks them as read
            AnnouncementRead.objects.get_or_create(announcement=item, user=request.user)
        return render(request, 'features/announcements.html', {'items': items, 'is_owner': False})
    form = _style(AnnouncementForm(request.POST or None))
    if request.method == 'POST' and request.POST.get('archive'):
        a = get_object_or_404(Announcement, pk=request.POST['archive'], company=company)
        a.status = 'ARCHIVED'
        a.save(update_fields=['status'])
        messages.success(request, 'Announcement archived.')
        return redirect('announcements')
    if request.method == 'POST' and form.is_valid():
        a = form.save(commit=False)
        a.company, a.created_by = company, request.user
        a.save()
        from .models import Employee
        recipients = [e.user for e in Employee.objects.filter(company=company, user__isnull=False).select_related('user')]
        if a.audience == 'ALL':
            recipients += list(_owners(company).exclude(pk=request.user.pk))
        _notify(recipients, f'Announcement: {a.title}', '/announcements/')
        log_action(request, 'CREATE', a, details='Announcement published')
        messages.success(request, 'Announcement published.')
        return redirect('announcements')
    return render(request, 'features/announcements.html', {'items': Announcement.objects.filter(company=company),
                                                           'form': form, 'is_owner': True})


# ================================================================ loans / insurance / PT (HR)
class LoanForm(forms.ModelForm):
    class Meta:
        model = Loan
        fields = ['employee', 'reference_no', 'loan_amount', 'interest_rate', 'tenure_months', 'emi_amount', 'start_date', 'notes']
        widgets = {'start_date': forms.DateInput(attrs={'type': 'date'})}

    def __init__(self, company, *a, **kw):
        super().__init__(*a, **kw)
        from .models import Employee
        self.company = company
        self.fields['employee'].queryset = Employee.objects.filter(company=company)

    def clean(self):
        c = super().clean()
        if c.get('emi_amount') and c.get('loan_amount') and c['emi_amount'] > c['loan_amount']:
            self.add_error('emi_amount', 'EMI cannot be more than the loan amount.')
        if c.get('reference_no') and Loan.objects.filter(company=self.company, reference_no=c['reference_no']).exists():
            self.add_error('reference_no', 'This loan reference already exists.')
        return c


class InsuranceForm(forms.ModelForm):
    class Meta:
        model = InsurancePolicy
        fields = ['employee', 'provider', 'policy_number', 'policy_type', 'sum_insured', 'employee_premium_monthly',
                  'employer_contribution_monthly', 'effective_from', 'effective_to']
        widgets = {'effective_from': forms.DateInput(attrs={'type': 'date'}), 'effective_to': forms.DateInput(attrs={'type': 'date'})}

    def __init__(self, company, *a, **kw):
        super().__init__(*a, **kw)
        from .models import Employee
        self.fields['employee'].queryset = Employee.objects.filter(company=company)


class PTSlabForm(forms.ModelForm):
    class Meta:
        model = ProfessionalTaxSlab
        fields = ['min_monthly_gross', 'max_monthly_gross', 'monthly_amount', 'february_amount']


@company_owner_required
def hr_loans(request):
    company = request.user.company
    form = _style(LoanForm(company, request.POST or None))
    if request.method == 'POST' and request.POST.get('close'):
        loan = get_object_or_404(Loan, pk=request.POST['close'], company=company)
        loan.status = 'CLOSED'
        loan.save(update_fields=['status'])
        messages.success(request, f'Loan {loan.reference_no} closed.')
        return redirect('hr_loans')
    if request.method == 'POST' and form.is_valid():
        loan = form.save(commit=False)
        loan.company = company
        loan.save()
        log_action(request, 'CREATE', loan, details=f'Loan {loan.reference_no} added')
        if loan.employee.user_id:
            _notify([loan.employee.user], f'Loan {loan.reference_no} added: EMI {loan.emi_amount} from {loan.start_date:%b %Y}', '/me/')
        messages.success(request, 'Loan added.')
        return redirect('hr_loans')
    return render(request, 'features/hr_loans.html', {'form': form, 'items': Loan.objects.filter(company=company).select_related('employee')})


@company_owner_required
def hr_insurance(request):
    company = request.user.company
    form = _style(InsuranceForm(company, request.POST or None))
    if request.method == 'POST' and request.POST.get('deactivate'):
        p = get_object_or_404(InsurancePolicy, pk=request.POST['deactivate'], company=company)
        p.status = 'INACTIVE'
        p.save(update_fields=['status'])
        messages.success(request, 'Policy deactivated.')
        return redirect('hr_insurance')
    if request.method == 'POST' and form.is_valid():
        p = form.save(commit=False)
        p.company = company
        p.save()
        log_action(request, 'CREATE', p, details='Insurance policy added')
        messages.success(request, 'Insurance policy added.')
        return redirect('hr_insurance')
    return render(request, 'features/hr_insurance.html', {'form': form, 'items': InsurancePolicy.objects.filter(company=company).select_related('employee')})


@company_owner_required
def hr_professional_tax(request):
    company = request.user.company
    form = _style(PTSlabForm(request.POST or None))
    if request.method == 'POST' and request.POST.get('delete'):
        get_object_or_404(ProfessionalTaxSlab, pk=request.POST['delete'], company=company).delete()
        messages.success(request, 'Slab deleted.')
        return redirect('hr_professional_tax')
    if request.method == 'POST' and form.is_valid():
        s = form.save(commit=False)
        s.company = company
        s.save()
        log_action(request, 'CREATE', s, details='Professional tax slab added')
        messages.success(request, 'Slab added.')
        return redirect('hr_professional_tax')
    return render(request, 'features/hr_pt.html', {'form': form, 'items': ProfessionalTaxSlab.objects.filter(company=company)})


# ================================================================ investment proofs
@owner_or_employee_required
def declaration_proof(request, pk):
    d = get_object_scoped(request, InvestmentDeclaration, employee_field='employee', pk=pk)
    return _file(d.proof_document)


@company_owner_required
def hr_declarations(request):
    items = InvestmentDeclaration.objects.filter(employee__company=request.user.company).select_related('employee')
    return render(request, 'features/hr_declarations.html', {'items': items})


# ================================================================ admin: demo request CRM
@admin_required
def admin_demo_list(request):
    qs = DemoRequest.objects.select_related('assigned_to')
    q_ = request.GET.get('q', '').strip()
    status = request.GET.get('status', '')
    if q_:
        qs = qs.filter(Q(request_code__icontains=q_) | Q(full_name__icontains=q_) | Q(company_name__icontains=q_)
                       | Q(email__icontains=q_) | Q(phone__icontains=q_))
    if status:
        qs = qs.filter(status=status)
    page = Paginator(qs, 20).get_page(request.GET.get('page'))
    return render(request, 'features/admin_demo_list.html', {'demos': page, 'q': q_, 'status': status,
                                                             'statuses': DemoRequest.STATUS_CHOICES})


class DemoUpdateForm(forms.Form):
    status = forms.ChoiceField(choices=DemoRequest.STATUS_CHOICES)
    assigned_to = forms.ModelChoiceField(queryset=User.objects.none(), required=False)
    follow_up_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    note = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), max_length=2000, required=False)


@admin_required
def admin_demo_detail(request, pk):
    demo = get_object_or_404(DemoRequest, pk=pk)
    form = DemoUpdateForm(request.POST or None, initial={'status': demo.status, 'assigned_to': demo.assigned_to_id,
                                                         'follow_up_date': demo.follow_up_date})
    form.fields['assigned_to'].queryset = User.objects.filter(Q(is_superuser=True) | Q(role='ADMIN'), is_active=True)
    _style(form)
    if request.method == 'POST' and form.is_valid():
        c = form.cleaned_data
        changes = []
        if c['status'] != demo.status:
            changes.append(f'Status: {demo.get_status_display()} -> {dict(DemoRequest.STATUS_CHOICES)[c["status"]]}')
            demo.status = c['status']
        if (c['assigned_to'].pk if c['assigned_to'] else None) != demo.assigned_to_id:
            changes.append(f'Assigned to: {c["assigned_to"] or "nobody"}')
            demo.assigned_to = c['assigned_to']
        if c['follow_up_date'] != demo.follow_up_date:
            changes.append(f'Follow-up: {c["follow_up_date"] or "none"}')
            demo.follow_up_date = c['follow_up_date']
        demo.reviewed_at = timezone.now()
        demo.save()
        if changes or c['note']:
            DemoRequestActivity.objects.create(demo=demo, actor=request.user, action='; '.join(changes) or 'Note added',
                                               note=c['note'])
            log_action(request, 'UPDATE', demo, details=f'Demo {demo.request_code} updated')
        messages.success(request, 'Demo request updated.')
        return redirect('admin_demo_detail', pk=demo.pk)
    return render(request, 'features/admin_demo_detail.html', {'demo': demo, 'form': form,
                                                               'activities': demo.activities.select_related('actor')})