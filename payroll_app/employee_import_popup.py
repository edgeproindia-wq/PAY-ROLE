"""Employee import inside a popup (no page change): check and confirm as JSON.
It uses the same rules (validate_rows) and the same fast bulk save as the Import Employees page."""
import logging
import secrets
import time

from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.urls import path
from django.views.decorators.http import require_POST

from .audit import log_action
from .employee_import import ConfirmForm, UploadForm, _ImportEmployeeForm, read_rows, validate_rows
from .models import Employee, User
from .permissions import company_owner_required

SESSION_KEY = 'employee_popup_import'
MAX_BYTES = 5 * 1024 * 1024
TOKEN_MINUTES = 30


def create_employees(company, good_rows, create_logins, password):
    """Save the employees (and optionally their logins) in one go. Returns (employees_created, logins_created)."""
    people = []
    for data in good_rows:
        form = _ImportEmployeeForm(data=data)
        form.is_valid()
        e = form.save(commit=False)
        e.company = company
        people.append(e)
    logins = 0
    with transaction.atomic():
        if people and create_logins:
            shared_hash = make_password(password)                      # hash once: hashing is slow on purpose
            taken = {u.lower() for u in User.objects.filter(username__istartswith=f'emp{company.pk}-').values_list('username', flat=True)}
            users = []
            for e in people:
                base = f'emp{company.pk}-{e.employee_code}'.lower().replace(' ', '')[:140]
                username, n = base, 1
                while username.lower() in taken:
                    n += 1
                    username = f'{base}-{n}'
                taken.add(username.lower())
                users.append(User(username=User.normalize_username(username), password=shared_hash, role='EMPLOYEE', company=company,
                                  email=e.email, first_name=e.first_name, last_name=e.last_name))
            User.objects.bulk_create(users)
            for e, u in zip(people, users):
                e.user = u
            logins = len(users)
        Employee.objects.bulk_create(people)
    return len(people), logins


def _error(message, status=400):
    return JsonResponse({'ok': False, 'error': message}, status=status)


@company_owner_required
@require_POST
def check(request):
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return _error(' '.join(e for errs in form.errors.values() for e in errs) or 'Choose a file first.')
    upload = form.cleaned_data['file']
    if upload.size > MAX_BYTES:
        return _error('The file is too large (maximum 5 MB).')
    rows, err = read_rows(upload)
    if err:
        return _error(err)
    report = validate_rows(rows, request.user.company, fill_gaps=form.cleaned_data.get('fill_gaps', False))
    good = [r['data'] for r in report if not r['errors']]
    token = secrets.token_urlsafe(16)
    request.session[SESSION_KEY] = {'token': token, 'at': time.time(), 'rows': good}
    shown = sorted(report, key=lambda r: 0 if r['errors'] else 1)[:200]       # problems first
    return JsonResponse({'ok': True, 'token': token, 'total': len(report), 'ready': len(good), 'errors': len(report) - len(good),
                         'rows': [{'line': r['line'], 'code': r['code'], 'name': r['name'], 'ok': not r['errors'], 'text': '; '.join(r['errors'])} for r in shown],
                         'truncated': len(report) > len(shown)})


@company_owner_required
@require_POST
def confirm(request):
    company = request.user.company
    saved = request.session.get(SESSION_KEY)
    if not saved or not secrets.compare_digest(saved['token'], request.POST.get('token', '')) or time.time() - saved['at'] > TOKEN_MINUTES * 60:
        return _error('This check has expired. Check the file again.')
    form = ConfirmForm(request.POST)
    if not form.is_valid():
        return _error(' '.join(m for errs in form.errors.values() for m in errs))        # the check stays valid so the password can be corrected
    request.session.pop(SESSION_KEY, None)                                               # one use only
    rows = saved['rows']
    fresh = validate_rows(rows, company)                                                  # re-check: data may have changed since the check
    good = [r['data'] for r in fresh if not r['errors']]
    try:
        created, logins = create_employees(company, good, form.cleaned_data['create_logins'], form.cleaned_data['password1'])
    except IntegrityError:
        logging.getLogger(__name__).exception('Employee popup import failed on a duplicate')
        return _error('The import was cancelled because some of these employees were added by someone else a moment ago. Nothing was saved. Check the file again.', 409)
    log_action(request, 'CREATE', company, details=f'Employee import: {created} employees, {logins} logins')
    return JsonResponse({'ok': True, 'created': created, 'logins': logins, 'skipped': len(rows) - len(good)})


urlpatterns = [
    path('hr/employee-import/popup/check/', check, name='emp_popup_check'),
    path('hr/employee-import/popup/confirm/', confirm, name='emp_popup_confirm'),
]