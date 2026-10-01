"""Public contact details for the website footer come from environment variables,
so no personal email or phone number is hard-coded in templates."""
from django.conf import settings


def public_contact(request):
    return {'public_contact_email': getattr(settings, 'PUBLIC_CONTACT_EMAIL', ''),
            'public_contact_phone': getattr(settings, 'PUBLIC_CONTACT_PHONE', '')}