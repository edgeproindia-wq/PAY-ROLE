"""Biometric integration: a secure receiver for punches sent by the sync program that runs in your office.

Nothing here reaches into the device. The sync program reads the device over your own network and sends the new punches to
POST /api/biometric/punches/ over HTTPS with a secret key. The key identifies the company, so a punch can never be written to
another company. Each punch is stored once; the attendance of the day is worked out from the punches of that day.

Attendance rules (they never overwrite a decision made by a person):
  * first punch of the day = check-in, last punch (at least a minute later) = check-out
  * no record yet                 -> a Present record is created
  * Present / Half day record     -> check-in and check-out are updated, but only if they were set by the biometric (or are empty)
  * Absent / On leave record      -> left alone, and listed on the Biometric page so HR can decide
"""
import datetime
import json
import re
import zoneinfo
from collections import defaultdict

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .audit import log_action
from .biometric_models import BiometricDevice, BiometricIdMap, BiometricPunch
from .models import Attendance, Employee
from .permissions import company_owner_required

IST = zoneinfo.ZoneInfo('Asia/Kolkata')
MAX_BODY = 1024 * 1024
MAX_PUNCHES = 2000
MAX_AGE_DAYS = 62
MAX_FUTURE = datetime.timedelta(minutes=10)
MIN_CHECKOUT_GAP = datetime.timedelta(seconds=60)
TIME_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})$')
BIO_TAG = 'Biometric'
PAGE_SIZE = 50


def _company(request):
    company = getattr(request.user, 'company', None)
    if company is None:
        raise PermissionDenied('This page is for company owners.')
    return company


def parse_punch_time(value):
    """A device time such as 2026-11-02T09:02:11 (local India time) as an aware datetime, or None."""
    m = TIME_RE.match(str(value or '').strip())
    if not m:
        return None
    try:
        return datetime.datetime(*[int(x) for x in m.groups()], tzinfo=IST)
    except ValueError:
        return None


def local_day_bounds(day):
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=IST)
    return start, start + datetime.timedelta(days=1)


# --------------------------------------------------------------------------- matching device IDs to employees
def _digits_key(value):
    """EPRO0076 and 76 and 0076 all give '76'. Returns '' when there are no digits."""
    m = re.search(r'(\d+)$', str(value or '').strip())
    return m.group(1).lstrip('0') or '0' if m else ''


def resolve_employees(company, device_ids):
    """{device user id: Employee} using the explicit map first, then an exact Employee ID (any letter case)."""
    out = {}
    ids = set(device_ids)
    if not ids:
        return out
    for m in BiometricIdMap.objects.filter(company=company, device_user_id__in=ids).select_related('employee'):
        out[m.device_user_id] = m.employee
    by_code = {e.employee_code.lower(): e for e in Employee.objects.filter(company=company)}
    for i in ids:
        if i not in out and i.lower() in by_code:
            out[i] = by_code[i.lower()]
    return out


def suggestions(company, device_ids):
    """{device id: Employee} when exactly one employee has the same number at the end of the Employee ID (76 -> EPRO0076)."""
    by_digits = defaultdict(list)
    for e in Employee.objects.filter(company=company):
        k = _digits_key(e.employee_code)
        if k:
            by_digits[k].append(e)
    out = {}
    for i in device_ids:
        cands = by_digits.get(_digits_key(i), []) if str(i).strip().isdigit() else []
        if len(cands) == 1:
            out[i] = cands[0]
    return out


