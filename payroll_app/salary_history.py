"""Salary effective-date engine + the Salary history page.
salary_for_period(employee, period) is what payroll calls: the revision in force on the FIRST day of the payroll month."""
import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.shortcuts import redirect, render
from django.urls import path
from django.utils import timezone

from .audit import log_action
from .models import Employee, PayrollRun, SalaryStructure
from .permissions import company_owner_required
from .salary_history_models import SalaryRevision

BASE_DATE = datetime.date(2000, 1, 1)           # the first revision of every employee starts here, so any past month finds a salary
FIELDS = ('basic', 'hra', 'conveyance', 'special_allowance')


def salary_for_period(employee, period):
    """The salary structure in force for the payroll period (start, end). Returns an unsaved SalaryStructure-like object,
    the employee's saved structure when there is no history yet, or None when the employee has no salary at all."""
    start = period[0] if period else timezone.localdate()
    rev = SalaryRevision.objects.filter(employee=employee, effective_from__lte=start).order_by('-effective_from').first()
    if rev is not None:
        return SalaryStructure(employee=employee, basic=rev.basic, hra=rev.hra, conveyance=rev.conveyance, special_allowance=rev.special_allowance)
    try:
        return employee.salary_structure
    except SalaryStructure.DoesNotExist:
        return None


def sync_current_structure(employee, today=None):
    """Keep the saved 'current salary' (used by lists and the employee dashboard) equal to the revision in force today."""
    today = today or timezone.localdate()
    rev = SalaryRevision.objects.filter(employee=employee, effective_from__lte=today).order_by('-effective_from').first()
    if rev is None:
        return
    SalaryStructure.objects.update_or_create(employee=employee, defaults={f: getattr(rev, f) for f in FIELDS})


@receiver(post_save, sender=SalaryStructure)
def _structure_saved(sender, instance, created, **kwargs):
    """A salary structure saved the old way (Salary Structure page) also becomes / updates the base revision."""
    revs = SalaryRevision.objects.filter(employee_id=instance.employee_id)
    if not revs.exists():
        SalaryRevision.objects.create(employee_id=instance.employee_id, effective_from=BASE_DATE, note='Opening salary',
                                      **{f: getattr(instance, f) for f in FIELDS})
    elif revs.count() == 1 and revs.first().effective_from == BASE_DATE:
        revs.update(**{f: getattr(instance, f) for f in FIELDS})


def month_choices(back=36, ahead=12):
    today = timezone.localdate()
    out = []
    for i in range(-back, ahead + 1):
        y, m = divmod(today.year * 12 + today.month - 1 + i, 12)
        d = datetime.date(y, m + 1, 1)
        out.append((d.isoformat(), d.strftime('%B %Y')))
    return list(reversed(out))


def _amount(raw, label, errors, required=False):
    try:
        v = Decimal((raw or '0').strip() or '0')
    except InvalidOperation:
        errors.append(f'{label} must be a number.')
        return Decimal('0')
    if v < 0 or (required and v <= 0):
        errors.append(f'{label} must be {"more than zero" if required else "zero or more"}.')
    return v


@company_owner_required
def salary_history(request):
    company = request.user.company
    employees = Employee.objects.filter(company=company).order_by('employee_code')
    chosen = employees.filter(pk=request.GET.get('employee') or request.POST.get('employee') or 0).first()
    errors = []
    if request.method == 'POST' and chosen is not None:
        try:
            eff = datetime.date.fromisoformat(request.POST.get('effective_from', ''))
        except ValueError:
            eff = None
            errors.append('Choose the month the new salary starts.')
        if eff and eff.day != 1:
            errors.append('A salary change must start on the first day of a month. For a mid-month change use Arrears.')
        vals = {'basic': _amount(request.POST.get('basic'), 'Basic', errors, required=True)}
        for f, label in (('hra', 'HRA'), ('conveyance', 'Conveyance'), ('special_allowance', 'Special allowance')):
            vals[f] = _amount(request.POST.get(f), label, errors)
        if eff and SalaryRevision.objects.filter(employee=chosen, effective_from=eff).exists():
            errors.append(f'{chosen.employee_code} already has a salary starting {eff:%B %Y}. Pick another month.')
        if not errors:
            try:
                with transaction.atomic():
                    SalaryRevision.objects.create(employee=chosen, effective_from=eff, note=(request.POST.get('note') or '')[:255],
                                                  created_by=request.user, **vals)
                    sync_current_structure(chosen)
            except IntegrityError:
                errors.append('That salary was just saved by someone else. Reload the page.')
            else:
                log_action(request, 'UPDATE', chosen, details=f'Salary revised from {eff:%B %Y}: gross {sum(vals.values())}')
                runs = PayrollRun.objects.filter(company=company).exclude(status='DRAFT')
                released = [r.month for r in runs if _month_start(r.month) and _month_start(r.month) >= eff and r.status == 'RELEASED']
                drafts = [r.month for r in PayrollRun.objects.filter(company=company, status__in=('DRAFT', 'VALIDATED', 'APPROVED'))
                          if _month_start(r.month) and _month_start(r.month) >= eff]
                messages.success(request, f'New salary saved for {chosen.employee_code} from {eff:%B %Y}.')
                if released:
                    messages.warning(request, 'Already released and paid at the old salary (unchanged): ' + ', '.join(released) +
                                     '. Use Arrears to pay any difference.')
                if drafts:
                    messages.info(request, 'Not released yet - reprocess to use the new salary: ' + ', '.join(drafts))
                return redirect(f'{request.path}?employee={chosen.pk}')
    history = []
    if chosen is not None:
        sync_current_structure(chosen)
        revs = list(SalaryRevision.objects.filter(employee=chosen).select_related('created_by').order_by('effective_from'))
        today = timezone.localdate()
        for i, r in enumerate(revs):
            nxt = revs[i + 1].effective_from if i + 1 < len(revs) else None
            history.append({'rev': r, 'effective_to': (nxt - datetime.timedelta(days=1)) if nxt else None,
                            'in_force': r.effective_from <= today and (nxt is None or nxt > today)})
        history.reverse()
    latest = history[0]['rev'] if history else None
    current = next((h['rev'] for h in history if h['in_force']), latest)
    return render(request, 'salary_history.html', {'employees': employees, 'chosen': chosen, 'history': history, 'errors': errors,
                                                   'months': month_choices(), 'current': current, 'post': request.POST if request.method == 'POST' else {}})


def _month_start(label):
    from .payroll_engine import parse_month
    p = parse_month(label)
    return p[0] if p else None


urlpatterns = [path('salary-history/', salary_history, name='salary_history')]
