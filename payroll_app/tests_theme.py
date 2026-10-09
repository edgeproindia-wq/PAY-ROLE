"""The new sidebar and theme: files exist, pages render for every role, no menu link was lost."""
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from .models import Employee, User
from .tests import make_company, make_employee, make_user

OWNER_LINKS = [
    'dashboard', 'employee_master', 'hr_employee_import', 'hr_employee_logins', 'attendance', 'leave_management',
    'hr_shifts', 'hr_work_hours', 'grievances', 'announcements', 'salary_structure', 'payroll_processing',
    'payslips', 'bank_transfer', 'reimbursement', 'hr_loans', 'hr_insurance', 'hr_pay_components', 'arrears',
    'full_final_settlement', 'statutory_compliance', 'hr_professional_tax', 'income_tax', 'investment_declaration',
    'hr_declarations', 'hr_form16', 'hr_documents', 'compliance_reports', 'reports_analytics', 'ess',
    'user_roles_permissions', 'settings', 'client_complaints', 'client_requests', 'notifications',
]
ADMIN_LINKS = ['admin_dashboard', 'admin_company_list', 'admin_company_approvals', 'admin_demo_requests',
               'admin_client_complaints', 'admin_client_requests', 'admin_audit_log', 'notifications']


def hrefs(names):
    out = []
    for n in names:
        try:
            out.append((n, reverse(n)))
        except NoReverseMatch:
            pass                      # a page that does not exist in this project never had a menu link
    return out


class ThemeTests(TestCase):
    def setUp(self):
        self.co = make_company('Theme Co')
        self.owner = make_user('themeowner', 'COMPANY_OWNER', self.co)
        self.emp_user = make_user('themeemp', 'EMPLOYEE', self.co)
        emp = make_employee(self.co, 'TH1', 'th1@example.com')
        Employee.objects.filter(pk=emp.pk).update(user=self.emp_user)
        self.admin = User.objects.create_superuser('themeadmin', 'themeadmin@example.com', 'Adm1n-Pass#2026')

    def test_theme_files_are_served_by_the_static_system(self):
        self.assertIsNotNone(finders.find('css/namma-theme.css'))
        self.assertIsNotNone(finders.find('js/namma-theme.js'))

    def test_owner_pages_use_the_theme_and_keep_every_menu_link(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse('dashboard'), follow=True).content.decode()
        self.assertIn('namma-theme.css', html)
        self.assertIn('namma-theme.js', html)
        self.assertIn('sb-group-head', html)
        missing = [n for n, href in hrefs(OWNER_LINKS) if f'href="{href}"' not in html]
        self.assertEqual(missing, [], f'menu links lost: {missing}')

    def test_admin_sidebar_keeps_every_link(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse('admin_dashboard'), follow=True).content.decode()
        self.assertIn('sb-group-head', html)
        missing = [n for n, href in hrefs(ADMIN_LINKS) if f'href="{href}"' not in html]
        self.assertEqual(missing, [], f'menu links lost: {missing}')

    def test_employee_sidebar_renders_without_owner_only_links(self):
        self.client.force_login(self.emp_user)
        resp = self.client.get(reverse('payslips'))              # a page every employee can open
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('sb-group-head', html)
        for n, href in hrefs(['employee_master', 'hr_employee_import', 'bank_transfer', 'payroll_processing']):
            self.assertNotIn(f'href="{href}"', html, f'{n} must not be in the employee menu')

    def test_sidebar_has_collapse_button_and_user_card(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse('dashboard'), follow=True).content.decode()
        self.assertIn('id="sbCollapse"', html)
        self.assertIn('sb-user', html)
