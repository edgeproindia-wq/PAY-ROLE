"""Phase-5 pages: Form 16 (Part B) draft generation, employee document uploads with
HR verification, document publish/verify/delete, unpaid-leave payroll hook."""
import datetime
import io
import os
import re
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .audit import log_action
from .models import CompanySettings, Employee, LeaveRequest, Notification, PayrollRunLine
from .models_documents import EmployeeDocument
from .payroll_engine import CURRENT_TAX_YEAR, TAX_RULES, annual_tax, parse_month
from .permissions import company_owner_required, effective_role, owner_or_employee_required

ALLOWED_EXT = {'.pdf': 'application/pdf', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png'}
MAX_BYTES = 5 * 1024 * 1024
PERSONAL_TYPES = [('ID_PROOF', 'ID proof'), ('ADDRESS', 'Address proof'), ('EDUCATION', 'Education certificate'),
                  ('EXPERIENCE', 'Experience letter'), ('OTHER', 'Other document')]


# ------------------------------------------------------------------ payroll hook: unpaid leave = loss of pay
def unpaid_leave_days(employee, start, end, absent_dates):
    """Approved UNPAID leave days inside the month, not already marked ABSENT."""
    days = Decimal('0')
    for lv in LeaveRequest.objects.filter(employee=employee, status='APPROVED', leave_type='UNPAID',
                                          from_date__lte=end, to_date__gte=start):
        d = max(lv.from_date, start)
        while d <= min(lv.to_date, end):
            if d not in absent_dates:
                days += 1
            d += datetime.timedelta(days=1)
    return days


# ------------------------------------------------------------------ Form 16 (Part B) draft
def fy_bounds(fy):
    m = re.fullmatch(r'(\d{4})-(\d{2})', fy or '')
    if not m or (int(m.group(1)) + 1) % 100 != int(m.group(2)):
        return None
    y = int(m.group(1))
    return datetime.date(y, 4, 1), datetime.date(y + 1, 3, 31)


def form16_figures(employee, fy):
    """Salary and TDS for the financial year from RELEASED/LOCKED payroll lines."""
    start, end = fy_bounds(fy)
    lines = []
    for line in PayrollRunLine.objects.filter(employee=employee, payroll_run__status__in=['RELEASED', 'LOCKED']).select_related('payroll_run'):
        period = parse_month(line.payroll_run.month)
        if period and start <= period[0] <= end:
            lines.append(line)
    gross = sum((l.total_earnings - l.reimbursements for l in lines), Decimal('0'))
    tds = sum((l.tds for l in lines), Decimal('0'))
    pt = sum((getattr(l, 'professional_tax', 0) or 0 for l in lines), Decimal('0'))
    year = fy if fy in TAX_RULES else CURRENT_TAX_YEAR      # rules for that year, if configured
    std = TAX_RULES[year]['standard_deduction']
    taxable = max(gross - std - pt, Decimal('0'))
    return {'months': len(lines), 'gross': gross, 'standard_deduction': std, 'professional_tax': pt,
            'taxable': taxable, 'tax_on_income': annual_tax(gross - pt, year), 'tds_deducted': tds, 'rules_year': year}


def form16_pdf(employee, fy, fig, tan):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    cs = CompanySettings.objects.filter(company=employee.company).first()
    esc = lambda t: str(t).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=f'Form 16 Part B {fy}')
    st = getSampleStyleSheet()
    story = [Paragraph('FORM 16 &ndash; PART B (DRAFT)', st['Title']),
             Paragraph('Salary and tax deducted statement generated from payroll. <b>DRAFT for verification by '
                       'your CA / tax professional. Part A must be downloaded from TRACES.</b>', st['Normal']), Spacer(1, 10)]
    info = [['Employer', esc(employee.company.name), 'Employer TAN', esc(tan or '-')],
            ['Employer PAN', esc(getattr(cs, 'pan_number', '') or '-'), 'Financial year', fy],
            ['Employee', esc(f'{employee.first_name} {employee.last_name}'.strip()), 'Employee PAN', esc(employee.pan_number or '-')],
            ['Employee code', esc(employee.employee_code), 'Assessment year', f'{int(fy[:4]) + 1}-{(int(fy[:4]) + 2) % 100:02d}']]
    t = Table(info, colWidths=[85, 170, 90, 150])
    t.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.4, colors.grey), ('FONTSIZE', (0, 0), (-1, -1), 9)]))
    rows = [['Particulars', 'Amount (Rs.)'],
            ['1. Gross salary (from payroll, excl. reimbursements)', f"{fig['gross']:,.2f}"],
            ['2. Less: standard deduction u/s 16(ia)', f"{fig['standard_deduction']:,.2f}"],
            ['3. Less: professional tax u/s 16(iii)', f"{fig['professional_tax']:,.2f}"],
            ['4. Income chargeable under "Salaries" (new regime)', f"{fig['taxable']:,.2f}"],
            ['5. Tax on total income incl. 4% cess (new regime)', f"{fig['tax_on_income']:,.2f}"],
            ['6. Tax deducted at source (TDS) during the year', f"{fig['tds_deducted']:,.2f}"],
            ['Months included', str(fig['months'])]]
    a = Table(rows, colWidths=[360, 135])
    a.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.4, colors.grey), ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                           ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eef0ff')), ('FONTSIZE', (0, 0), (-1, -1), 9)]))
    story += [t, Spacer(1, 12), a, Spacer(1, 12),
              Paragraph('Only salary paid through this payroll is included. Other income, exemptions and old-regime '
                        'deductions are not considered.', st['Italic'])]
    doc.build(story)
    return buf.getvalue()


