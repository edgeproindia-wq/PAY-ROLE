"""Previous / Next navigation for days and months. Every page that moves through time uses these two tags, so it all looks and works the same.

    {% load period_nav %}
    {% day_nav day %}                     - [Previous day]  16 October 2026  [Next day]  [Today]
    {% month_nav month valid_months %}    - [Previous month]  October 2026  [Next month]  [Current month]
Other search and filter values in the address are kept while moving; the page number is reset."""
import datetime

from django import template
from django.utils import timezone

register = template.Library()


def _url(request, param, value):
    q = request.GET.copy()
    q.pop('page', None)                                # a new day or month starts on page 1
    q[param] = value
    return f'{request.path}?{q.urlencode()}'


@register.inclusion_tag('partials/period_nav.html', takes_context=True)
def day_nav(context, day, param='date', allow_future=False):
    request = context['request']
    today = timezone.localdate()
    one = datetime.timedelta(days=1)
    return {'kind': 'day', 'aria': 'Day navigation', 'label': f'{day.day} {day:%B %Y}', 'sublabel': f'{day:%A}',
            'prev_url': _url(request, param, (day - one).isoformat()), 'prev_label': 'Previous day', 'prev_disabled': False,
            'next_url': _url(request, param, (day + one).isoformat()), 'next_label': 'Next day',
            'next_disabled': (not allow_future) and day >= today,          # attendance cannot be marked for a day that has not happened
            'today_url': _url(request, param, today.isoformat()), 'today_label': 'Today', 'today_active': day == today}


@register.inclusion_tag('partials/period_nav.html', takes_context=True)
def month_nav(context, month_label, valid_months, param='month'):
    request = context['request']
    valid = list(valid_months)
    try:
        cur = datetime.datetime.strptime(str(month_label), '%B %Y').date().replace(day=1)
    except ValueError:
        cur = timezone.localdate().replace(day=1)
    prev_m = (cur - datetime.timedelta(days=1)).replace(day=1)
    next_m = (cur + datetime.timedelta(days=32)).replace(day=1)
    fmt = lambda d: d.strftime('%B %Y')
    this_month = fmt(timezone.localdate().replace(day=1))
    return {'kind': 'month', 'aria': 'Month navigation', 'label': fmt(cur), 'sublabel': '',
            'prev_url': _url(request, param, fmt(prev_m)), 'prev_label': 'Previous month', 'prev_disabled': fmt(prev_m) not in valid,
            'next_url': _url(request, param, fmt(next_m)), 'next_label': 'Next month', 'next_disabled': fmt(next_m) not in valid,
            'today_url': _url(request, param, this_month), 'today_label': 'Current month', 'today_active': fmt(cur) == this_month}