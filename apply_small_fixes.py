"""Applies 4 small payroll fixes to YOUR project without replacing whole files.
Run from the project folder (the one with manage.py):   python apply_small_fixes.py
Each file is backed up as <name>.bak first. A step whose text is not found is SKIPPED and reported,
so your own newer code is never overwritten. Running it twice is safe."""
import os, re, shutil, sys

BANK_REFS = '"""One rule for a bank reference (UTR) typed by hand or read from a bank statement.\n\nA real reference is 8 to 35 letters/digits (NEFT 16, RTGS 22, IMPS/UPI 12; a few banks add - or /).\nA one-letter "x" used to be accepted and marked the salary as paid, so it is refused now.\nThe reference is stored in capitals without spaces, so "utr 123" and "UTR123" are the same reference\nand cannot be used for two payments."""\nimport re\n\nUTR_PATTERN = re.compile(r\'^[A-Z0-9][A-Z0-9/\\-]{7,34}$\')\nUTR_HELP = (\'Enter the bank reference (UTR) exactly as the bank shows it: 8 to 35 letters or digits \'\n            \'(for example HDFCN52026111700123 or 432112345678).\')\n\n\ndef normalise_utr(value):\n    return re.sub(r\'\\s+\', \'\', str(value or \'\')).upper()\n\n\ndef utr_problem(value):\n    """\'\' when the reference looks real, otherwise the message to show."""\n    ref = normalise_utr(value)\n    if not ref:\n        return \'A bank UTR / reference number is required.\'\n    if not UTR_PATTERN.match(ref):\n        return UTR_HELP\n    return \'\'\n'

THROTTLE_NEW = '"""Brute-force protection for the login form: after MAX_FAILURES failed attempts for\nthe same username FROM THE SAME IP (or MAX_FAILURES_PER_IP from one IP) within WINDOW seconds,\nfurther attempts from that IP are refused for WINDOW seconds.\n\nThe username counter is per (username, IP) on purpose. It used to count the username alone, so\na stranger typing wrong passwords for "owner1" locked the real owner out for 15 minutes.\nNow the stranger locks only himself. (A global per-username lock would bring that problem back.) Uses Django\'s cache (per-process LocMem by default; use a\nshared cache such as Redis in production for multi-worker accuracy)."""\nfrom django.conf import settings\nfrom django.contrib.auth.signals import user_logged_in, user_login_failed\nfrom django.core.cache import cache\nfrom django.dispatch import receiver\n\nMAX_FAILURES = 5            # per account\nMAX_FAILURES_PER_IP = 50    # per IP: an office sharing one connection is not locked out by one person\nWINDOW = 15 * 60\n\n\ndef _ip(request):\n    """The client address. X-Forwarded-For is written by whoever sends the request, so only the entries\n    added by OUR proxies can be trusted: the last TRUSTED_PROXY_COUNT ones. The first entry (used before)\n    can be invented by the attacker to dodge the limit. TRUSTED_PROXY_COUNT=0 means: no proxy, ignore the header."""\n    if request is None:\n        return \'unknown\'\n    remote = request.META.get(\'REMOTE_ADDR\', \'\') or \'unknown\'\n    fwd = request.META.get(\'HTTP_X_FORWARDED_FOR\', \'\')\n    trusted = int(getattr(settings, \'TRUSTED_PROXY_COUNT\', 0) or 0)\n    parts = [p.strip() for p in fwd.split(\',\') if p.strip()]\n    if trusted and parts:\n        return parts[-min(trusted, len(parts))]\n    return remote\n\n\ndef _keys(request, username):\n    ip = _ip(request)\n    return [f\'login-fail:user:{(username or "").strip().lower()}:{ip}\', f\'login-fail:ip:{ip}\']\n\n\ndef is_locked(request, username):\n    try:\n        user_key, ip_key = _keys(request, username)\n        return (cache.get(user_key) or 0) >= MAX_FAILURES or (cache.get(ip_key) or 0) >= MAX_FAILURES_PER_IP\n    except Exception:                                   # cache down: do not lock everybody out\n        import logging\n        logging.getLogger(__name__).exception(\'Login throttle cache unavailable\')\n        return False\n\n\n@receiver(user_login_failed, dispatch_uid=\'login_throttle_failed\')\ndef _failed(sender, credentials, request=None, **kwargs):\n    for k in _keys(request, (credentials or {}).get(\'username\')):\n        try:\n            try:\n                cache.incr(k)\n            except ValueError:\n                cache.set(k, 1, WINDOW)\n        except Exception:\n            import logging\n            logging.getLogger(__name__).exception(\'Login throttle cache unavailable\')\n\n\n@receiver(user_logged_in, dispatch_uid=\'login_throttle_success\')\ndef _succeeded(sender, request, user, **kwargs):\n    cache.delete(f\'login-fail:user:{user.get_username().lower()}:{_ip(request)}\')'

