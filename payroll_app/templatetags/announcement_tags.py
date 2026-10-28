from django import template
from django.urls import NoReverseMatch, reverse

from ..models_features import Announcement
from ..models_phase5 import AnnouncementRead

register = template.Library()
LIMIT = 3


@register.inclusion_tag('partials/sidebar_announcements.html', takes_context=True)
def sidebar_announcements(context):
    """The newest most-important announcements for the signed-in owner or employee, read from the database."""
    request = context.get('request')
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {'items': []}
    qs = Announcement.visible_to(user)
    items = list(qs[:LIMIT])
    seen = set(AnnouncementRead.objects.filter(user=user, announcement__in=items).values_list('announcement_id', flat=True))
    for a in items:
        a.unread = a.pk not in seen
    try:
        url = reverse('announcements')
    except NoReverseMatch:
        url = ''
    return {'items': items, 'url': url, 'total': qs.count()}
