"""Signal receivers that turn key business events into in-app notifications
for the right audience (admins for new demo/company requests, company
owners for their employees' leave/reimbursement submissions)."""
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models import Q
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import DemoRequest, Company, LeaveRequest, Reimbursement, Notification, User


def _notify_admins(message, link=''):
    admin_ids = User.objects.filter(Q(role='ADMIN') | Q(is_superuser=True), is_active=True).values_list('id', flat=True)
    Notification.objects.bulk_create([
        Notification(recipient_id=uid, message=message, link=link) for uid in admin_ids
    ])


def _notify_company_owner(company, message, link=''):
    if not company:
        return
    owner_ids = User.objects.filter(role='COMPANY_OWNER', company=company).values_list('id', flat=True)
    Notification.objects.bulk_create([
        Notification(recipient_id=uid, message=message, link=link) for uid in owner_ids
    ])


@receiver(post_save, sender=DemoRequest)
def on_demo_request_created(sender, instance, created, **kwargs):
    if created and not kwargs.get('raw'):
        _notify_admins(f"New demo request from {instance.company_name}", link='/admin-panel/demo-requests/')


@receiver(post_save, sender=Company)
def on_company_registered(sender, instance, created, **kwargs):
    if created and not kwargs.get('raw'):
        _notify_admins(f"New company registration pending approval: {instance.name}", link='/admin-panel/company-approvals/')


@receiver(post_save, sender=LeaveRequest)
def on_leave_request_created(sender, instance, created, **kwargs):
    if created and not kwargs.get('raw'):
        _notify_company_owner(
            instance.employee.company,
            f"{instance.employee.full_name} requested {instance.get_leave_type_display()}",
            link='/leave_management/',
        )


@receiver(post_save, sender=Reimbursement)
def on_reimbursement_created(sender, instance, created, **kwargs):
    if created and not kwargs.get('raw'):
        _notify_company_owner(
            instance.employee.company,
            f"{instance.employee.full_name} submitted a {instance.get_category_display()} claim",
            link='/reimbursement/',
        )


# ---------------------------------------------------------------------------
# Authentication audit trail (never records passwords)
# ---------------------------------------------------------------------------

@receiver(user_logged_in)
def on_user_logged_in(sender, request, user, **kwargs):
    from .audit import log_action
    log_action(request, 'LOGIN', details='User logged in', company=user.company)


@receiver(user_logged_out)
def on_user_logged_out(sender, request, user, **kwargs):
    if user is None:
        return
    from .audit import log_action
    from .models import AuditLog
    from .audit import _client_ip
    AuditLog.objects.create(actor=user, action='LOGOUT', details='User logged out',
                            company=user.company, ip_address=_client_ip(request) if request else None)


@receiver(user_login_failed)
def on_user_login_failed(sender, credentials, request=None, **kwargs):
    from .audit import log_action
    ident = str(credentials.get('username', ''))[:150]
    if request is not None:
        log_action(request, 'OTHER', details=f'Failed login attempt for "{ident}"')
from . import login_throttle  # noqa: E402,F401  (failed-login counting)
