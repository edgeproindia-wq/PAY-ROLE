"""Data check page: shows exactly what the database holds for the signed-in company (owner) or for every company (platform admin)."""
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.shortcuts import render
from django.urls import path

from .models import Company, Employee, PayrollRun, User


@login_required
def data_check(request):
    user = request.user
    is_admin = user.is_superuser or user.role == 'ADMIN'
    if not is_admin and user.role != 'COMPANY_OWNER':
        raise PermissionDenied
    ctx = {'is_admin': is_admin, 'username': user.username, 'role': user.get_role_display(),
           'company': user.company, 'company_id': user.company_id}
    if is_admin:
        emp_counts = dict(Employee.objects.order_by().values_list('company_id').annotate(n=Count('id')))
        login_counts = dict(User.objects.exclude(company__isnull=True).order_by().values_list('company_id').annotate(n=Count('id')))
        ctx['rows'] = [{'id': c.id, 'name': c.name, 'status': c.status, 'emp_count': emp_counts.get(c.id, 0), 'login_count': login_counts.get(c.id, 0)}
                       for c in Company.objects.order_by('name')]
        ctx['no_company_users'] = User.objects.filter(company__isnull=True, is_superuser=False).count()
    else:
        emps = Employee.objects.filter(company=user.company)
        ctx['total'] = emps.count()
        ctx['by_status'] = list(emps.values('employment_status').annotate(n=Count('id')).order_by('-n'))
        ctx['with_login'] = emps.filter(user__isnull=False).count()
        ctx['without_login'] = emps.filter(user__isnull=True).count()
        ctx['sample'] = list(emps.order_by('employee_code')[:8])
        ctx['runs'] = PayrollRun.objects.filter(company=user.company).count()
    return render(request, 'data_check.html', ctx)


urlpatterns = [path('data-check/', data_check, name='data_check')]