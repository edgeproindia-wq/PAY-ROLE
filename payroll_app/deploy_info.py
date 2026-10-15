"""Tells which version is live: the short commit code that Render gives the running app.
It is public and harmless (7 characters), and the deploy command uses it to confirm that a push is really live."""
import os

from django.http import JsonResponse
from django.urls import path
from django.views.decorators.cache import never_cache


@never_cache
def version(request):
    return JsonResponse({'commit': (os.environ.get('RENDER_GIT_COMMIT') or '').strip()[:7]})


urlpatterns = [path('version/', version, name='public_version')]