# --------------------------------------------------------------------------- attendance from punches
def apply_attendance(company, pairs):
    """Work out attendance for (employee id, date) pairs from their punches. Returns counts."""
    counts = defaultdict(int)
    emps = {e.pk: e for e in Employee.objects.filter(company=company, pk__in={p[0] for p in pairs})}
    for emp_id, day in sorted(pairs, key=lambda p: (p[0], p[1])):
        emp = emps.get(emp_id)
        if emp is None or day < emp.date_of_joining:
            counts['before_joining' if emp else 'unknown'] += 1
            continue
        start, end = local_day_bounds(day)
        times = list(BiometricPunch.objects.filter(company=company, employee=emp, punched_at__gte=start, punched_at__lt=end)
                     .order_by('punched_at').values_list('punched_at', flat=True))
        if not times:
            continue
        first, last = times[0], times[-1]
        check_in = first.astimezone(IST).time().replace(microsecond=0)
        check_out = last.astimezone(IST).time().replace(microsecond=0) if last - first >= MIN_CHECKOUT_GAP else None
        rec = Attendance.objects.select_for_update().filter(employee=emp, date=day).first()
        if rec is None:
            Attendance.objects.create(employee=emp, date=day, status='PRESENT', check_in=check_in, check_out=check_out, remarks=BIO_TAG)
            counts['created'] += 1
        elif rec.status in ('ABSENT', 'LEAVE'):
            counts['conflict'] += 1
        elif rec.remarks == BIO_TAG or (rec.check_in is None and rec.check_out is None):
            if (rec.check_in, rec.check_out) != (check_in, check_out) or rec.remarks != BIO_TAG:
                rec.check_in, rec.check_out = check_in, check_out
                if not rec.remarks:
                    rec.remarks = BIO_TAG
                rec.save()
                counts['updated'] += 1
        else:
            counts['manual_kept'] += 1
    return dict(counts)


def ingest(company, device, raw_punches):
    """Store new punches and update attendance. Returns the summary sent back to the sync program."""
    now = timezone.now()
    summary = defaultdict(int)
    seen, accepted = set(), []
    for item in raw_punches:
        if not isinstance(item, dict):
            summary['rejected'] += 1
            continue
        uid = str(item.get('id', '')).strip()
        ts = parse_punch_time(item.get('time'))
        if not uid or len(uid) > 30 or ts is None:
            summary['rejected'] += 1
        elif ts > now + MAX_FUTURE or ts < now - datetime.timedelta(days=MAX_AGE_DAYS):
            summary['rejected'] += 1
        elif (uid, ts) in seen:
            summary['duplicates'] += 1
        else:
            seen.add((uid, ts))
            accepted.append((uid, ts))
    summary['received'] = len(raw_punches)
    if accepted:
        lo, hi = min(t for _u, t in accepted), max(t for _u, t in accepted)
        existing = set(BiometricPunch.objects.filter(company=company, punched_at__gte=lo, punched_at__lte=hi,
                                                     device_user_id__in={u for u, _t in accepted}).values_list('device_user_id', 'punched_at'))
        fresh = [(u, t) for u, t in accepted if (u, t) not in existing]
        summary['duplicates'] += len(accepted) - len(fresh)
        people = resolve_employees(company, {u for u, _t in fresh})
        BiometricPunch.objects.bulk_create([BiometricPunch(company=company, device=device, device_user_id=u, employee=people.get(u), punched_at=t)
                                            for u, t in fresh], ignore_conflicts=True)
        summary['new'] = len(fresh)
        summary['unmapped'] = sum(1 for u, _t in fresh if u not in people)
        pairs = {(people[u].pk, t.astimezone(IST).date()) for u, t in fresh if u in people}
        for k, v in apply_attendance(company, pairs).items():
            summary['attendance_' + k] += v
    device.last_seen, device.last_batch = now, summary['received']
    device.save(update_fields=['last_seen', 'last_batch'])
    return dict(summary)


