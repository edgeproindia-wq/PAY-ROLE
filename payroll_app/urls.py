from django.urls import path
from django.contrib.auth import views as auth_views
from . import views
from .forms import LoginForm

urlpatterns = [
    # ---- Public / auth ----------------------------------------------------
    path('welcome/', views.landing, name='landing'),
    path('request-demo/', views.request_demo, name='request_demo'),
    path('register/', views.company_register, name='company_register'),
    path('login/', auth_views.LoginView.as_view(
        template_name='auth/login.html', authentication_form=LoginForm, redirect_authenticated_user=True,
    ), name='login'),
    path('verify-email/', views.verify_email, name='verify_email'),
    path('logout/', views.logout_view, name='logout'),
    path('post-login/', views.post_login_redirect, name='post_login_redirect'),
    path('password-reset/', auth_views.PasswordResetView.as_view(
        template_name='auth/password_reset.html',
        email_template_name='auth/password_reset_email.html',
        subject_template_name='auth/password_reset_subject.txt',
    ), name='password_reset'),
    path('password-reset/done/', auth_views.PasswordResetDoneView.as_view(
        template_name='auth/password_reset_done.html'), name='password_reset_done'),
    path('reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(
        template_name='auth/password_reset_confirm.html'), name='password_reset_confirm'),
    path('reset/done/', auth_views.PasswordResetCompleteView.as_view(
        template_name='auth/password_reset_complete.html'), name='password_reset_complete'),
    path('account/password-change/', auth_views.PasswordChangeView.as_view(
        template_name='auth/password_change.html'), name='password_change'),
    path('account/password-change/done/', auth_views.PasswordChangeDoneView.as_view(
        template_name='auth/password_change_done.html'), name='password_change_done'),

    # ---- Admin panel (platform ADMIN only) --------------------------------
    path('admin-panel/', views.admin_dashboard, name='admin_dashboard'),
    path('admin-panel/demo-requests/', views.admin_demo_requests, name='admin_demo_requests'),
    path('admin-panel/demo-requests/<int:pk>/decide/', views.admin_demo_request_decide, name='admin_demo_request_decide'),
    path('admin-panel/company-approvals/', views.admin_company_approvals, name='admin_company_approvals'),
    path('admin-panel/company-approvals/<int:pk>/decide/', views.admin_company_decide, name='admin_company_decide'),
    path('admin-panel/companies/', views.admin_company_list, name='admin_company_list'),
    path('admin-panel/complaints/', views.admin_client_complaints, name='admin_client_complaints'),
    path('admin-panel/complaints/<int:pk>/respond/', views.admin_complaint_respond, name='admin_complaint_respond'),
    path('admin-panel/requests/', views.admin_client_requests, name='admin_client_requests'),
    path('admin-panel/requests/<int:pk>/respond/', views.admin_request_respond, name='admin_request_respond'),
    path('admin-panel/audit-log/', views.admin_audit_log, name='admin_audit_log'),

    # ---- Client (company owner) workflows ----------------------------------
    path('client/complaints/', views.client_complaints, name='client_complaints'),
    path('client/requests/', views.client_requests, name='client_requests'),
    path('employee/<int:pk>/create-account/', views.employee_create_account, name='employee_create_account'),

    # ---- Core payroll app (existing routes, now access-controlled) --------
    path('', views.dashboard, name='dashboard'),

    path('employee_master/', views.employee_master, name='employee_master'),
    path('employee_master/export/csv/', views.employee_master_export_csv, name='employee_master_export_csv'),
    path('employee/<int:pk>/edit/', views.employee_edit, name='employee_edit'),
    path('employee/<int:pk>/delete/', views.employee_delete, name='employee_delete'),

    path('salary_structure/', views.salary_structure, name='salary_structure'),
    path('attendance/', views.attendance, name='attendance'),
    path('attendance/check-<str:action>/', views.attendance_check, name='attendance_check'),
    path('leave_management/', views.leave_management, name='leave_management'),
    path('leave_management/<int:pk>/decide/', views.leave_decision, name='leave_decision'),
    path('reimbursement/', views.reimbursement, name='reimbursement'),
    path('reimbursement/<int:pk>/decide/', views.reimbursement_decision, name='reimbursement_decision'),
    path('reimbursement/<int:pk>/receipt/', views.reimbursement_receipt, name='reimbursement_receipt'),

    path('payslips/', views.payslips, name='payslips'),
    path('payslips/<int:pk>/', views.payslip_detail, name='payslip_detail'),
    path('payslips/<int:pk>/pdf/', views.payslip_pdf, name='payslip_pdf'),
    path('payslips/export/csv/', views.payslips_export_csv, name='payslips_export_csv'),
    path('payslips/export/excel/', views.payslips_export_excel, name='payslips_export_excel'),

    path('statutory_compliance/', views.statutory_compliance, name='statutory_compliance'),
    path('investment_declaration/', views.investment_declaration, name='investment_declaration'),
    path('income_tax/', views.income_tax, name='income_tax'),
    path('compliance_reports/', views.compliance_reports, name='compliance_reports'),

    path('total_employees/', views.total_employees_report, name='total_employees_report'),
    path('new_joiners/', views.new_joiners_report, name='new_joiners_report'),
    path('payroll_cost/', views.payroll_cost_report, name='payroll_cost_report'),
    path('pending_payroll/', views.pending_payroll_report, name='pending_payroll_report'),
    path('employees_on_leave/', views.employees_on_leave_report, name='employees_on_leave_report'),

    path('payroll_run/<int:pk>/', views.payroll_run_detail, name='payroll_run_detail'),
    path('payroll_run/<int:pk>/download/docx/', views.payroll_run_download_docx, name='payroll_run_download_docx'),
    path('payroll/', views.payroll_combined, name='payroll_combined'),
    path('payroll_processing/', views.payroll_combined, name='payroll_processing'),
    path('payroll_preview/', views.payroll_combined, name='payroll_preview'),
    path('payroll_validation/', views.payroll_combined, name='payroll_validation'),
    path('payroll_approval/', views.payroll_combined, name='payroll_approval'),
    path('payroll_release/', views.payroll_combined, name='payroll_release'),

    path('arrears/', views.arrears, name='arrears'),
    path('full_final_settlement/', views.full_final_settlement, name='full_final_settlement'),

    path('bank_transfer/', views.bank_transfer, name='bank_transfer'),
    path('bank_transfer/payment/<int:pk>/update/', views.bank_payment_update, name='bank_payment_update'),
    path('bank_transfer/download_pdf/', views.download_pdf, name='download_pdf'),
    path('bank_transfer/failed_transaction_report/', views.failed_transaction_report, name='failed_transaction_report'),
    path('bank_transfer/payment_states/', views.payment_states, name='payment_states'),
    path('bank_transfer/salary_transfer_file/', views.salary_transfer_file, name='salary_transfer_file'),

    path('reports_analytics/', views.reports_analytics, name='reports_analytics'),
    path('ess/', views.ess, name='ess'),
    path('notifications/', views.notifications, name='notifications'),
    path('notifications/<int:pk>/read/', views.notification_mark_read, name='notification_mark_read'),
    path('user_roles_permissions/', views.user_roles_permissions, name='user_roles_permissions'),
    path('settings/', views.settings_view, name='settings'),

    path('payslip_history/', views.payslip_history, name='payslip_history'),
    path('email_payslip/', views.email_payslip, name='email_payslip'),
    path('generate_payslip/', views.generate_payslip, name='generate_payslip'),

    path('page/<path:template_path>/', views.generic_page, name='generic_page'),
]

