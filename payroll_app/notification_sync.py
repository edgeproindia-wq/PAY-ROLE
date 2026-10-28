"""Notifications - one place that CREATES them for business events and COUNTS them for the bell and dashboards.
(Leave/claim submitted, leave/claim decided, payslip released, company registered, demo request and documents are created elsewhere.)
Every receiver is wrapped so a notification problem can never break the real action."""
import logging

from django.db.models import Q
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.urls import NoReverseMatch, reverse

from .models import (ClientComplaint, ClientRequest, InvestmentDeclaration, LeaveRequest, Notification, PayrollRun, User)

try:
    from .models_features import Announcement, Grievance
except ImportError:                                   # optional modules
    Announcement = Grievance = None

logger = logging.getLogger(__name__)


def _link(name, *args, fallback='/'):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return fallback


def _send(user_ids, message, link=''):
    user_ids = [u for u in dict.fromkeys(user_ids) if u]
    if user_ids:
        Notification.objects.bulk_create([Notification(recipient_id=u, message=message[:255], link=link) for u in user_ids])


def _owners(company_id):
    return list(User.objects.filter(role='COMPANY_OWNER', company_id=company_id, is_active=True).values_list('id', flat=True))


def _admins():
    return list(User.objects.filter(Q(role='ADMIN') | Q(is_superuser=True), is_active=True).values_list('id', flat=True))


def _remember(sender, instance, field):
    """Store the value a field had before saving, so post_save can see what changed."""
    old = None
    if instance.pk:
        old = sender.objects.filter(pk=instance.pk).values_list(field, flat=True).first()
    setattr(instance, f'_old_{field}', old)


def _safe(fn):
    def wrapper(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception:
            logger.exception('Could not create a notification')
    wrapper.__name__ = fn.__name__
    return wrapper


# ------------------------------------------------------------------ leave cancelled -> owner
@receiver(pre_save, sender=LeaveRequest)
def _leave_before(sender, instance, **kw):
    _remember(sender, instance, 'status')


@receiver(post_save, sender=LeaveRequest)
@_safe
def _leave_after(sender, instance, created, **kw):
    if not created and instance.status == 'CANCELLED' and getattr(instance, '_old_status', None) not in (None, 'CANCELLED'):
        emp = instance.employee
        _send(_owners(emp.company_id), f'{emp.full_name} cancelled a leave request ({instance.from_date:%d %b} - {instance.to_date:%d %b}).',
              _link('leave_management', fallback='/leave_management/'))


# ------------------------------------------------------------------ payroll validated -> owner
@receiver(pre_save, sender=PayrollRun)
def _run_before(sender, instance, **kw):
    _remember(sender, instance, 'status')


@receiver(post_save, sender=PayrollRun)
@_safe
def _run_after(sender, instance, created, **kw):
    old = getattr(instance, '_old_status', None)
    if instance.status == 'VALIDATED' and old != 'VALIDATED':
        _send(_owners(instance.company_id), f'{instance.month} payroll is validated and waiting for your approval.',
              _link('payroll_run_detail', instance.pk, fallback='/payroll_processing/'))


# ------------------------------------------------------------------ investment declaration
@receiver(pre_save, sender=InvestmentDeclaration)
def _decl_before(sender, instance, **kw):
    _remember(sender, instance, 'is_verified')


@receiver(post_save, sender=InvestmentDeclaration)
@_safe
def _decl_after(sender, instance, created, **kw):
    emp = instance.employee
    if created:
        _send(_owners(emp.company_id), f'{emp.full_name} submitted an investment declaration ({instance.section}).',
              _link('hr_declarations', fallback='/investment_declaration/'))
    elif instance.is_verified and not getattr(instance, '_old_is_verified', False):
        _send([emp.user_id], f'Your {instance.section} investment declaration was verified.', _link('investment_declaration', fallback='/investment_declaration/'))


# ------------------------------------------------------------------ grievances
if Grievance is not None:
    @receiver(pre_save, sender=Grievance)
    def _griev_before(sender, instance, **kw):
        _remember(sender, instance, 'status')

    @receiver(post_save, sender=Grievance)
    @_safe
    def _griev_after(sender, instance, created, **kw):
        if created:
            _send(_owners(instance.company_id), f'New grievance {instance.tracking_id} from {instance.employee.full_name}: {instance.subject}',
                  _link('grievance_detail', instance.pk, fallback='/grievances/'))
        elif getattr(instance, '_old_status', None) not in (None, instance.status):
            _send([instance.employee.user_id], f'Your grievance {instance.tracking_id} is now {instance.get_status_display()}.',
                  _link('grievance_detail', instance.pk, fallback='/grievances/'))

    @receiver(post_save, sender=Announcement)
    @_safe
    def _announcement_after(sender, instance, created, **kw):
        if created and instance.status == 'PUBLISHED':
            ids = []
            if instance.audience in ('ALL', 'EMPLOYEES'):
                ids += User.objects.filter(role='EMPLOYEE', company_id=instance.company_id, is_active=True).values_list('id', flat=True)
            if instance.audience in ('ALL', 'OWNERS'):
                ids += [u for u in _owners(instance.company_id) if u != instance.created_by_id]
            _send(ids, f'New announcement: {instance.title}', _link('announcements', fallback='/announcements/'))


# ------------------------------------------------------------------ client complaints and requests -> admins, answers -> owner
@receiver(pre_save, sender=ClientComplaint)
def _complaint_before(sender, instance, **kw):
    _remember(sender, instance, 'status')


@receiver(post_save, sender=ClientComplaint)
@_safe
def _complaint_after(sender, instance, created, **kw):
    if created:
        _send(_admins(), f'New complaint from {instance.company.name}: {instance.subject}', '/admin-panel/client-complaints/')
    elif getattr(instance, '_old_status', None) not in (None, instance.status):
        _send([instance.raised_by_id], f'Your complaint "{instance.subject}" is now {instance.get_status_display()}.', _link('client_complaints', fallback='/'))


@receiver(pre_save, sender=ClientRequest)
def _request_before(sender, instance, **kw):
    _remember(sender, instance, 'status')


@receiver(post_save, sender=ClientRequest)
@_safe
def _request_after(sender, instance, created, **kw):
    if created:
        _send(_admins(), f'New request from {instance.company.name}: {instance.request_type}', '/admin-panel/client-requests/')
    elif getattr(instance, '_old_status', None) not in (None, instance.status):
        _send([instance.raised_by_id], f'Your request ({instance.request_type}) is now {instance.get_status_display()}.', _link('client_requests', fallback='/'))


# ------------------------------------------------------------------ bell and dashboards read from here
def notification_context(request):
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {}

    def latest():                                     # only runs on pages that show the list
        return list(Notification.objects.filter(recipient=user)[:6])

    return {'unread_notification_count': Notification.objects.filter(recipient=user, is_read=False).count(),
            'latest_notifications': latest}