# --------------------------------------------------------------------------- the API used by the sync program
def _device_from_request(request):
    header = request.META.get('HTTP_AUTHORIZATION', '')
    if not header.lower().startswith('bearer '):
        return None
    key = header[7:].strip()
    if not key or len(key) > 200:
        return None
    return BiometricDevice.objects.filter(key_hash=BiometricDevice.hash_key(key), active=True).select_related('company').first()


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def bio_api_punches(request):
    """POST {"punches": [{"id": "76", "time": "2026-11-02T09:02:11"}, ...]} with Authorization: Bearer <device key>."""
    device = _device_from_request(request)
    if device is None:
        return JsonResponse({'ok': False, 'error': 'Not authorised.'}, status=401)
    if request.method == 'GET':                      # connection test for the sync program
        device.last_seen = timezone.now()
        device.save(update_fields=['last_seen'])
        return JsonResponse({'ok': True, 'device': device.name})
    if len(request.body) > MAX_BODY:
        return JsonResponse({'ok': False, 'error': 'The request is too large.'}, status=413)
    try:
        payload = json.loads(request.body.decode('utf-8'))
        punches = payload['punches']
        if not isinstance(punches, list):
            raise ValueError
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return JsonResponse({'ok': False, 'error': 'Send JSON like {"punches": [{"id": "76", "time": "2026-11-02T09:02:11"}]}.'}, status=400)
    if len(punches) > MAX_PUNCHES:
        return JsonResponse({'ok': False, 'error': f'Send at most {MAX_PUNCHES} punches at a time.'}, status=400)
    with transaction.atomic():
        summary = ingest(device.company, device, punches)
    return JsonResponse({'ok': True, **summary})


# --------------------------------------------------------------------------- owner pages
def _public_url(request, name):
    url = request.build_absolute_uri(name)
    host = request.get_host().split(':')[0]
    return url if host in ('127.0.0.1', 'localhost') else url.replace('http://', 'https://', 1)


@company_owner_required
def bio_setup(request):
    """Feature: Biometric Integration. Devices and keys, ID mapping, what arrived, and what needs a decision."""
    company = _company(request)
    new_key = None
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add_device':
            name = (request.POST.get('name') or '').strip()[:80]
            if not name:
                messages.error(request, 'Give the device a name, for example "Main office".')
                return redirect('bio_setup')
            new_key = BiometricDevice.new_key()
            device = BiometricDevice.objects.create(company=company, name=name, key_hash=BiometricDevice.hash_key(new_key), key_prefix=new_key[:8])
            log_action(request, 'CREATE', device, company=company, details=f'Biometric device "{name}" added (key {device.key_prefix}...)')
        elif action == 'revoke':
            device = get_object_or_404(BiometricDevice, pk=request.POST.get('device'), company=company)
            device.active, device.revoked_at = False, timezone.now()
            device.save(update_fields=['active', 'revoked_at'])
            log_action(request, 'UPDATE', device, company=company, details=f'Biometric device "{device.name}" key revoked')
            messages.success(request, f'The key of "{device.name}" no longer works. Add a new device to get a new key.')
            return redirect('bio_setup')
        elif action == 'map':
            uid = (request.POST.get('device_user_id') or '').strip()[:30]
            emp = Employee.objects.filter(pk=request.POST.get('employee') or 0, company=company).first()
            if not uid or emp is None:
                messages.error(request, 'Choose the device number and the employee.')
            else:
                BiometricIdMap.objects.update_or_create(company=company, device_user_id=uid, defaults={'employee': emp})
                log_action(request, 'UPDATE', company, company=company, details=f'Biometric ID {uid} mapped to {emp.employee_code}')
                messages.success(request, f'Device number {uid} is now {emp.employee_code} {emp.full_name}. Use "Apply to waiting punches" to update attendance.')
            return redirect('bio_setup')
        elif action == 'accept_suggestions':
            waiting = list(BiometricPunch.objects.filter(company=company, employee__isnull=True).values_list('device_user_id', flat=True).distinct())
            made = 0
            for uid, emp in suggestions(company, waiting).items():
                BiometricIdMap.objects.update_or_create(company=company, device_user_id=uid, defaults={'employee': emp})
                made += 1
            log_action(request, 'UPDATE', company, company=company, details=f'Biometric: {made} suggested ID mappings accepted')
            messages.success(request, f'{made} device number(s) matched by the number at the end of the Employee ID. Use "Apply to waiting punches" next.')
            return redirect('bio_setup')
        elif action == 'reprocess':
            with transaction.atomic():
                waiting = list(BiometricPunch.objects.filter(company=company, employee__isnull=True))
                people = resolve_employees(company, {p.device_user_id for p in waiting})
                pairs, fixed = set(), 0
                for p in waiting:
                    emp = people.get(p.device_user_id)
                    if emp:
                        p.employee = emp
                        p.save(update_fields=['employee'])
                        pairs.add((emp.pk, p.punched_at.astimezone(IST).date()))
                        fixed += 1
                done = apply_attendance(company, pairs)
            log_action(request, 'UPDATE', company, company=company, details=f'Biometric: {fixed} waiting punches applied')
            messages.success(request, f'{fixed} waiting punch(es) matched. Attendance: {done.get("created", 0)} added, {done.get("updated", 0)} updated.')
            return redirect('bio_setup')
    now = timezone.now()
    day_ago = now - datetime.timedelta(days=1)
    devices = []
    for d in BiometricDevice.objects.filter(company=company):
        d.recent = BiometricPunch.objects.filter(device=d, received_at__gte=day_ago).count()
        d.connected = bool(d.active and d.last_seen and now - d.last_seen < datetime.timedelta(minutes=15))
        devices.append(d)
    unmapped = list(BiometricPunch.objects.filter(company=company, employee__isnull=True).values('device_user_id').annotate(n=Count('id')).order_by('-n')[:50])
    sug = suggestions(company, [u['device_user_id'] for u in unmapped])
    for u in unmapped:
        u['suggest'] = sug.get(u['device_user_id'])
    cutoff = now - datetime.timedelta(days=14)
    conflicts = []
    seen_pairs = set()
    for p in BiometricPunch.objects.filter(company=company, employee__isnull=False, punched_at__gte=cutoff).select_related('employee'):
        key = (p.employee_id, p.punched_at.astimezone(IST).date())
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
    if seen_pairs:
        atts = Attendance.objects.filter(employee__company=company, status__in=['ABSENT', 'LEAVE'], employee_id__in={k[0] for k in seen_pairs},
                                         date__in={k[1] for k in seen_pairs}).select_related('employee')
        conflicts = [a for a in atts if (a.employee_id, a.date) in seen_pairs][:30]
    employees = Employee.objects.filter(company=company).order_by('employee_code')[:1000]
    return render(request, 'bio/setup.html', {
        'devices': devices, 'new_key': new_key, 'endpoint': _public_url(request, '/api/biometric/punches/'), 'unmapped': unmapped,
        'has_suggestions': any(u['suggest'] for u in unmapped), 'conflicts': conflicts, 'employees': employees,
        'recent': BiometricPunch.objects.filter(company=company).select_related('employee')[:15], 'waiting_total': sum(u['n'] for u in unmapped)})


