"""The PAYROLL MANAGEMENT SYSTEM structure: the 20 numbered sections of the company owner's menu.

One text block is the single source for three things:
  * the owner's sidebar (main items only, built into base.html and checked against this list by a test),
  * the owner's Previous / Next order (np_nav.SECTIONS['COMPANY_OWNER'] is built from it),
  * the overview page of every section (every feature, "Available" or "Planned").

Line format
  section :  number|key|Title
  feature : (2 spaces)Label|url_name|owner
An empty url_name means "planned: the page is not built yet". "owner" marks a feature only company owners see.
To add a feature to the menu later, put its URL name after the "|" on the right line - nothing else changes
except running the tests (they tell you if base.html needs the matching link).
"""
from django.urls import NoReverseMatch, reverse

STRUCTURE = """
1|dashboard|Dashboard
2|organisation|Organisation & Payroll Configuration
  Company Master|settings
  Branch Master|
  Department & Cost Centre|
  Payroll Calendar|pay_cycles
  Payroll Frequency|
  Working Days Configuration|
  Salary Calculation Method|
  Rounding Rules|
  Currency Configuration|
  Payroll Policy Settings|settings
3|employee-payroll-master|Employee Payroll Master
  Employee Information|employee_master
  Employment Information|
  Bank Information|
  Statutory Information|
  Salary Information|
  Payroll Eligibility|
  Employee Payroll History|
  Employee Data Import / HRMS Integration|hr_employee_import
4|salary-component-master|Salary Component Master
  Earnings|
  Employee Deductions|
  Employer Contributions|
  Component Calculation Rules|
  Component Tax Treatment|
  Statutory Wage Treatment|
  Attendance-Based Calculation|
  Effective Date Configuration|
  Component History|
  Other Pay Items|hr_pay_components
5|salary-structure|Salary Structure & CTC Management
  Salary Structure Templates|
  Employee Salary Assignment|salary_structure
  Monthly Salary Structure|
  Annual Salary Structure|
  CTC Breakup|
  Gross Salary Calculation|
  Employer Contribution Calculation|
  Net Salary Estimation|
  Salary Revision|
  Increment Management|
  Salary Structure History|salary_history
6|attendance-payable-days|Attendance & Payable Days
  Attendance Import from HRMS|
  Attendance Register|attendance
  Total Calendar Days|
  Scheduled Working Days|
  Present Days|
  Paid Leave|leave_management
  Unpaid Leave|
  Weekly Offs|
  Public Holidays|working_calendar
  Half-Day Attendance|
  Late Coming / Early Going|
  Overtime Hours|
  Weekend Work|
  Compensatory Off|
  Attendance Approval Status|
  Payable Days Calculation|
  LOP Calculation|
  Attendance Lock|
  Bulk Attendance|attendance_bulk
  Shifts|hr_shifts
  Assign Shifts|shift_assign
  Working Hours|hr_work_hours
  Daily Working Hours|hr_day_hours
7|monthly-payroll-processing|Monthly Payroll Processing
  Step 1: Create Payroll Run|payroll_processing
  Step 2: Employee Eligibility Check|
  Step 3: Import Approved Data|
  Step 4: Calculate Earnings|
  Step 5: Calculate Deductions|
  Step 6: Calculate Employer Contributions|
  Step 7: Calculate Payroll Totals|
  Step 8: Payroll Validation|
  Step 9: Draft Payroll Register|payroll_preview
  Step 10: Payroll Verification|
  Step 11: Management Approval|
  Step 12: Lock Payroll|
  Step 13: Generate Final Outputs|
8|statutory-compliance|Statutory Compliance
  Provident Fund (PF)|statutory_compliance
  Employee State Insurance (ESI)|statutory_compliance
  Professional Tax (PT)|hr_professional_tax
  Income Tax / TDS|income_tax
  Labour Welfare Fund|
  Bonus Compliance|
  Gratuity Calculation|
  Minimum Wage Validation|
  Overtime Compliance|
  Statutory Registers|compliance_reports
  Compliance Calendar|
  Investment Declaration|investment_declaration
  Investment Proofs|hr_declarations
  Form 16|hr_form16
  Tax Documents|hr_documents
9|arrears-adjustments|Arrears & Adjustments
  Salary Revision Arrears|arrears
  Previous Month Corrections|
  Incentive Adjustments|
  Bonus Adjustments|
  Reimbursement Adjustments|reimbursement
  Recovery Adjustments|
  Adjustment Approval|
  Adjustment History|
10|loan-advance|Loan & Advance Management
  Employee Loan Records|hr_loans
  Loan Approval Reference|
  Loan Amount|
  Repayment Schedule|
  Monthly Instalment|
  Outstanding Balance|
  Salary Advance|
  Recovery Calculation|
  Loan Closure|
  Insurance|hr_insurance
11|payslip-management|Payslip Management
  Payslip Template|
  Company Information|
  Employee Information|
  Attendance Details|
  Earnings Breakdown|
  Deductions Breakdown|
  Net Salary|
  Net Salary in Words|
  PDF Generation|payslips
  Bulk Payslip Generation|
  Email Distribution|
  Employee Self-Service Access|ess
  Payslip History|
12|bank-payments|Bank Payment Management
  Salary Payment Register|bank_transfer
  Bank Detail Validation|
  Bank Payment File|
  Payment Approval|
  Payment Instruction Export|
  Payment Status|
  Failed Payment Handling|
  Duplicate Payment Prevention|
  Bank Reconciliation|
  Payment Audit Trail|
13|full-final-settlement|Full & Final Settlement
  Employee Exit Details|
  Pending Salary|
  Final Payable Days|
  Notice Pay Adjustment|
  Leave Encashment|
  Eligible Bonus / Incentives|
  Gratuity (Where Applicable)|
  Loan Recovery|
  Other Authorised Recoveries|
  Statutory Deductions|
  Final Settlement Calculation|full_final_settlement
  Settlement Approval|
  Settlement Statement|
  Payment Status|
14|accounting-integration|Accounting Integration
  Salary Expense Journal|
  Salary Payable Entries|
  Employer Contribution Entries|
  Statutory Liability Entries|
  Loan Recovery Entries|
  Branch-Wise Cost Allocation|
  General Ledger Mapping|
  Accounting Export|
  Reconciliation|
15|reports-analytics|Payroll Reports & Analytics
  Monthly Payroll Register|
  Employee Salary Register|
  Branch-Wise Payroll|
  Department-Wise Payroll|
  CTC Report|
  Gross Salary Report|
  Net Salary Report|
  Earnings & Deductions Report|
  PF Report|
  ESI Report|
  PT Report|
  TDS Report|
  LOP Report|
  Overtime Report|
  Arrears Report|
  Loan Recovery Report|
  F&F Settlement Report|
  Bank Payment Report|
  Payroll Variance Report|
  Payroll Cost Trend|
  Excel / PDF / CSV Export|
  Reports & Analytics|reports_analytics
16|approvals|Approval & Workflow Management
  Payroll Maker|
  Payroll Checker|
  HR Approval|
  Finance Verification|
  Management Approval|
  Payment Authorisation|
  Rejection and Resubmission|
  Payroll Amendment|
  Payroll Reversal|
  Approval History|
17|user-security|User Management & Security
  User Accounts|hr_employee_logins
  Role-Based Access|user_roles_permissions
  Branch-Level Permissions|
  Salary Data Restrictions|
  Multi-Factor Authentication|
  Encryption|
  Session Management|
  Audit Logs|
  Backup & Recovery|
  Data Retention Policies|
18|notifications|Notifications & Automation
  Payroll Cut-Off Reminder|
  Attendance Pending Reminder|
  Payroll Approval Reminder|
  Salary Payment Notification|
  Payslip Notification|
  Statutory Due-Date Alerts|
  Missing Information Alerts|
  Payroll Exception Alerts|
  Failed Payment Alerts|
  Notifications|notifications
  Announcements|announcements
  Grievances|grievances
19|system-administration|System Administration
  Application Settings|settings
  Payroll Rule Configuration|
  Statutory Rule Versioning|
  Approval Configuration|
  API Configuration|
  Import / Export Settings|
  Error Logs|
  Integration Monitoring|
  Backup Settings|
  System Health Monitoring|
  Raise Complaint|client_complaints|owner
  Raise Request|client_requests|owner
20|hrms-integration|HRMS & External System Integration
  Employee Master Synchronisation|
  Attendance Import|
  Leave Data Import|
  Approved Salary Revision Import|
  Employee Joining / Exit Updates|
  Biometric Integration|
  Accounting Software Integration|
  Bank Integration|
  Email Integration|
  Secure API / CSV / Excel Integration|
"""


