"""Hours worked, shown the way people read them, and the automatic Half day rule for days recorded by the biometric.

Rules (change the numbers just below):
  * Hours = check-out minus check-in. A check-out earlier than the check-in is a night shift that ends the next day (up to 16 hours).
  * Break: 60 minutes are taken off a day that is longer than 4 hours. A shorter stay has no lunch break, so its real time is shown.
  * Half day: a finished day (before today) that the biometric recorded, with fewer than 6 hours after the break, becomes a Half day.
    The biometric only ever changes records it wrote itself (remarks "Biometric"). A day that HR edited, or marked Absent or On leave,
    is never touched, and when HR sets such a day back to Present the biometric does not change it again.
"""
import datetime
import zoneinfo
from collections import namedtuple
from decimal import ROUND_HALF_UP, Decimal

BREAK = datetime.timedelta(minutes=60)
BREAK_APPLIES_ABOVE = datetime.timedelta(hours=4)
HALF_DAY_BELOW = datetime.timedelta(hours=6)
MAX_SHIFT = datetime.timedelta(hours=16)
DAYS_BACK = 3
BIO_TAG = 'Biometric'
AUTO_TAG = 'Biometric - auto half day'
IST = zoneinfo.ZoneInfo('Asia/Kolkata')

Worked = namedtuple('Worked', 'state gross net overnight')


def worked(day, check_in, check_out):
    """state is none, missing_out, missing_in, bad or ok. gross and net are timedeltas when ok."""
    if check_in is None and check_out is None:
        return Worked('none', None, None, False)
    if check_out is None:
        return Worked('missing_out', None, None, False)
    if check_in is None:
        return Worked('missing_in', None, None, False)
    start = datetime.datetime.combine(day, check_in)
    end = datetime.datetime.combine(day, check_out)
    overnight = end < start
    if overnight:
        end += datetime.timedelta(days=1)
    gross = end - start
    if gross > MAX_SHIFT:
        return Worked('bad', None, None, False)
    net = gross - BREAK if gross > BREAK_APPLIES_ABOVE else gross
    return Worked('ok', gross, net, overnight)


def clock(delta):
    """9h 05m"""
    minutes = int(delta.total_seconds() // 60)
    return f'{minutes // 60}h {minutes % 60:02d}m'


def label(day, check_in, check_out):
    w = worked(day, check_in, check_out)
    return {'none': '\u2014', 'missing_out': 'Check-out missing', 'missing_in': 'Check-in missing', 'bad': 'Check times look wrong'}.get(
        w.state, None) or clock(w.net) + (' (night)' if w.overnight else '')


def net_hours_decimal(att):
    """Hours after the break as a number with two decimals, or None when there are no usable times."""
    w = worked(att.date, att.check_in, att.check_out)
    if w.state != 'ok':
        return None
    return (Decimal(w.net.total_seconds()) / 3600).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def finalize_statuses(company, today=None, days_back=DAYS_BACK):
    """Set Half day on finished days the biometric recorded with fewer than 6 hours; put them back when late punches fix the hours."""
    from .models import Attendance
    today = today or datetime.datetime.now(IST).date()
    changed = 0
    qs = Attendance.objects.select_for_update().filter(
        employee__company=company, date__gte=today - datetime.timedelta(days=days_back), date__lt=today,
        status__in=['PRESENT', 'HALF_DAY'], remarks__startswith=BIO_TAG, check_in__isnull=False, check_out__isnull=False)
    for rec in qs:
        w = worked(rec.date, rec.check_in, rec.check_out)
        if w.state != 'ok':
            continue
        low = w.net < HALF_DAY_BELOW
        note = f'{AUTO_TAG} ({clock(w.net)})'
        if rec.remarks == BIO_TAG and rec.status == 'PRESENT' and low:
            rec.status, rec.remarks = 'HALF_DAY', note
            rec.save()
            changed += 1
        elif rec.remarks.startswith(AUTO_TAG) and rec.status == 'HALF_DAY':
            if not low:
                rec.status, rec.remarks = 'PRESENT', BIO_TAG
                rec.save()
                changed += 1
            elif rec.remarks != note:
                rec.remarks = note
                rec.save()
    return changed
