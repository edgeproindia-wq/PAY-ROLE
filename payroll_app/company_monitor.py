"""Company Monitoring: how each client company is using the product (platform admins only).

Usage and health only. This page never shows salaries, payroll amounts, bank details, payslips, employee names or the free-text
details of the audit log: the platform admin manages companies, not their payroll data (see permissions.py). Every view here is
read-only and protected with admin_required, so company owners and employees are refused, and the menu link is for admins only.

Health of an approved company: Active (someone logged in within INACTIVE_DAYS), Inactive (nobody did), Never logged in.
"""
import csv
import datetime

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import Count, F, IntegerField, Max, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import path
from django.utils import timezone

from .audit import log_action
from .models import AuditLog, Company, Employee
from .permissions import admin_required

User = get_user_model()
INACTIVE_DAYS = 14
WINDOW_DAYS = 30
PAGE_SIZE = 25
RECENT_EVENTS = 20
HEALTH_CHOICES = [('active', 'Active'), ('inactive', 'Inactive'), ('never', 'Never logged in'), ('waiting', 'Waiting for approval'), ('blocked', 'Suspended or rejected')]
SORT_CHOICES = [('activity', 'Last activity'), ('login', 'Last login'), ('registered', 'Newest first'), ('name', 'Name'), ('employees', 'Most employees')]


def _max_sub(model, field, extra=None):
    qs = model.objects.filter(company=OuterRef('pk'))
    if extra is not None:
        qs = qs.filter(extra)
    return Subquery(qs.order_by().values('company').annotate(m=Max(field)).values('m')[:1])


def _count_sub(model, **flt):
    qs = model.objects.filter(company=OuterRef('pk'), **flt).order_by().values('company').annotate(n=Count('pk')).values('n')
    return Coalesce(Subquery(qs[:1], output_field=IntegerField()), 0)


def monitored_companies(now=None):
    """Every company with its usage numbers worked out inside the database (no query per row)."""
    now = now or timezone.now()
    since = now - datetime.timedelta(days=WINDOW_DAYS)
    return Company.objects.annotate(
        active_users=_count_sub(User, is_active=True),
        active_employees=_count_sub(Employee, employment_status__in=['ACTIVE', 'ON_LEAVE']),
        last_login=_max_sub(User, 'last_login'),
        last_activity=_max_sub(AuditLog, 'timestamp'),
        last_payroll=_max_sub(AuditLog, 'timestamp', Q(action='PROCESS_PAYROLL')),
        actions_30d=_count_sub(AuditLog, timestamp__gte=since),
    )


def apply_health(qs, health, now):
    cutoff = now - datetime.timedelta(days=INACTIVE_DAYS)
    if health == 'inactive':
        return qs.filter(status='APPROVED', last_login__lt=cutoff)
    if health == 'never':
        return qs.filter(status='APPROVED', last_login__isnull=True)
    if health == 'active':
        return qs.filter(status='APPROVED', last_login__gte=cutoff)
    if health == 'waiting':
        return qs.filter(status='PENDING_APPROVAL')
    if health == 'blocked':
        return qs.filter(status__in=['SUSPENDED', 'REJECTED'])
    return qs


def health_of(c, now):
    """(key, label, badge class) for one company."""
    if c.status == 'PENDING_APPROVAL':
        days = (now - c.created_at).days
        return 'waiting', 'Waiting %d day%s' % (days, '' if days == 1 else 's'), 'badge-warn'
    if c.status in ('SUSPENDED', 'REJECTED'):
        return 'blocked', c.get_status_display(), 'badge-danger'
    if c.last_login is None:
        return 'never', 'Never logged in', 'badge-warn'
    if now - c.last_login > datetime.timedelta(days=INACTIVE_DAYS):
        return 'inactive', 'Inactive %d days' % (now - c.last_login).days, 'badge-danger'
    return 'active', 'Active', 'badge-success'


def summary(now):
    base = monitored_companies(now)
    seven = now - datetime.timedelta(days=7)
    return [
        ('Companies', base.count()), ('Approved', base.filter(status='APPROVED').count()), ('Waiting for approval', base.filter(status='PENDING_APPROVAL').count()),
        ('Suspended or rejected', base.filter(status__in=['SUSPENDED', 'REJECTED']).count()),
        ('Active in the last 7 days', base.filter(status='APPROVED', last_login__gte=seven).count()),
        ('Inactive (%d+ days)' % INACTIVE_DAYS, apply_health(base, 'inactive', now).count()), ('Never logged in', apply_health(base, 'never', now).count()),
    ]


def _safe(value):
    s = str(value)
    return "'" + s if s[:1] in ('=', '+', '-', '@', '\t', '\r') else s


def _fmt(when):
    return timezone.localtime(when).strftime('%Y-%m-%d %H:%M') if when else ''


