from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import (
    Company, User, Employee, SalaryStructure, Attendance, LeaveRequest,
    Reimbursement, PayrollRun, PayrollRunLine, CompanySettings, ArrearsRecord,
    FullFinalSettlement, UserRoleAssignment, InvestmentDeclaration,
    DemoRequest, ClientComplaint, ClientRequest, Notification, AuditLog,
)


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ('Payroll SaaS role', {'fields': ('role', 'company')}),
    )
    list_display = ('username', 'email', 'role', 'company', 'is_active', 'is_staff')
    list_filter = ('role', 'company', 'is_active')


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ('name', 'status', 'contact_email', 'created_at')
    list_filter = ('status',)
    search_fields = ('name', 'contact_email')


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ('employee_code', 'first_name', 'last_name', 'company', 'employment_status')
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
