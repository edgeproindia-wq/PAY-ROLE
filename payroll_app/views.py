import csv
import json
import random
from decimal import Decimal

from django.conf import settings as dj_settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Sum, F, Q
from django.http import HttpResponse, Http404, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone

from .audit import log_action
from .forms import (
    EmployeeForm, SalaryStructureForm, AttendanceForm, LeaveRequestForm, LeaveDecisionForm,
    ReimbursementForm, PayrollRunForm, InvestmentDeclarationForm, ArrearsForm,
    FullFinalSettlementForm, UserRoleAssignmentForm, CompanySettingsForm,
    DemoRequestForm, CompanyRegistrationForm, ClientComplaintForm, ClientRequestForm,
    EmployeeAccountForm, EmployeeSelfRegisterForm,
)
from .models import (
    Employee, SalaryStructure, Attendance, LeaveRequest, Reimbursement,
    PayrollRun, PayrollRunLine, InvestmentDeclaration, Company, User,
    DemoRequest, ClientComplaint, ClientRequest, Notification, CompanySettings, AuditLog,
)
from .permissions import (
    role_required, admin_required, company_owner_required, owner_or_admin_required,
    any_authenticated_required, scope_employees, scope_by_employee_fk, get_user_company,
    get_object_scoped,
)

PAGE_SIZE = 10

# Explicit whitelist for the legacy generic template loader — prevents path
# traversal / arbitrary template disclosure via the URL.
GENERIC_PAGE_WHITELIST = {
    'Staffing module mobile ui mockup',
}


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

def generic_page(request, template_path):
    if template_path not in GENERIC_PAGE_WHITELIST:
        raise Http404("Page not found.")
    return render(request, f'{template_path}.html')


def home(request):
    """Public marketing page at '/'. Signed-in users are sent to their workspace."""
    if request.user.is_authenticated:
        return redirect('post_login_redirect')
    return render(request, 'public/landing.html')


def landing(request):
    return render(request, 'public/landing.html')


