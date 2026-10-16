"""Start fresh: delete every employee of the signed-in company (and their logins), only while the company has no payroll yet.
Everything attached to an employee (salary, attendance, leave, loans, documents ...) is deleted with them, so the page shows the counts first."""
from django.contrib import messages
from django.contrib.admin.utils import NestedObjects
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import path

from .audit import log_action
from .models import Employee, PayrollRun, User
from .permissions import company_owner_required


def impact(company):
    """What a reset would delete, and whether it is allowed at all."""
    employees = list(Employee.objects.filter(company=company))
    collector = NestedObjects(using='default')
    collector.collect(employees)
    related = {}
    for model, objs in collector.model_objs.items():
        if model is not Employee and objs:
            related[str(model._meta.verbose_name_plural).capitalize()] = len(objs)
    user_ids = [e.user_id for e in employees if e.user_id]
    logins = User.objects.filter(pk__in=user_ids, role='EMPLOYEE', company=company).count()
    runs = PayrollRun.objects.filter(company=company).count()
    reasons = []
    if runs:
        reasons.append(f'This company already has {runs} payroll run(s). Payroll and payslip history must never be lost, so employees can no longer be cleared in one go. Delete or deactivate individual employees instead.')
    if collector.protected:
        reasons.append('Some records are protected from deletion, so the employees cannot be cleared.')
    return {'employees': len(employees), 'logins': logins, 'related': sorted(related.items()), 'related_total': sum(related.values()),
            'blocked': bool(reasons), 'reasons': reasons}


@company_owner_required
def employee_reset(request):
    company = request.user.company
    info = impact(company)
    if request.method == 'POST':
        typed = (request.POST.get('confirm_name') or '').strip()
        if info['blocked']:
            messages.error(request, 'Nothing was deleted: ' + info['reasons'][0])
        elif not info['employees']:
            messages.info(request, 'There are no employees to delete.')
        elif request.POST.get('understand') != 'yes':
            messages.error(request, 'Nothing was deleted. Tick the box to confirm that you understand.')
        elif typed != company.name.strip():
            messages.error(request, 'Nothing was deleted. The company name you typed does not match.')
        else:
            with transaction.atomic():
                if PayrollRun.objects.filter(company=company).exists():          # re-check inside the transaction
                    messages.error(request, 'Nothing was deleted: payroll was created in the meantime.')
                    return redirect('employee_reset')
                user_ids = [u for u in Employee.objects.filter(company=company, user__isnull=False).values_list('user_id', flat=True)]
                Employee.objects.filter(company=company).delete()
                User.objects.filter(pk__in=user_ids, role='EMPLOYEE', company=company).delete()      # never owners or admins
            log_action(request, 'DELETE', None, company=company,
                       details=f'All employees cleared: {info["employees"]} employees, {info["logins"]} logins, {info["related_total"]} related records')
            messages.success(request, f'Deleted {info["employees"]} employees, {info["logins"]} logins and {info["related_total"]} related records. You can import fresh now.')
            return redirect('employee_master')
        return redirect('employee_reset')
    return render(request, 'employee_reset.html', {'info': info, 'company': company})


urlpatterns = [path('employee-master/reset/', employee_reset, name='employee_reset')]