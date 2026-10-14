"""Every page with its own Employee box says clearly when the company has no employees, and lists names when it has."""
import re

from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from .tests import make_company, make_employee, make_user

PAGES = ('salary_structure', 'attendance', 'leave_management', 'reimbursement', 'arrears', 'full_final_settlement')


def options(html):
    m = re.search(r'<select[^>]*name="employee".*?</select>', html, re.S)
    return re.findall(r'<option[^>]*>([^<]*)</option>', m.group(0)) if m else None


class EmployeeBoxNoticeTests(TestCase):
    def setUp(self):
        self.co, self.other = make_company('Notice Co'), make_company('Notice Other')
        self.owner = make_user('noticeowner', 'COMPANY_OWNER', self.co)
        make_employee(self.other, 'OTHER1', 'other1@notice.test')
        self.client.force_login(self.owner)

    def urls(self):
        out = []
        for name in PAGES:
            try:
                out.append((name, reverse(name)))
            except NoReverseMatch:
                pass
        return out

    def test_an_empty_company_sees_a_clear_message_with_links(self):
        for name, url in self.urls():
            html = self.client.get(url).content.decode()
            if options(html) is None:
                continue
            self.assertIn('No employees found for your company yet', html, name)
            self.assertIn('Import employees', html, name)

    def test_a_company_with_employees_sees_names_and_no_message(self):
        make_employee(self.co, 'MINE1', 'mine1@notice.test')
        for name, url in self.urls():
            html = self.client.get(url).content.decode()
            opts = options(html)
            if opts is None:
                continue
            self.assertTrue(any(o.startswith('MINE1 - ') for o in opts), f'{name}: {opts}')
            self.assertFalse(any('OTHER1' in o for o in opts), name)
            self.assertNotIn('No employees found for your company yet', html, name)