def send_registration_otp(request):
    """AJAX endpoint: step 1 of employee self-registration.

    Any email can register (no pre-existing Employee Master record needed) as
    long as it isn't already tied to a login account.
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'Invalid request method.'}, status=405)

    email = request.POST.get('email', '').strip().lower()
    if not email or '@' not in email:
        return JsonResponse({'ok': False, 'error': 'Enter a valid email address.'})

    if User.objects.filter(email__iexact=email).exists() or Employee.objects.filter(email__iexact=email, user__isnull=False).exists():
        return JsonResponse({'ok': False, 'error': 'An account already exists for this email. Try signing in instead.'})

    otp = f'{random.randint(0, 999999):06d}'
    request.session['reg_otp'] = otp
    request.session['reg_otp_email'] = email
    request.session['reg_otp_sent_at'] = timezone.now().isoformat()

    send_mail(
        subject='Your EDGEPRO Payroll verification code',
        message=f'Your one-time verification code is {otp}. It is valid for 10 minutes.',
        from_email=getattr(dj_settings, 'DEFAULT_FROM_EMAIL', 'no-reply@edgepro-payroll.local'),
        recipient_list=[email],
        fail_silently=True,
    )
    return JsonResponse({'ok': True, 'message': f'A verification code has been sent to {email}.'})


def _next_employee_code():
    """Generate a unique EMPnnnn code, tolerant of gaps/races."""
    n = Employee.objects.count() + 1
    while True:
        code = f'EMP{n:04d}'
        if not Employee.objects.filter(employee_code=code).exists():
            return code
        n += 1


def employee_register(request):
    """Step 2+3 of employee self-registration: OTP-verified signup, pending admin approval.

    Creates a new Employee (unassigned to any company yet) + a login account
    together — no pre-existing Employee Master record is required. An admin
    assigns the employee to a company and approves the account afterwards.
    """
    otp_verified_email = request.session.get('reg_otp_email') or ''

    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        otp_entered = request.POST.get('otp', '').strip()
        session_otp = request.session.get('reg_otp')
        session_email = request.session.get('reg_otp_email')

        if not session_otp or not email or email != session_email or otp_entered != session_otp:
            messages.error(request, 'OTP verification failed. Request a new code and try again.')
            form = EmployeeSelfRegisterForm(request.POST)
            return render(request, 'public/employee_register.html', {'form': form, 'email': email})

        form = EmployeeSelfRegisterForm(request.POST)
        if User.objects.filter(email__iexact=email).exists():
            messages.error(request, 'An account already exists for this email. Try signing in instead.')
        elif form.is_valid():
            name_parts = form.cleaned_data['full_name'].strip().split(' ', 1)
            first_name = name_parts[0]
            last_name = name_parts[1] if len(name_parts) > 1 else ''
            with transaction.atomic():
                user = User.objects.create_user(
                    username=form.cleaned_data['username'],
                    password=form.cleaned_data['password1'],
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    role='EMPLOYEE',
                    is_active=False,  # cannot sign in until an admin approves
                )
                employee = Employee.objects.create(
                    employee_code=_next_employee_code(),
                    first_name=first_name,
                    last_name=last_name,
                    email=email,
                    phone=form.cleaned_data['mobile_number'],
                    date_of_joining=timezone.now().date(),
                    department='Unassigned',
                    designation='Unassigned',
                    company=None,
                    user=user,
                )
                log_action(request, 'CREATE', employee, details='Employee self-registered via OTP, pending admin approval')

            request.session.pop('reg_otp', None)
            request.session.pop('reg_otp_email', None)
            request.session.pop('reg_otp_sent_at', None)
            messages.success(request, 'Account created. An admin will approve it shortly — you can sign in once approved.')
            return redirect('login')
        return render(request, 'public/employee_register.html', {'form': form, 'email': email})

    form = EmployeeSelfRegisterForm()
    return render(request, 'public/employee_register.html', {'form': form, 'email': otp_verified_email})


def request_demo(request):
    if request.method == 'POST':
        form = DemoRequestForm(request.POST)
        if form.is_valid():
            demo = form.save()
            log_action(request, 'CREATE', demo, details='Public demo request submitted')
            messages.success(request, "Thanks! Your demo request has been received — our team will reach out shortly.")
            return redirect('request_demo')
    else:
        form = DemoRequestForm()
    return render(request, 'public/request_demo.html', {'form': form})


def company_register(request):
    if request.method == 'POST':
        form = CompanyRegistrationForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            with transaction.atomic():
                company = Company.objects.create(
                    name=data['company_name'],
                    contact_email=data['contact_email'],
                    contact_phone=data['contact_phone'],
                    address=data['address'],
                    status='PENDING_APPROVAL',
                )
                owner = User.objects.create_user(
                    username=data['username'],
                    email=data['contact_email'],
                    password=data['password1'],
                    role='COMPANY_OWNER',
                    company=company,
                    is_active=False,  # cannot log in until the company is approved
                )
                log_action(request, 'REGISTER', company, details=f'Company self-registered, owner={owner.username}')
            messages.success(
                request,
                "Registration submitted. Your account will be activated once our team approves your company."
            )
            return redirect('login')
    else:
        form = CompanyRegistrationForm()
    return render(request, 'public/company_register.html', {'form': form})


@login_required
def post_login_redirect(request):
    user = request.user
    log_action(request, 'LOGIN', details='User logged in')
    if user.is_superuser or user.role == 'ADMIN':
        return redirect('admin_dashboard')
    return redirect('dashboard')


def logout_view(request):
    if request.user.is_authenticated:
        log_action(request, 'LOGOUT', details='User logged out')
    logout(request)
    return redirect('login')


# ---------------------------------------------------------------------------
# Admin panel — platform administrator only
# ---------------------------------------------------------------------------

@admin_required
def admin_dashboard(request):
    context = {
        'pending_demo_requests': DemoRequest.objects.filter(status='PENDING').count(),
        'pending_company_approvals': Company.objects.filter(status='PENDING_APPROVAL').count(),
        'open_complaints': ClientComplaint.objects.filter(status__in=['OPEN', 'IN_PROGRESS']).count(),
        'pending_client_requests': ClientRequest.objects.filter(status='PENDING').count(),
        'total_companies': Company.objects.filter(status='APPROVED').count(),
        'total_employees': Employee.objects.count(),
        'recent_demo_requests': DemoRequest.objects.all()[:5],
        'recent_companies': Company.objects.all()[:5],
    }
    return render(request, 'admin_panel/dashboard.html', context)


@admin_required
def admin_demo_requests(request):
    demos = DemoRequest.objects.all()
    paginator = Paginator(demos, PAGE_SIZE)
    page = paginator.get_page(request.GET.get('page'))
    return render(request, 'admin_panel/demo_requests.html', {'demos': page})


@admin_required
def admin_demo_request_decide(request, pk):
    demo = get_object_or_404(DemoRequest, pk=pk)
    if request.method == 'POST':
        decision = request.POST.get('decision')
        if decision in ('APPROVED', 'REJECTED', 'CONTACTED'):
            from django.utils import timezone
            demo.status = decision
            demo.admin_notes = request.POST.get('admin_notes', '')[:500]
            demo.reviewed_at = timezone.now()
            demo.save()
            log_action(request, 'APPROVE' if decision == 'APPROVED' else 'REJECT', demo,
                       details=f'Demo request marked {decision}')
            messages.success(request, f"Demo request for {demo.company_name} marked {decision.title()}.")
    return redirect('admin_demo_requests')


@admin_required
def admin_company_approvals(request):
    companies = Company.objects.filter(status='PENDING_APPROVAL')
    return render(request, 'admin_panel/company_approvals.html', {'companies': companies})


@admin_required
def admin_company_decide(request, pk):
    company = get_object_or_404(Company, pk=pk)
    if request.method == 'POST':
        decision = request.POST.get('decision')
        if decision == 'APPROVED':
            from django.utils import timezone
            company.status = 'APPROVED'
            company.approved_at = timezone.now()
            company.save()
            User.objects.filter(company=company, role='COMPANY_OWNER').update(is_active=True)
            log_action(request, 'APPROVE', company, details='Company registration approved')
            messages.success(request, f"{company.name} approved. The owner's account is now active.")
        elif decision == 'REJECTED':
            company.status = 'REJECTED'
            company.rejection_reason = request.POST.get('reason', '')[:500]
            company.save()
            log_action(request, 'REJECT', company, details='Company registration rejected')
            messages.success(request, f"{company.name} rejected.")
    return redirect('admin_company_approvals')


@admin_required
def admin_company_list(request):
    companies = Company.objects.all()
    paginator = Paginator(companies, PAGE_SIZE)
    page = paginator.get_page(request.GET.get('page'))
    return render(request, 'admin_panel/company_list.html', {'companies': page})


@admin_required
def admin_client_complaints(request):
    complaints = ClientComplaint.objects.select_related('company', 'raised_by').all()
    paginator = Paginator(complaints, PAGE_SIZE)
    page = paginator.get_page(request.GET.get('page'))
    return render(request, 'admin_panel/client_complaints.html', {'complaints': page})


@admin_required
def admin_complaint_respond(request, pk):
    complaint = get_object_or_404(ClientComplaint, pk=pk)
    if request.method == 'POST':
        from django.utils import timezone
        complaint.admin_response = request.POST.get('admin_response', '')[:2000]
        new_status = request.POST.get('status')
        if new_status in dict(ClientComplaint.STATUS_CHOICES):
            complaint.status = new_status
            if new_status in ('RESOLVED', 'CLOSED'):
                complaint.resolved_at = timezone.now()
        complaint.save()
        log_action(request, 'UPDATE', complaint, details=f'Complaint updated to {complaint.status}')
        messages.success(request, "Complaint updated.")
    return redirect('admin_client_complaints')


@admin_required
def admin_client_requests(request):
    reqs = ClientRequest.objects.select_related('company', 'raised_by').all()
    paginator = Paginator(reqs, PAGE_SIZE)
    page = paginator.get_page(request.GET.get('page'))
    return render(request, 'admin_panel/client_requests.html', {'requests': page})


@admin_required
def admin_request_respond(request, pk):
    client_request = get_object_or_404(ClientRequest, pk=pk)
    if request.method == 'POST':
        client_request.admin_response = request.POST.get('admin_response', '')[:2000]
        new_status = request.POST.get('status')
        if new_status in dict(ClientRequest.STATUS_CHOICES):
            client_request.status = new_status
        client_request.save()
        log_action(request, 'UPDATE', client_request, details=f'Client request updated to {client_request.status}')
        messages.success(request, "Request updated.")
    return redirect('admin_client_requests')


@admin_required
def admin_audit_log(request):
    logs = AuditLog.objects.select_related('actor', 'company').all()[:500]
    return render(request, 'admin_panel/audit_log.html', {'logs': logs})


# ---------------------------------------------------------------------------
# Company owner workflows
# ---------------------------------------------------------------------------

@company_owner_required
def client_complaints(request):
    company = request.user.company
    if request.method == 'POST':
        form = ClientComplaintForm(request.POST)
        if form.is_valid():
            complaint = form.save(commit=False)
            complaint.company = company
            complaint.raised_by = request.user
            complaint.save()
            log_action(request, 'CREATE', complaint, details='Client complaint raised')
            messages.success(request, "Your complaint has been submitted to the admin team.")
            return redirect('client_complaints')
    else:
        form = ClientComplaintForm()
    complaints = ClientComplaint.objects.filter(company=company)
    return render(request, 'client_panel/complaints.html', {'form': form, 'complaints': complaints})


@company_owner_required
def client_requests(request):
    company = request.user.company
    if request.method == 'POST':
        form = ClientRequestForm(request.POST)
        if form.is_valid():
            req = form.save(commit=False)
            req.company = company
            req.raised_by = request.user
            req.save()
            log_action(request, 'CREATE', req, details='Client request raised')
            messages.success(request, "Your request has been submitted to the admin team.")
            return redirect('client_requests')
    else:
        form = ClientRequestForm()
    reqs = ClientRequest.objects.filter(company=company)
    return render(request, 'client_panel/requests.html', {'form': form, 'requests': reqs})


@owner_or_admin_required
def employee_create_account(request, pk):
    employee = get_object_scoped(request, Employee, employee_field='', pk=pk)
    if employee.user_id:
        messages.error(request, "This employee already has a login account.")
        return redirect('employee_master')
    if request.method == 'POST':
        form = EmployeeAccountForm(request.POST)
        if form.is_valid():
            user = User.objects.create_user(
                username=form.cleaned_data['username'],
                password=form.cleaned_data['password1'],
                role='EMPLOYEE',
                company=employee.company,
                email=employee.email,
                first_name=employee.first_name,
                last_name=employee.last_name,
            )
            employee.user = user
            employee.save(update_fields=['user'])
            log_action(request, 'CREATE', employee, details=f'ESS login created for {employee.employee_code}')
            messages.success(request, f"Login account created for {employee.full_name}.")
            return redirect('employee_master')
    else:
        form = EmployeeAccountForm()
    return render(request, 'client_panel/employee_account_form.html', {'form': form, 'employee': employee})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@any_authenticated_required
def dashboard(request):
    employees = scope_employees(request, Employee.objects.all())
    dept_data = employees.filter(employment_status='ACTIVE').values('department').annotate(c=Count('id')).order_by('-c')
    dept_labels = json.dumps([d['department'] or 'Unassigned' for d in dept_data])
    dept_values = json.dumps([d['c'] for d in dept_data])
    status_data = employees.values('employment_status').annotate(c=Count('id'))
    status_map = {'ACTIVE': 'Active', 'ON_LEAVE': 'On Leave', 'RESIGNED': 'Resigned', 'TERMINATED': 'Terminated'}
    status_labels = json.dumps([status_map.get(s['employment_status'], s['employment_status']) for s in status_data])
    status_values = json.dumps([s['c'] for s in status_data])

    runs = PayrollRun.objects.all()
    if request.user.role != 'ADMIN' and not request.user.is_superuser:
        runs = runs.filter(lines__employee__in=employees).distinct()
    run_status_data = runs.values('status').annotate(c=Count('id'))
    run_status_map = {'DRAFT': 'Draft', 'VALIDATED': 'Validated', 'APPROVED': 'Approved', 'RELEASED': 'Released'}
    run_labels = json.dumps([run_status_map.get(r['status'], r['status']) for r in run_status_data])
    run_values = json.dumps([r['c'] for r in run_status_data])

    salary_generated_count = runs.filter(lines__isnull=False).distinct().count()
    latest_payroll_run = runs.first()
    latest_run_net_pay = 0
    latest_run_employee_count = 0
    if latest_payroll_run:
        run_lines = latest_payroll_run.lines.filter(employee__in=employees)
        latest_run_net_pay = sum(l.net_pay for l in run_lines)
        latest_run_employee_count = run_lines.count()

    leaves = scope_by_employee_fk(request, LeaveRequest.objects.select_related('employee'))
    reimbursements = scope_by_employee_fk(request, Reimbursement.objects.select_related('employee'))
    declarations = scope_by_employee_fk(request, InvestmentDeclaration.objects.all())

    context = {
        'dept_labels': dept_labels, 'dept_values': dept_values,
        'status_labels': status_labels, 'status_values': status_values,
        'run_labels': run_labels, 'run_values': run_values,
        'total_employees': employees.filter(employment_status='ACTIVE').count(),
        'total_salary_structures': SalaryStructure.objects.filter(employee__in=employees).count(),
        'salary_generated_count': salary_generated_count,
        'pending_leave': leaves.filter(status='PENDING').count(),
        'pending_reimbursements': reimbursements.filter(status='PENDING').count(),
        'latest_payroll_run': latest_payroll_run,
        'latest_run_net_pay': latest_run_net_pay,
        'latest_run_employee_count': latest_run_employee_count,
        'pending_investment_declarations': declarations.filter(is_verified=False).count(),
        'pending_approvals': runs.filter(status='VALIDATED').count(),
        'recent_leaves': leaves.order_by('-id')[:5],
        'recent_reimbursements': reimbursements.order_by('-id')[:5],
    }
    return render(request, 'Dashboard.html', context)


# ---------------------------------------------------------------------------
# Employee master
# ---------------------------------------------------------------------------

@owner_or_admin_required
def employee_master_export_csv(request):
    employees = scope_employees(request, Employee.objects.all())
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="employees.csv"'
    writer = csv.writer(response)
    writer.writerow(['Code', 'Name', 'Email', 'Department', 'Designation', 'Status', 'Date of Joining'])
    for emp in employees:
        writer.writerow([emp.employee_code, emp.full_name, emp.email, emp.department, emp.designation, emp.get_employment_status_display(), emp.date_of_joining])
    log_action(request, 'OTHER', details='Exported employee master CSV')
    return response


@owner_or_admin_required
def employee_master(request):
    company = get_user_company(request)
    base_qs = scope_employees(request, Employee.objects.all())
    last_employee = base_qs.order_by('-id').first()
    next_number = 1
    if last_employee and last_employee.employee_code:
        digits = ''.join(filter(str.isdigit, last_employee.employee_code))
        if digits:
            next_number = int(digits) + 1
    number_options = [str(n).zfill(3) for n in range(next_number, next_number + 20)]

    if request.method == 'POST':
        if request.user.role == 'ADMIN' and not company:
            messages.error(request, "An admin cannot create employees without selecting a company context.")
            return redirect('employee_master')
        post_data = request.POST.copy()
        prefix = post_data.get('code_prefix', 'EPRO')
        number = post_data.get('employee_code', '')
        post_data['employee_code'] = f'{prefix}{number}'
        form = EmployeeForm(post_data)
        if form.is_valid():
            if Employee.objects.filter(company=company, employee_code=post_data['employee_code']).exists():
                form.add_error(None, f"Employee code {post_data['employee_code']} is already used in this company.")
            else:
                employee = form.save(commit=False)
                employee.company = company
                employee.save()
                log_action(request, 'CREATE', employee, details='Employee created')
                return redirect('employee_master')
    else:
        form = EmployeeForm()

    all_employees = base_qs
    existing_codes = base_qs.values_list('employee_code', flat=True).order_by('employee_code')
    paginator = Paginator(all_employees, PAGE_SIZE)
    employees = paginator.get_page(request.GET.get('page'))
    return render(request, 'Employee Master.html', {
        'employees': employees, 'form': form, 'existing_codes': existing_codes, 'number_options': number_options,
    })


@owner_or_admin_required
def employee_edit(request, pk):
    employee = get_object_scoped(request, Employee, employee_field='', pk=pk)
    if request.method == 'POST':
        form = EmployeeForm(request.POST, instance=employee)
        if form.is_valid():
            form.save()
            log_action(request, 'UPDATE', employee, details='Employee updated')
            return redirect('employee_master')
    else:
        form = EmployeeForm(instance=employee)
    return render(request, 'Employee Master.html', {
        'employees': scope_employees(request, Employee.objects.all()), 'form': form, 'edit_employee': employee,
    })


@owner_or_admin_required
def employee_delete(request, pk):
    employee = get_object_scoped(request, Employee, employee_field='', pk=pk)
    if request.method != 'POST':
        raise PermissionDenied("Delete requires a POST request.")
    log_action(request, 'DELETE', employee, details=f'Employee {employee.employee_code} deleted')
    employee.delete()
    return redirect('employee_master')


# ---------------------------------------------------------------------------
# Salary structure
# ---------------------------------------------------------------------------

@owner_or_admin_required
def salary_structure(request):
    employees = scope_employees(request, Employee.objects.all())
    if request.method == 'POST':
        form = SalaryStructureForm(request.POST, employee_queryset=employees)
        if form.is_valid():
            ss = form.save()
            log_action(request, 'CREATE', ss, details='Salary structure saved')
            return redirect('salary_structure')
    else:
        form = SalaryStructureForm(employee_queryset=employees)

    all_structures = SalaryStructure.objects.filter(employee__in=employees).select_related('employee').order_by('employee__employee_code')
    paginator = Paginator(all_structures, PAGE_SIZE)
    structures = paginator.get_page(request.GET.get('page'))
    chart_qs = all_structures[:15]
    chart_labels = json.dumps([s.employee.full_name for s in chart_qs])
    chart_basic = json.dumps([float(s.basic) for s in chart_qs])
    chart_hra = json.dumps([float(s.hra) for s in chart_qs])
    return render(request, 'Salary Structure.html', {
        'form': form, 'structures': structures,
        'chart_labels': chart_labels, 'chart_basic': chart_basic, 'chart_hra': chart_hra,
    })


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------

@any_authenticated_required
def attendance(request):
    employees = scope_employees(request, Employee.objects.all())
    records_qs = scope_by_employee_fk(request, Attendance.objects.select_related('employee'))

    if request.method == 'POST':
        if request.user.role == 'EMPLOYEE':
            messages.error(request, "Employees cannot mark their own attendance manually.")
            return redirect('attendance')
        form = AttendanceForm(request.POST, employee_queryset=employees)
        if form.is_valid():
            record = form.save()
            log_action(request, 'CREATE', record, details='Attendance recorded')
            return redirect('attendance')
    else:
        form = AttendanceForm(employee_queryset=employees)

    paginator = Paginator(records_qs, PAGE_SIZE)
    records = paginator.get_page(request.GET.get('page'))
    status_counts = records_qs.values('status').annotate(c=Count('id'))
    status_map = {'PRESENT': 'Present', 'ABSENT': 'Absent', 'HALF_DAY': 'Half Day', 'LEAVE': 'On Leave'}
    chart_labels = json.dumps([status_map.get(s['status'], s['status']) for s in status_counts])
    chart_values = json.dumps([s['c'] for s in status_counts])
    return render(request, 'Attendance.html', {
        'form': form, 'records': records, 'chart_labels': chart_labels, 'chart_values': chart_values,
    })


# ---------------------------------------------------------------------------
# Leave management (with approval workflow)
# ---------------------------------------------------------------------------

@any_authenticated_required
def leave_management(request):
    employees = scope_employees(request, Employee.objects.all())
    leave_qs = scope_by_employee_fk(request, LeaveRequest.objects.select_related('employee'))

    if request.method == 'POST':
        form = LeaveRequestForm(request.POST, employee_queryset=employees)
        if form.is_valid():
            leave = form.save(commit=False)
            if request.user.role == 'EMPLOYEE':
                # Employees may only file leave for themselves — never trust the client's choice.
                own_employee = getattr(request.user, 'employee_profile', None)
                if own_employee is None:
                    raise PermissionDenied("No employee profile linked to this account.")
                leave.employee = own_employee
            leave.save()
            log_action(request, 'CREATE', leave, details='Leave request submitted')
            return redirect('leave_management')
    else:
        form = LeaveRequestForm(employee_queryset=employees)

    paginator = Paginator(leave_qs, PAGE_SIZE)
    leave_requests = paginator.get_page(request.GET.get('page'))
    type_counts = leave_qs.values('leave_type').annotate(c=Count('id'))
    type_map = dict(LeaveRequest.LEAVE_TYPE_CHOICES)
    chart_labels = json.dumps([type_map.get(t['leave_type'], t['leave_type']) for t in type_counts])
    chart_values = json.dumps([t['c'] for t in type_counts])
    return render(request, 'Leave Management.html', {
        'form': form, 'leave_requests': leave_requests, 'chart_labels': chart_labels, 'chart_values': chart_values,
        'decision_form': LeaveDecisionForm(),
    })


@owner_or_admin_required
def leave_decision(request, pk):
    leave = get_object_scoped(request, LeaveRequest, employee_field='employee', pk=pk)
    if request.method == 'POST':
        form = LeaveDecisionForm(request.POST)
        if form.is_valid():
            leave.status = form.cleaned_data['decision']
            leave.save()
            log_action(request, 'APPROVE' if leave.status == 'APPROVED' else 'REJECT', leave,
                       details=f'Leave request {leave.status}')
            messages.success(request, f"Leave request {leave.status.lower()}.")
    return redirect('leave_management')


# ---------------------------------------------------------------------------
# Reimbursement (with approval workflow)
# ---------------------------------------------------------------------------

@any_authenticated_required
def reimbursement(request):
    employees = scope_employees(request, Employee.objects.all())
    reimb_qs = scope_by_employee_fk(request, Reimbursement.objects.select_related('employee'))

    if request.method == 'POST':
        form = ReimbursementForm(request.POST, employee_queryset=employees)
        if form.is_valid():
            reimb = form.save(commit=False)
            if request.user.role == 'EMPLOYEE':
                own_employee = getattr(request.user, 'employee_profile', None)
                if own_employee is None:
                    raise PermissionDenied("No employee profile linked to this account.")
                reimb.employee = own_employee
            reimb.save()
            log_action(request, 'CREATE', reimb, details='Reimbursement claim submitted')
            return redirect('reimbursement')
    else:
        form = ReimbursementForm(employee_queryset=employees)

    paginator = Paginator(reimb_qs, PAGE_SIZE)
    reimbursements = paginator.get_page(request.GET.get('page'))
    return render(request, 'Reimbursement.html', {
        'form': form, 'reimbursements': reimbursements, 'decision_form': LeaveDecisionForm(),
    })


@owner_or_admin_required
def reimbursement_decision(request, pk):
    reimb = get_object_scoped(request, Reimbursement, employee_field='employee', pk=pk)
    if request.method == 'POST':
        form = LeaveDecisionForm(request.POST)
        if form.is_valid():
            reimb.status = form.cleaned_data['decision']
            reimb.save()
            log_action(request, 'APPROVE' if reimb.status == 'APPROVED' else 'REJECT', reimb,
                       details=f'Reimbursement {reimb.status}')
            messages.success(request, f"Reimbursement claim {reimb.status.lower()}.")
    return redirect('reimbursement')


# ---------------------------------------------------------------------------
# Statutory / tax reports (owner+admin only — company financial data)
# ---------------------------------------------------------------------------

@owner_or_admin_required
def statutory_compliance(request):
    employees = scope_employees(request, Employee.objects.all())
    rows = []
    for ss in SalaryStructure.objects.filter(employee__in=employees).select_related('employee'):
        pf = ss.basic * Decimal('0.12')
        esi = ss.gross_salary * Decimal('0.0075') if ss.gross_salary <= 21000 else Decimal('0')
        rows.append({'employee': ss.employee, 'basic': ss.basic, 'gross': ss.gross_salary, 'pf': round(pf, 2), 'esi': round(esi, 2)})
    chart_labels = json.dumps([r['employee'].full_name for r in rows])
    chart_pf = json.dumps([float(r['pf']) for r in rows])
    chart_esi = json.dumps([float(r['esi']) for r in rows])
    return render(request, 'Tax and Compliance/Statutory Compliance.html', {'rows': rows, 'chart_labels': chart_labels, 'chart_pf': chart_pf, 'chart_esi': chart_esi})


@any_authenticated_required
def investment_declaration(request):
    employees = scope_employees(request, Employee.objects.all())
    if request.method == 'POST':
        form = InvestmentDeclarationForm(request.POST, request.FILES, employee_queryset=employees)
        if form.is_valid():
            decl = form.save(commit=False)
            if request.user.role == 'EMPLOYEE':
                own_employee = getattr(request.user, 'employee_profile', None)
                if own_employee is None:
                    raise PermissionDenied("No employee profile linked to this account.")
                decl.employee = own_employee
            decl.save()
            log_action(request, 'CREATE', decl, details='Investment declaration submitted')
            return redirect('investment_declaration')
    else:
        form = InvestmentDeclarationForm(employee_queryset=employees)
    declarations = scope_by_employee_fk(request, InvestmentDeclaration.objects.select_related('employee'))
    return render(request, 'Income Tax Management/Investment declartion.html', {'form': form, 'declarations': declarations})


@owner_or_admin_required
def income_tax(request):
    employees = scope_employees(request, Employee.objects.all())
    rows = []
    for ss in SalaryStructure.objects.filter(employee__in=employees).select_related('employee'):
        annual_gross = ss.gross_salary * Decimal('12')
        if annual_gross <= 300000:
            tds_annual = Decimal('0')
        elif annual_gross <= 700000:
            tds_annual = (annual_gross - Decimal('300000')) * Decimal('0.05')
        else:
            tds_annual = Decimal('400000') * Decimal('0.05') + (annual_gross - Decimal('700000')) * Decimal('0.10')
        rows.append({'employee': ss.employee, 'annual_gross': round(annual_gross, 2), 'tds_annual': round(tds_annual, 2), 'tds_monthly': round(tds_annual / 12, 2)})
    chart_labels = json.dumps([r['employee'].full_name for r in rows])
    chart_values = json.dumps([float(r['tds_monthly']) for r in rows])
    return render(request, 'Tax and Compliance/Income Tax.html', {'rows': rows, 'chart_labels': chart_labels, 'chart_values': chart_values})


@owner_or_admin_required
def compliance_reports(request):
    employees = scope_employees(request, Employee.objects.all())
    structures = SalaryStructure.objects.filter(employee__in=employees)
    total_pf = sum((s.basic * Decimal('0.12') for s in structures), Decimal('0'))
    total_esi = sum((s.gross_salary * Decimal('0.0075') for s in structures if s.gross_salary <= 21000), Decimal('0'))
    total_gross = sum((s.gross_salary for s in structures), Decimal('0'))
    chart_labels = json.dumps(['PF', 'ESI', 'Gross'])
    chart_values = json.dumps([float(round(total_pf, 2)), float(round(total_esi, 2)), float(round(total_gross, 2))])
    return render(request, 'Tax and Compliance/Compliance Reports.html', {
        'total_pf': round(total_pf, 2), 'total_esi': round(total_esi, 2), 'total_gross': round(total_gross, 2), 'employee_count': structures.count(),
        'chart_labels': chart_labels, 'chart_values': chart_values,
    })


# ---------------------------------------------------------------------------
# Reports (owner+admin — company-scoped; admin sees global data)
# ---------------------------------------------------------------------------

@owner_or_admin_required
def total_employees_report(request):
    employees = scope_employees(request, Employee.objects.all())
    dept_summary_qs = employees.values('department').annotate(
        total=Count('id'), active=Count('id', filter=Q(employment_status='ACTIVE'))
    ).order_by('department')
    dept_summary = [{'department': row['department'] or '(No Department)', 'total': row['total'], 'active': row['active']} for row in dept_summary_qs]
    chart_labels = json.dumps([d['department'] for d in dept_summary])
    chart_values = json.dumps([d['total'] for d in dept_summary])
    context = {
        'total': employees.count(),
        'active': employees.filter(employment_status='ACTIVE').count(),
        'on_leave': employees.filter(employment_status='ON_LEAVE').count(),
        'resigned': employees.filter(employment_status='RESIGNED').count(),
        'dept_summary': dept_summary, 'employees': employees,
        'chart_labels': chart_labels, 'chart_values': chart_values,
    }
    return render(request, 'Payroll/Total Employees.html', context)


@owner_or_admin_required
def new_joiners_report(request):
    from datetime import date, timedelta
    employees = scope_employees(request, Employee.objects.all())
    cutoff = date.today() - timedelta(days=90)
    employees = employees.filter(date_of_joining__gte=cutoff).order_by('-date_of_joining')
    return render(request, 'Payroll/New Joiners.html', {'employees': employees, 'cutoff': cutoff})


@owner_or_admin_required
def payroll_cost_report(request):
    employees = scope_employees(request, Employee.objects.all())
    dept_costs_qs = SalaryStructure.objects.filter(employee__in=employees).values('employee__department').annotate(
        employee_count=Count('id'),
        total_cost=Sum(F('basic') + F('hra') + F('conveyance') + F('special_allowance'))
    ).order_by('employee__department')
    dept_costs = [{'department': row['employee__department'], 'employee_count': row['employee_count'], 'total_cost': row['total_cost'] or 0} for row in dept_costs_qs]
    overall_total = sum(d['total_cost'] for d in dept_costs)
    chart_labels = json.dumps([d['department'] for d in dept_costs])
    chart_values = json.dumps([float(d['total_cost']) for d in dept_costs])
    return render(request, 'Payroll/Payroll Cost.html', {'dept_costs': dept_costs, 'overall_total': overall_total, 'chart_labels': chart_labels, 'chart_values': chart_values})


@owner_or_admin_required
def pending_payroll_report(request):
    employees = scope_employees(request, Employee.objects.all())
    pending_runs = PayrollRun.objects.exclude(status='RELEASED').filter(
        Q(lines__employee__in=employees) | Q(lines__isnull=True)
    ).distinct().prefetch_related('lines')
    return render(request, 'Payroll/Pending Payroll.html', {'pending_runs': pending_runs})


@owner_or_admin_required
def employees_on_leave_report(request):
    from datetime import date
    today = date.today()
    leave_qs = scope_by_employee_fk(request, LeaveRequest.objects.select_related('employee'))
    on_leave = leave_qs.filter(status='APPROVED', from_date__lte=today, to_date__gte=today)
    return render(request, 'Payroll/Employees On Leave.html', {'on_leave': on_leave, 'today': today})


# ---------------------------------------------------------------------------
# Payroll processing
# ---------------------------------------------------------------------------

@owner_or_admin_required
def payroll_run_download_docx(request, pk):
    from docx import Document
    run = get_object_or_404(PayrollRun, pk=pk)
    employees = scope_employees(request, Employee.objects.all())
    lines = run.lines.filter(employee__in=employees).select_related('employee')
    if not lines.exists() and run.lines.exists():
        raise PermissionDenied("This payroll run does not belong to your company.")
    doc = Document()
    doc.add_heading(f'Payroll Report - {run.month}', level=1)
    doc.add_paragraph(f'Status: {run.get_status_display()}')
    doc.add_paragraph(f'Created: {run.created_at.strftime("%d %b %Y")}')
    table = doc.add_table(rows=1, cols=5)
    table.style = 'Light Grid Accent 1'
    hdr_cells = table.rows[0].cells
    for i, text in enumerate(['Employee Code', 'Name', 'Basic', 'Gross Salary', 'Net Pay']):
        hdr_cells[i].text = text
    for line in lines:
        row_cells = table.add_row().cells
        row_cells[0].text = line.employee.employee_code
        row_cells[1].text = line.employee.full_name
        row_cells[2].text = str(line.basic)
        row_cells[3].text = str(line.gross_salary)
        row_cells[4].text = str(line.net_pay)
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    response['Content-Disposition'] = f'attachment; filename="Payroll_{run.month}.docx"'
    doc.save(response)
    log_action(request, 'OTHER', run, details='Downloaded payroll run as DOCX')
    return response


@owner_or_admin_required
def payroll_run_detail(request, pk):
    run = get_object_or_404(PayrollRun, pk=pk)
    employees = scope_employees(request, Employee.objects.all())
    lines = run.lines.filter(employee__in=employees).select_related('employee')
    if not lines.exists() and run.lines.exists():
        raise PermissionDenied("This payroll run does not belong to your company.")
    return render(request, 'Payroll/Payroll Run Detail.html', {'run': run, 'lines': lines})


@owner_or_admin_required
def payroll_combined(request):
    employees_qs = scope_employees(request, Employee.objects.filter(employment_status='ACTIVE'))

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            form = PayrollRunForm(request.POST)
            if form.is_valid():
                payroll_run = form.save()
                for emp in employees_qs:
                    try:
                        ss = emp.salary_structure
                        PayrollRunLine.objects.create(
                            payroll_run=payroll_run, employee=emp,
                            basic=ss.basic, gross_salary=ss.gross_salary, net_pay=ss.gross_salary,
                        )
                    except SalaryStructure.DoesNotExist:
                        pass
                log_action(request, 'PROCESS_PAYROLL', payroll_run, details='Payroll run created')
        elif action in ('validate', 'approve', 'release'):
            run_id = request.POST.get('run_id')
            run = get_object_or_404(PayrollRun, pk=run_id)
            _assert_run_in_scope(request, run, employees_qs)
            next_status = {'validate': 'VALIDATED', 'approve': 'APPROVED', 'release': 'RELEASED'}
            run.status = next_status[action]
            run.save()
            log_action(request, 'PROCESS_PAYROLL', run, details=f'Payroll run status -> {run.status}')
        elif action == 'lock':
            run = get_object_or_404(PayrollRun, pk=request.POST.get('run_id'))
            _assert_run_in_scope(request, run, employees_qs)
            run.is_locked = True
            run.save()
        elif action == 'unlock':
            run = get_object_or_404(PayrollRun, pk=request.POST.get('run_id'))
            _assert_run_in_scope(request, run, employees_qs)
            run.is_locked = False
            run.save()
        elif action == 'reject':
            run = get_object_or_404(PayrollRun, pk=request.POST.get('run_id'))
            _assert_run_in_scope(request, run, employees_qs)
            run.status = 'DRAFT'
            run.save()
            log_action(request, 'PROCESS_PAYROLL', run, details='Payroll run rejected back to draft')
        elif action == 'reprocess':
            run = get_object_or_404(PayrollRun, pk=request.POST.get('run_id'))
            _assert_run_in_scope(request, run, employees_qs)
            if run.status == 'RELEASED':
                messages.error(request, f"Cannot reprocess '{run.month}' - it has already been released.")
            elif run.is_locked:
                messages.error(request, f"Cannot reprocess '{run.month}' - it is locked. Unlock it first.")
            else:
                run.lines.filter(employee__in=employees_qs).delete()
                for emp in employees_qs:
                    try:
                        ss = emp.salary_structure
                        PayrollRunLine.objects.create(
                            payroll_run=run, employee=emp,
                            basic=ss.basic, gross_salary=ss.gross_salary, net_pay=ss.gross_salary,
                        )
                    except SalaryStructure.DoesNotExist:
                        pass
                log_action(request, 'PROCESS_PAYROLL', run, details='Payroll run reprocessed')
                messages.success(request, f"'{run.month}' reprocessed successfully.")
        return redirect('payroll_combined')
    else:
        form = PayrollRunForm()

    all_runs = PayrollRun.objects.filter(
        Q(lines__employee__in=employees_qs) | Q(lines__isnull=True)
    ).distinct().prefetch_related('lines__employee')
    released_lines = PayrollRunLine.objects.filter(payroll_run__status='RELEASED', employee__in=employees_qs)
    summary = {
        'total_runs': all_runs.count(),
        'total_employees_paid': released_lines.count(),
        'total_net_payout': released_lines.aggregate(t=Sum('net_pay'))['t'] or 0,
        'locked_count': all_runs.filter(is_locked=True).count(),
    }
    paginator = Paginator(all_runs, PAGE_SIZE)
    payroll_runs = paginator.get_page(request.GET.get('page'))
    return render(request, 'Payroll/Payroll Combined.html', {'form': form, 'payroll_runs': payroll_runs, 'summary': summary})


def _assert_run_in_scope(request, run, employees_qs):
    """A payroll run with lines belongs to whichever company those employees
    belong to. Prevent a company owner from approving/releasing another
    company's payroll by guessing a run_id."""
    if request.user.role == 'ADMIN' or request.user.is_superuser:
        return
    if run.lines.exists() and not run.lines.filter(employee__in=employees_qs).exists():
        raise PermissionDenied("This payroll run does not belong to your company.")


