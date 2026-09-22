def role_context(request):
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {'is_admin_role': False, 'is_company_owner': False, 'is_employee_role': False}
    return {
        'is_admin_role': user.is_superuser or user.role == 'ADMIN',
        'is_company_owner': user.role == 'COMPANY_OWNER',
        'is_employee_role': user.role == 'EMPLOYEE',
    }