# ---- Phase 3: employee dashboard, documents, grievances, announcements, loans, insurance,
#      professional tax, investment proofs and the admin demo-request CRM ----
from . import employee_portal as _ep, features as _fx  # noqa: E402

urlpatterns.insert(0, path('post-login/', _ep.post_login, name='post_login_employee_aware'))
urlpatterns.insert(0, path('admin-panel/demo-requests/', _fx.admin_demo_list, name='admin_demo_list'))
urlpatterns += [
    path('me/', _ep.employee_home, name='employee_home'),
    path('me/documents/<int:pk>/', _ep.document_download, name='employee_document_download'),
    path('hr/tax-documents/', _ep.hr_documents, name='hr_documents'),
    path('hr/tax-documents/<int:pk>/download/', _ep.hr_document_download, name='hr_document_download'),
    path('leave/<int:pk>/cancel/', _fx.leave_cancel, name='leave_cancel'),
    path('grievances/', _fx.grievances, name='grievances'),
    path('grievances/<int:pk>/', _fx.grievance_detail, name='grievance_detail'),
    path('grievances/<int:pk>/attachment/', _fx.grievance_attachment, name='grievance_attachment'),
    path('announcements/', _fx.announcements, name='announcements'),
    path('hr/loans/', _fx.hr_loans, name='hr_loans'),
    path('hr/insurance/', _fx.hr_insurance, name='hr_insurance'),
    path('hr/professional-tax/', _fx.hr_professional_tax, name='hr_professional_tax'),
    path('hr/declarations/', _fx.hr_declarations, name='hr_declarations'),
    path('declarations/<int:pk>/proof/', _fx.declaration_proof, name='declaration_proof'),
    path('admin-panel/demo-requests/<int:pk>/', _fx.admin_demo_detail, name='admin_demo_detail'),
]

# ---- Phase 4: shifts / working hours and other pay components ----
from . import phase4 as _p4  # noqa: E402

urlpatterns += [
    path('hr/shifts/', _p4.hr_shifts, name='hr_shifts'),
    path('hr/work-hours/', _p4.hr_work_hours, name='hr_work_hours'),
    path('hr/pay-components/', _p4.hr_pay_components, name='hr_pay_components'),
]
