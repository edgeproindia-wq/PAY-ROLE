"""Platform admin only: move ALL employees (and their logins) from one company to another - only when it is safe.
Refuses if any employee already has payroll, loans, insurance, shifts, documents or other records that belong to the old company."""
from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path

from .audit import log_action
from .models import Company, Employee, User
from .permissions import admin_required

ALWAYS_BLOCK = {'PayrollRunLine', 'ShiftAssignment', 'EmployeeCalendar', 'ArrearsRecord', 'FullFinalSettlement', 'UserRoleAssignment'}


def blockers_for(employee_ids):
    """Records that point at these employees AND belong to a company - moving the employees would split them from it."""
    found = {}
    for rel in Employee._meta.related_objects:
        model = rel.related_model
        if model is User:
            continue
        has_company = any(f.name == 'company' for f in model._meta.get_fields() if hasattr(f, 'column'))
        if has_company or model.__name__ in ALWAYS_BLOCK:
            n = model.objects.filter(**{f'{rel.field.name}__in': employee_ids}).count()
            if n:
                found[model.__name__] = n
    return found


@admin_required
def move_employees(request):
    counts = dict(Employee.objects.order_by().values_list('company_id').annotate(n=Count('id')))
    companies = [{'id': c.id, 'name': c.name, 'employees': counts.get(c.id, 0)} for c in Company.objects.order_by('name')]
    ctx = {'companies': companies, 'preview': None, 'problems': []}
    if request.method == 'POST':
        src = get_object_or_404(Company, pk=request.POST.get('source') or 0)
        dst = get_object_or_404(Company, pk=request.POST.get('target') or 0)
        ids = list(Employee.objects.filter(company=src).values_list('pk', flat=True))
        problems = []
        if src.pk == dst.pk:
            problems.append('Choose two different companies.')
        if not ids:
            problems.append(f'{src.name} has no employees to move.')
        blocked = blockers_for(ids) if ids else {}
        if blocked:
            problems.append('These employees already have records tied to ' + src.name + ': ' +
                            ', '.join(f'{k} ({v})' for k, v in sorted(blocked.items())) + '. Moving them now would split those records, so nothing was moved.')
        src_codes = set(Employee.objects.filter(pk__in=ids).values_list('employee_code', flat=True))
        clash = sorted(src_codes & set(Employee.objects.filter(company=dst).values_list('employee_code', flat=True)))
        if clash:
            problems.append(f'{dst.name} already uses these Employee IDs: {", ".join(clash[:10])}. Nothing was moved.')
        login_ids = list(Employee.objects.filter(pk__in=ids, user__isnull=False).values_list('user_id', flat=True))
        ctx.update(problems=problems, src=src, dst=dst, n_emp=len(ids), n_login=len(login_ids),
                   preview=None if problems else True)
        if not problems and request.POST.get('confirm') == '1':
            with transaction.atomic():
                Employee.objects.filter(pk__in=ids).update(company=dst)
                User.objects.filter(pk__in=login_ids).update(company=dst)
            log_action(request, 'UPDATE', dst, details=f'Moved {len(ids)} employees and {len(login_ids)} logins from {src.name} to {dst.name}')
            messages.success(request, f'Moved {len(ids)} employees and {len(login_ids)} logins from {src.name} to {dst.name}.')
            return redirect('admin_move_employees')
    return render(request, 'admin_panel/move_employees.html', ctx)


urlpatterns = [path('admin-panel/move-employees/', move_employees, name='admin_move_employees')]