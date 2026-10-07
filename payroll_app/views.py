import csv
import json


def _js(value):
    """JSON safe to embed in <script> with |safe: escapes <, > and & so a name or
    department such as '</script><script>...' cannot run as code (stored XSS)."""
    return (json.dumps(value)
            .replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026'))

import logging
import secrets
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Sum, F, Q
from django.http import FileResponse, HttpResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .audit import log_action
from .forms import (
    EmployeeForm, SalaryStructureForm, AttendanceForm, LeaveRequestForm, LeaveDecisionForm,
    ReimbursementForm, PayrollRunForm, InvestmentDeclarationForm, ArrearsForm,
    FullFinalSettlementForm, UserRoleAssignmentForm, CompanySettingsForm,
    DemoRequestForm, CompanyRegistrationForm, ClientComplaintForm, ClientRequestForm,
    EmployeeAccountForm, OTPVerifyForm, BankPaymentUpdateForm,
)
from .leave import leave_balance
from .models import (
    Employee, SalaryStructure, Attendance, LeaveRequest, Reimbursement,
    PayrollRun, PayrollRunLine, InvestmentDeclaration, Company, User,
    DemoRequest, ClientComplaint, ClientRequest, Notification, CompanySettings, AuditLog,
    EmailOTP, BankPayment,
)
from .notifications import notify_demo_request, send_email_safe
from .payroll_engine import annual_tax, build_run_lines, parse_month, release_claims
from .permissions import (
    role_required, admin_required, company_owner_required, owner_or_admin_required,
    any_authenticated_required, owner_or_employee_required, scope_employees, scope_by_employee_fk, get_user_company,
    get_object_scoped,
)

PAGE_SIZE = 10
logger = logging.getLogger('payroll_app.views')
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5

# Explicit whitelist for the legacy generic template loader — prevents path
# traversal / arbitrary template disclosure via the URL.
GENERIC_PAGE_WHITELIST = set()   # the old mock-up page had no template (500) and was removed


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

def generic_page(request, template_path):
    if template_path not in GENERIC_PAGE_WHITELIST:
        raise Http404("Page not found.")
    return render(request, f'{template_path}.html')


def landing(request):
    return render(request, 'public/landing.html')


def request_demo(request):
    if request.method == 'POST':
        form = DemoRequestForm(request.POST)
        if form.is_valid():
            demo = form.save()
            log_action(request, 'CREATE', demo, details='Public demo request submitted')
            # External email/SMS failures are logged and stored on the request;
            # they never block the submission itself.
            notify_demo_request(demo)
            messages.success(request, "Thank you! Your demo request has been received. Our team will contact you shortly to confirm the demo.")
            return redirect('request_demo')
    else:
        form = DemoRequestForm()
    return render(request, 'public/request_demo.html', {'form': form})


REG_OTP_KEY = 'reg_otp'
REG_VERIFIED_KEY = 'reg_verified_email'
REG_VERIFIED_TTL_SECONDS = 30 * 60


REG_PENDING_MESSAGE = (
    'Registration successful. Your account is pending admin approval. '
    'We will email you as soon as it is approved - you can sign in only after that.'
)


def _email_admins_new_registration(request, company, owner):
    """Tell the platform admin(s) by email that a company is waiting for approval.

    Sent only after the registration is safely saved. A mail problem is logged
    and never stops or undoes the registration (the in-app bell notification is
    created separately by the Company post_save signal)."""
    from django.conf import settings

    def _send():
        recipients = []
        configured = (getattr(settings, 'DEMO_NOTIFY_EMAIL', '') or '').replace(';', ',')
        recipients += [e.strip() for e in configured.split(',') if e.strip()]
        recipients += list(User.objects.filter(Q(role='ADMIN') | Q(is_superuser=True), is_active=True)
                           .exclude(email='').values_list('email', flat=True))
        seen, unique = set(), []
        for e in recipients:
            if e.lower() not in seen:
                seen.add(e.lower())
                unique.append(e)
        if not unique:
            logger.warning('New registration %r is waiting for approval but no admin email address is set.', company.name)
            return
        try:
            link = request.build_absolute_uri(reverse('admin_company_approvals'))
            ok, err = send_email_safe(
                f'New registration waiting for approval: {company.name}',
                f"A new company has registered on Namma Payroll and is waiting for your approval.\n\n"
                f"Company : {company.name}\n"
                f"Owner   : {owner.get_full_name() or owner.username} (username: {owner.username})\n"
                f"Email   : {company.contact_email}\n"
                f"Mobile  : {company.contact_phone or '-'}\n\n"
                f"Review and approve here:\n{link}\n\n"
                "The owner cannot sign in until you approve.",
                unique,
            )
            if not ok:
                logger.warning('Admin approval-needed email failed for %r: %s', company.name, err)
        except Exception:
            logger.exception('Admin approval-needed email crashed for %r', company.name)

    transaction.on_commit(_send)



def company_register(request):
    """Company sign-up in three steps: (1) email, (2) emailed 6-digit OTP,
    (3) company and login details. Step 3's email is locked to the address
    verified in step 2. A full POST without the 'action' field keeps the
    older flow (create account, then verify on the verify-email page)."""
    from django.conf import settings
    if not settings.REQUIRE_EMAIL_VERIFICATION:
        return _company_register_single_step(request)
    from .forms import RegistrationEmailForm
    session = request.session
    now = timezone.now().timestamp()
    action = request.POST.get('action', '') if request.method == 'POST' else ''

    verified = session.get(REG_VERIFIED_KEY)
    if verified and now - verified.get('at', 0) > REG_VERIFIED_TTL_SECONDS:
        session.pop(REG_VERIFIED_KEY, None)
        verified = None
    pending = session.get(REG_OTP_KEY)

    if action == 'change_email':
        session.pop(REG_OTP_KEY, None)
        session.pop(REG_VERIFIED_KEY, None)
        return redirect('company_register')

    email_form = RegistrationEmailForm(request.POST if action == 'send_otp' else None)
    otp_form = OTPVerifyForm(request.POST if action == 'verify_otp' else None)
    form = None

    if action in ('send_otp', 'resend_otp'):
        email = None
        if action == 'resend_otp':
            email = (pending or {}).get('email')
        elif email_form.is_valid():
            email = email_form.cleaned_data['email']
        if email:
            if pending and pending.get('email') == email and now - pending.get('sent', 0) < 60:
                messages.error(request, 'Please wait a minute before requesting another code.')
            else:
                code = f"{secrets.randbelow(1_000_000):06d}"
                ok, err = send_email_safe(
                    'Your Namma Payroll verification code',
                    f"Hello,\n\nYour verification code is {code}. It expires in {OTP_TTL_MINUTES} minutes.\n\n"
                    "If you did not start a registration, please ignore this email.",
                    [email],
                )
                if ok:
                    session[REG_OTP_KEY] = {'email': email, 'hash': make_password(code), 'sent': now,
                                            'exp': now + OTP_TTL_MINUTES * 60, 'attempts': 0}
                    session.pop(REG_VERIFIED_KEY, None)
                    messages.success(request, f"We've emailed a 6-digit code to {email}.")
                else:
                    logger.warning('Pre-registration OTP email failed: %s', err)
                    messages.error(request, 'We could not send the email right now. Please try again shortly.')
            return redirect('company_register')

    elif action == 'verify_otp' and pending:
        if otp_form.is_valid():
            if now > pending.get('exp', 0):
                otp_form.add_error('code', 'This code has expired. Request a new one.')
            elif pending.get('attempts', 0) >= OTP_MAX_ATTEMPTS:
                otp_form.add_error('code', 'Too many wrong attempts. Request a new code.')
            elif not check_password(otp_form.cleaned_data['code'], pending['hash']):
                pending['attempts'] = pending.get('attempts', 0) + 1
                session[REG_OTP_KEY] = pending
                otp_form.add_error('code', 'Incorrect code.')
            else:
                session.pop(REG_OTP_KEY, None)
                session[REG_VERIFIED_KEY] = {'email': pending['email'], 'at': now}
                messages.success(request, 'Email verified. Now complete your company details.')
                return redirect('company_register')

    elif request.method == 'POST' and action in ('register', ''):
        data = request.POST.copy()
        if verified:
            data['contact_email'] = verified['email']
        form = CompanyRegistrationForm(data)
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
                name_parts = data['owner_full_name'].strip().split(' ', 1)
                owner = User.objects.create_user(
                    username=data['username'],
                    email=data['contact_email'],
                    password=data['password1'],
                    first_name=name_parts[0],
                    last_name=name_parts[1] if len(name_parts) > 1 else '',
                    role='COMPANY_OWNER',
                    company=company,
                    is_active=False,               # cannot log in until the company is approved
                    email_verified=bool(verified),  # already verified in step 2 of the new flow
                )
                log_action(request, 'REGISTER', company, details=f'Company self-registered, owner={owner.username}')
            _email_admins_new_registration(request, company, owner)
            if verified:
                session.pop(REG_VERIFIED_KEY, None)
                messages.success(request, REG_PENDING_MESSAGE)
                return redirect('login')
            _issue_registration_otp(owner)
            session['otp_user_id'] = owner.pk
            messages.success(request, f"We've emailed a 6-digit verification code to {owner.email}.")
            return redirect('verify_email')

    if verified or (form is not None and form.is_bound):
        step = 3
        if form is None:
            form = CompanyRegistrationForm(initial={'contact_email': verified['email']})
        if verified:
            form.fields['contact_email'].widget.attrs['readonly'] = 'readonly'
    elif pending:
        step = 2
    else:
        step = 1
    return render(request, 'public/company_register.html', {
        'step': step, 'form': form or CompanyRegistrationForm(), 'email_form': email_form, 'otp_form': otp_form,
        'pending_email': (pending or {}).get('email', ''), 'verified_email': (verified or {}).get('email', ''),
    })


def _company_register_single_step(request):
    """One-page company sign-up with no email verification (default).
    The owner stays inactive until an admin approves the company."""
    form = CompanyRegistrationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            company = Company.objects.create(
                name=data['company_name'],
                contact_email=data['contact_email'],
                contact_phone=data['contact_phone'],
                address=data['address'],
                status='PENDING_APPROVAL',
            )
            name_parts = data['owner_full_name'].strip().split(' ', 1)
            owner = User.objects.create_user(
                username=data['username'],
                email=data['contact_email'],
                password=data['password1'],
                first_name=name_parts[0],
                last_name=name_parts[1] if len(name_parts) > 1 else '',
                role='COMPANY_OWNER',
                company=company,
                is_active=False,      # cannot log in until the company is approved
                email_verified=True,  # verification switched off, nothing to wait for
            )
            log_action(request, 'REGISTER', company, details=f'Company self-registered, owner={owner.username}')
        _email_admins_new_registration(request, company, owner)
        messages.success(request, REG_PENDING_MESSAGE)
        return redirect('login')
    return render(request, 'public/company_register.html', {'step': 3, 'form': form, 'simple': True})


def _issue_registration_otp(user):
    """Create a fresh OTP (hash only stored) and email it. Returns True if
    the email was handed to the mail backend."""
    EmailOTP.objects.filter(user=user, purpose='REGISTRATION', is_used=False).update(is_used=True)
    code = f"{secrets.randbelow(1_000_000):06d}"
    EmailOTP.objects.create(
        user=user, purpose='REGISTRATION', code_hash=make_password(code),
        expires_at=timezone.now() + timezone.timedelta(minutes=OTP_TTL_MINUTES),
    )
    ok, err = send_email_safe(
        'Your Namma Payroll verification code',
        f"Hello {user.get_full_name() or user.username},\n\n"
        f"Your verification code is {code}. It expires in {OTP_TTL_MINUTES} minutes.\n\n"
        "If you did not register, please ignore this email.",
        [user.email],
    )
    if not ok:
        logger.warning('Registration OTP email to user #%s failed: %s', user.pk, err)
    return ok


def verify_email(request):
    user_id = request.session.get('otp_user_id')
    user = User.objects.filter(pk=user_id, email_verified=False).first() if user_id else None
    if user is None:
        messages.info(request, 'There is no pending email verification. Please sign in.')
        return redirect('login')

    if request.method == 'POST' and request.POST.get('resend'):
        last = EmailOTP.objects.filter(user=user, purpose='REGISTRATION').first()
        if last and (timezone.now() - last.created_at).total_seconds() < 60:
            messages.error(request, 'Please wait a minute before requesting another code.')
        elif _issue_registration_otp(user):
            messages.success(request, 'A new code has been sent.')
        else:
            messages.error(request, 'We could not send the email right now. Please try again shortly.')
        return redirect('verify_email')

    form = OTPVerifyForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        otp = EmailOTP.objects.filter(user=user, purpose='REGISTRATION', is_used=False).first()
        if otp is None or otp.expires_at < timezone.now():
            form.add_error('code', 'This code has expired. Request a new one.')
        elif otp.attempts >= OTP_MAX_ATTEMPTS:
            form.add_error('code', 'Too many wrong attempts. Request a new code.')
        elif not check_password(form.cleaned_data['code'], otp.code_hash):
            otp.attempts += 1
            otp.save(update_fields=['attempts'])
            form.add_error('code', 'Incorrect code.')
        else:
            otp.is_used = True
            otp.save(update_fields=['is_used'])
            user.email_verified = True
            user.save(update_fields=['email_verified'])
            request.session.pop('otp_user_id', None)
            log_action(request, 'UPDATE', user, details='Email verified via OTP', company=user.company)
            messages.success(request, 'Email verified. Your registration is now pending admin approval — '
                                      'you will be able to sign in once it is approved.')
            return redirect('login')
    return render(request, 'auth/verify_email.html', {'form': form, 'email': user.email})


@login_required
def post_login_redirect(request):
    user = request.user
    if user.is_superuser or user.role == 'ADMIN':
        return redirect('admin_dashboard')
    return redirect('dashboard')


@require_POST
def logout_view(request):
    logout(request)  # the user_logged_out signal writes the audit entry
    messages.success(request, 'You have been signed out.')
    return redirect('login')


# ---------------------------------------------------------------------------
# Admin panel — platform administrator only
# ---------------------------------------------------------------------------

@admin_required
def admin_dashboard(request):
    released = PayrollRunLine.objects.filter(payroll_run__status='RELEASED')
    context = {
        'total_clients': Company.objects.count(),
        'active_clients': Company.objects.filter(status='APPROVED').count(),
        'pending_clients': Company.objects.filter(status='PENDING_APPROVAL').count(),
        'suspended_clients': Company.objects.filter(status__in=['SUSPENDED', 'REJECTED']).count(),
        'pending_demo_requests': DemoRequest.objects.filter(status='PENDING').count(),
        'pending_company_approvals': Company.objects.filter(status='PENDING_APPROVAL').count(),
        'open_complaints': ClientComplaint.objects.filter(status__in=['OPEN', 'IN_PROGRESS']).count(),
        'pending_client_requests': ClientRequest.objects.filter(status='PENDING').count(),
        'total_companies': Company.objects.filter(status='APPROVED').count(),
        'total_employees': Employee.objects.count(),
        'active_employees': Employee.objects.filter(employment_status='ACTIVE').count(),
        'total_payroll_runs': PayrollRun.objects.count(),
        'runs_awaiting_approval': PayrollRun.objects.filter(status='VALIDATED').count(),
        'total_net_released': released.aggregate(t=Sum('net_pay'))['t'] or 0,
        'payments_pending': BankPayment.objects.exclude(status='PAID').count(),
        'payments_failed': BankPayment.objects.filter(status='FAILED').count(),
        'recent_demo_requests': DemoRequest.objects.all()[:5],
        'recent_companies': Company.objects.prefetch_related('users').all()[:5],
        'recent_notifications': Notification.objects.filter(recipient=request.user)[:6],
        'recent_activity': AuditLog.objects.select_related('actor', 'company')[:8],
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
    companies = Company.objects.filter(status='PENDING_APPROVAL').prefetch_related('users')
    return render(request, 'admin_panel/company_approvals.html', {'companies': companies})


@admin_required
@transaction.atomic
def admin_company_decide(request, pk):
    company = get_object_or_404(Company.objects.select_for_update(), pk=pk)
    next_url = request.POST.get('next') or 'admin_company_approvals'
    if next_url not in ('admin_company_approvals', 'admin_company_list'):
        next_url = 'admin_company_approvals'
    if request.method != 'POST':
        return redirect(next_url)
    decision = request.POST.get('decision')
    owners = User.objects.filter(company=company, role='COMPANY_OWNER')
    if decision == 'APPROVED' and company.status in ('PENDING_APPROVAL', 'REJECTED'):
        unverified = owners.filter(email_verified=False).exists()
        if unverified and not request.POST.get('override_unverified'):
            messages.error(request, f"{company.name}: the owner has not verified their email yet. "
                                    "Tick 'approve anyway' if you have confirmed them another way.")
            return redirect(next_url)
        company.status = 'APPROVED'
        company.approved_at = timezone.now()
        company.rejection_reason = ''
        company.save()
        owners.update(is_active=True, email_verified=True)
        log_action(request, 'APPROVE', company,
                   details='Company registration approved' + (' (email unverified - admin override)' if unverified else ''))
        login_link = request.build_absolute_uri(reverse('login'))
        email_failed = []
        for owner in owners:
            Notification.objects.create(recipient=owner, message='Your company registration has been approved.', link='/')
            to_addr = owner.email or company.contact_email
            ok, err = send_email_safe(
                'Your Namma Payroll account is approved',
                f"Hello {owner.get_full_name() or owner.username},\n\n"
                f"Good news - {company.name} has been approved on Namma Payroll.\n"
                f"You can now sign in with your username ({owner.username}) or your email:\n{login_link}\n",
                [to_addr],
            ) if to_addr else (False, 'no email address on file')
            if not ok:
                logger.warning('Approval email to user #%s failed: %s', owner.pk, err)
                email_failed.append(to_addr or owner.username)
        messages.success(request, f"{company.name} approved. The owner's account is now active.")
        if email_failed:
            messages.warning(request, 'The approval email could not be sent to: ' + ', '.join(email_failed)
                             + '. The account IS active - please tell them they can sign in now.')
    elif decision == 'REJECTED' and company.status == 'PENDING_APPROVAL':
        company.status = 'REJECTED'
        company.rejection_reason = request.POST.get('reason', '')[:500]
        company.save()
        log_action(request, 'REJECT', company, details='Company registration rejected')
        reject_failed = []
        for owner in owners:
            to_addr = owner.email or company.contact_email
            reason_line = f"Reason: {company.rejection_reason}\n\n" if company.rejection_reason else "\n"
            ok, err = send_email_safe(
                'Your Namma Payroll registration was not approved',
                f"Hello {owner.get_full_name() or owner.username},\n\n"
                f"Sorry - the registration for {company.name} was not approved.\n"
                f"{reason_line}"
                "If you think this is a mistake, please contact us and we will look at it again.\n",
                [to_addr],
            ) if to_addr else (False, 'no email address on file')
            if not ok:
                logger.warning('Rejection email to user #%s failed: %s', owner.pk, err)
                reject_failed.append(to_addr or owner.username)
        messages.success(request, f"{company.name} rejected.")
        if reject_failed:
            messages.warning(request, 'The rejection email could not be sent to: ' + ', '.join(reject_failed) + '.')
    elif decision == 'SUSPENDED' and company.status == 'APPROVED':
        company.status = 'SUSPENDED'
        company.save(update_fields=['status'])
        log_action(request, 'UPDATE', company, details='Company suspended (all its users blocked from login)')
        messages.success(request, f"{company.name} suspended. Its users can no longer sign in; no data was deleted.")
    elif decision == 'REACTIVATE' and company.status == 'SUSPENDED':
        company.status = 'APPROVED'
        company.save(update_fields=['status'])
        log_action(request, 'UPDATE', company, details='Company reactivated')
        messages.success(request, f"{company.name} reactivated.")
    else:
        messages.error(request, 'That action is not allowed for the current status.')
    return redirect(next_url)


@admin_required
def admin_company_list(request):
    companies = Company.objects.prefetch_related('users').annotate(employee_count=Count('employees')).order_by('-created_at')
    q_text = request.GET.get('q', '').strip()
    status = request.GET.get('status', '')
    if q_text:
        companies = companies.filter(Q(name__icontains=q_text) | Q(contact_email__icontains=q_text) | Q(contact_phone__icontains=q_text))
    if status in dict(Company.STATUS_CHOICES):
        companies = companies.filter(status=status)
    paginator = Paginator(companies, PAGE_SIZE)
    page = paginator.get_page(request.GET.get('page'))
    return render(request, 'admin_panel/company_list.html', {
        'companies': page, 'q': q_text, 'status': status, 'status_choices': Company.STATUS_CHOICES,
    })


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


@company_owner_required
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
    if request.user.is_superuser or request.user.role == 'ADMIN':
        return redirect('admin_dashboard')  # platform admin has its own dashboard
    if request.user.role == 'EMPLOYEE' and getattr(request.user, 'employee_profile', None) is not None:
        return redirect('employee_home')    # employees use their own dashboard (/me/)
    employees = scope_employees(request, Employee.objects.all())
    dept_data = employees.filter(employment_status='ACTIVE').values('department').annotate(c=Count('id')).order_by('-c')
    dept_labels = _js([d['department'] or 'Unassigned' for d in dept_data])
    dept_values = _js([d['c'] for d in dept_data])
    status_data = employees.values('employment_status').annotate(c=Count('id'))
    status_map = {'ACTIVE': 'Active', 'ON_LEAVE': 'On Leave', 'RESIGNED': 'Resigned', 'TERMINATED': 'Terminated'}
    status_labels = _js([status_map.get(s['employment_status'], s['employment_status']) for s in status_data])
    status_values = _js([s['c'] for s in status_data])

    if request.user.role == 'EMPLOYEE':
        runs = PayrollRun.objects.filter(lines__employee__in=employees, status='RELEASED').distinct()
    else:
        runs = scope_runs(request)
    run_status_data = runs.values('status').annotate(c=Count('id'))
    run_status_map = {'DRAFT': 'Draft', 'VALIDATED': 'Validated', 'APPROVED': 'Approved', 'RELEASED': 'Released'}
    run_labels = _js([run_status_map.get(r['status'], r['status']) for r in run_status_data])
    run_values = _js([r['c'] for r in run_status_data])

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

@company_owner_required
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


@company_owner_required
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

    is_admin = request.user.is_superuser or request.user.role == 'ADMIN'
    company_qs = Company.objects.filter(status='APPROVED') if is_admin else None
    if request.method == 'POST':
        post_data = request.POST.copy()
        prefix = (post_data.get('code_prefix') or 'EPRO').strip().upper()
        number = post_data.get('employee_code', '')
        post_data['employee_code'] = f'{prefix}{number}'
        form = EmployeeForm(post_data, company_queryset=company_qs)
        if form.is_valid():
            employee = form.save(commit=False)
            # Owners: always their own company (never trusted from the form).
            employee.company = form.cleaned_data['company'] if is_admin else company
            employee.save()
            log_action(request, 'CREATE', employee, details='Employee created')
            messages.success(request, f"Employee {employee.full_name} ({employee.employee_code}) added.")
            return redirect('employee_master')
        messages.error(request, 'Please correct the highlighted fields.')
    else:
        form = EmployeeForm(company_queryset=company_qs)

    all_employees = base_qs
    existing_codes = base_qs.values_list('employee_code', flat=True).order_by('employee_code')
    paginator = Paginator(all_employees, PAGE_SIZE)
    employees = paginator.get_page(request.GET.get('page'))
    return render(request, 'Employee Master.html', {
        'employees': employees, 'form': form, 'existing_codes': existing_codes, 'number_options': number_options,
    })


@company_owner_required
def employee_edit(request, pk):
    from .concurrency import remember_version, stale_warning
    employee = get_object_scoped(request, Employee, employee_field='', pk=pk)
    is_admin = request.user.is_superuser or request.user.role == 'ADMIN'
    company_qs = Company.objects.all() if is_admin else None
    form = None
    if request.method == 'POST':
        with transaction.atomic():
            # Lock this one employee row until the request ends. A second person saving the
            # same employee waits here, then is told what changed instead of overwriting it.
            locked = Employee.objects.select_for_update().get(pk=employee.pk)
            warning = stale_warning(request, locked, 'employee record')
            if warning:
                messages.warning(request, warning)
            else:
                form = EmployeeForm(request.POST, instance=locked, company_queryset=company_qs)
                if form.is_valid():
                    changed = ', '.join(form.changed_data)[:300]
                    employee = form.save(commit=False)
                    if is_admin:
                        employee.company = form.cleaned_data['company']
                    employee.save()
                    # Keep the linked login's email in step with the employee record.
                    if employee.user_id and 'email' in form.changed_data:
                        User.objects.filter(pk=employee.user_id).update(email=employee.email)
                    log_action(request, 'UPDATE', employee, details=f'Employee updated: {changed}')
                    messages.success(request, f"{employee.full_name} updated.")
                    return redirect('employee_master')
            employee = locked
    if form is None:
        form = EmployeeForm(instance=employee, company_queryset=company_qs)
    remember_version(request, employee)
    paginator = Paginator(scope_employees(request, Employee.objects.all()), PAGE_SIZE)
    return render(request, 'Employee Master.html', {
        'employees': paginator.get_page(request.GET.get('page')), 'form': form, 'edit_employee': employee,
    })


@company_owner_required
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

@company_owner_required
def salary_structure(request):
    from django.db import IntegrityError
    employees = scope_employees(request, Employee.objects.all())
    if request.method == 'POST':
        form = SalaryStructureForm(request.POST, employee_queryset=employees)
        if form.is_valid():
            try:
                with transaction.atomic():
                    ss = form.save()
            except IntegrityError:
                # Two people saved the structure for the same employee at the same moment.
                messages.warning(request, 'A salary structure for this employee was just saved by someone else. '
                                          'Nothing was changed - the saved one is shown below.')
                return redirect('salary_structure')
            log_action(request, 'CREATE', ss, details='Salary structure saved')
            return redirect('salary_structure')
    else:
        form = SalaryStructureForm(employee_queryset=employees)

    all_structures = SalaryStructure.objects.filter(employee__in=employees).select_related('employee').order_by('employee__employee_code')
    paginator = Paginator(all_structures, PAGE_SIZE)
    structures = paginator.get_page(request.GET.get('page'))
    chart_qs = all_structures[:15]
    chart_labels = _js([s.employee.full_name for s in chart_qs])
    chart_basic = _js([float(s.basic) for s in chart_qs])
    chart_hra = _js([float(s.hra) for s in chart_qs])
    return render(request, 'Salary Structure.html', {
        'form': form, 'structures': structures,
        'chart_labels': chart_labels, 'chart_basic': chart_basic, 'chart_hra': chart_hra,
    })


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------

@owner_or_employee_required
def attendance(request):
    employees = scope_employees(request, Employee.objects.all())
    records_qs = scope_by_employee_fk(request, Attendance.objects.select_related('employee'))

    today_record = None
    if request.user.role == 'EMPLOYEE':
        today_record = Attendance.objects.filter(
            employee=getattr(request.user, 'employee_profile', None), date=timezone.localdate()
        ).first()

    if request.method == 'POST':
        if request.user.role == 'EMPLOYEE':
            messages.error(request, "Use the Check In / Check Out buttons to record your own attendance.")
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
    chart_labels = _js([status_map.get(s['status'], s['status']) for s in status_counts])
    chart_values = _js([s['c'] for s in status_counts])
    return render(request, 'Attendance.html', {
        'form': form, 'records': records, 'chart_labels': chart_labels, 'chart_values': chart_values,
        'today_record': today_record,
    })


@require_POST
@role_required('EMPLOYEE')
def attendance_check(request, action):
    """Employee self check-in / check-out for *today only*. The employee is
    always taken from the session, never from the request body."""
    employee = getattr(request.user, 'employee_profile', None)
    if employee is None:
        raise PermissionDenied("No employee profile linked to this account.")
    now = timezone.localtime()
    record, created = Attendance.objects.get_or_create(
        employee=employee, date=now.date(), defaults={'status': 'PRESENT'}
    )
    if action == 'in':
        if record.check_in:
            messages.info(request, f"You already checked in at {record.check_in:%I:%M %p}.")
        else:
            record.check_in = now.time().replace(microsecond=0)
            record.status = 'PRESENT'
            record.save(update_fields=['check_in', 'status'])
            log_action(request, 'CREATE', record, details='Self check-in', company=employee.company)
            messages.success(request, f"Checked in at {record.check_in:%I:%M %p}.")
    elif action == 'out':
        if not record.check_in:
            messages.error(request, "Please check in before checking out.")
        elif record.check_out:
            messages.info(request, f"You already checked out at {record.check_out:%I:%M %p}.")
        else:
            record.check_out = now.time().replace(microsecond=0)
            record.save(update_fields=['check_out'])
            log_action(request, 'UPDATE', record, details='Self check-out', company=employee.company)
            messages.success(request, f"Checked out at {record.check_out:%I:%M %p}.")
    else:
        raise Http404
    return redirect('attendance')


# ---------------------------------------------------------------------------
# Leave management (with approval workflow)
# ---------------------------------------------------------------------------

@owner_or_employee_required
def leave_management(request):
    employees = scope_employees(request, Employee.objects.all())
    leave_qs = scope_by_employee_fk(request, LeaveRequest.objects.select_related('employee'))

    own_employee = getattr(request.user, 'employee_profile', None) if request.user.role == 'EMPLOYEE' else None
    if request.method == 'POST':
        form = LeaveRequestForm(request.POST, employee_queryset=employees, forced_employee=own_employee)
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
            messages.success(request, 'Leave request submitted for approval.')
            return redirect('leave_management')
    else:
        form = LeaveRequestForm(employee_queryset=employees, forced_employee=own_employee)

    paginator = Paginator(leave_qs, PAGE_SIZE)
    leave_requests = paginator.get_page(request.GET.get('page'))
    type_counts = leave_qs.values('leave_type').annotate(c=Count('id'))
    type_map = dict(LeaveRequest.LEAVE_TYPE_CHOICES)
    chart_labels = _js([type_map.get(t['leave_type'], t['leave_type']) for t in type_counts])
    chart_values = _js([t['c'] for t in type_counts])
    return render(request, 'Leave Management.html', {
        'form': form, 'leave_requests': leave_requests, 'chart_labels': chart_labels, 'chart_values': chart_values,
        'decision_form': LeaveDecisionForm(),
        'balance': leave_balance(own_employee, timezone.localdate().year) if own_employee else None,
    })


@company_owner_required
def leave_decision(request, pk):
    leave = get_object_scoped(request, LeaveRequest, employee_field='employee', pk=pk)
    if request.method == 'POST':
        form = LeaveDecisionForm(request.POST)
        with transaction.atomic():
            # Lock the request, then look at its CURRENT status: if another approver got here
            # first, this person waits and is told, instead of both approvals going through.
            leave = LeaveRequest.objects.select_for_update().get(pk=leave.pk)
            if leave.status != 'PENDING':
                who = leave.decided_by.get_username() if leave.decided_by_id else 'another user'
                messages.warning(request, f"This leave request was already {leave.status.lower()} by {who}. "
                                          "Your decision was NOT saved.")
            elif leave.employee.user_id and leave.employee.user_id == request.user.pk:
                raise PermissionDenied("You cannot approve your own leave.")
            elif form.is_valid():
                leave.status = form.cleaned_data['decision']
                leave.decided_by = request.user
                leave.decided_at = timezone.now()
                leave.approver_comment = form.cleaned_data.get('comment', '').strip()
                leave.save()
                if leave.employee.user_id:
                    Notification.objects.create(recipient_id=leave.employee.user_id,
                                                message=(f"Your {leave.get_leave_type_display()} request was {leave.status.lower()}."
                                                         + (f" Comment: {leave.approver_comment}" if leave.approver_comment else ''))[:255],
                                                link='/leave_management/')
                log_action(request, 'APPROVE' if leave.status == 'APPROVED' else 'REJECT', leave,
                           details=f'Leave request {leave.status}')
                messages.success(request, f"Leave request {leave.status.lower()}.")
    return redirect('leave_management')


# ---------------------------------------------------------------------------
# Reimbursement (with approval workflow)
# ---------------------------------------------------------------------------

@owner_or_employee_required
def reimbursement(request):
    employees = scope_employees(request, Employee.objects.all())
    reimb_qs = scope_by_employee_fk(request, Reimbursement.objects.select_related('employee'))

    own_employee = getattr(request.user, 'employee_profile', None) if request.user.role == 'EMPLOYEE' else None
    if request.method == 'POST':
        form = ReimbursementForm(request.POST, request.FILES, employee_queryset=employees, forced_employee=own_employee)
        if form.is_valid():
            reimb = form.save(commit=False)
            if request.user.role == 'EMPLOYEE':
                own_employee = getattr(request.user, 'employee_profile', None)
                if own_employee is None:
                    raise PermissionDenied("No employee profile linked to this account.")
                reimb.employee = own_employee
            reimb.save()
            log_action(request, 'CREATE', reimb, details='Reimbursement claim submitted')
            messages.success(request, 'Claim submitted for approval.')
            return redirect('reimbursement')
    else:
        form = ReimbursementForm(employee_queryset=employees, forced_employee=own_employee)

    paginator = Paginator(reimb_qs, PAGE_SIZE)
    reimbursements = paginator.get_page(request.GET.get('page'))
    return render(request, 'Reimbursement.html', {
        'form': form, 'reimbursements': reimbursements, 'decision_form': LeaveDecisionForm(),
    })


@company_owner_required
def reimbursement_decision(request, pk):
    reimb = get_object_scoped(request, Reimbursement, employee_field='employee', pk=pk)
    if request.method == 'POST':
        form = LeaveDecisionForm(request.POST)
        with transaction.atomic():
            reimb = Reimbursement.objects.select_for_update().get(pk=reimb.pk)
            if reimb.status != 'PENDING':
                who = reimb.decided_by.get_username() if reimb.decided_by_id else 'another user'
                messages.warning(request, f"This claim was already {reimb.status.lower()} by {who}. "
                                          "Your decision was NOT saved.")
            elif reimb.employee.user_id and reimb.employee.user_id == request.user.pk:
                raise PermissionDenied("You cannot approve your own claim.")
            elif form.is_valid():
                reimb.status = form.cleaned_data['decision']
                reimb.decided_by = request.user
                reimb.decided_at = timezone.now()
                reimb.approver_comment = form.cleaned_data.get('comment', '').strip()
                reimb.save()
                if reimb.employee.user_id:
                    Notification.objects.create(recipient_id=reimb.employee.user_id,
                                                message=(f"Your {reimb.get_category_display()} claim of {reimb.amount} was {reimb.status.lower()}."
                                                         + (f" Comment: {reimb.approver_comment}" if reimb.approver_comment else ''))[:255],
                                                link='/reimbursement/')
                log_action(request, 'APPROVE' if reimb.status == 'APPROVED' else 'REJECT', reimb,
                           details=f'Reimbursement {reimb.status}')
                messages.success(request, f"Reimbursement claim {reimb.status.lower()}.")
    return redirect('reimbursement')


@owner_or_employee_required
def reimbursement_receipt(request, pk):
    """Receipts are served through this permission-checked view — never as
    public /media/ URLs — so one employee can't fetch another's document."""
    reimb = get_object_scoped(request, Reimbursement, employee_field='employee', pk=pk)
    if not reimb.receipt:
        raise Http404("No receipt uploaded.")
    try:
        try:
            handle = reimb.receipt.open('rb')
        except Exception:       # missing from storage (lost / not uploaded): never a 500
            raise Http404('This receipt is no longer available. Please upload it again.')
        return FileResponse(handle, as_attachment=False, filename=reimb.receipt.name.rsplit('/', 1)[-1])
    except FileNotFoundError:
        raise Http404("Receipt file is missing on the server.")


# ---------------------------------------------------------------------------
# Statutory / tax reports (owner+admin only — company financial data)
# ---------------------------------------------------------------------------

@company_owner_required
def statutory_compliance(request):
    employees = scope_employees(request, Employee.objects.all())
    rows = []
    for ss in SalaryStructure.objects.filter(employee__in=employees).select_related('employee'):
        pf = ss.basic * Decimal('0.12')
        esi = ss.gross_salary * Decimal('0.0075') if ss.gross_salary <= 21000 else Decimal('0')
        rows.append({'employee': ss.employee, 'basic': ss.basic, 'gross': ss.gross_salary, 'pf': round(pf, 2), 'esi': round(esi, 2)})
    chart_labels = _js([r['employee'].full_name for r in rows])
    chart_pf = _js([float(r['pf']) for r in rows])
    chart_esi = _js([float(r['esi']) for r in rows])
    return render(request, 'Tax and Compliance/Statutory Compliance.html', {'rows': rows, 'chart_labels': chart_labels, 'chart_pf': chart_pf, 'chart_esi': chart_esi})


@owner_or_employee_required
def investment_declaration(request):
    employees = scope_employees(request, Employee.objects.all())
    own_employee = getattr(request.user, 'employee_profile', None) if request.user.role == 'EMPLOYEE' else None
    if request.method == 'POST':
        form = InvestmentDeclarationForm(request.POST, employee_queryset=employees, forced_employee=own_employee)
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
        form = InvestmentDeclarationForm(employee_queryset=employees, forced_employee=own_employee)
    declarations = scope_by_employee_fk(request, InvestmentDeclaration.objects.select_related('employee'))
    return render(request, 'Income Tax Management/Investment declartion.html', {'form': form, 'declarations': declarations})


@company_owner_required
def income_tax(request):
    employees = scope_employees(request, Employee.objects.all())
    rows = []
    for ss in SalaryStructure.objects.filter(employee__in=employees).select_related('employee'):
        annual_gross = ss.gross_salary * Decimal('12')
        tds_annual = annual_tax(annual_gross)   # same rules as payroll (payroll_engine)
        rows.append({'employee': ss.employee, 'annual_gross': round(annual_gross, 2), 'tds_annual': round(tds_annual, 2), 'tds_monthly': round(tds_annual / 12, 2)})
    chart_labels = _js([r['employee'].full_name for r in rows])
    chart_values = _js([float(r['tds_monthly']) for r in rows])
    return render(request, 'Tax and Compliance/Income Tax.html', {'rows': rows, 'chart_labels': chart_labels, 'chart_values': chart_values})


@company_owner_required
def compliance_reports(request):
    employees = scope_employees(request, Employee.objects.all())
    structures = SalaryStructure.objects.filter(employee__in=employees)
    total_pf = sum((s.basic * Decimal('0.12') for s in structures), Decimal('0'))
    total_esi = sum((s.gross_salary * Decimal('0.0075') for s in structures if s.gross_salary <= 21000), Decimal('0'))
    total_gross = sum((s.gross_salary for s in structures), Decimal('0'))
    chart_labels = _js(['PF', 'ESI', 'Gross'])
    chart_values = _js([float(round(total_pf, 2)), float(round(total_esi, 2)), float(round(total_gross, 2))])
    return render(request, 'Tax and Compliance/Compliance Reports.html', {
        'total_pf': round(total_pf, 2), 'total_esi': round(total_esi, 2), 'total_gross': round(total_gross, 2), 'employee_count': structures.count(),
        'chart_labels': chart_labels, 'chart_values': chart_values,
    })


# ---------------------------------------------------------------------------
# Reports (owner+admin — company-scoped; admin sees global data)
# ---------------------------------------------------------------------------

@company_owner_required
def total_employees_report(request):
    employees = scope_employees(request, Employee.objects.all())
    dept_summary_qs = employees.values('department').annotate(
        total=Count('id'), active=Count('id', filter=Q(employment_status='ACTIVE'))
    ).order_by('department')
    dept_summary = [{'department': row['department'] or '(No Department)', 'total': row['total'], 'active': row['active']} for row in dept_summary_qs]
    chart_labels = _js([d['department'] for d in dept_summary])
    chart_values = _js([d['total'] for d in dept_summary])
    context = {
        'total': employees.count(),
        'active': employees.filter(employment_status='ACTIVE').count(),
        'on_leave': employees.filter(employment_status='ON_LEAVE').count(),
        'resigned': employees.filter(employment_status='RESIGNED').count(),
        'dept_summary': dept_summary, 'employees': employees,
        'chart_labels': chart_labels, 'chart_values': chart_values,
    }
    return render(request, 'Payroll/Total Employees.html', context)


@company_owner_required
def new_joiners_report(request):
    from datetime import date, timedelta
    employees = scope_employees(request, Employee.objects.all())
    cutoff = date.today() - timedelta(days=90)
    employees = employees.filter(date_of_joining__gte=cutoff).order_by('-date_of_joining')
    return render(request, 'Payroll/New Joiners.html', {'employees': employees, 'cutoff': cutoff})


@company_owner_required
def payroll_cost_report(request):
    employees = scope_employees(request, Employee.objects.all())
    dept_costs_qs = SalaryStructure.objects.filter(employee__in=employees).values('employee__department').annotate(
        employee_count=Count('id'),
        total_cost=Sum(F('basic') + F('hra') + F('conveyance') + F('special_allowance'))
    ).order_by('employee__department')
    dept_costs = [{'department': row['employee__department'], 'employee_count': row['employee_count'], 'total_cost': row['total_cost'] or 0} for row in dept_costs_qs]
    overall_total = sum(d['total_cost'] for d in dept_costs)
    chart_labels = _js([d['department'] for d in dept_costs])
    chart_values = _js([float(d['total_cost']) for d in dept_costs])
    return render(request, 'Payroll/Payroll Cost.html', {'dept_costs': dept_costs, 'overall_total': overall_total, 'chart_labels': chart_labels, 'chart_values': chart_values})


@company_owner_required
def pending_payroll_report(request):
    employees = scope_employees(request, Employee.objects.all())
    pending_runs = scope_runs(request).exclude(status='RELEASED').prefetch_related('lines')
    return render(request, 'Payroll/Pending Payroll.html', {'pending_runs': pending_runs})


@company_owner_required
def employees_on_leave_report(request):
    from datetime import date
    today = date.today()
    leave_qs = scope_by_employee_fk(request, LeaveRequest.objects.select_related('employee'))
    on_leave = leave_qs.filter(status='APPROVED', from_date__lte=today, to_date__gte=today)
    return render(request, 'Payroll/Employees On Leave.html', {'on_leave': on_leave, 'today': today})


# ---------------------------------------------------------------------------
# Payroll processing
# ---------------------------------------------------------------------------

def scope_runs(request, queryset=None):
    """Payroll runs visible to the caller. Runs are owned by a company."""
    qs = queryset if queryset is not None else PayrollRun.objects.all()
    user = request.user
    if user.is_superuser or user.role == 'ADMIN':
        return qs
    if user.role == 'COMPANY_OWNER':
        return qs.filter(company=user.company)
    return qs.none()


def _get_run_scoped(request, pk):
    run = get_object_or_404(PayrollRun, pk=pk)
    if not scope_runs(request, PayrollRun.objects.filter(pk=run.pk)).exists():
        raise PermissionDenied("This payroll run does not belong to your company.")
    return run


def _assert_run_in_scope(request, run, employees_qs=None):
    """Kept for backwards compatibility with existing callers/tests."""
    if not scope_runs(request, PayrollRun.objects.filter(pk=run.pk)).exists():
        raise PermissionDenied("This payroll run does not belong to your company.")


@company_owner_required
def payroll_run_download_docx(request, pk):
    from docx import Document
    run = _get_run_scoped(request, pk)
    lines = run.lines.select_related('employee')
    doc = Document()
    doc.add_heading(f'Payroll Report - {run.month}', level=1)
    doc.add_paragraph(f'Company: {run.company.name if run.company else "-"}')
    doc.add_paragraph(f'Status: {run.get_status_display()}')
    doc.add_paragraph(f'Created: {run.created_at.strftime("%d %b %Y")}')
    headers = ['Employee Code', 'Name', 'Gross', 'LOP', 'Arrears', 'Reimb.', 'Deductions', 'Net Pay']
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = 'Light Grid Accent 1'
    for i, text in enumerate(headers):
        table.rows[0].cells[i].text = text
    for line in lines:
        cells = table.add_row().cells
        values = [line.employee.employee_code, line.employee.full_name, line.gross_salary, line.lop_amount,
                  line.arrears, line.reimbursements, line.total_deductions, line.net_pay]
        for i, v in enumerate(values):
            cells[i].text = str(v)
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    response['Content-Disposition'] = f'attachment; filename="Payroll_{run.month.replace(" ", "_")}.docx"'
    doc.save(response)
    log_action(request, 'OTHER', run, details='Downloaded payroll run as DOCX')
    return response


@company_owner_required
def payroll_run_detail(request, pk):
    run = _get_run_scoped(request, pk)
    lines = run.lines.select_related('employee')
    totals = lines.aggregate(gross=Sum('gross_salary'), deductions=Sum('total_deductions'), net=Sum('net_pay'))
    return render(request, 'Payroll/Payroll Run Detail.html', {'run': run, 'lines': lines, 'totals': totals})


# Allowed status transitions — a run can only move one step at a time.
RUN_TRANSITIONS = {
    'validate': ('DRAFT', 'VALIDATED'),
    'approve': ('VALIDATED', 'APPROVED'),
    'release': ('APPROVED', 'RELEASED'),
}


@company_owner_required
def payroll_combined(request):
    is_admin = request.user.is_superuser or request.user.role == 'ADMIN'
    company_qs = Company.objects.filter(status='APPROVED') if is_admin else None

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            form = PayrollRunForm(request.POST)
            if is_admin:
                company = Company.objects.filter(pk=request.POST.get('company'), status='APPROVED').first()
            else:
                company = request.user.company
            if company is None:
                messages.error(request, 'Select the company to run payroll for.')
            elif form.is_valid():
                month = form.cleaned_data['month']
                payroll_run = None
                with transaction.atomic():
                    # Lock the company row so two people clicking "create" for the same month
                    # cannot both pass the "already exists" check.
                    Company.objects.select_for_update().get(pk=company.pk)
                    if PayrollRun.objects.filter(company=company, month__iexact=month).exists():
                        messages.error(request, f"A payroll run for {month} already exists for {company.name}. "
                                                "Reprocess the existing run instead of creating a duplicate.")
                    else:
                        payroll_run = form.save(commit=False)
                        payroll_run.company = company
                        payroll_run.created_by = request.user
                        payroll_run.save()
                        emps = Employee.objects.filter(company=company, employment_status='ACTIVE')
                        period = parse_month(month)
                        if period:   # BUG-06: people who join after this month are not in its run
                            emps = emps.filter(Q(date_of_joining__isnull=True) | Q(date_of_joining__lte=period[1]))
                        created, skipped = build_run_lines(payroll_run, emps)
                if payroll_run is not None:
                    log_action(request, 'PROCESS_PAYROLL', payroll_run,
                               details=f'Payroll run created: {created} lines, {len(skipped)} skipped (no salary structure)')
                    messages.success(request, f"Payroll for {month} created with {created} employee(s).")
                    if skipped:
                        messages.warning(request, "Skipped (no salary structure): " +
                                         ', '.join(e.employee_code for e in skipped[:15]))
            else:
                messages.error(request, '; '.join(form.errors.get('month', ['Invalid payroll month.'])))
            return redirect('payroll_combined')

        run = _get_run_scoped(request, request.POST.get('run_id'))
        with transaction.atomic():
            # Lock this payroll run: two people pressing approve / release / reprocess at the
            # same time are handled one after the other, each seeing the latest status.
            run = PayrollRun.objects.select_for_update().get(pk=run.pk)
            if action in RUN_TRANSITIONS:
                required, target = RUN_TRANSITIONS[action]
                if run.is_locked:
                    messages.error(request, f"'{run.month}' is locked. Unlock it first.")
                elif run.status != required:
                    messages.error(request, f"Cannot {action} '{run.month}': it is {run.get_status_display()}, "
                                            f"expected {dict(PayrollRun.STATUS_CHOICES)[required]}. "
                                            "Someone else may have just changed it.")
                elif not run.lines.exists():
                    messages.error(request, f"'{run.month}' has no payslip lines. Add salary structures and reprocess.")
                else:
                    run.status = target
                    run.save(update_fields=['status'])
                    log_action(request, 'PROCESS_PAYROLL', run, details=f'Payroll run status -> {target}')
                    if target == 'RELEASED':
                        for line in run.lines.select_related('employee').exclude(employee__user__isnull=True):
                            Notification.objects.create(recipient_id=line.employee.user_id,
                                                        message=f"Your payslip for {run.month} is available.",
                                                        link=reverse('payslip_detail', args=[line.pk]))
                    messages.success(request, f"'{run.month}' is now {run.get_status_display()}.")
            elif action in ('lock', 'unlock'):
                run.is_locked = action == 'lock'
                run.save(update_fields=['is_locked'])
                log_action(request, 'PROCESS_PAYROLL', run, details=f'Payroll run {action}ed')
            elif action == 'reject':
                if run.status == 'RELEASED':
                    messages.error(request, 'A released payroll cannot be sent back to draft.')
                elif run.is_locked:
                    messages.error(request, f"'{run.month}' is locked. Unlock it first.")
                else:
                    run.status = 'DRAFT'
                    run.save(update_fields=['status'])
                    log_action(request, 'PROCESS_PAYROLL', run, details='Payroll run rejected back to draft')
            elif action == 'reprocess':
                if run.status == 'RELEASED':
                    messages.error(request, f"Cannot reprocess '{run.month}' - it has already been released.")
                elif run.is_locked:
                    messages.error(request, f"Cannot reprocess '{run.month}' - it is locked. Unlock it first.")
                else:
                    emps = Employee.objects.filter(company=run.company, employment_status='ACTIVE') if run.company \
                        else Employee.objects.filter(pk__in=run.lines.values('employee'))
                    with transaction.atomic():
                        release_claims(run, Employee.objects.filter(pk__in=run.lines.values('employee')))
                        run.lines.all().delete()
                        created, skipped = build_run_lines(run, emps)
                        run.status = 'DRAFT'
                        run.save(update_fields=['status'])
                    log_action(request, 'PROCESS_PAYROLL', run, details=f'Payroll run reprocessed ({created} lines)')
                    messages.success(request, f"'{run.month}' reprocessed successfully.")
            else:
                messages.error(request, 'Unknown action.')
        return redirect('payroll_combined')

    form = PayrollRunForm()
    all_runs = scope_runs(request).select_related('company').prefetch_related('lines__employee')
    released_lines = PayrollRunLine.objects.filter(payroll_run__in=scope_runs(request), payroll_run__status='RELEASED')
    summary = {
        'total_runs': all_runs.count(),
        'total_employees_paid': released_lines.count(),
        'total_net_payout': released_lines.aggregate(t=Sum('net_pay'))['t'] or 0,
        'locked_count': all_runs.filter(is_locked=True).count(),
    }
    paginator = Paginator(all_runs, PAGE_SIZE)
    payroll_runs = paginator.get_page(request.GET.get('page'))
    return render(request, 'Payroll/Payroll Combined.html', {
        'form': form, 'payroll_runs': payroll_runs, 'summary': summary, 'company_choices': company_qs,
    })


# ---------------------------------------------------------------------------
# Arrears / Full & Final Settlement
# ---------------------------------------------------------------------------

@company_owner_required
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


@company_owner_required
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

@owner_or_employee_required
def payslips(request):
    lines = scope_by_employee_fk(
        request, PayrollRunLine.objects.select_related('employee', 'payroll_run')
    ).filter(payroll_run__status='RELEASED')
    return render(request, 'Payslips.html', {'lines': lines})


def _scope_payments(request):
    qs = BankPayment.objects.select_related('payroll_line__employee', 'payroll_line__payroll_run', 'verified_by')
    user = request.user
    if user.is_superuser or user.role == 'ADMIN':
        return qs
    if user.role == 'COMPANY_OWNER':
        return qs.filter(company=user.company)
    return qs.none()


@company_owner_required
def bank_transfer(request):
    employees = scope_employees(request, Employee.objects.all())
    lines = (PayrollRunLine.objects
             .filter(employee__in=employees, payroll_run__status='RELEASED')
             .select_related('employee', 'payroll_run', 'bank_payment')
             .order_by('-payroll_run__created_at', 'employee__employee_code'))

    if request.method == 'POST' and request.POST.get('action') == 'prepare':
        created, missing = 0, 0
        with transaction.atomic():
            for line in lines.filter(bank_payment__isnull=True):
                emp = line.employee
                if not emp.has_bank_details:
                    missing += 1
                    continue
                # get_or_create: if another person prepared this payslip a moment ago, skip it
                # instead of crashing on the one-payment-per-payslip rule.
                payment, made = BankPayment.objects.get_or_create(
                    payroll_line=line,
                    defaults=dict(
                        company=emp.company, amount=line.net_pay,
                        bank_name=emp.bank_name, account_holder_name=emp.account_holder_name or emp.full_name,
                        account_no=emp.bank_account_no, ifsc_code=emp.ifsc_code,
                    ),
                )
                if not made:
                    continue
                log_action(request, 'PAYMENT_STATUS_CHANGE', payment, company=emp.company,
                           details=f'Payment prepared for {line.payslip_number}: {line.net_pay}')
                created += 1
        if created:
            messages.success(request, f"{created} payment instruction(s) prepared. Download the transfer file, "
                                      "upload it to your bank, then record each UTR to mark it paid.")
        if missing:
            messages.warning(request, f"{missing} employee(s) skipped: Missing Bank Details.")
        if not created and not missing:
            messages.info(request, 'All released payslips already have payment instructions.')
        return redirect('bank_transfer')

    rows = []
    for line in lines:
        payment = getattr(line, 'bank_payment', None)
        if payment:
            state = payment.get_status_display()
        elif not line.employee.has_bank_details:
            state = 'Missing Bank Details'
        else:
            state = 'Not prepared'
        rows.append({'line': line, 'payment': payment, 'state': state})
    payments = _scope_payments(request)
    return render(request, 'Bank Transfer.html', {
        'rows': rows, 'tab': 'overview',
        'kpi': {
            'to_pay': sum(1 for r in rows if not r['payment'] or r['payment'].status != 'PAID'),
            'missing': sum(1 for r in rows if r['state'] == 'Missing Bank Details'),
            'paid': payments.filter(status='PAID').count(),
            'failed': payments.filter(status='FAILED').count(),
        },
        'update_form': BankPaymentUpdateForm(),
    })


@company_owner_required
@require_POST
def bank_payment_update(request, pk):
    from django.db import IntegrityError
    payment = get_object_or_404(BankPayment, pk=pk)
    if not _scope_payments(request).filter(pk=pk).exists():
        raise PermissionDenied("This payment does not belong to your company.")
    form = BankPaymentUpdateForm(request.POST)
    back = request.POST.get('next') if request.POST.get('next') in ('bank_transfer', 'payment_states', 'failed_transaction_report') else 'payment_states'
    if not form.is_valid():
        messages.error(request, ' '.join(form.non_field_errors()) or 'Invalid payment update.')
        return redirect(back)
    action = form.cleaned_data['action']
    with transaction.atomic():
        # Lock the payment row: a payment can be marked paid only once, even if two people
        # type the UTR at the same moment.
        payment = BankPayment.objects.select_for_update().get(pk=pk)
        old = payment.status
        if payment.status == 'PAID':
            messages.error(request, 'This payment is already marked paid and cannot be changed.')
            return redirect(back)
        if action == 'INITIATE' and payment.status == 'PENDING':
            payment.status, payment.initiated_at = 'INITIATED', timezone.now()
        elif action == 'PAID' and payment.status in ('PENDING', 'INITIATED'):
            ref = form.cleaned_data['reference_number']
            if BankPayment.objects.filter(reference_number=ref).exclude(pk=payment.pk).exists():
                messages.error(request, f"Reference {ref} is already recorded against another payment.")
                return redirect(back)
            payment.status, payment.reference_number = 'PAID', ref
            payment.paid_at, payment.verified_by = timezone.now(), request.user
            payment.failure_reason = ''
        elif action == 'FAILED' and payment.status in ('PENDING', 'INITIATED'):
            payment.status, payment.failure_reason = 'FAILED', form.cleaned_data['failure_reason'][:255]
        elif action == 'RETRY' and payment.status == 'FAILED':
            emp = payment.payroll_line.employee
            if not emp.has_bank_details:
                messages.error(request, f"{emp.full_name}: Missing Bank Details - update the employee record first.")
                return redirect(back)
            payment.status, payment.attempts = 'PENDING', payment.attempts + 1
            payment.account_no, payment.ifsc_code = emp.bank_account_no, emp.ifsc_code
            payment.bank_name, payment.account_holder_name = emp.bank_name, emp.account_holder_name or emp.full_name
        else:
            messages.error(request, f"Cannot {action.lower()} a payment that is {payment.get_status_display().lower()}.")
            return redirect(back)
        try:
            with transaction.atomic():
                payment.save()
        except IntegrityError:
            messages.error(request, 'That reference number was just recorded against another payment.')
            return redirect(back)
        log_action(request, 'PAYMENT_STATUS_CHANGE', payment, company=payment.company,
                   details=f'{payment.payroll_line.payslip_number}: {old} -> {payment.status}'
                           + (f' ref={payment.reference_number}' if payment.status == 'PAID' else ''))
        messages.success(request, f"Payment for {payment.payroll_line.employee.full_name} is now {payment.get_status_display()}.")
    return redirect(back)


@owner_or_employee_required
def reports_analytics(request):
    employees = scope_employees(request, Employee.objects.all())
    if request.user.role == 'EMPLOYEE':
        runs = PayrollRun.objects.filter(lines__employee__in=employees, status='RELEASED').distinct()
    else:
        runs = scope_runs(request)
    leaves = scope_by_employee_fk(request, LeaveRequest.objects.all())
    context = {
        'total_employees': employees.count(),
        'total_payroll_runs': runs.count(),
        'total_released': runs.filter(status='RELEASED').count(),
        'total_pending_leave': leaves.filter(status='PENDING').count(),
    }
    chart_labels = _js(['Employees', 'Payroll Runs', 'Released', 'Pending Leave'])
    chart_values = _js([context['total_employees'], context['total_payroll_runs'], context['total_released'], context['total_pending_leave']])
    context['chart_labels'] = chart_labels
    context['chart_values'] = chart_values
    return render(request, 'Reports and Analytics.html', context)


@owner_or_employee_required
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
        run_qs = scope_runs(request).filter(status='VALIDATED')[:10]
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


@company_owner_required
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


@company_owner_required
def settings_view(request):
    from .concurrency import remember_version, stale_warning
    company = get_user_company(request)
    if company is None and not (request.user.is_superuser or request.user.role == 'ADMIN'):
        raise PermissionDenied
    company_settings, _ = CompanySettings.objects.get_or_create(company=company) if company else (
        CompanySettings.objects.get_or_create(pk=1)
    )
    form = None
    if request.method == 'POST':
        with transaction.atomic():
            locked = CompanySettings.objects.select_for_update().get(pk=company_settings.pk)
            warning = stale_warning(request, locked, 'company settings')
            if warning:
                messages.warning(request, warning)
            else:
                form = CompanySettingsForm(request.POST, instance=locked)
                if form.is_valid():
                    form.save()
                    log_action(request, 'UPDATE', locked, details='Company settings updated')
                    return redirect('settings')
            company_settings = locked
    if form is None:
        form = CompanySettingsForm(instance=company_settings)
    remember_version(request, company_settings)
    return render(request, 'Settings.html', {'form': form})


@owner_or_employee_required
def payslip_history(request):
    lines = scope_by_employee_fk(
        request, PayrollRunLine.objects.select_related('employee', 'payroll_run')
    ).filter(payroll_run__status='RELEASED').order_by('-payroll_run__created_at')
    paginator = Paginator(lines, 15)
    lines_page = paginator.get_page(request.GET.get('page'))
    return render(request, 'payslip management/payslip History.html', {'lines': lines_page})


def _scoped_released_lines(request):
    return scope_by_employee_fk(
        request, PayrollRunLine.objects.select_related('employee', 'employee__company', 'payroll_run')
    ).filter(payroll_run__status='RELEASED')


@owner_or_employee_required
def download_pdf(request):
    return render(request, 'payslip management/Download PDF.html', {'lines': _scoped_released_lines(request)})


@owner_or_employee_required
def payslip_detail(request, pk):
    line = get_object_or_404(_scoped_released_lines(request), pk=pk)
    return render(request, 'payslip management/Generate payslip.html', {'line': line})


@owner_or_employee_required
def payslip_pdf(request, pk):
    # get_object_or_404 on the *scoped* queryset: another employee's payslip
    # id simply doesn't exist for this user (no IDOR).
    line = get_object_or_404(_scoped_released_lines(request), pk=pk)
    from .pdf import build_payslip_pdf
    pdf_bytes = build_payslip_pdf(line)
    log_action(request, 'OTHER', line, company=line.employee.company, details=f'Payslip PDF downloaded {line.payslip_number}')
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{line.payslip_number or "payslip"}.pdf"'
    return response


@owner_or_employee_required
def email_payslip(request):
    lines = _scoped_released_lines(request)
    sent = False
    if request.method == 'POST':
        from django.core.mail import EmailMessage
        from django.conf import settings as dj_settings
        from .pdf import build_payslip_pdf
        line = get_object_or_404(lines, pk=request.POST.get('line_id'))
        try:
            msg = EmailMessage(
                subject=f'Payslip - {line.payroll_run.month}',
                body=f'Dear {line.employee.full_name},\n\nPlease find attached your payslip for {line.payroll_run.month}.\n'
                     f'Net Pay: {line.net_pay}\n\nRegards,\nPayroll Team',
                from_email=dj_settings.DEFAULT_FROM_EMAIL, to=[line.employee.email],
            )
            msg.attach(f'{line.payslip_number or "payslip"}.pdf', build_payslip_pdf(line), 'application/pdf')
            msg.send(fail_silently=False)
            sent = True
            log_action(request, 'OTHER', line, company=line.employee.company, details='Payslip emailed')
            messages.success(request, f"Payslip emailed to {line.employee.email}.")
        except Exception:
            logger.exception('Emailing payslip %s failed', line.pk)
            messages.error(request, "Could not send the email right now (mail server unavailable). Please try again later.")
    return render(request, 'payslip management/Email payslip.html', {'lines': lines, 'sent': sent})


@company_owner_required
def failed_transaction_report(request):
    payments = _scope_payments(request).filter(status='FAILED')
    return render(request, 'Bank Transfer.html', {'payments': payments, 'tab': 'failed', 'update_form': BankPaymentUpdateForm()})


@owner_or_employee_required
def generate_payslip(request):
    # Legacy URL without an id: show the list the user may pick from.
    return redirect('download_pdf')


@company_owner_required
def payment_states(request):
    payments = _scope_payments(request)
    status = request.GET.get('status', '')
    if status in dict(BankPayment.STATUS_CHOICES):
        payments = payments.filter(status=status)
    return render(request, 'Bank Transfer.html', {
        'payments': payments, 'tab': 'states', 'status': status,
        'status_choices': BankPayment.STATUS_CHOICES, 'update_form': BankPaymentUpdateForm(),
    })


@company_owner_required
def salary_transfer_file(request):
    """Bank upload file for payments not yet paid (PENDING / INITIATED)."""
    payments = _scope_payments(request).filter(status__in=['PENDING', 'INITIATED'])
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="salary_transfer_{timezone.localdate():%Y%m%d}.csv"'
    writer = csv.writer(response)
    writer.writerow(['Payment ID', 'Beneficiary Name', 'Account Number', 'IFSC', 'Bank', 'Amount', 'Mode', 'Narration'])
    for p in payments:
        writer.writerow([p.pk, p.account_holder_name, p.account_no, p.ifsc_code, p.bank_name,
                         f'{p.amount:.2f}', p.mode, f'Salary {p.payroll_line.payroll_run.month} {p.payroll_line.payslip_number}'])
    log_action(request, 'OTHER', details=f'Salary transfer file exported ({payments.count()} payments)')
    return response


PAYSLIP_EXPORT_HEADERS = ['Payslip No', 'Month', 'Employee Code', 'Name', 'Basic', 'Gross Salary', 'LOP Days',
                          'LOP Amount', 'Arrears', 'Reimbursements', 'Total Earnings', 'PF', 'ESI', 'TDS',
                          'Total Deductions', 'Net Pay']


def _payslip_export_row(line):
    return [line.payslip_number, line.payroll_run.month, line.employee.employee_code, line.employee.full_name,
            line.basic, line.gross_salary, line.lop_days, line.lop_amount, line.arrears, line.reimbursements,
            line.total_earnings, line.pf, line.esi, line.tds, line.total_deductions, line.net_pay]


@company_owner_required
def payslips_export_csv(request):
    employees = scope_employees(request, Employee.objects.all())
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="payslips.csv"'
    writer = csv.writer(response)
    writer.writerow(PAYSLIP_EXPORT_HEADERS)
    lines = PayrollRunLine.objects.filter(payroll_run__status='RELEASED', employee__in=employees).select_related('employee', 'payroll_run')
    for line in lines:
        writer.writerow(_payslip_export_row(line))
    log_action(request, 'OTHER', details='Exported payslips CSV')
    return response


@company_owner_required
def payslips_export_excel(request):
    import openpyxl
    from openpyxl.utils import get_column_letter

    employees = scope_employees(request, Employee.objects.all())
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Payslips"
    headers = PAYSLIP_EXPORT_HEADERS
    ws.append(headers)

    lines = PayrollRunLine.objects.filter(payroll_run__status='RELEASED', employee__in=employees).select_related('employee', 'payroll_run')
    for line in lines:
        ws.append([float(v) if isinstance(v, Decimal) else v for v in _payslip_export_row(line)])

    for i, header in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(i)].width = max(14, len(header) + 4)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="payslips.xlsx"'
    wb.save(response)
    log_action(request, 'OTHER', details='Exported payslips Excel')
    return response