def _filtered(request, now):
    qs = monitored_companies(now).prefetch_related(Prefetch('users', queryset=User.objects.filter(role='COMPANY_OWNER'), to_attr='owners'))
    q = (request.GET.get('q') or '').strip()
    status = request.GET.get('status', '')
    health = request.GET.get('health', '')
    sort = request.GET.get('sort', 'activity')
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(contact_email__icontains=q) | Q(contact_phone__icontains=q))
    if status in dict(Company.STATUS_CHOICES):
        qs = qs.filter(status=status)
    if health in dict(HEALTH_CHOICES):
        qs = apply_health(qs, health, now)
    order = {'activity': [F('last_activity').desc(nulls_last=True)], 'login': [F('last_login').desc(nulls_last=True)], 'registered': ['-created_at'],
             'name': ['name'], 'employees': ['-active_employees']}.get(sort) or [F('last_activity').desc(nulls_last=True)]
    return qs.order_by(*order, 'name'), {'q': q, 'status': status, 'health': health, 'sort': sort if sort in dict(SORT_CHOICES) else 'activity'}


@admin_required
def admin_company_monitor(request):
    """Feature: Company Monitoring (platform admins only)."""
    now = timezone.now()
    qs, params = _filtered(request, now)
    if request.GET.get('export') == 'csv':
        rows = list(qs[:5000])
        log_action(request, 'EXPORT', details=f'Company monitoring exported ({len(rows)} companies)')
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="company_monitoring_{now:%Y%m%d}.csv"'
        w = csv.writer(resp)
        w.writerow(['Company', 'Status', 'Health', 'Registered', 'Owner login', 'Contact email', 'Active users', 'Active employees', 'Last login', 'Last activity',
                    'Last payroll processed', f'Actions in {WINDOW_DAYS} days'])
        for c in rows:
            w.writerow([_safe(c.name), c.get_status_display(), health_of(c, now)[1], _fmt(c.created_at), _safe(', '.join(o.username for o in c.owners)), _safe(c.contact_email),
                        c.active_users, c.active_employees, _fmt(c.last_login), _fmt(c.last_activity), _fmt(c.last_payroll), c.actions_30d])
        return resp
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get('page'))
    rows = []
    for c in page.object_list:
        key, label, badge = health_of(c, now)
        rows.append({'c': c, 'health': key, 'label': label, 'badge': badge, 'owners': c.owners})
    from urllib.parse import urlencode
    return render(request, 'admin_panel/monitor.html', {
        'rows': rows, 'page': page, 'cards': summary(now), 'params': params, 'qs': urlencode({k: v for k, v in params.items() if v}),
        'status_choices': Company.STATUS_CHOICES, 'health_choices': HEALTH_CHOICES, 'sort_choices': SORT_CHOICES, 'inactive_days': INACTIVE_DAYS})


@admin_required
def admin_company_monitor_detail(request, pk):
    """One company: its profile, its users, headcount numbers and what it has been doing. No payroll data, no employee names."""
    now = timezone.now()
    company = get_object_or_404(monitored_companies(now), pk=pk)
    key, label, badge = health_of(company, now)
    since = now - datetime.timedelta(days=WINDOW_DAYS)
    users = list(User.objects.filter(company=company).order_by('role', 'username'))
    headcount = {r['employment_status']: r['n'] for r in Employee.objects.filter(company=company).values('employment_status').annotate(n=Count('pk'))}
    labels = dict(Employee.EMPLOYMENT_STATUS_CHOICES)
    action_names = dict(AuditLog.ACTION_CHOICES)
    counts = [{'name': action_names.get(r['action'], r['action']), 'n': r['n']} for r in
              AuditLog.objects.filter(company=company, timestamp__gte=since).order_by().values('action').annotate(n=Count('pk')).order_by('-n')]
    events = [{'when': e.timestamp, 'who': e.actor.username if e.actor_id else 'anonymous', 'what': e.get_action_display(), 'where': e.model_name}
              for e in AuditLog.objects.filter(company=company).select_related('actor').order_by('-timestamp')[:RECENT_EVENTS]]
    devices = []
    try:
        from .biometric_models import BiometricDevice
        devices = list(BiometricDevice.objects.filter(company=company).order_by('name'))
    except Exception:
        devices = []
    return render(request, 'admin_panel/monitor_detail.html', {
        'company': company, 'health': key, 'label': label, 'badge': badge, 'users': users, 'inactive_days': INACTIVE_DAYS, 'window': WINDOW_DAYS,
        'headcount': [(labels[k], headcount.get(k, 0)) for k in ('ACTIVE', 'ON_LEAVE', 'RESIGNED', 'TERMINATED')], 'action_counts': counts, 'events': events, 'devices': devices})


urlpatterns = [
    path('admin-panel/monitoring/', admin_company_monitor, name='admin_company_monitor'),
    path('admin-panel/monitoring/<int:pk>/', admin_company_monitor_detail, name='admin_company_monitor_detail'),
]
