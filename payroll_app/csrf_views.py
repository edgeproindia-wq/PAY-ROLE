"""A clear page instead of Django's raw 'CSRF verification failed', and a log line saying exactly why."""
import logging

from django.shortcuts import render

logger = logging.getLogger('payroll_app.csrf')


def csrf_failed(request, reason=''):
    logger.warning('CSRF failure on %s %s | reason: %s | host: %s | origin: %s | signed in: %s', request.method, request.path, reason,
                   request.get_host(), request.META.get('HTTP_ORIGIN', '-'), request.user.is_authenticated)
    return render(request, 'errors/csrf_failed.html', status=403)