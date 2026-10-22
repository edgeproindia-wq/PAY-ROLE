"""Add the company's four shifts in one go from the Shifts page: First, Second, General and Third.
The card shows only the shifts that do not exist yet, with the timings filled in; break and grace are boxes to confirm, not guesses.
Nothing is saved unless every ticked row is valid, and a shift that already exists is never touched."""
import datetime

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.shortcuts import redirect
from django.urls import path

from .audit import log_action
from .models_phase4 import Shift
from .permissions import company_owner_required

FOUR = [('First Shift', '06:30', '14:30'),
        ('Second Shift', '14:30', '00:30'),          # ends 12:30 AM the next day
        ('General Shift', '10:00', '20:00'),
        ('Third Shift', '00:30', '06:00')]
DEFAULT_BREAK, DEFAULT_GRACE = 0, 10                 # break 0 = nothing is deducted until you set it; grace 10 is the app's own usual value


def missing_for(company):
    have = {n.lower() for n in Shift.objects.filter(company=company).values_list('name', flat=True)}
    return [(i, n, s, e) for i, (n, s, e) in enumerate(FOUR) if n.lower() not in have]


def _time(text):
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            return datetime.datetime.strptime((text or '').strip(), fmt).time()
        except ValueError:
            continue
    return None


@company_owner_required
def shift_add_four(request):
    if request.method != 'POST':
        return redirect('hr_shifts')
    company = request.user.company
    taken = {n.lower() for n in Shift.objects.filter(company=company).values_list('name', flat=True)}
    rows, errors, seen = [], [], set()
    for idx in request.POST.getlist('row'):
        if request.POST.get(f'add_{idx}') != 'on':
            continue
        name = (request.POST.get(f'name_{idx}') or '').strip()
        label = name or 'A shift'
        start, end = _time(request.POST.get(f'start_{idx}')), _time(request.POST.get(f'end_{idx}'))
        try:
            brk, grace = int(request.POST.get(f'break_{idx}') or 0), int(request.POST.get(f'grace_{idx}') or 0)
        except ValueError:
            errors.append(f'{label}: the break and the grace time must be whole numbers of minutes.')
            continue
        if not name:
            errors.append('Every ticked shift needs a name.')
        elif len(name) > 60:
            errors.append(f'{label}: the name is too long (60 letters at most).')
        elif name.lower() in taken or name.lower() in seen:
            errors.append(f'{name}: a shift with this name already exists.')
        elif start is None or end is None:
            errors.append(f'{name}: enter a valid start and end time.')
        elif start == end:
            errors.append(f'{name}: the start and end time cannot be the same.')
        elif brk < 0 or grace < 0 or grace > 240:
            errors.append(f'{name}: the break and the grace time cannot be negative (grace up to 240 minutes).')
        elif brk >= ((end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)) % (24 * 60):
            errors.append(f'{name}: the break must be shorter than the shift.')
        else:
            seen.add(name.lower())
            rows.append((name, start, end, brk, grace))
    if errors:
        for e in errors:
            messages.error(request, e)
        messages.error(request, 'Nothing was saved. Fix the points above and press Add again.')
        return redirect('hr_shifts')
    if not rows:
        messages.info(request, 'No shift was ticked, so nothing was added.')
        return redirect('hr_shifts')
    try:
        with transaction.atomic():
            for name, start, end, brk, grace in rows:
                Shift.objects.create(company=company, name=name, start_time=start, end_time=end, break_minutes=brk, grace_minutes=grace)
    except IntegrityError:
        messages.error(request, 'A shift with one of these names was added a moment ago. Nothing was saved. Open the page again.')
        return redirect('hr_shifts')
    log_action(request, 'CREATE', None, company=company, details='Shifts added: ' + ', '.join(r[0] for r in rows))
    messages.success(request, f'Added {len(rows)} shift(s): ' + ', '.join(r[0] for r in rows) + '. They are in the Assign shift list now.')
    return redirect('hr_shifts')


urlpatterns = [path('hr/shifts/add-four/', shift_add_four, name='shift_add_four')]
