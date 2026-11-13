"""Pair a sync program with a short code (no key to copy). See biometric_pairing_models for the idea.

   POST /api/biometric/pair/start/   the sync program sends {"nonce": "..."}; it gets a code to show on its screen.
   POST /api/biometric/pair/poll/    the sync program sends {"code": "...", "nonce": "..."} until the owner has approved; it then receives its key once.
   POST /biometric/pair/approve/     the signed-in company owner types the code and approves it.
"""
import datetime
import json
import re
import secrets

from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import path
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .audit import log_action
from .biometric import _company
from .biometric_models import BiometricDevice
from .biometric_pairing_models import COLLECT_MINUTES, CODE_ALPHABET, CODE_LENGTH, PAIR_MINUTES, BiometricPairing, normalize_code
from .permissions import company_owner_required

MAX_BODY = 4096
ACTIVE_CAP = 200                       # open requests at any time, so nobody can fill the table
MAX_WRONG_CODES = 8                    # per signed-in user, per 15 minutes
WRONG_CODE_SECONDS = 15 * 60
NONCE_RE = re.compile(r'^[A-Za-z0-9_\-]{32,128}$')


def _json(request):
    if len(request.body or b'') > MAX_BODY:
        return None
    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _reply(payload, status=200):
    resp = JsonResponse(payload, status=status)
    resp['Cache-Control'] = 'no-store'
    return resp


def _clean_old():
    cutoff = timezone.now() - datetime.timedelta(days=1)
    BiometricPairing.objects.filter(created_at__lt=cutoff).delete()


@csrf_exempt
@require_POST
def bio_pair_start(request):
    data = _json(request)
    nonce = (data or {}).get('nonce')
    if not isinstance(nonce, str) or not NONCE_RE.match(nonce):
        return _reply({'ok': False, 'error': 'Send JSON like {"nonce": "<32 to 128 letters and digits>"}.'}, 400)
    _clean_old()
    now = timezone.now()
    if BiometricPairing.objects.filter(expires_at__gt=now, delivered_at__isnull=True).count() >= ACTIVE_CAP:
        return _reply({'ok': False, 'error': 'Too many pairing requests are open. Try again in a few minutes.'}, 429)
    for _ in range(6):
        code = BiometricPairing.new_code()
        if not BiometricPairing.objects.filter(code_hash=BiometricPairing.hash_code(code)).exists():
            BiometricPairing.objects.create(code_hash=BiometricPairing.hash_code(code), nonce_hash=BiometricPairing.hash_text(nonce),
                                            expires_at=now + datetime.timedelta(minutes=PAIR_MINUTES))
            return _reply({'ok': True, 'code': BiometricPairing.show_code(code), 'expires_in': PAIR_MINUTES * 60})
    return _reply({'ok': False, 'error': 'Could not make a code. Try again.'}, 503)


def _find(code, nonce):
    code = normalize_code(code)
    if len(code) != CODE_LENGTH or not isinstance(nonce, str) or not NONCE_RE.match(nonce):
        return None
    pairing = BiometricPairing.objects.filter(code_hash=BiometricPairing.hash_code(code)).first()
    if pairing is None or not secrets.compare_digest(pairing.nonce_hash, BiometricPairing.hash_text(nonce)):
        return None
    return pairing


@csrf_exempt
@require_POST
def bio_pair_poll(request):
    data = _json(request)
    if data is None:
        return _reply({'ok': False, 'error': 'Send JSON like {"code": "...", "nonce": "..."}.'}, 400)
    pairing = _find(data.get('code'), data.get('nonce'))
    if pairing is None:
        return _reply({'ok': False, 'status': 'unknown', 'error': 'This code is not known. Start the setup again.'}, 404)
    now = timezone.now()
    if pairing.delivered_at:
        return _reply({'ok': False, 'status': 'delivered', 'error': 'The key was already collected. Start the setup again.'}, 410)
    if pairing.expires_at <= now:
        return _reply({'ok': False, 'status': 'expired', 'error': 'The code expired. Start the setup again.'}, 410)
    if pairing.approved_at is None:
        return _reply({'ok': True, 'status': 'waiting'})
    with transaction.atomic():
        claimed = BiometricPairing.objects.filter(pk=pairing.pk, delivered_at__isnull=True).update(delivered_at=now)
        if not claimed:
            return _reply({'ok': False, 'status': 'delivered', 'error': 'The key was already collected. Start the setup again.'}, 410)
        key = BiometricDevice.new_key()
        device = BiometricDevice.objects.create(company=pairing.company, name=pairing.device_name or 'Sync program',
                                                key_hash=BiometricDevice.hash_key(key), key_prefix=key[:8])
    return _reply({'ok': True, 'status': 'approved', 'api_key': key, 'device': device.name})


@company_owner_required
@require_POST
def bio_pair_approve(request):
    company = _company(request)
    fail_key = 'bio_pair_wrong_%s' % request.user.pk
    if cache.get(fail_key, 0) >= MAX_WRONG_CODES:
        messages.error(request, 'Too many wrong codes. Wait 15 minutes and try again.')
        return redirect('bio_setup')
    code = normalize_code(request.POST.get('code'))
    name = (request.POST.get('name') or '').strip()[:80] or 'Sync program'
    now = timezone.now()
    pairing = None
    if len(code) == CODE_LENGTH and all(ch in CODE_ALPHABET for ch in code):
        pairing = BiometricPairing.objects.filter(code_hash=BiometricPairing.hash_code(code), approved_at__isnull=True,
                                                  delivered_at__isnull=True, expires_at__gt=now).first()
    if pairing is None:
        cache.set(fail_key, cache.get(fail_key, 0) + 1, WRONG_CODE_SECONDS)
        messages.error(request, 'That code was not found or has expired. Check the code on the screen of the computer running the setup, and type it again.')
        return redirect('bio_setup')
    pairing.company, pairing.device_name, pairing.approved_at = company, name, now
    pairing.expires_at = now + datetime.timedelta(minutes=COLLECT_MINUTES)
    pairing.save(update_fields=['company', 'device_name', 'approved_at', 'expires_at'])
    cache.delete(fail_key)
    log_action(request, 'CREATE', company, company=company, details=f'Biometric pairing code approved for device "{name}"')
    messages.success(request, f'Approved. The sync program receives its key by itself in a few seconds and "{name}" appears in the list below.')
    return redirect('bio_setup')


urlpatterns = [
    path('api/biometric/pair/start/', bio_pair_start, name='bio_pair_start'),
    path('api/biometric/pair/poll/', bio_pair_poll, name='bio_pair_poll'),
    path('biometric/pair/approve/', bio_pair_approve, name='bio_pair_approve'),
]
