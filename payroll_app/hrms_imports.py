"""HRMS & External System Integration (company owners only).

Imports (upload -> preview -> confirm -> audit log):
  Employee Master Synchronisation, Attendance Import (also takes a biometric export file), Leave Data Import,
  Employee Joining / Exit Updates.
Pages: Biometric, Accounting Software, Bank and Email connection status, and the Import Center.

There is no live connection to another system here. Every import reads a CSV or Excel file the owner uploads, shows exactly
what will change, and saves nothing until the owner confirms. Bank account, PAN and Aadhaar numbers are never imported here.
"""
import csv
import datetime
import io
import re
import time
from collections import OrderedDict, defaultdict

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import NoReverseMatch, path, reverse
from django.utils import timezone

from .audit import log_action
from .models import Attendance, Employee, LeaveRequest
from .permissions import company_owner_required

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_ROWS = 2000
PREVIEW_ROWS = 500
PREVIEW_MINUTES = 30
SESSION_KEY = 'hrms_import'


# --------------------------------------------------------------------------- small helpers
def _company(request):
    company = getattr(request.user, 'company', None)
    if company is None:
        raise PermissionDenied('This page is for company owners.')
    return company


def _norm(value):
    return re.sub(r'[^a-z0-9]+', '', str(value or '').lower())


