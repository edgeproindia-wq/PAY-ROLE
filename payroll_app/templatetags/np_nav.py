"""One ordered list of every page, per role, driving two things:
  1. the sidebar shows only the MAIN items (no dropdowns); a main item opens the first page of its section
  2. one Previous / Next footer at the bottom of every page ("Page X of N", the section name, Previous, Next / Finish)

    {% np_nav_css %}   in <head>      - hides the sub-menus and the arrows, styles the highlighted main item and the footer
    {% np_nav %}       after the page - the footer and the small script (sidebar clicks, highlight, Previous / Next without a reload)
A test checks this list against the sidebar in base.html, so a page can never become unreachable."""
from django import template
from django.urls import NoReverseMatch, reverse

from ..permissions import effective_role

register = template.Library()

# (key, label shown in the sidebar and the footer, sidebar data-group or None for a plain link, [(url name, label), ...])
SECTIONS = {
    'COMPANY_OWNER': [
        ('dashboard', 'Dashboard', None, [('dashboard', 'Dashboard')]),
        ('people', 'Employee Management', 'people', [('employee_master', 'Employee Master'), ('hr_employee_logins', 'Employee Logins'),
                                                      ('user_roles_permissions', 'User Roles'), ('ess', 'ESS')]),
        ('attendance', 'Attendance', 'attendance', [('attendance', 'Attendance'), ('attendance_bulk', 'Bulk Attendance'),
                                                    ('working_calendar', 'Working Calendar & Holidays'), ('hr_shifts', 'Shifts'),
                                                    ('shift_assign', 'Assign Shifts'), ('hr_work_hours', 'Working Hours'), ('hr_day_hours', 'Daily Working Hours')]),
        ('leave', 'Leave Management', None, [('leave_management', 'Leave Management')]),
        ('payroll', 'Salary & Payroll', 'payroll', [('pay_cycles', 'Pay Cycle'), ('salary_structure', 'Salary Structure'), ('salary_history', 'Salary History'),
                                                    ('hr_pay_components', 'Other Pay Items'), ('payroll_processing', 'Payroll Processing'),
                                                    ('payroll_preview', 'Payroll Preview'), ('arrears', 'Arrears'), ('full_final_settlement', 'Full & Final'),
                                                    ('payslips', 'Payslips'), ('bank_transfer', 'Bank Transfer'),
                                                    ('reimbursement', 'Reimbursements'), ('hr_loans', 'Loans'), ('hr_insurance', 'Insurance')]),
        ('compliance', 'Tax & Compliance', 'compliance', [('statutory_compliance', 'Statutory Compliance'), ('hr_professional_tax', 'Professional Tax'),
                                                          ('income_tax', 'Income Tax'), ('investment_declaration', 'Investment Declaration'),
                                                          ('hr_declarations', 'Investment Proofs'), ('hr_form16', 'Form 16'),
                                                          ('hr_documents', 'Tax Documents'), ('compliance_reports', 'Compliance Reports')]),
        ('reports', 'Reports & Analytics', None, [('reports_analytics', 'Reports & Analytics')]),
        ('engage', 'Communication', 'engage', [('grievances', 'Grievances'), ('announcements', 'Announcements'), ('notifications', 'Notifications')]),
        ('settings', 'Settings', 'settings', [('settings', 'Company Settings'), ('client_complaints', 'Raise Complaint'), ('client_requests', 'Raise Request')]),
    ],
    'EMPLOYEE': [
        ('dashboard', 'Dashboard', None, [('employee_home', 'Dashboard'), ('dashboard', 'Dashboard')]),   # whichever of the two this project has
        ('mypay', 'My Pay', 'mypay', [('payslips', 'Payslips'), ('investment_declaration', 'Investment Declaration'), ('reimbursement', 'Reimbursements')]),
        ('selfservice', 'Self Service', 'selfservice', [('attendance', 'Attendance'), ('leave_management', 'Leave'), ('grievances', 'Grievances'),
                                                        ('announcements', 'Announcements'), ('ess', 'My Profile (ESS)')]),
        ('notifications', 'Notifications', None, [('notifications', 'Notifications')]),
    ],
    'ADMIN': [
        ('dashboard', 'Dashboard', None, [('admin_dashboard', 'Dashboard')]),
        ('platform', 'Platform', 'platform', [('admin_company_list', 'Client Management'), ('admin_company_approvals', 'Company Approvals'),
                                              ('admin_demo_requests', 'Demo Requests'), ('admin_client_complaints', 'Client Complaints'),
                                              ('admin_client_requests', 'Client Requests'), ('admin_audit_log', 'Audit Log')]),
        ('notifications', 'Notifications', None, [('notifications', 'Notifications')]),
    ],
}


def flat_pages(role):
    """Every page of a role in order, each with its section and the address its main sidebar item opens (the first page of the section)."""
    out, seen = [], set()
    for key, label, group, pages in SECTIONS.get(role, []):
        resolved = []
        for name, page_label in pages:
            try:
                resolved.append({'name': name, 'label': page_label, 'url': reverse(name)})
            except NoReverseMatch:
                continue                                      # a page this project does not have (or has not installed yet) is skipped
        if key == 'dashboard' and resolved:
            resolved = resolved[:1]                           # the first dashboard that exists
        for page in resolved:
            if page['name'] in seen:
                continue
            seen.add(page['name'])
            page.update({'section': label, 'key': key, 'group': group or '', 'main_href': resolved[0]['url']})
            out.append(page)
    return out


def locate(pages, request):
    """Index of the current page: by its address name, else the page whose address is the longest start of this address (a detail page of a list page)."""
    match = getattr(request, 'resolver_match', None)
    name = match.url_name if match else None
    for i, p in enumerate(pages):
        if p['name'] == name:
            return i
    best, best_len = None, 1
    for i, p in enumerate(pages):
        if len(p['url']) > best_len and request.path.startswith(p['url']):
            best, best_len = i, len(p['url'])
    return best


def _request(context):
    request = context.get('request')
    user = getattr(request, 'user', None)
    if request is None or user is None or not user.is_authenticated:
        return None, None
    return request, effective_role(user)


@register.inclusion_tag('partials/np_css.html', takes_context=True)
def np_nav_css(context):
    request, role = _request(context)
    if request is None:
        return {'on': False}
    try:
        reimbursement_top = reverse('reimbursement') if role == 'COMPANY_OWNER' else ''
    except NoReverseMatch:
        reimbursement_top = ''
    return {'on': True, 'reimbursement_top': reimbursement_top}


@register.inclusion_tag('partials/np_pager.html', takes_context=True)
def np_nav(context):
    request, role = _request(context)
    if request is None:
        return {'on': False}
    pages = flat_pages(role)
    i = locate(pages, request)
    if i is None:
        return {'on': True, 'show': False}                    # not a page of the menu (and not under one): no footer, the sidebar script still runs
    page, last = pages[i], i == len(pages) - 1
    at_end = last or pages[i + 1]['key'] != page['key']        # Next stays inside the section: on its last page Next is switched off
    return {'on': True, 'show': True, 'section': page['section'], 'group': page['group'], 'main_href': page['main_href'],
            'position': i + 1, 'total': len(pages), 'prev': pages[i - 1] if i > 0 else None, 'next': pages[i + 1] if not last else None,
            'is_last': last, 'at_end': at_end, 'finish_url': pages[0]['url'],
            'hide_bar': i == 0}                                     # the Dashboard has no Previous / Next: the bar is only a hidden marker so the sidebar still highlights it
