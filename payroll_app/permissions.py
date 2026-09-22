"""Backend role-based access control helpers.

These are enforced server-side on every view — hiding a sidebar link is
never treated as sufficient protection on its own.
"""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404


def role_required(*allowed_roles):
    """Restrict a view to specific User.role values. Superusers/ADMIN always pass.
    Must be combined with @login_required (role_required applies it automatically)."""

    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def _wrapped(request, *args, **kwargs):
            user = request.user
            if user.is_superuser or getattr(user, 'role', None) == 'ADMIN':
                return view_func(request, *args, **kwargs)
            if getattr(user, 'role', None) in allowed_roles:
                return view_func(request, *args, **kwargs)
            raise PermissionDenied("You do not have permission to access this page.")
        return _wrapped
    return decorator


def admin_required(view_func):
    return role_required('ADMIN')(view_func)


def company_owner_required(view_func):
    return role_required('COMPANY_OWNER')(view_func)


def owner_or_admin_required(view_func):
    return role_required('COMPANY_OWNER', 'ADMIN')(view_func)


def any_authenticated_required(view_func):
    return role_required('ADMIN', 'COMPANY_OWNER', 'EMPLOYEE')(view_func)


def get_user_company(request):
    """Return the company a non-admin user is scoped to, or None for ADMIN
    (meaning: no restriction / sees everything)."""
    user = request.user
    if user.is_superuser or user.role == 'ADMIN':
        return None
    return user.company


def scope_employees(request, queryset):
    """Restrict an Employee queryset by the caller's role.
    ADMIN: no restriction.
    COMPANY_OWNER: only employees of their own company.
    EMPLOYEE: only their own employee record.
    """
    user = request.user
    if user.is_superuser or user.role == 'ADMIN':
        return queryset
    if user.role == 'COMPANY_OWNER':
        return queryset.filter(company=user.company)
    if user.role == 'EMPLOYEE':
        return queryset.filter(user=user)
    return queryset.none()


def scope_by_employee_fk(request, queryset, employee_field='employee'):
    """Restrict any queryset that has a FK to Employee (attendance, leave,
    reimbursement, payslips, etc.) using the same rules as scope_employees."""
    user = request.user
    if user.is_superuser or user.role == 'ADMIN':
        return queryset
    if user.role == 'COMPANY_OWNER':
        return queryset.filter(**{f'{employee_field}__company': user.company})
    if user.role == 'EMPLOYEE':
        return queryset.filter(**{employee_field: getattr(user, 'employee_profile', None)})
    return queryset.none()


def get_object_scoped(request, model, employee_field='employee', **lookup):
    """get_object_or_404 that also enforces company/employee isolation,
    closing IDOR holes on direct URL/PK access."""
    obj = get_object_or_404(model, **lookup)
    user = request.user
    if user.is_superuser or user.role == 'ADMIN':
        return obj
    target = obj
    for part in employee_field.split('__'):
        target = getattr(target, part, None)
        if target is None:
            break
    if user.role == 'COMPANY_OWNER':
        emp = obj if employee_field == '' else getattr(obj, employee_field.split('__')[0], obj)
        company = getattr(emp, 'company', None) if hasattr(emp, 'company') else None
        if company is None and hasattr(obj, 'company'):
            company = obj.company
        if company != user.company:
            raise PermissionDenied("You cannot access another company's data.")
    elif user.role == 'EMPLOYEE':
        emp = getattr(obj, employee_field.split('__')[0], obj) if employee_field else obj
        if emp != getattr(user, 'employee_profile', None):
            raise PermissionDenied("You cannot access another employee's data.")
    return obj
