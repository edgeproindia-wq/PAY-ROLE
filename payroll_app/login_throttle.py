"""Brute-force protection for the login form: after MAX_FAILURES failed attempts for
the same username (or from the same IP) within WINDOW seconds, further attempts are
refused for WINDOW seconds. Uses Django's cache (per-process LocMem by default; use a
shared cache such as Redis in production for multi-worker accuracy)."""
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.core.cache import cache
from django.dispatch import receiver

MAX_FAILURES = 5            # per account
MAX_FAILURES_PER_IP = 50    # per IP: an office sharing one connection is not locked out by one person
WINDOW = 15 * 60


def _ip(request):
    if request is None:
        return 'unknown'
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR', '')) or 'unknown'


def _keys(request, username):
    return [f'login-fail:user:{(username or "").strip().lower()}', f'login-fail:ip:{_ip(request)}']


def is_locked(request, username):
    try:
        user_key, ip_key = _keys(request, username)
        return (cache.get(user_key) or 0) >= MAX_FAILURES or (cache.get(ip_key) or 0) >= MAX_FAILURES_PER_IP
    except Exception:                                   # cache down: do not lock everybody out
        import logging
        logging.getLogger(__name__).exception('Login throttle cache unavailable')
        return False


@receiver(user_login_failed, dispatch_uid='login_throttle_failed')
def _failed(sender, credentials, request=None, **kwargs):
    for k in _keys(request, (credentials or {}).get('username')):
        try:
            try:
                cache.incr(k)
            except ValueError:
                cache.set(k, 1, WINDOW)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Login throttle cache unavailable')


@receiver(user_logged_in, dispatch_uid='login_throttle_success')
def _succeeded(sender, request, user, **kwargs):
    cache.delete(f'login-fail:user:{user.get_username().lower()}')