# ---------------------------------------------------------------------------
# Arrears / Full & Final Settlement
# ---------------------------------------------------------------------------

@owner_or_admin_required
def arrears(request):
    from .models import ArrearsRecord
    employees = scope_employees(request, Employee.objects.all())
    if request.method == 'POST':
        form = ArrearsForm(request.POST, employee_queryset=employees)
        if form.is_valid():
            rec = form.save()
            log_action(request, 'CREATE', rec, details='Arrears record created')
            return redirect('arrears')
    else:
        form = ArrearsForm(employee_queryset=employees)
    records = ArrearsRecord.objects.filter(employee__in=employees).select_related('employee')
    return render(request, 'Adjustments/Arrears.html', {'form': form, 'records': records})


@owner_or_admin_required
def full_final_settlement(request):
    from .models import FullFinalSettlement
    employees = scope_employees(request, Employee.objects.all())
    if request.method == 'POST':
        form = FullFinalSettlementForm(request.POST, employee_queryset=employees)
        if form.is_valid():
            rec = form.save()
            log_action(request, 'CREATE', rec, details='Full & Final settlement recorded')
            return redirect('full_final_settlement')
    else:
        form = FullFinalSettlementForm(employee_queryset=employees)
    records = FullFinalSettlement.objects.filter(employee__in=employees).select_related('employee')
    return render(request, 'Adjustments/Full and Final Settlement.html', {'form': form, 'records': records})


