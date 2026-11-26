from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import (
    Company, User, Employee, SalaryStructure, Attendance, LeaveRequest,
    Reimbursement, PayrollRun, PayrollRunLine, CompanySettings, ArrearsRecord,
    FullFinalSettlement, UserRoleAssignment, InvestmentDeclaration,
    DemoRequest, ClientComplaint, ClientRequest, Notification, AuditLog,
    EmailOTP, BankPayment,
)


class CompanyScopedAdmin(admin.ModelAdmin):
    """Django admin for company payroll data, locked to ONE company.

    Rule: nobody sees another company's records here. A superuser (platform admin) manages companies and
    logins, never payroll data, exactly like the rest of the app, so these screens are empty for a superuser.
    A staff user who belongs to a company sees only that company's rows. `company_path` is the ORM path from
    the model to its Company."""
    company_path = 'company'

    def _company_id(self, request):
        u = request.user
        return None if u.is_superuser else getattr(u, 'company_id', None)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        cid = self._company_id(request)
        return qs.none() if cid is None else qs.filter(**{self.company_path + '_id': cid})

    def has_module_permission(self, request):
        return self._company_id(request) is not None and super().has_module_permission(request)

    def _owns(self, request, obj):
        if obj is None:
            return self._company_id(request) is not None
        return self.get_queryset(request).filter(pk=obj.pk).exists()

    def has_view_permission(self, request, obj=None):
        return self._owns(request, obj) and super().has_view_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._owns(request, obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._owns(request, obj) and super().has_delete_permission(request, obj)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # a drop-down must not list another company's employees / runs / companies
        cid = self._company_id(request)
        related = db_field.remote_field.model
        if cid is not None:
            if related is Company:
                kwargs['queryset'] = Company.objects.filter(pk=cid)
            elif related is Employee:
                kwargs['queryset'] = Employee.objects.filter(company_id=cid)
            elif related is PayrollRun:
                kwargs['queryset'] = PayrollRun.objects.filter(company_id=cid)
        elif related in (Company, Employee, PayrollRun):
            kwargs['queryset'] = related.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


def _scoped(model, path):
    """Register a payroll-data model with company scoping (path = ORM route to its company)."""
    cls = type(model.__name__ + 'ScopedAdmin', (CompanyScopedAdmin,), {'company_path': path})
    admin.site.register(model, cls)



@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ('Payroll SaaS role', {'fields': ('role', 'company', 'email_verified')}),
    )
    list_display = ('username', 'email', 'role', 'company', 'email_verified', 'is_active', 'is_staff')
    list_filter = ('role', 'company', 'is_active', 'email_verified')

    def has_delete_permission(self, request, obj=None):
        # The owner account of an approved client must never be deleted —
        # deactivate or suspend the company instead.
        if obj is not None and obj.role == 'COMPANY_OWNER' and obj.company and obj.company.status == 'APPROVED':
            return False
        return super().has_delete_permission(request, obj)


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ('name', 'status', 'contact_email', 'contact_phone', 'created_at', 'approved_at')
    list_filter = ('status',)
    search_fields = ('name', 'contact_email', 'contact_phone')

    def has_delete_permission(self, request, obj=None):
        # Deleting a company cascades to all its employees/payroll. Approved
        # (or once-approved) clients are never deletable; use SUSPENDED.
        if obj is not None and (obj.status in ('APPROVED', 'SUSPENDED') or obj.approved_at):
            return False
        if obj is None:
            return False  # disables bulk "delete selected" for companies
        return super().has_delete_permission(request, obj)


@admin.register(Employee)
class EmployeeAdmin(CompanyScopedAdmin):
    list_display = ('employee_code', 'first_name', 'last_name', 'company', 'employment_status', 'bank_status')
    list_filter = ('employment_status', 'department')
    search_fields = ('employee_code', 'first_name', 'last_name', 'email')


_scoped(SalaryStructure, 'employee__company')
_scoped(Attendance, 'employee__company')
_scoped(LeaveRequest, 'employee__company')
_scoped(Reimbursement, 'employee__company')
_scoped(PayrollRun, 'company')
_scoped(PayrollRunLine, 'payroll_run__company')
_scoped(CompanySettings, 'company')
_scoped(ArrearsRecord, 'employee__company')
_scoped(FullFinalSettlement, 'employee__company')
_scoped(UserRoleAssignment, 'employee__company')
_scoped(InvestmentDeclaration, 'employee__company')


@admin.register(DemoRequest)
class DemoRequestAdmin(admin.ModelAdmin):
    list_display = ('company_name', 'full_name', 'email', 'status', 'created_at')
    list_filter = ('status',)


@admin.register(ClientComplaint)
class ClientComplaintAdmin(admin.ModelAdmin):
    list_display = ('company', 'subject', 'status', 'created_at')
    list_filter = ('status', 'company')


@admin.register(ClientRequest)
class ClientRequestAdmin(admin.ModelAdmin):
    list_display = ('company', 'request_type', 'status', 'created_at')
    list_filter = ('status', 'company')


_scoped(Notification, 'recipient__company')


@admin.register(AuditLog)
class AuditLogAdmin(CompanyScopedAdmin):
    list_display = ('timestamp', 'actor', 'action', 'model_name', 'object_id', 'company')
    list_filter = ('action',)
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(BankPayment)
class BankPaymentAdmin(CompanyScopedAdmin):
    list_display = ('payroll_line', 'company', 'amount', 'status', 'reference_number', 'updated_at')
    list_filter = ('status', 'company')
    search_fields = ('reference_number', 'account_no', 'payroll_line__payslip_number')
    readonly_fields = ('payroll_line', 'company', 'amount', 'account_no', 'ifsc_code', 'verified_by', 'paid_at', 'created_at')

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EmailOTP)
class EmailOTPAdmin(admin.ModelAdmin):
    list_display = ('user', 'purpose', 'is_used', 'attempts', 'created_at', 'expires_at')
    exclude = ('code_hash',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


from .models_features import Announcement  # noqa: E402


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ('title', 'company', 'audience', 'priority', 'publish_date', 'expiry_date', 'status')
    list_filter = ('company', 'audience', 'priority', 'status')
    search_fields = ('title', 'message')
    date_hierarchy = 'publish_date'
