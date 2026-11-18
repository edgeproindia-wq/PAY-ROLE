import logging

from .models import AuditLog

logger = logging.getLogger('payroll_app.audit')


def _client_ip(request):
    from .login_throttle import _ip
    return _ip(request)


def log_action(request, action, obj=None, details='', company=None):
    """Write a single audit trail entry. Never pass passwords/secrets in details."""
    actor = getattr(request, 'user', None)
    if actor is not None and not actor.is_authenticated:
        actor = None
    model_name = obj.__class__.__name__ if obj is not None else ''
    object_id = str(getattr(obj, 'pk', '')) if obj is not None else ''
    if company is None and obj is not None:
        company = getattr(obj, 'company', None)
    try:
        AuditLog.objects.create(
            actor=actor,
            action=action,
            model_name=model_name,
            object_id=object_id,
            company=company,
            details=details[:500],
            ip_address=_client_ip(request) if request is not None else None,
        )
    except Exception:
        # Audit logging must never break the primary workflow — but the
        # failure is recorded, not silently swallowed.
        logger.exception('Failed to write audit log entry (action=%s, model=%s)', action, model_name)