# ---------------------------------------------------------------------------
# Payslips / bank transfer
# ---------------------------------------------------------------------------

@any_authenticated_required
def payslips(request):
    lines = scope_by_employee_fk(
        request, PayrollRunLine.objects.select_related('employee', 'payroll_run')
    ).filter(payroll_run__status='RELEASED')
    return render(request, 'Payslips.html', {'lines': lines})


@owner_or_admin_required
def bank_transfer(request):
    employees = scope_employees(request, Employee.objects.all())
    lines = PayrollRunLine.objects.filter(employee__in=employees, payroll_run__status='RELEASED').select_related('employee', 'payroll_run')

    if request.method == 'POST':
        target_ids = request.POST.getlist('line_id') or [request.POST.get('line_id')]
        target_ids = [i for i in target_ids if i]
        processed, skipped = 0, 0
        for line in lines.filter(pk__in=target_ids):
            if line.payment_status == 'SUCCESS':
                skipped += 1  # duplicate-payment prevention: never re-pay an already-successful line
                continue
            if not line.employee.bank_account_no or not line.employee.ifsc_code:
                line.payment_status = 'FAILED'
                line.failure_reason = 'Missing bank account number or IFSC code on employee record.'
            else:
                line.payment_status = 'SUCCESS'
                line.payment_reference = f"NEFT{line.payroll_run_id:04d}{line.pk:06d}"
                line.failure_reason = ''
            line.payment_attempted_at = timezone.now()
            line.save(update_fields=['payment_status', 'payment_reference', 'payment_attempted_at', 'failure_reason'])
            log_action(request, 'UPDATE', line, details=f'Bank transfer {line.payment_status} for {line.employee}')
            processed += 1
        if processed:
            messages.success(request, f'Processed {processed} transfer(s).' + (f' Skipped {skipped} already-paid line(s).' if skipped else ''))
        elif skipped:
            messages.info(request, 'Selected line(s) were already marked Success — not re-processed.')
        return redirect('bank_transfer')

    return render(request, 'Bank Transfer.html', {'lines': lines})