def _s(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    return str(value).strip()


def parse_date(value):
    """A date from Excel or text: 2024-06-03, 03-06-2024, 03/06/2024 (day first), 03 Jun 2024. Raises ValueError."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = _s(value)
    if not text:
        return None
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%d.%m.%Y', '%d-%b-%Y', '%d %b %Y', '%d-%B-%Y', '%d %B %Y', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    if text.isdigit() and 30000 <= int(text) <= 80000:               # an Excel date that was not formatted as a date
        return datetime.date(1899, 12, 30) + datetime.timedelta(days=int(text))
    raise ValueError(text)


def parse_time(value):
    """A time from Excel or text: 09:30, 09:30:15, 9:30 AM, or an Excel fraction of a day. Raises ValueError."""
    if isinstance(value, datetime.datetime):
        return value.time().replace(microsecond=0)
    if isinstance(value, datetime.time):
        return value.replace(microsecond=0)
    if isinstance(value, float) and 0 <= value < 1:
        seconds = int(round(value * 86400))
        return datetime.time(seconds // 3600 % 24, seconds % 3600 // 60, seconds % 60)
    text = _s(value)
    if not text:
        return None
    for fmt in ('%H:%M', '%H:%M:%S', '%I:%M %p', '%I:%M:%S %p', '%I:%M%p', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.datetime.strptime(text.upper(), fmt).time()
        except ValueError:
            continue
    try:
        return parse_time(float(text))                              # an Excel fraction of a day
    except (ValueError, TypeError):
        pass
    raise ValueError(text)


def read_rows(upload):
    """(rows as lists, error message)."""
    name = (upload.name or '').lower()
    if upload.size > MAX_UPLOAD_BYTES:
        return None, 'The file is larger than 2 MB.'
    data = upload.read()
    if name.endswith('.csv'):
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = data.decode('latin-1')
        rows = list(csv.reader(io.StringIO(text)))
    elif name.endswith('.xlsx'):
        try:
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            rows = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
        except Exception:
            return None, 'This Excel file could not be read. Save it again as .xlsx or .csv.'
    else:
        return None, 'Upload a .csv or .xlsx file.'
    if len(rows) > MAX_ROWS + 20:
        return None, f'The file has more than {MAX_ROWS} rows. Split it and upload it in parts.'
    return rows, ''


# --------------------------------------------------------------------------- the import definitions
class Spec:
    def __init__(self, key, title, name, description, columns, validate, apply, options=(), example=(), notes=(), prepare=None):
        self.key, self.title, self.name, self.description = key, title, name, description
        self.columns = columns                      # (field, label, required, aliases)
        self.validate, self.apply, self.prepare = validate, apply, prepare
        self.options = options                      # (field, label)
        self.example, self.notes = example, notes
        self.aliases = {f: {_norm(f), _norm(label)} | {_norm(a) for a in al} for f, label, _r, al in columns}


SPECS = OrderedDict()


def spec(*args, **kwargs):
    s = Spec(*args, **kwargs)
    SPECS[s.key] = s
    return s


def parse_table(rows, sp):
    """([(row number, {field: cell})], error). The header row is found in the first ten rows."""
    required = [f for f, _l, req, _a in sp.columns if req]
    for i, r in enumerate(rows[:10]):
        mapping = {}
        for idx, cell in enumerate(r):
            h = _norm(cell)
            for f, names in sp.aliases.items():
                if h and h in names and f not in mapping:
                    mapping[f] = idx
        if all(f in mapping for f in required):
            items = []
            for n, raw in enumerate(rows[i + 1:], start=i + 2):
                if not any(_s(c) for c in raw):
                    continue
                items.append((n, {f: (raw[idx] if idx < len(raw) else None) for f, idx in mapping.items()}))
            if not items:
                return None, 'The file has a header but no rows.'
            return items, ''
    labels = ', '.join(l for f, l, req, _a in sp.columns if req)
    return None, f'Could not find the column headings. The file needs these columns: {labels}.'


def employees_by_code(company):
    return {e.employee_code.lower(): e for e in Employee.objects.filter(company=company)}


def _result(n, who, outcome, summary='', errors=None, warnings=None, data=None):
    return {'n': n, 'who': who, 'outcome': outcome, 'summary': summary, 'errors': errors or [], 'warnings': warnings or [], 'data': data or {}}


def run_validation(sp, company, items, options):
    ctx = {'company': company, 'emps': employees_by_code(company), 'today': timezone.localdate(), 'seen': defaultdict(set)}
    if sp.prepare:
        sp.prepare(ctx, items)
    return [sp.validate(ctx, n, raw, options) for n, raw in items]


# ---- 1. Employee Master Synchronisation ------------------------------------------------------------------
SYNC_TEXT = (('first_name', 'First Name', 100), ('last_name', 'Last Name', 100), ('department', 'Department', 200), ('designation', 'Designation', 200))
GENDERS = {'m': 'M', 'male': 'M', 'f': 'F', 'female': 'F', 'o': 'O', 'other': 'O'}


def validate_sync(ctx, n, raw, options):
    code = _s(raw.get('employee_id'))
    emp = ctx['emps'].get(code.lower()) if code else None
    if emp is None:
        return _result(n, code or '(blank)', 'error', errors=['No employee with this Employee ID in your company'])
    errors, changes, data = [], [], {'employee_id': emp.employee_code}
    for field, label, size in SYNC_TEXT:
        v = _s(raw.get(field))
        if v:
            if len(v) > size:
                errors.append(f'{label} is longer than {size} characters')
            elif v != getattr(emp, field):
                data[field] = v
                changes.append(f'{label}: {getattr(emp, field) or "(empty)"} -> {v}')
    email = _s(raw.get('email')).lower()
    if email:
        try:
            validate_email(email)
        except ValidationError:
            errors.append('Email is not a valid address')
        else:
            if email != emp.email.lower():
                if Employee.objects.filter(email__iexact=email).exclude(pk=emp.pk).exists() or email in ctx['seen']['email']:
                    errors.append('This email is already used by another employee')
                else:
                    ctx['seen']['email'].add(email)
                    data['email'] = email
                    changes.append(f'Email: {emp.email} -> {email}')
    phone = re.sub(r'[\s\-()]', '', _s(raw.get('phone')))
    if phone:
        if not re.fullmatch(r'\+?\d{7,15}', phone):
            errors.append('Phone must be 7 to 15 digits')
        elif phone != emp.phone:
            data['phone'] = phone
            changes.append(f'Phone: {emp.phone or "(empty)"} -> {phone}')
    gender = _s(raw.get('gender'))
    if gender:
        g = GENDERS.get(gender.lower())
        if g is None:
            errors.append('Gender must be M, F or O')
        elif g != emp.gender:
            data['gender'] = g
            changes.append(f'Gender: {emp.gender or "(empty)"} -> {g}')
    dob = raw.get('date_of_birth')
    if _s(dob):
        try:
            d = parse_date(dob)
        except ValueError:
            errors.append('Date of birth is not a date (use 2024-06-03 or 03-06-2024)')
        else:
            if not (datetime.date(1900, 1, 1) < d < ctx['today']):
                errors.append('Date of birth must be in the past')
            elif d != emp.date_of_birth:
                data['date_of_birth'] = d.isoformat()
                changes.append(f'Date of birth: {emp.date_of_birth or "(empty)"} -> {d}')
    who = f'{emp.employee_code} {emp.full_name}'
    if errors:
        return _result(n, who, 'error', errors=errors)
    if not changes:
        return _result(n, who, 'skip', 'No change')
    return _result(n, who, 'ok', '; '.join(changes), data=data)


def apply_sync(request, company, results, options):
    done = 0
    for r in results:
        d = dict(r['data'])
        emp = Employee.objects.select_for_update().get(company=company, employee_code=d.pop('employee_id'))
        for field, value in d.items():
            setattr(emp, field, datetime.date.fromisoformat(value) if field == 'date_of_birth' else value)
        emp.save()
        done += 1
    return f'{done} employee(s) updated'


spec('employee-sync', 'Employee Master Synchronisation', 'hrms_employee_sync',
     'Update existing employees from a file exported from your HR system. Employees are matched by Employee ID. A blank cell leaves the value unchanged. Status, joining date, bank, PAN and Aadhaar are not changed here.',
     [('employee_id', 'Employee ID', True, ('empid', 'employeecode', 'empcode', 'code')), ('first_name', 'First Name', False, ('firstname', 'givenname')),
      ('last_name', 'Last Name', False, ('lastname', 'surname')), ('email', 'Email', False, ('emailid', 'mail')),
      ('phone', 'Phone', False, ('mobile', 'mobileno', 'phoneno', 'contactnumber')), ('gender', 'Gender', False, ('sex',)),
      ('date_of_birth', 'Date of Birth', False, ('dob', 'birthdate')), ('department', 'Department', False, ('dept',)),
      ('designation', 'Designation', False, ('jobtitle', 'position', 'title'))],
     validate_sync, apply_sync, example=['EPRO0001', 'Anu', 'Kumar', 'anu.kumar@company.com', '9876543210', 'F', '1995-04-12', 'Detailing', 'Tekla Modeler'],
     notes=['Employees are matched by Employee ID; a new Employee ID is not created here (use the Employee Import for new employees).',
            'The login email of an employee is not changed.'])


# ---- 2. Employee Joining / Exit Updates -------------------------------------------------------------------
STATUSES = {'active': 'ACTIVE', 'onleave': 'ON_LEAVE', 'resigned': 'RESIGNED', 'terminated': 'TERMINATED'}


def validate_joining_exit(ctx, n, raw, options):
    code = _s(raw.get('employee_id'))
    emp = ctx['emps'].get(code.lower()) if code else None
    if emp is None:
        return _result(n, code or '(blank)', 'error', errors=['No employee with this Employee ID in your company'])
    errors, warnings, changes, data = [], [], [], {'employee_id': emp.employee_code}
    joining, status = raw.get('date_of_joining'), _s(raw.get('status'))
    if not _s(joining) and not status:
        errors.append('Give a joining date or a status')
    if _s(joining):
        try:
            d = parse_date(joining)
        except ValueError:
            errors.append('Date of joining is not a date (use 2024-06-03 or 03-06-2024)')
        else:
            if d > ctx['today'] + datetime.timedelta(days=366) or d.year < 1950:
                errors.append('Date of joining is not realistic')
            elif d != emp.date_of_joining:
                data['date_of_joining'] = d.isoformat()
                changes.append(f'Joining date: {emp.date_of_joining} -> {d}')
                warnings.append('Changing the joining date changes which days count as employed')
    if status:
        s = STATUSES.get(_norm(status))
        if s is None:
            errors.append('Status must be ACTIVE, ON_LEAVE, RESIGNED or TERMINATED')
        elif s != emp.employment_status:
            data['employment_status'] = s
            changes.append(f'Status: {emp.get_employment_status_display()} -> {dict(Employee.EMPLOYMENT_STATUS_CHOICES)[s]}')
            if s in ('RESIGNED', 'TERMINATED'):
                warnings.append('Exit: check this employee in Full & Final Settlement')
            elif emp.employment_status in ('RESIGNED', 'TERMINATED'):
                warnings.append('Re-activating a former employee')
    who = f'{emp.employee_code} {emp.full_name}'
    if errors:
        return _result(n, who, 'error', errors=errors)
    if not changes:
        return _result(n, who, 'skip', 'No change')
    return _result(n, who, 'ok', '; '.join(changes), warnings=warnings, data=data)


def apply_joining_exit(request, company, results, options):
    done = 0
    for r in results:
        d = dict(r['data'])
        emp = Employee.objects.select_for_update().get(company=company, employee_code=d.pop('employee_id'))
        if 'date_of_joining' in d:
            emp.date_of_joining = datetime.date.fromisoformat(d['date_of_joining'])
        if 'employment_status' in d:
            emp.employment_status = d['employment_status']
        emp.save()
        done += 1
    return f'{done} employee(s) updated'


spec('joining-exit', 'Employee Joining / Exit Updates', 'hrms_joining_exit',
     'Set the joining date or the employment status (Active, On leave, Resigned, Terminated) of existing employees from a file. New employees are added with the Employee Import.',
     [('employee_id', 'Employee ID', True, ('empid', 'employeecode', 'empcode', 'code')), ('date_of_joining', 'Date of Joining', False, ('joiningdate', 'doj', 'dateofjoin')),
      ('status', 'Status', False, ('employmentstatus', 'employeestatus'))],
     validate_joining_exit, apply_joining_exit, example=['EPRO0001', '2024-06-03', 'ACTIVE'],
     notes=['Give a joining date, a status, or both. Blank cells are left unchanged.', 'This does not create the Full & Final Settlement: do that for each exit.'])


# ---- 3. Attendance Import (also takes a biometric export) -------------------------------------------------
ATT_STATUS = {'p': 'PRESENT', 'present': 'PRESENT', 'a': 'ABSENT', 'absent': 'ABSENT', 'hd': 'HALF_DAY', 'halfday': 'HALF_DAY', 'half': 'HALF_DAY',
              'l': 'LEAVE', 'leave': 'LEAVE', 'onleave': 'LEAVE', 'ol': 'LEAVE'}


def prepare_attendance(ctx, items):
    dates = []
    for _n, raw in items:
        try:
            d = parse_date(raw.get('date'))
        except ValueError:
            d = None
        if d:
            dates.append(d)
    ctx['existing'] = {}
    ctx['leaves'] = defaultdict(list)
    if dates:
        lo, hi = min(dates), max(dates)
        for a in Attendance.objects.filter(employee__company=ctx['company'], date__gte=lo, date__lte=hi):
            ctx['existing'][(a.employee_id, a.date)] = a
        for lv in LeaveRequest.objects.filter(employee__company=ctx['company'], status='APPROVED', from_date__lte=hi, to_date__gte=lo):
            ctx['leaves'][lv.employee_id].append((lv.from_date, lv.to_date))


def validate_attendance(ctx, n, raw, options):
    code = _s(raw.get('employee_id'))
    emp = ctx['emps'].get(code.lower()) if code else None
    if emp is None:
        return _result(n, code or '(blank)', 'error', errors=['No employee with this Employee ID in your company'])
    who = f'{emp.employee_code} {emp.full_name}'
    errors, warnings = [], []
    try:
        day = parse_date(raw.get('date'))
    except ValueError:
        day = None
        errors.append('Date is not a date (use 2024-06-03 or 03-06-2024)')
    else:
        if day is None:
            errors.append('Date is missing')
        elif day > ctx['today']:
            errors.append('Attendance cannot be imported for a future date')
        elif day < emp.date_of_joining:
            errors.append(f'Date is before the joining date ({emp.date_of_joining})')
    cin = cout = None
    try:
        cin = parse_time(raw.get('check_in'))
    except ValueError:
        errors.append('Check-in is not a time (use 09:30)')
    try:
        cout = parse_time(raw.get('check_out'))
    except ValueError:
        errors.append('Check-out is not a time (use 18:30)')
    if cin and cout and cout <= cin:
        errors.append('Check-out must be after check-in')
    status_text = _s(raw.get('status'))
    if status_text:
        status = ATT_STATUS.get(_norm(status_text))
        if status is None:
            errors.append('Status must be P/Present, A/Absent, HD/Half day or L/Leave')
    elif cin:
        status = 'PRESENT'
    else:
        status = None
        errors.append('Give a status, or a check-in time')
    if day and not errors:
        key = (emp.pk, day)
        if key in ctx['seen']['rows']:
            errors.append('This employee and date appear twice in the file')
        ctx['seen']['rows'].add(key)
    if errors:
        return _result(n, who, 'error', errors=errors)
    if status in ('PRESENT', 'HALF_DAY') and any(a <= day <= b for a, b in ctx['leaves'].get(emp.pk, [])):
        warnings.append('This day is inside approved leave')
    remarks = _s(raw.get('remarks'))[:255]
    data = {'employee_id': emp.employee_code, 'date': day.isoformat(), 'status': status, 'check_in': cin.isoformat() if cin else '',
            'check_out': cout.isoformat() if cout else '', 'remarks': remarks}
    label = f'{day:%d %b %Y}: {dict(Attendance.STATUS_CHOICES)[status]}' + (f' {cin:%H:%M}-{cout:%H:%M}' if cin and cout else '')
    existing = ctx['existing'].get((emp.pk, day))
    if existing is None:
        return _result(n, who, 'ok', 'New: ' + label, warnings=warnings, data=dict(data, action='create'))
    if not options.get('overwrite'):
        return _result(n, who, 'skip', f'Already has a record ({dict(Attendance.STATUS_CHOICES)[existing.status]}), left unchanged')
    same = (existing.status == status and existing.check_in == cin and existing.check_out == cout and (existing.remarks or '') == remarks)
    if same:
        return _result(n, who, 'skip', 'No change')
    return _result(n, who, 'ok', f'Update: {label} (was {dict(Attendance.STATUS_CHOICES)[existing.status]})', warnings=warnings, data=dict(data, action='update'))


def apply_attendance(request, company, results, options):
    emps = employees_by_code(company)
    create, update = [], []
    existing = {(a.employee_id, a.date): a for a in Attendance.objects.filter(
        employee__company=company, date__in={datetime.date.fromisoformat(r['data']['date']) for r in results})}
    for r in results:
        d = r['data']
        emp, day = emps[d['employee_id'].lower()], datetime.date.fromisoformat(d['date'])
        cin = datetime.time.fromisoformat(d['check_in']) if d['check_in'] else None
        cout = datetime.time.fromisoformat(d['check_out']) if d['check_out'] else None
        rec = existing.get((emp.pk, day))
        if rec is None:
            create.append(Attendance(employee=emp, date=day, status=d['status'], check_in=cin, check_out=cout, remarks=d['remarks']))
        else:
            rec.status, rec.check_in, rec.check_out, rec.remarks = d['status'], cin, cout, d['remarks']
            update.append(rec)
    Attendance.objects.bulk_create(create)
    if update:
        Attendance.objects.bulk_update(update, ['status', 'check_in', 'check_out', 'remarks'])
    return f'{len(create)} attendance record(s) added, {len(update)} updated'


spec('attendance', 'Attendance Import', 'hrms_attendance_import',
     'Import attendance from a file: one row per employee per day. It also reads the export file of a biometric system (columns such as Employee Code, Date, In Time, Out Time, Status P/A).',
     [('employee_id', 'Employee ID', True, ('empid', 'employeecode', 'empcode', 'code')), ('date', 'Date', True, ('attendancedate', 'punchdate', 'logdate', 'day')),
      ('status', 'Status', False, ('attendancestatus', 'attstatus')),
      ('check_in', 'Check In', False, ('checkin', 'intime', 'in', 'punchin', 'firstpunch', 'timein', 'checkintime')),
      ('check_out', 'Check Out', False, ('checkout', 'outtime', 'out', 'punchout', 'lastpunch', 'timeout', 'checkouttime')),
      ('remarks', 'Remarks', False, ('notes', 'comment'))],
     validate_attendance, apply_attendance, options=(('overwrite', 'Also update days that already have an attendance record (otherwise they are left unchanged)'),),
     example=['EPRO0001', '2026-10-01', 'P', '09:02', '18:31', ''], prepare=prepare_attendance,
     notes=['Status: P (Present), A (Absent), HD (Half day), L (Leave). If the status is blank, a check-in time means Present.',
            'Future dates and dates before the joining date are refused. Check-out must be after check-in.'])


# ---- 4. Leave Data Import -----------------------------------------------------------------------------------
LEAVE_TYPES = {'casual': 'CASUAL', 'cl': 'CASUAL', 'casualleave': 'CASUAL', 'sick': 'SICK', 'sl': 'SICK', 'sickleave': 'SICK',
               'earned': 'EARNED', 'el': 'EARNED', 'pl': 'EARNED', 'privilege': 'EARNED', 'earnedleave': 'EARNED',
               'unpaid': 'UNPAID', 'lop': 'UNPAID', 'lwp': 'UNPAID', 'unpaidleave': 'UNPAID', 'maternity': 'MATERNITY', 'ml': 'MATERNITY',
               'paternity': 'PATERNITY', 'compoff': 'COMP_OFF', 'co': 'COMP_OFF', 'compensatoryoff': 'COMP_OFF'}
LEAVE_STATUS = {'approved': 'APPROVED', 'pending': 'PENDING', 'rejected': 'REJECTED'}


def prepare_leave(ctx, items):
    ctx['live'] = defaultdict(list)
    for lv in LeaveRequest.objects.filter(employee__company=ctx['company'], status__in=['PENDING', 'APPROVED']):
        ctx['live'][lv.employee_id].append((lv.from_date, lv.to_date))
    ctx['balances'] = {}
    ctx['used'] = defaultdict(int)


def _remaining(ctx, emp, year, leave_type):
    key = (emp.pk, year)
    if key not in ctx['balances']:
        try:
            from .leave import leave_balance
            ctx['balances'][key] = leave_balance(emp, year)
        except Exception:                                   # a balance problem must never stop an import
            ctx['balances'][key] = {}
    return (ctx['balances'][key].get(leave_type) or {}).get('remaining')


def validate_leave(ctx, n, raw, options):
    code = _s(raw.get('employee_id'))
    emp = ctx['emps'].get(code.lower()) if code else None
    if emp is None:
        return _result(n, code or '(blank)', 'error', errors=['No employee with this Employee ID in your company'])
    who = f'{emp.employee_code} {emp.full_name}'
    errors, warnings = [], []
    leave_type = LEAVE_TYPES.get(_norm(raw.get('leave_type')))
    if leave_type is None:
        errors.append('Leave type must be Casual, Sick, Earned, Unpaid, Maternity, Paternity or Comp off')
    status_text = _s(raw.get('status'))
    status = LEAVE_STATUS.get(_norm(status_text)) if status_text else 'APPROVED'
    if status is None:
        errors.append('Status must be Approved, Pending or Rejected (blank means Approved)')
    start = end = None
    try:
        start, end = parse_date(raw.get('from_date')), parse_date(raw.get('to_date'))
        if start is None or end is None:
            errors.append('From and to dates are needed')
    except ValueError:
        errors.append('From / to date is not a date (use 2024-06-03 or 03-06-2024)')
    if start and end:
        if end < start:
            errors.append('To date cannot be earlier than from date')
        elif (end - start).days > 365:
            errors.append('A leave cannot be longer than a year')
    reason = _s(raw.get('reason'))
    if len(reason) > 255:
        errors.append('Reason is longer than 255 characters')
    if errors:
        return _result(n, who, 'error', errors=errors)
    if status in ('PENDING', 'APPROVED'):
        if any(a <= end and b >= start for a, b in ctx['live'].get(emp.pk, [])):
            return _result(n, who, 'error', errors=['These dates overlap an existing pending or approved leave'])
        ctx['live'][emp.pk].append((start, end))
        days = (end - start).days + 1
        remaining = _remaining(ctx, emp, start.year, leave_type)
        used = ctx['used'][(emp.pk, start.year, leave_type)]
        if remaining is not None and days + used > remaining:
            warnings.append(f'More than the balance: {days + used} day(s) against {remaining} remaining')
        ctx['used'][(emp.pk, start.year, leave_type)] += days
    data = {'employee_id': emp.employee_code, 'leave_type': leave_type, 'from_date': start.isoformat(), 'to_date': end.isoformat(), 'reason': reason,
            'status': status, 'comment': _s(raw.get('comment'))[:200]}
    summary = f'{dict(LeaveRequest.LEAVE_TYPE_CHOICES)[leave_type]} {start:%d %b} - {end:%d %b %Y} ({(end - start).days + 1} day(s)), {status.lower()}'
    return _result(n, who, 'ok', summary, warnings=warnings, data=data)


def apply_leave(request, company, results, options):
    emps = employees_by_code(company)
    now = timezone.now()
    new = []
    for r in results:
        d = r['data']
        decided = d['status'] in ('APPROVED', 'REJECTED')
        new.append(LeaveRequest(employee=emps[d['employee_id'].lower()], leave_type=d['leave_type'], from_date=datetime.date.fromisoformat(d['from_date']),
                                to_date=datetime.date.fromisoformat(d['to_date']), reason=d['reason'], status=d['status'],
                                decided_by=request.user if decided else None, decided_at=now if decided else None,
                                approver_comment=('Imported. ' + d['comment']).strip()[:255] if decided else ''))
    LeaveRequest.objects.bulk_create(new)
    return f'{len(new)} leave record(s) added'


spec('leave', 'Leave Data Import', 'hrms_leave_import',
     'Import leave records from a file (for example approved leave from your HR system). Overlapping dates are refused; a leave above the balance is allowed but flagged in the preview.',
     [('employee_id', 'Employee ID', True, ('empid', 'employeecode', 'empcode', 'code')), ('leave_type', 'Leave Type', True, ('type', 'leavecode', 'leavetype')),
      ('from_date', 'From Date', True, ('fromdate', 'start', 'startdate', 'from')), ('to_date', 'To Date', True, ('todate', 'end', 'enddate', 'to')),
      ('reason', 'Reason', False, ('remarks', 'notes')), ('status', 'Status', False, ('leavestatus',)), ('comment', 'Comment', False, ('approvercomment',))],
     validate_leave, apply_leave, example=['EPRO0001', 'Casual', '2026-10-05', '2026-10-06', 'Family function', 'Approved', ''], prepare=prepare_leave,
     notes=['Leave type: Casual, Sick, Earned, Unpaid, Maternity, Paternity or Comp off (CL, SL, EL, LOP are understood).',
            'Status: Approved, Pending or Rejected. Blank means Approved. Approved and rejected rows are recorded as decided by you.'])


# --------------------------------------------------------------------------- the import pages
def make_import_view(sp):
    @company_owner_required
    def view(request):
        company = _company(request)
        if request.method == 'POST':
            step = request.POST.get('step')
            if step == 'upload':
                upload = request.FILES.get('file')
                if upload is None:
                    messages.error(request, 'Choose the file first.')
                    return redirect(sp.name)
                options = {f: bool(request.POST.get(f)) for f, _l in sp.options}
                rows, error = read_rows(upload)
                items, error2 = (None, error) if rows is None else parse_table(rows, sp)
                if items is None:
                    messages.error(request, error2)
                    return redirect(sp.name)
                results = run_validation(sp, company, items, options)
                raw = [{'n': n, 'raw': {f: _s(v) for f, v in r.items()}} for (n, r), res in zip(items, results) if res['outcome'] == 'ok']
                counts = {'ok': len(raw), 'error': sum(1 for r in results if r['outcome'] == 'error'), 'skip': sum(1 for r in results if r['outcome'] == 'skip'),
                          'warn': sum(1 for r in results if r['outcome'] == 'ok' and r['warnings'])}
                request.session[SESSION_KEY] = {'spec': sp.key, 'company': company.pk, 'at': time.time(), 'name': upload.name[:80], 'options': options,
                                                'items': raw, 'results': [{k: v for k, v in r.items() if k != 'data'} for r in results[:PREVIEW_ROWS]],
                                                'total': len(results), 'counts': counts}
                return redirect(sp.name)
            if step == 'confirm':
                saved = request.session.get(SESSION_KEY)
                if not (saved and saved.get('spec') == sp.key and saved.get('company') == company.pk and time.time() - saved.get('at', 0) < PREVIEW_MINUTES * 60):
                    request.session.pop(SESSION_KEY, None)
                    messages.error(request, 'The preview has expired. Upload the file again.')
                    return redirect(sp.name)
                items = [(i['n'], i['raw']) for i in saved['items']]
                try:
                    with transaction.atomic():
                        fresh = [r for r in run_validation(sp, company, items, saved['options']) if r['outcome'] == 'ok']    # data may have changed since the preview
                        summary = sp.apply(request, company, fresh, saved['options'])
                        log_action(request, 'UPDATE', company, company=company, details=f'{sp.title}: {summary} (file {saved["name"]})'[:500])
                except IntegrityError:
                    request.session.pop(SESSION_KEY, None)
                    messages.error(request, 'The import was cancelled because some of the data was changed by someone else a moment ago. Nothing was saved. Upload the file again.')
                    return redirect(sp.name)
                request.session.pop(SESSION_KEY, None)
                skipped = len(items) - len(fresh)
                messages.success(request, f'{summary}.' + (f' {skipped} row(s) were skipped because they changed after the preview.' if skipped else ''))
                return redirect(sp.name)
            if step == 'discard':
                request.session.pop(SESSION_KEY, None)
                return redirect(sp.name)
        saved = request.session.get(SESSION_KEY)
        preview = None
        if saved and saved.get('spec') == sp.key and saved.get('company') == company.pk and time.time() - saved.get('at', 0) < PREVIEW_MINUTES * 60:
            results = saved['results']
            preview = dict(saved['counts'], rows=results, name=saved['name'], total=saved['total'], shown=len(results), minutes=PREVIEW_MINUTES)
        return render(request, 'hrms/import.html', {'sp': sp, 'preview': preview, 'max_rows': MAX_ROWS, 'columns': sp.columns})
    view.__name__ = 'import_' + sp.key.replace('-', '_')
    return view


def make_template_view(sp):
    @company_owner_required
    def view(request):
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        wb = Workbook()
        ws = wb.active
        ws.title = 'Data'
        ws.append([label for _f, label, _r, _a in sp.columns])
        ws.append(list(sp.example))
        for i, (_f, label, req, _a) in enumerate(sp.columns, start=1):
            cell = ws.cell(row=1, column=i)
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='4A4AD8' if req else '64748B')
            ws.column_dimensions[cell.column_letter].width = max(14, len(label) + 2)
        notes = wb.create_sheet('Instructions')
        for line in ['Fill one row per line on the Data sheet and delete the example row.',
                     'Purple headings are required: ' + ', '.join(l for _f, l, req, _a in sp.columns if req) + '.',
                     'Dates: 2026-10-05 or 05-10-2026 or 05/10/2026 (day first). Times: 09:30.'] + list(sp.notes):
            notes.append([line])
        notes.column_dimensions['A'].width = 120
        buf = io.BytesIO()
        wb.save(buf)
        resp = HttpResponse(buf.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        resp['Content-Disposition'] = f'attachment; filename="{sp.key.replace("-", "_")}_template.xlsx"'
        return resp
    view.__name__ = 'template_' + sp.key.replace('-', '_')
    return view


# --------------------------------------------------------------------------- connection pages
def _links(*pairs):
    out = []
    for label, name in pairs:
        try:
            out.append((label, reverse(name)))
        except NoReverseMatch:
            continue
    return out


def email_status():
    """Email settings as the owner needs to see them. Passwords and keys are never shown, only whether they are set."""
    from django.conf import settings
    backend = str(getattr(settings, 'EMAIL_BACKEND', '') or '')
    host = str(getattr(settings, 'EMAIL_HOST', '') or '')
    user_set = bool(getattr(settings, 'EMAIL_HOST_USER', ''))
    pass_set = bool(getattr(settings, 'EMAIL_HOST_PASSWORD', ''))
    smtp = 'smtp' in backend.lower()
    if not smtp:
        state, tone, note = 'Emails are not sent', 'badge-danger', f'The email backend is "{backend or "not set"}", which does not send real email.'
    elif not host:
        state, tone, note = 'Not configured', 'badge-danger', 'EMAIL_HOST is empty, so the server has nowhere to send email. Payslip emails will fail until it is set.'
    elif user_set != pass_set:
        state, tone, note = 'Incomplete', 'badge-warn', 'Only one of the email username and password is set.'
    else:
        state, tone, note = 'Configured', 'badge-success', 'Use the test button to confirm that a real email arrives.'
    rows = [('Backend', backend or 'not set'), ('Server', host or 'EMPTY'), ('Port', str(getattr(settings, 'EMAIL_PORT', '') or 'not set')),
            ('TLS', 'on' if getattr(settings, 'EMAIL_USE_TLS', False) else 'off'), ('SSL', 'on' if getattr(settings, 'EMAIL_USE_SSL', False) else 'off'),
            ('Username', 'set' if user_set else 'empty'), ('Password', 'set' if pass_set else 'empty'),
            ('From address', str(getattr(settings, 'DEFAULT_FROM_EMAIL', '') or 'not set'))]
    return {'state': state, 'tone': tone, 'note': note, 'rows': rows}


CONNECTIONS = OrderedDict([
    ('biometric', {'title': 'Biometric Integration', 'name': 'hrms_biometric', 'state': 'No live connection', 'tone': 'badge-neutral',
                   'summary': 'Your biometric device software is not connected to payroll. Its punch data comes in through a file.',
                   'today': ['Export the attendance report from your biometric software (for example eTimeTrack) as CSV or Excel.',
                             'Upload it in Attendance Import. Columns such as Employee Code, Date, In Time, Out Time and Status (P/A) are recognised.',
                             'You see exactly what will be added or changed, and nothing is saved until you confirm.'],
                   'needs': 'A live link needs the biometric system to expose an interface and its access details. That is not available from here.',
                   'links': (('Attendance Import', 'hrms_attendance_import'), ('Bulk attendance', 'attendance_bulk'))}),
    ('accounting', {'title': 'Accounting Software Integration', 'name': 'hrms_accounting_sw', 'state': 'No live connection', 'tone': 'badge-neutral',
                    'summary': 'Payroll is not connected to Tally, Zoho Books or any other accounting software. Journals leave as a file.',
                    'today': ['Post the salary journal in Accounting Integration.', 'Set the ledger names your software uses in General Ledger Mapping.',
                              'Download the posted journal lines as CSV or Excel from Accounting Export and import them into your software.'],
                    'needs': 'A live link needs the accounting software API and its access details.',
                    'links': (('Accounting Export', 'acc_export'), ('General Ledger Mapping', 'acc_mapping'), ('Salary Expense Journal', 'acc_journals'))}),
    ('bank', {'title': 'Bank Integration', 'name': 'hrms_bank', 'state': 'No live connection', 'tone': 'badge-neutral',
              'summary': 'Payroll does not talk to your bank. Salary goes out as a file that you upload to the bank portal.',
              'today': ['Create a payment batch, get it approved, and download the bank file in Bank Payment File.',
                        'Upload the file to your bank portal, then bring the bank statement or response file back into Bank Reconciliation.'],
              'needs': 'A live link needs a corporate API agreement with your bank. Until then no payment is sent from here.',
              'links': (('Bank Payment File', 'bank_batches'), ('Bank Reconciliation', 'bank_reconcile'), ('Payment Approval', 'bank_approvals'))}),
    ('email', {'title': 'Email Integration', 'name': 'hrms_email', 'summary': 'Payslips and notices are sent by email through the server settings below.',
               'today': ['Payslips can be emailed from Payslip Management.', 'Use the button below to send a test email to your own address.'],
               'needs': 'Email settings live on the server (environment variables), so they are changed there, not on this page.',
               'links': (('Email payslips', 'email_payslip'),)}),
])


def make_connection_view(key):
    info = CONNECTIONS[key]

    @company_owner_required
    def view(request):
        _company(request)
        status = email_status() if key == 'email' else None
        if key == 'email' and request.method == 'POST':
            last = request.session.get('hrms_test_mail', 0)
            to = (request.user.email or '').strip()
            if time.time() - last < 20:
                messages.error(request, 'Please wait a few seconds before sending another test email.')
            elif not to:
                messages.error(request, 'Your login has no email address, so there is nowhere to send the test.')
            else:
                request.session['hrms_test_mail'] = time.time()
                try:
                    from django.core.mail import send_mail
                    send_mail('Test email from payroll', 'This is a test email. If you can read it, payroll email works.', None, [to], fail_silently=False)
                except Exception as exc:                          # show the reason, never a password
                    messages.error(request, f'The test email failed: {type(exc).__name__}: {str(exc)[:200]}')
                    log_action(request, 'OTHER', company=request.user.company, details=f'Test email failed ({type(exc).__name__})')
                else:
                    messages.success(request, f'A test email was sent to {to}. Check that inbox (and spam).')
                    log_action(request, 'OTHER', company=request.user.company, details='Test email sent')
            return redirect(info['name'])
        shown = dict(info, links=_links(*info['links']))
        if status:
            shown.update(state=status['state'], tone=status['tone'], status_note=status['note'], status_rows=status['rows'])
        return render(request, 'hrms/connection.html', {'info': shown, 'key': key})
    view.__name__ = 'connection_' + key
    return view


def _url(name):
    try:
        return reverse(name)
    except NoReverseMatch:
        return ''


@company_owner_required
def hrms_import_center(request):
    """Feature: Secure API / CSV / Excel Integration. Every file import in one place."""
    _company(request)
    items = [{'title': 'Employee Import (new employees)', 'description': 'Add new employees, and optionally their logins, from an Excel file.',
              'url': _url('hr_employee_import'), 'template': _url('hr_employee_import_template')}]
    items += [{'title': sp.title, 'description': sp.description, 'url': reverse(sp.name), 'template': reverse(sp.name + '_template')} for sp in SPECS.values()]
    return render(request, 'hrms/center.html', {'items': [i for i in items if i['url']]})


urlpatterns = []
for _sp in SPECS.values():
    urlpatterns.append(path('integration/' + _sp.key + '/', make_import_view(_sp), name=_sp.name))
    urlpatterns.append(path('integration/' + _sp.key + '/template.xlsx', make_template_view(_sp), name=_sp.name + '_template'))
for _key, _info in CONNECTIONS.items():
    urlpatterns.append(path('integration/' + _key + '/', make_connection_view(_key), name=_info['name']))
urlpatterns.append(path('integration/import-center/', hrms_import_center, name='hrms_import_center'))