def _parse(text):
    sections = []
    for raw in text.strip('\n').splitlines():
        if not raw.strip():
            continue
        parts = [p.strip() for p in raw.strip().split('|')]
        if not raw.startswith(' '):
            sections.append({'n': int(parts[0]), 'key': parts[1], 'title': parts[2], 'children': []})
        else:
            sections[-1]['children'].append({'label': parts[0], 'url_name': parts[1] if len(parts) > 1 else '',
                                             'owner_only': len(parts) > 2 and parts[2] == 'owner'})
    return sections


SECTIONS = _parse(STRUCTURE)


def overview_name(section):
    """URL name of a section's overview page (the page a section opens when it has no real page yet)."""
    return 'module_' + section['key'].replace('-', '_')


def _assign_pages():
    """Each real page belongs to exactly one section: the first one that lists it. A section left with no page opens its overview."""
    used, out = set(), {}
    for s in SECTIONS:
        if s['key'] == 'dashboard':
            pages = [('dashboard', 'Dashboard')]
        else:
            pages = []
            for child in s['children']:
                name = child['url_name']
                if name and name not in used:
                    pages.append((name, child['label']))
                    used.add(name)
        out[s['key']] = pages or [(overview_name(s), 'Overview')]
    return out


PAGES = _assign_pages()


def section_pages(section):
    """The section's pages in menu order: (url name, label)."""
    return PAGES[section['key']]


def owner_sections():
    """np_nav.SECTIONS format: (key, label with its number, sidebar data-group or None, [(url name, label), ...])."""
    return [(s['key'], '%d. %s' % (s['n'], s['title']), None if s['key'] == 'dashboard' else s['key'], section_pages(s))
            for s in SECTIONS]


def get_section(key):
    return next((s for s in SECTIONS if s['key'] == key), None)


def rows(section, is_owner=True):
    """Overview rows: every feature of the section with its address when the page exists."""
    out = []
    for child in section['children']:
        if child['owner_only'] and not is_owner:
            continue
        url = ''
        if child['url_name']:
            try:
                url = reverse(child['url_name'])
            except NoReverseMatch:
                url = ''
        out.append({'label': child['label'], 'url': url})
    return out
