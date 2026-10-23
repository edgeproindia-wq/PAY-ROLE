"""Live notifications: the bell and a small popup update by themselves, like Teams, without reloading the page.
This only READS the notifications your project already stores; how they are created and marked read is unchanged."""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.urls import path, reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import Notification

MAX_ITEMS = 5                                         # at most this many popups at once; the rest are summarised as "N more"


@login_required
@require_GET
@never_cache
def notification_poll(request):
    """?after=<id>  -> the unread count, the newest id, and the unread notifications newer than <id> (newest first).
    No ?after   -> the count and the newest id only (the first answer of a page: never a flood of popups)."""
    try:
        after = int(request.GET.get('after', ''))
        if after < 0:
            after = None
    except ValueError:
        after = None
    mine = Notification.objects.filter(recipient=request.user)                      # always and only the signed-in user's own
    unread = mine.filter(is_read=False).count()
    latest = mine.order_by('-id').values_list('id', flat=True).first() or 0
    items, new_total = [], 0
    if after is not None and latest > after:
        fresh = mine.filter(id__gt=after, is_read=False)
        new_total = fresh.count()
        for n in fresh.order_by('-id')[:MAX_ITEMS]:
            items.append({'id': n.id, 'message': n.message, 'url': reverse('notification_open', args=[n.id]), 'created': n.created_at.isoformat()})
    return JsonResponse({'unread': unread, 'latest': latest, 'items': items, 'new_total': new_total})


urlpatterns = [path('notifications/poll/', notification_poll, name='notification_poll')]