@any_authenticated_required
def reports_analytics(request):
    employees = scope_employees(request, Employee.objects.all())
    runs = PayrollRun.objects.filter(Q(lines__employee__in=employees) | Q(lines__isnull=True)).distinct()
    leaves = scope_by_employee_fk(request, LeaveRequest.objects.all())
    context = {
        'total_employees': employees.count(),
        'total_payroll_runs': runs.count(),
        'total_released': runs.filter(status='RELEASED').count(),
        'total_pending_leave': leaves.filter(status='PENDING').count(),
    }
    chart_labels = json.dumps(['Employees', 'Payroll Runs', 'Released', 'Pending Leave'])
    chart_values = json.dumps([context['total_employees'], context['total_payroll_runs'], context['total_released'], context['total_pending_leave']])
    context['chart_labels'] = chart_labels
    context['chart_values'] = chart_values
    return render(request, 'Reports and Analytics.html', context)


@any_authenticated_required
def ess(request):
    lines = scope_by_employee_fk(request, PayrollRunLine.objects.all()).filter(payroll_run__status='RELEASED')
    leaves = scope_by_employee_fk(request, LeaveRequest.objects.all())
    reimbursements = scope_by_employee_fk(request, Reimbursement.objects.all())
    context = {
        'total_payslips': lines.count(),
        'pending_leave': leaves.filter(status='PENDING').count(),
        'pending_reimbursements': reimbursements.filter(status='PENDING').count(),
    }
    return render(request, 'ESS.html', context)