def patch(path, pairs, label):
    if not os.path.exists(path):
        print(f"  [SKIP] {label}: {path} not found"); return False
    src = open(path, encoding='utf-8').read()
    out, changed = src, False
    for old, new, marker in pairs:
        if marker in out:
            continue
        if old not in out:
            print(f"  [SKIP] {label}: expected code not found in {path} (your file differs; tell Claude)"); return False
        out = out.replace(old, new, 1); changed = True
    if changed:
        shutil.copy(path, path + '.bak')
        open(path, 'w', encoding='utf-8').write(out)
        print(f"  [DONE] {label}: {path}")
    else:
        print(f"  [ok]   {label}: already applied ({path})")
    return True

def main():
    if not os.path.exists('manage.py'):
        sys.exit('Run this from the folder that contains manage.py')
    print('Applying fixes...')

    p = 'payroll_app/bank_refs.py'
    if not os.path.exists(p):
        open(p, 'w', encoding='utf-8').write(BANK_REFS); print(f"  [DONE] UTR rule file created: {p}")
    patch('payroll_app/forms.py', [(
        """        if action == 'PAID' and not ref:
            raise forms.ValidationError('A bank UTR / reference number is required to mark a payment as paid.')
""",
        """        from .bank_refs import normalise_utr, utr_problem
        if action == 'PAID':
            problem = utr_problem(ref)
            if problem:
                raise forms.ValidationError(problem if ref else 'A bank UTR / reference number is required to mark a payment as paid.')
            ref = normalise_utr(ref)
""", "from .bank_refs import normalise_utr, utr_problem")], 'UTR check on the bank page')
    patch('payroll_app/bank_batches.py', [
        ("from .bank_batch_models import BankBatch, BankBatchItem\n",
         "from .bank_batch_models import BankBatch, BankBatchItem\nfrom .bank_refs import normalise_utr, utr_problem\n", "from .bank_refs import"),
        ("wanted = {_cell_text(r[cols['utr']]) for r in rows[head_i + 1:] if cols['utr'] < len(r)} - {''}",
         "wanted = {normalise_utr(_cell_text(r[cols['utr']])) for r in rows[head_i + 1:] if cols['utr'] < len(r)} - {''}", "wanted = {normalise_utr("),
        ("utr, status_text, reason = get('utr'), get('status').lower(), get('reason')",
         "utr, status_text, reason = normalise_utr(get('utr')), get('status').lower(), get('reason')", "utr, status_text, reason = normalise_utr("),
        ("""                if not utr or len(utr) > 60:
                    row['action'], row['outcome'], row['note'] = 'REVIEW', 'review', 'UTR is missing or longer than 60 characters'""",
         """                if utr_problem(utr):
                    row['action'], row['outcome'], row['note'] = 'REVIEW', 'review', ('UTR is missing' if not utr else 'UTR does not look like a bank reference (8 to 35 letters or digits)')""", "if utr_problem(utr):"),
    ], 'UTR check in bulk upload')

    patch('payroll_app/payroll_checks.py', [(
        "        if period and Attendance.objects.filter(employee__company=run.company, date__range=period).exists():\n",
        """        if period and people and not Attendance.objects.filter(employee__company=run.company, date__range=period).exists():
            issues.append(Issue(WARNING, 'NO_ATTENDANCE_AT_ALL', f'No attendance has been marked for anyone in {run.month}, so all {len(people)} '
                                                                  'employee(s) are paid the full month with no loss of pay. If you track attendance, '
                                                                  'mark it (Attendance or Bulk attendance) and reprocess this run before approving.'))
        if period and Attendance.objects.filter(employee__company=run.company, date__range=period).exists():
""", "NO_ATTENDANCE_AT_ALL")], 'No-attendance warning')

    p = 'payroll_app/login_throttle.py'
    if os.path.exists(p):
        cur = open(p, encoding='utf-8').read()
        if 'TRUSTED_PROXY_COUNT' in cur:
            print(f"  [ok]   Login lock: already applied ({p})")
        elif "fwd.split(',')[0].strip()" in cur and "login-fail:user:{(username or \"\").strip().lower()}'" in cur:
            shutil.copy(p, p + '.bak'); open(p, 'w', encoding='utf-8').write(THROTTLE_NEW); print(f"  [DONE] Login lock: {p}")
        else:
            print(f"  [SKIP] Login lock: {p} differs from the original; tell Claude")
    patch('config/settings.py', [(
        "SECURE_CONTENT_TYPE_NOSNIFF = True\n",
        "SECURE_CONTENT_TYPE_NOSNIFF = True\n"
        "# Reverse proxies in front of the app (Render = 1; 2 if also behind Cloudflare; 0 = local). Used to find the real client IP.\n"
        "TRUSTED_PROXY_COUNT = int(os.environ.get('TRUSTED_PROXY_COUNT', '1' if ON_RENDER else '0'))\n", "TRUSTED_PROXY_COUNT")], 'Proxy setting')

    patch('payroll_app/audit.py', [(
        """    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')
""",
        """    from .login_throttle import _ip
    return _ip(request)
""", "from .login_throttle import _ip")], 'Audit-log IP')
    print('Done. Now run:  python manage.py check')

main()
