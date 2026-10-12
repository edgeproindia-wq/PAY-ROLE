"""Mark all read, and open a notification (marks it read, then goes to what it is about)."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect
from django.urls import path
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .models import Notification


def _safe(url, request):
    return bool(url) and url.startswith('/') and not url.startswith('//') and url_has_allowed_host_and_scheme(url, {request.get_host()})


@login_required
@require_POST
def mark_all_read(request):
    Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
    nxt = request.POST.get('next', '')
    return redirect(nxt if _safe(nxt, request) else 'notifications')


@login_required
def open_notification(request, pk):
    note = get_object_or_404(Notification, pk=pk, recipient=request.user)       # never someone else's notification
    if not note.is_read:
        note.is_read = True
        note.save(update_fields=['is_read'])
    return redirect(note.link if _safe(note.link, request) else 'notifications')


urlpatterns = [
    path('notifications/read-all/', mark_all_read, name='notification_mark_all_read'),
    path('notifications/open/<int:pk>/', open_notification, name='notification_open'),
]