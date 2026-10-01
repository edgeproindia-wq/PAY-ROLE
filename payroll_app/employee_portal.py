"""Employee self-service dashboard and HR tax-document upload.

Written to work with the existing models without changing them: optional
fields are read defensively, and every lookup is scoped to the logged-in
employee (or, for HR pages, to the owner's company) so changing an id in the
URL can never show someone else's payslip or document."""
import logging
import os
import re
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import models as dj_models
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Employee, InvestmentDeclaration, PayrollRunLine
from .models_features import Announcement, Grievance, InsurancePolicy, Loan
from .models_phase4 import PayComponent
from .phase4 import month_summary, shift_for
from .models_documents import EmployeeDocument

logger = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_EXT = {'.pdf': 'application/pdf', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png'}


# ------------------------------------------------------------------ helpers
def _role(user):
    return 'ADMIN' if user.is_superuser else getattr(user, 'role', None)


def _employee_only(view):
    @login_required
    def inner(request, *args, **kwargs):
        if _role(request.user) != 'EMPLOYEE':
            raise PermissionDenied
        return view(request, *args, **kwargs)
    inner.__name__ = view.__name__
    return inner


def _owner_only(view):
    @login_required
    def inner(request, *args, **kwargs):
        if _role(request.user) != 'COMPANY_OWNER' or not getattr(request.user, 'company_id', None):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    inner.__name__ = view.__name__
    return inner


def _my_employee(request):
    return Employee.objects.filter(user=request.user).select_related('company').first()


def _released_statuses():
    """Payroll run statuses after which an employee may see the payslip."""
    try:
        field = PayrollRunLine._meta.get_field('payroll_run').related_model._meta.get_field('status')
        codes = [c for c, _ in field.choices]
    except Exception:
        return None
    visible = [c for c in codes if any(k in c.upper() for k in ('RELEAS', 'LOCK', 'PAID'))]
    return visible or [c for c in codes if 'DRAFT' not in c.upper()]


def _my_payslips(emp):
    qs = PayrollRunLine.objects.filter(employee=emp).select_related('payroll_run')
    statuses = _released_statuses()
    if statuses:
        qs = qs.filter(payroll_run__status__in=statuses)
    return qs.order_by('-payroll_run__id')


def _money_fields(line):
    """(label, amount) for every money field on the payslip line, skipping zeros."""
    rows = []
    for f in line._meta.concrete_fields:
        if isinstance(f, dj_models.DecimalField):
            val = getattr(line, f.attname) or Decimal('0')
            if val:
                rows.append((str(f.verbose_name).title(), val))
    return rows


def _period(line):
    run = getattr(line, 'payroll_run', None)
    for attr in ('month', 'period', 'name'):
        val = getattr(run, attr, None)
        if val:
            return str(val)
    return f'Run #{getattr(run, "pk", "")}'


def _safe_count(model_name, **filters):
    from django.apps import apps
    try:
        model = apps.get_model('payroll_app', model_name)
        return model.objects.filter(**filters).count()
    except Exception:
        return None


def _safe_list(model_name, order='-id', limit=5, **filters):
    from django.apps import apps
    try:
        model = apps.get_model('payroll_app', model_name)
        return list(model.objects.filter(**filters).order_by(order)[:limit])
    except Exception:
        return []


def _mask(value, keep=4):
    value = str(value or '')
    return ('*' * max(len(value) - keep, 0) + value[-keep:]) if value else ''


# ------------------------------------------------------------------ employee pages
@_employee_only
def employee_home(request):
    emp = _my_employee(request)
    if emp is None:
        return render(request, 'employee/home.html', {'no_employee': True})
    today = timezone.localdate()
    payslips = list(_my_payslips(emp)[:12])
    latest = payslips[0] if payslips else None
    docs = list(EmployeeDocument.objects.filter(employee=emp).exclude(status='DRAFT').defer('data'))
    doc_slots = []
    for code, label in EmployeeDocument.DOC_TYPES[:2]:            # Form 16 and Form 22 always shown
        have = [d for d in docs if d.doc_type == code]
        doc_slots.append({'code': code, 'label': label, 'docs': have})
    other_docs = [d for d in docs if d.doc_type not in ('FORM16', 'FORM22')]
    ctx = {
        'emp': emp,
        'latest': latest,
        'latest_period': _period(latest) if latest else '',
        'payslips': [{'line': p, 'period': _period(p), 'net': getattr(p, 'net_pay', None)} for p in payslips],
        'doc_slots': doc_slots,
        'other_docs': other_docs,
        'present_days': _safe_count('Attendance', employee=emp, date__year=today.year, date__month=today.month),
        'pending_leaves': _safe_count('LeaveRequest', employee=emp, status='PENDING'),
        'pending_claims': _safe_count('Reimbursement', employee=emp, status='PENDING'),
        'recent_leaves': _safe_list('LeaveRequest', order='-from_date', employee=emp),
        'recent_claims': _safe_list('Reimbursement', employee=emp),
        'bank_masked': _mask(getattr(emp, 'bank_account_no', '')),
        'today': today,
        'unread_announcements': Announcement.active_for(emp.company).exclude(reads__user=request.user).count() if emp.company_id else 0,
        'shift': shift_for(emp, today),
        'work': month_summary(emp, today.year, today.month),
        'pay_items': list(PayComponent.objects.filter(employee=emp, active=True)),
        'announcements': list(Announcement.active_for(emp.company)[:5]) if emp.company_id else [],
        'grievances': list(Grievance.objects.filter(employee=emp)[:5]),
        'loans': list(Loan.objects.filter(employee=emp)),
        'insurance': list(InsurancePolicy.objects.filter(employee=emp, status='ACTIVE')),
        'declarations': list(InvestmentDeclaration.objects.filter(employee=emp).order_by('-id')[:5]),
        'pan_masked': _mask(getattr(emp, 'pan_number', ''), keep=3),
    }
    return render(request, 'employee/home.html', ctx)


def emp_name(emp):
    full = getattr(emp, 'full_name', None)
    if callable(full):
        full = full()
    return full or f'{getattr(emp, "first_name", "")} {getattr(emp, "last_name", "")}'.strip() or str(emp)


def _esc(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _file_response(doc):
    resp = HttpResponse(bytes(doc.data), content_type=doc.content_type or 'application/octet-stream')
    name = re.sub(r'[^A-Za-z0-9._-]+', '_', doc.file_name or 'document')
    resp['Content-Disposition'] = f'attachment; filename="{name}"'
    return resp


@_employee_only
def document_download(request, pk):
    emp = _my_employee(request)
    if emp is None:
        raise Http404
    return _file_response(get_object_or_404(EmployeeDocument.objects.exclude(status='DRAFT'), pk=pk, employee=emp))   # own documents only


# ------------------------------------------------------------------ HR (company owner) pages
class DocumentUploadForm(forms.Form):
    employee = forms.ModelChoiceField(queryset=Employee.objects.none())
    doc_type = forms.ChoiceField(choices=EmployeeDocument.DOC_TYPES, label='Document type')
    financial_year = forms.CharField(max_length=9, label='Financial year', help_text='e.g. 2025-26')
    title = forms.CharField(max_length=150, required=False, help_text='Optional, e.g. "Form 16 Part B"')
    file = forms.FileField(help_text='PDF, JPG or PNG, up to 5 MB')

    def __init__(self, company, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['employee'].queryset = Employee.objects.filter(company=company).order_by('first_name')

    def clean_financial_year(self):
        fy = self.cleaned_data['financial_year'].strip()
        m = re.fullmatch(r'(\d{4})-(\d{2})', fy)
        if not m or (int(m.group(1)) + 1) % 100 != int(m.group(2)):
            raise forms.ValidationError('Use the format 2025-26.')
        return fy

    def clean_file(self):
        f = self.cleaned_data['file']
        ext = os.path.splitext(f.name)[1].lower()
        if ext not in ALLOWED_EXT:
            raise forms.ValidationError('Only PDF, JPG or PNG files are allowed.')
        if f.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError('File must be 5 MB or smaller.')
        return f


def _notify(user, message):
    if not user:
        return
    try:
        from .models import Notification
        fields = {f.name for f in Notification._meta.get_fields()}
        kw = {'recipient': user, 'message': message[:255]}
        if 'link' in fields:
            kw['link'] = '/me/'
        Notification.objects.create(**kw)
    except Exception:
        logger.exception('Could not create document notification')


@_owner_only
def hr_documents(request):
    company = request.user.company
    form = DocumentUploadForm(company, request.POST or None, request.FILES or None)
    if request.method == 'POST' and request.POST.get('delete'):
        doc = get_object_or_404(EmployeeDocument, pk=request.POST['delete'], company=company)
        doc.delete()
        messages.success(request, 'Document deleted.')
        return redirect('hr_documents')
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        f = d['file']
        ext = os.path.splitext(f.name)[1].lower()
        # One document per employee + type + year + title: uploading again replaces it.
        EmployeeDocument.objects.filter(company=company, employee=d['employee'], doc_type=d['doc_type'],
                                        financial_year=d['financial_year'], title=d['title']).delete()
        data = f.read()
        doc = EmployeeDocument.objects.create(
            company=company, employee=d['employee'], doc_type=d['doc_type'], financial_year=d['financial_year'],
            title=d['title'], file_name=f.name, content_type=ALLOWED_EXT[ext], data=data, size=len(data),
            uploaded_by=request.user,
        )
        _notify(d['employee'].user, f'Your {doc.label} for {doc.financial_year} is now available.')
        messages.success(request, f'{doc.label} ({doc.financial_year}) uploaded for {emp_name(d["employee"])}.')
        return redirect('hr_documents')
    docs = EmployeeDocument.objects.filter(company=company).select_related('employee').defer('data')
    return render(request, 'hr/tax_documents.html', {'form': form, 'docs': docs})


@_owner_only
def hr_document_download(request, pk):
    return _file_response(get_object_or_404(EmployeeDocument, pk=pk, company=request.user.company))


# ------------------------------------------------------------------ after login
def post_login(request):
    """Send employees to their own dashboard; everyone else keeps the existing behaviour."""
    if request.user.is_authenticated and _role(request.user) == 'EMPLOYEE':
        return redirect('employee_home')
    from . import views
    return views.post_login_redirect(request)