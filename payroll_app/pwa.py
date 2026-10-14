"""Installable-app and Play Store support: web app manifest, service worker, Digital Asset Links, offline page, public legal pages
and the in-app account deletion request. The legal wording is a starting point: have it reviewed before you publish."""
import os
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.urls import path
from django.utils import timezone

from .audit import log_action
from .models import Notification, User

THEME = '#1f9347'
POLICY_DATE = '14 October 2026'
FINGERPRINT = re.compile(r'([0-9A-F]{2}:){31}[0-9A-F]{2}')

SERVICE_WORKER = """// Namma Payroll service worker. Pages are ALWAYS loaded from the network, because payroll data is private and must never be
// served from a cache on the device. Only the site's own static files and a small offline page are kept.
const VERSION = 'namma-v1';
self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(VERSION).then((cache) => cache.addAll(['/offline/'])).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(() => caches.match('/offline/')));
    return;
  }
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(caches.open(VERSION).then((cache) => cache.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res.ok) cache.put(req, res.clone());
      return res;
    }))));
  }
});
"""


def legal_context():
    return {'legal_name': os.environ.get('LEGAL_ENTITY_NAME', '').strip() or 'the operator of Namma Payroll',
            'support_email': os.environ.get('SUPPORT_EMAIL', '').strip(),
            'legal_address': os.environ.get('LEGAL_ADDRESS', '').strip(),
            'deletion_days': os.environ.get('DELETION_RESPONSE_DAYS', '30').strip() or '30',
            'updated': POLICY_DATE}


def _static(name):
    try:
        return static(name)
    except ValueError:                                  # file not in the static manifest yet
        return '/static/' + name


def manifest(request):
    data = {'id': '/', 'name': 'Namma Payroll', 'short_name': 'Namma', 'description': 'Payroll, attendance, leave and payslips for your company and employees.',
            'start_url': '/login/', 'scope': '/', 'display': 'standalone', 'orientation': 'portrait', 'lang': 'en-IN',
            'theme_color': THEME, 'background_color': '#ffffff', 'categories': ['business', 'finance', 'productivity'],
            'icons': [{'src': _static('pwa/icon-192.png'), 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
                      {'src': _static('pwa/icon-512.png'), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
                      {'src': _static('pwa/maskable-512.png'), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'}]}
    return JsonResponse(data, content_type='application/manifest+json')


def service_worker(request):
    resp = HttpResponse(SERVICE_WORKER, content_type='application/javascript')
    resp['Cache-Control'] = 'no-cache'
    resp['Service-Worker-Allowed'] = '/'
    return resp


def assetlinks(request):
    """Links the website to the signed Android app. Empty until ANDROID_PACKAGE_NAME and ANDROID_SHA256_FINGERPRINTS are set."""
    package = os.environ.get('ANDROID_PACKAGE_NAME', '').strip()
    prints = [p.strip().upper() for p in os.environ.get('ANDROID_SHA256_FINGERPRINTS', '').split(',') if p.strip()]
    ok = bool(package and prints and all(FINGERPRINT.fullmatch(p) for p in prints))
    data = [{'relation': ['delegate_permission/common.handle_all_urls'],
             'target': {'namespace': 'android_app', 'package_name': package, 'sha256_cert_fingerprints': prints}}] if ok else []
    return JsonResponse(data, safe=False)


def offline(request):
    return render(request, 'legal/offline.html')


def privacy(request):
    return render(request, 'legal/privacy.html', legal_context())


def terms(request):
    return render(request, 'legal/terms.html', legal_context())


def deletion_info(request):
    return render(request, 'legal/delete_account.html', legal_context())


@login_required
def delete_request(request):
    """Signed-in users ask for their account to be deleted. The request is written to the audit trail and the people who decide are notified."""
    if request.method == 'POST':
        last = request.session.get('deletion_requested_at', 0)
        if request.POST.get('confirm') != 'yes':
            messages.error(request, 'Please tick the box to confirm your request.')
        elif timezone.now().timestamp() - last < 86400:
            messages.info(request, 'Your request was already received. We will contact you.')
        else:
            request.session['deletion_requested_at'] = timezone.now().timestamp()
            who = request.user
            reason = (request.POST.get('reason') or '').strip()[:300]
            log_action(request, 'CREATE', None, details=f'Account deletion requested by {who.username}' + (f': {reason}' if reason else ''),
                       company=getattr(who, 'company', None))
            try:
                ids = set(User.objects.filter(role='ADMIN', is_active=True).values_list('pk', flat=True))
                if who.role == 'EMPLOYEE' and who.company_id:
                    ids |= set(User.objects.filter(role='COMPANY_OWNER', is_active=True, company_id=who.company_id).values_list('pk', flat=True))
                ids.discard(who.pk)
                Notification.objects.bulk_create([Notification(recipient_id=i, message=f'{who.username} asked for their account to be deleted.'[:255], link='') for i in ids])
            except Exception:                           # a notification problem must never lose the request itself
                pass
            messages.success(request, f'Your request was received. We will confirm within {legal_context()["deletion_days"]} days.')
        return redirect(request.path)
    return render(request, 'legal/delete_request.html', legal_context())


urlpatterns = [
    path('manifest.webmanifest', manifest, name='public_manifest'),
    path('sw.js', service_worker, name='public_service_worker'),
    path('.well-known/assetlinks.json', assetlinks, name='public_assetlinks'),
    path('offline/', offline, name='public_offline'),
    path('privacy/', privacy, name='public_privacy'),
    path('terms/', terms, name='public_terms'),
    path('delete-account/', deletion_info, name='public_account_deletion'),
    path('account/delete-request/', delete_request, name='account_delete_request'),
]