@company_owner_required
def bio_punches(request):
    """The punch log: everything the sync program has sent."""
    company = _company(request)
    qs = BiometricPunch.objects.filter(company=company).select_related('employee', 'device')
    q = (request.GET.get('q') or '').strip()
    state = request.GET.get('state', '')
    day = request.GET.get('date', '')
    if q:
        qs = qs.filter(Q(device_user_id__icontains=q) | Q(employee__employee_code__icontains=q) | Q(employee__first_name__icontains=q) | Q(employee__last_name__icontains=q))
    if state == 'unmapped':
        qs = qs.filter(employee__isnull=True)
    elif state == 'mapped':
        qs = qs.filter(employee__isnull=False)
    try:
        d = datetime.date.fromisoformat(day)
        start, end = local_day_bounds(d)
        qs = qs.filter(punched_at__gte=start, punched_at__lt=end)
    except ValueError:
        day = ''
    params = request.GET.copy()
    params.pop('page', None)
    return render(request, 'bio/punches.html', {'page': Paginator(qs, PAGE_SIZE).get_page(request.GET.get('page')), 'qs': params.urlencode(), 'q': q, 'state': state, 'date': day})


urlpatterns = [
    path('api/biometric/punches/', bio_api_punches, name='bio_api_punches'),
    path('biometric/', bio_setup, name='bio_setup'),
    path('biometric/punches/', bio_punches, name='bio_punches'),
]