class Form16Form(forms.Form):
    financial_year = forms.CharField(max_length=7, help_text='e.g. 2025-26')
    employee = forms.ModelChoiceField(queryset=Employee.objects.none(), required=False, help_text='Leave empty for all employees')
    tan = forms.CharField(max_length=10, required=False, label='Employer TAN')

    def __init__(self, company, *a, **kw):
        super().__init__(*a, **kw)
        self.fields['employee'].queryset = Employee.objects.filter(company=company)

    def clean_financial_year(self):
        fy = self.cleaned_data['financial_year'].strip()
        if not fy_bounds(fy):
            raise forms.ValidationError('Use the format 2025-26.')
        return fy


@company_owner_required
def hr_form16(request):
    company = request.user.company
    form = Form16Form(company, request.POST or None)
    for f in form.fields.values():
        f.widget.attrs.setdefault('class', 'form-control')
    if request.method == 'POST' and form.is_valid():
        fy = form.cleaned_data['financial_year']
        emps = [form.cleaned_data['employee']] if form.cleaned_data['employee'] else list(Employee.objects.filter(company=company))
        made = 0
        for emp in emps:
            fig = form16_figures(emp, fy)
            if not fig['months']:
                continue
            data = form16_pdf(emp, fy, fig, form.cleaned_data['tan'])
            EmployeeDocument.objects.filter(company=company, employee=emp, doc_type='FORM16', financial_year=fy,
                                            title='Form 16 Part B (draft)').delete()
            EmployeeDocument.objects.create(company=company, employee=emp, doc_type='FORM16', financial_year=fy,
                                            title='Form 16 Part B (draft)', file_name=f'Form16_PartB_{emp.employee_code}_{fy}.pdf',
                                            content_type='application/pdf', data=data, size=len(data), uploaded_by=request.user,
                                            status='DRAFT')
            made += 1
        log_action(request, 'CREATE', company, details=f'Form 16 Part B drafts generated for {fy} ({made})')
        messages.success(request, f'{made} Form 16 draft(s) generated. Review them, then publish to employees on Tax Documents.')
        return redirect('hr_documents')
    return render(request, 'features/hr_form16.html', {'form': form})


# ------------------------------------------------------------------ documents: employee upload, HR verify/publish/delete
class PersonalDocForm(forms.Form):
    doc_type = forms.ChoiceField(choices=PERSONAL_TYPES, label='Document type')
    title = forms.CharField(max_length=150, required=False)
    expiry_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    file = forms.FileField(help_text='PDF, JPG or PNG, up to 5 MB')

    def clean_file(self):
        f = self.cleaned_data['file']
        if os.path.splitext(f.name)[1].lower() not in ALLOWED_EXT:
            raise forms.ValidationError('Only PDF, JPG or PNG files are allowed.')
        if f.size > MAX_BYTES:
            raise forms.ValidationError('File must be 5 MB or smaller.')
        return f


@owner_or_employee_required
def my_document_upload(request):
    emp = getattr(request.user, 'employee_profile', None) if effective_role(request.user) == 'EMPLOYEE' else None
    if emp is None:
        raise PermissionDenied
    form = PersonalDocForm(request.POST or None, request.FILES or None)
    for f in form.fields.values():
        f.widget.attrs.setdefault('class', 'form-control')
    if request.method == 'POST' and form.is_valid():
        c = form.cleaned_data
        up = c['file']
        data = up.read()
        name = re.sub(r'[^A-Za-z0-9._-]+', '_', os.path.basename(up.name))[:150]
        doc = EmployeeDocument.objects.create(company=emp.company, employee=emp, doc_type=c['doc_type'],
                                              financial_year='-', title=c['title'], file_name=name,
                                              content_type=ALLOWED_EXT[os.path.splitext(name)[1].lower()], data=data,
                                              size=len(data), uploaded_by=request.user, status='PENDING',
                                              expiry_date=c['expiry_date'])
        for owner in emp.company.users.filter(role='COMPANY_OWNER', is_active=True):
            Notification.objects.create(recipient=owner, message=f'{emp} uploaded a document for verification', link='/hr/tax-documents/')
        log_action(request, 'CREATE', doc, details='Employee document uploaded')
        messages.success(request, 'Document uploaded. HR will verify it.')
        return redirect('employee_home')
    return render(request, 'features/my_document_upload.html', {'form': form})


@require_POST
@company_owner_required
def hr_document_action(request, pk):
    doc = get_object_or_404(EmployeeDocument, pk=pk, company=request.user.company)
    action = request.POST.get('action')
    if action == 'delete':
        doc.delete()
        messages.success(request, 'Document deleted.')
    elif action in ('verify', 'publish', 'reject'):
        doc.status = {'verify': 'VERIFIED', 'publish': 'VERIFIED', 'reject': 'REJECTED'}[action]
        doc.note = (request.POST.get('note') or '').strip()[:255]
        doc.save(update_fields=['status', 'note'])
        if doc.employee.user_id:
            Notification.objects.create(recipient=doc.employee.user,
                                        message=f'Your {doc.label} was {"rejected" if action == "reject" else "verified / published"}.',
                                        link='/me/')
        messages.success(request, 'Document updated.')
    log_action(request, 'UPDATE', doc if action != 'delete' else request.user.company, details=f'Document {action}')
    return redirect('hr_documents')