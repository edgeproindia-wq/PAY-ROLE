"""Deployment safety checks (shown by `manage.py check`)."""
import os

from django.conf import settings
from django.core.checks import Warning, register


@register()
def upload_storage_check(app_configs, **kwargs):
    # Render's disk is wiped on every deploy: uploads must go to object storage there.
    backend = settings.STORAGES.get('default', {}).get('BACKEND', '')
    if os.environ.get('RENDER') and backend.endswith('FileSystemStorage'):
        return [Warning('Uploaded files are saved on the Render container disk and will be lost on the next deploy.',
                        hint='Set STORAGE_BUCKET, STORAGE_ACCESS_KEY_ID, STORAGE_SECRET_ACCESS_KEY (and '
                             'STORAGE_ENDPOINT_URL for Cloudflare R2) in the Render Environment tab.',
                        id='payroll.W001')]
    return []