@any_authenticated_required
def notifications(request):
    from datetime import date, timedelta
    employees = scope_employees(request, Employee.objects.all())
    notices = []
    if request.user.role in ('ADMIN', 'COMPANY_OWNER'):
        for leave in scope_by_employee_fk(request, LeaveRequest.objects.filter(status='PENDING').select_related('employee'))[:10]:
            notices.append({'type': 'Leave', 'message': f'{leave.employee.full_name} requested {leave.get_leave_type_display()}', 'date': leave.from_date})
        for reimb in scope_by_employee_fk(request, Reimbursement.objects.filter(status='PENDING').select_related('employee'))[:10]:
            notices.append({'type': 'Reimbursement', 'message': f'{reimb.employee.full_name} submitted a {reimb.get_category_display()} claim of {reimb.amount}', 'date': reimb.date})
        run_qs = PayrollRun.objects.filter(status='VALIDATED', lines__employee__in=employees).distinct()[:10]
        for run in run_qs:
            notices.append({'type': 'Payroll', 'message': f'{run.month} payroll is validated and awaiting approval', 'date': run.created_at.date()})
    notices.sort(key=lambda n: n['date'], reverse=True)

    user_notifications = Notification.objects.filter(recipient=request.user)[:30]
    return render(request, 'Notifications.html', {'notices': notices, 'user_notifications': user_notifications})


