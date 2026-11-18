"""Brute-force protection for the login form: after MAX_FAILURES failed attempts for
the same username FROM THE SAME IP (or MAX_FAILURES_PER_IP from one IP) within WINDOW seconds,
further attempts from that IP are refused for WINDOW seconds.

The username counter is per (username, IP) on purpose. It used to count the username alone, so
a stranger typing wrong passwords for "owner1" locked the real owner out for 15 minutes.
Now the stranger locks only himself. (A global per-username lock would bring that problem back.) Uses Django's cache (per-process LocMem by default; use a
shared cache such as Redis in production for multi-worker accuracy)."""
from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.core.cache import cache
from django.dispatch import receiver

MAX_FAILURES = 5            # per account
MAX_FAILURES_PER_IP = 50    # per IP: an office sharing one connection is not locked out by one person
WINDOW = 15 * 60


def _ip(request):
    """The client address. X-Forwarded-For is written by whoever sends the request, so only the entries
    added by OUR proxies can be trusted: the last TRUSTED_PROXY_COUNT ones. The first entry (used before)
    can be invented by the attacker to dodge the limit. TRUSTED_PROXY_COUNT=0 means: no proxy, ignore the header."""
    if request is None:
        return 'unknown'
    remote = request.META.get('REMOTE_ADDR', '') or 'unknown'
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    trusted = int(getattr(settings, 'TRUSTED_PROXY_COUNT', 0) or 0)
    parts = [p.strip() for p in fwd.split(',') if p.strip()]
    if trusted and parts:
        return parts[-min(trusted, len(parts))]
    return remote


def _keys(request, username):
    ip = _ip(request)
    return [f'login-fail:user:{(username or "").strip().lower()}:{ip}', f'login-fail:ip:{ip}']


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
    cache.delete(f'login-fail:user:{user.get_username().lower()}:{_ip(request)}')