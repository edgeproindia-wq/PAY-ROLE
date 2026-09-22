"""Signal receivers that turn key business events into in-app notifications
for the right audience (admins for new demo/company requests, company
owners for their employees' leave/reimbursement submissions)."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import DemoRequest, Company, LeaveRequest, Reimbursement, Notification, User


def _notify_admins(message, link=''):
    admin_ids = User.objects.filter(role='ADMIN').values_list('id', flat=True)
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
    if created:
        _notify_admins(f"New demo request from {instance.company_name}", link='/admin-panel/demo-requests/')


@receiver(post_save, sender=Company)
def on_company_registered(sender, instance, created, **kwargs):
    if created:
        _notify_admins(f"New company registration pending approval: {instance.name}", link='/admin-panel/company-approvals/')


@receiver(post_save, sender=LeaveRequest)
def on_leave_request_created(sender, instance, created, **kwargs):
    if created:
        _notify_company_owner(
            instance.employee.company,
            f"{instance.employee.full_name} requested {instance.get_leave_type_display()}",
            link='/leave_management/',
        )


@receiver(post_save, sender=Reimbursement)
def on_reimbursement_created(sender, instance, created, **kwargs):
    if created:
        _notify_company_owner(
            instance.employee.company,
            f"{instance.employee.full_name} submitted a {instance.get_category_display()} claim",
            link='/reimbursement/',
        )