@login_required
def notification_mark_read(request, pk):
    notif = get_object_or_404(Notification, pk=pk, recipient=request.user)
    notif.is_read = True
    notif.save(update_fields=['is_read'])
    return redirect('notifications')


@owner_or_admin_required
def user_roles_permissions(request):
    from .models import UserRoleAssignment
    employees = scope_employees(request, Employee.objects.all())
    if request.method == 'POST':
        form = UserRoleAssignmentForm(request.POST, employee_queryset=employees)
        if form.is_valid():
            assignment = form.save()
            log_action(request, 'UPDATE', assignment, details='Role assignment saved')
            return redirect('user_roles_permissions')
    else:
        form = UserRoleAssignmentForm(employee_queryset=employees)
    assignments = UserRoleAssignment.objects.filter(employee__in=employees).select_related('employee')
    return render(request, 'User Roles and Permissions.html', {'form': form, 'assignments': assignments})


@owner_or_admin_required
def settings_view(request):
    company = get_user_company(request)
    if company is None and not (request.user.is_superuser or request.user.role == 'ADMIN'):
        raise PermissionDenied
    company_settings, _ = CompanySettings.objects.get_or_create(company=company) if company else (
        CompanySettings.objects.get_or_create(pk=1)
    )
    if request.method == 'POST':
        form = CompanySettingsForm(request.POST, instance=company_settings)
        if form.is_valid():
            form.save()
            log_action(request, 'UPDATE', company_settings, details='Company settings updated')
            return redirect('settings')
    else:
        form = CompanySettingsForm(instance=company_settings)
    return render(request, 'Settings.html', {'form': form})


