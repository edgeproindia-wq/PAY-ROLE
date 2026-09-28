from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import (
    Company, User, Employee, SalaryStructure, Attendance, LeaveRequest,
    Reimbursement, PayrollRun, PayrollRunLine, CompanySettings, ArrearsRecord,
    FullFinalSettlement, UserRoleAssignment, InvestmentDeclaration,
    DemoRequest, ClientComplaint, ClientRequest, Notification, AuditLog,
    EmailOTP, BankPayment,
)


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
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ('employee_code', 'first_name', 'last_name', 'company', 'employment_status', 'bank_status')
    list_filter = ('company', 'employment_status', 'department')
    search_fields = ('employee_code', 'first_name', 'last_name', 'email')


admin.site.register(SalaryStructure)
admin.site.register(Attendance)
admin.site.register(LeaveRequest)
admin.site.register(Reimbursement)
admin.site.register(PayrollRun)
admin.site.register(PayrollRunLine)
admin.site.register(CompanySettings)
admin.site.register(ArrearsRecord)
admin.site.register(FullFinalSettlement)
admin.site.register(UserRoleAssignment)
admin.site.register(InvestmentDeclaration)


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


admin.site.register(Notification)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'actor', 'action', 'model_name', 'object_id', 'company')
    list_filter = ('action', 'company')
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(BankPayment)
class BankPaymentAdmin(admin.ModelAdmin):
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
