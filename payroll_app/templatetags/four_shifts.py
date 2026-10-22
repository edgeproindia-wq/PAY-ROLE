"""{% four_shifts_card %} - the 'add your four shifts' card; it draws nothing once all four exist, and nothing for anyone but the company owner."""
from django import template

from ..permissions import effective_role
from ..shifts_four import DEFAULT_BREAK, DEFAULT_GRACE, missing_for

register = template.Library()


@register.inclusion_tag('partials/four_shifts_card.html', takes_context=True)
def four_shifts_card(context):
    user = getattr(context.get('request'), 'user', None)
    if user is None or not user.is_authenticated or effective_role(user) != 'COMPANY_OWNER' or not getattr(user, 'company', None):
        return {'rows': []}
    return {'rows': [{'idx': i, 'name': n, 'start': s, 'end': e, 'next_day': e < s, 'brk': DEFAULT_BREAK, 'grace': DEFAULT_GRACE}
                     for i, n, s, e in missing_for(user.company)]}