@any_authenticated_required
def payslip_history(request):
    lines = scope_by_employee_fk(
        request, PayrollRunLine.objects.select_related('employee', 'payroll_run')
    ).filter(payroll_run__status='RELEASED').order_by('-payroll_run__created_at')
    paginator = Paginator(lines, 15)
    lines_page = paginator.get_page(request.GET.get('page'))
    return render(request, 'payslip management/payslip History.html', {'lines': lines_page})


@any_authenticated_required
def download_pdf(request):
    lines = scope_by_employee_fk(
        request, PayrollRunLine.objects.select_related('employee', 'payroll_run')
    ).filter(payroll_run__status='RELEASED')
    return render(request, 'payslip management/Download PDF.html', {'lines': lines})


@any_authenticated_required
def payslip_pdf_download(request, pk):
    """Streams a real PDF for one released payslip line (object-level scoped)."""
    from .pdf_utils import render_payslip_pdf
    line = get_object_or_404(
        scope_by_employee_fk(request, PayrollRunLine.objects.select_related('employee', 'payroll_run')),
        pk=pk, payroll_run__status='RELEASED',
    )
    pdf_bytes = render_payslip_pdf(line)
    filename = f"Payslip_{line.employee.employee_code}_{line.payroll_run.month.replace(' ', '_')}.pdf"
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    log_action(request, 'EXPORT', line, details=f'Downloaded payslip PDF for {line.employee}')
    return response


@any_authenticated_required
def email_payslip(request):
    lines = scope_by_employee_fk(
        request, PayrollRunLine.objects.select_related('employee', 'payroll_run')
    ).filter(payroll_run__status='RELEASED')
    sent = False
    if request.method == 'POST':
        from django.core.mail import send_mail
        from django.conf import settings as dj_settings
        line_id = request.POST.get('line_id')
        line = get_object_or_404(lines, pk=line_id)
        try:
            send_mail(
                subject=f'Payslip - {line.payroll_run.month}',
                message=f'Dear {line.employee.full_name},\n\nYour payslip for {line.payroll_run.month} is attached to this notice.\nNet Pay: {line.net_pay}\n\nRegards,\nPayroll Team',
                from_email=dj_settings.DEFAULT_FROM_EMAIL,
                recipient_list=[line.employee.email],
                fail_silently=True,  # never crash the request if SMTP isn't configured
            )
            sent = True
            log_action(request, 'OTHER', line, details='Payslip emailed')
            messages.success(request, f"Payslip emailed to {line.employee.email}.")
        except Exception:
            messages.error(request, "Could not send the email right now. Please try again later.")
    return render(request, 'payslip management/Email payslip.html', {'lines': lines, 'sent': sent})


@owner_or_admin_required
def failed_transaction_report(request):
    employees = scope_employees(request, Employee.objects.all())
    lines = PayrollRunLine.objects.filter(
        employee__in=employees, payroll_run__status='RELEASED', payment_status='FAILED',
    ).select_related('employee', 'payroll_run')

    if request.method == 'POST':
        line = get_object_or_404(lines, pk=request.POST.get('line_id'))
        if line.employee.bank_account_no and line.employee.ifsc_code:
            line.payment_status = 'SUCCESS'
            line.payment_reference = f"NEFT{line.payroll_run_id:04d}{line.pk:06d}"
            line.failure_reason = ''
            line.payment_attempted_at = timezone.now()
            line.save(update_fields=['payment_status', 'payment_reference', 'payment_attempted_at', 'failure_reason'])
            log_action(request, 'UPDATE', line, details=f'Retried bank transfer, now SUCCESS for {line.employee}')
            messages.success(request, f'Retry succeeded for {line.employee.full_name}.')
        else:
            messages.error(request, 'Still missing bank details — cannot retry until the employee record is fixed.')
        return redirect('failed_transaction_report')

    return render(request, 'Bank Transfer.html', {'lines': lines, 'is_failed_view': True})


@any_authenticated_required
def generate_payslip(request):
    return render(request, 'payslip management/Generate payslip.html')


@owner_or_admin_required
def payment_states(request):
    employees = scope_employees(request, Employee.objects.all())
    lines = PayrollRunLine.objects.filter(
        employee__in=employees, payroll_run__status='RELEASED',
    ).select_related('employee', 'payroll_run')
    counts = {
        'pending': lines.filter(payment_status='PENDING').count(),
        'success': lines.filter(payment_status='SUCCESS').count(),
        'failed': lines.filter(payment_status='FAILED').count(),
    }
    return render(request, 'Bank Transfer.html', {'lines': lines, 'counts': counts, 'is_status_view': True})


@owner_or_admin_required
def salary_transfer_file(request):
    """Real downloadable NEFT-style CSV for payroll lines still pending transfer."""
    employees = scope_employees(request, Employee.objects.all())
    lines = PayrollRunLine.objects.filter(
        employee__in=employees, payroll_run__status='RELEASED', payment_status='PENDING',
    ).select_related('employee', 'payroll_run')

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="salary_transfer_file.csv"'
    writer = csv.writer(response)
    writer.writerow(['Employee Code', 'Employee Name', 'Bank Account No', 'IFSC', 'Amount', 'Month', 'Remarks'])
    for line in lines:
        writer.writerow([
            line.employee.employee_code, line.employee.full_name,
            line.employee.bank_account_no or 'MISSING', line.employee.ifsc_code or 'MISSING',
            line.net_pay, line.payroll_run.month, 'Salary',
        ])
    log_action(request, 'EXPORT', None, details=f'Downloaded salary transfer file ({lines.count()} lines)')
    return response


@owner_or_admin_required
def payslips_export_csv(request):
    employees = scope_employees(request, Employee.objects.all())
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="payslips.csv"'
    writer = csv.writer(response)
    writer.writerow(['Month', 'Employee Code', 'Name', 'Basic', 'Gross Salary', 'Net Pay'])
    lines = PayrollRunLine.objects.filter(payroll_run__status='RELEASED', employee__in=employees).select_related('employee', 'payroll_run')
    for line in lines:
        writer.writerow([line.payroll_run.month, line.employee.employee_code, line.employee.full_name, line.basic, line.gross_salary, line.net_pay])
    log_action(request, 'OTHER', details='Exported payslips CSV')
    return response


@owner_or_admin_required
def payslips_export_excel(request):
    import openpyxl
    from openpyxl.utils import get_column_letter

    employees = scope_employees(request, Employee.objects.all())
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Payslips"
    headers = ['Month', 'Employee Code', 'Name', 'Basic', 'Gross Salary', 'Net Pay']
    ws.append(headers)

    lines = PayrollRunLine.objects.filter(payroll_run__status='RELEASED', employee__in=employees).select_related('employee', 'payroll_run')
    for line in lines:
        ws.append([line.payroll_run.month, line.employee.employee_code, line.employee.full_name, float(line.basic), float(line.gross_salary), float(line.net_pay)])

    for i, header in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(i)].width = max(14, len(header) + 4)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="payslips.xlsx"'
    wb.save(response)
    log_action(request, 'OTHER', details='Exported payslips Excel')
    return response
