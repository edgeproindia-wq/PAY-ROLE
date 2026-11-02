"""HRMS & External System Integration in the payroll structure: nine of the ten features are real pages."""
from django.test import TestCase
from django.urls import reverse

from .payroll_structure import SECTIONS, get_section, overview_name
from .tests import make_company, make_user


class HrmsMenuTests(TestCase):
    def setUp(self):
        self.co = make_company('Hrms Menu Co')
        self.owner = make_user('hrms_menu_owner', 'COMPANY_OWNER', self.co)

    def test_nine_features_have_a_real_page_and_the_salary_revision_import_is_still_planned(self):
        section = get_section('hrms-integration')
        self.assertEqual(len(section['children']), 10)
        planned = [c['label'] for c in section['children'] if not c['url_name']]
        self.assertEqual(planned, ['Approved Salary Revision Import'])
        for child in section['children']:
            if child['url_name']:
                with self.subTest(feature=child['label']):
                    reverse(child['url_name'])

    def test_the_overview_says_nine_of_ten_are_available(self):
        self.client.force_login(self.owner)
        r = self.client.get(reverse(overview_name(get_section('hrms-integration'))))
        self.assertContains(r, '9 of 10 features are available')
        self.assertContains(r, 'Approved Salary Revision Import')

    def test_every_built_page_opens_for_the_owner(self):
        self.client.force_login(self.owner)
        for child in get_section('hrms-integration')['children']:
            if child['url_name']:
                with self.subTest(feature=child['label']):
                    self.assertEqual(self.client.get(reverse(child['url_name'])).status_code, 200)

    def test_the_other_sections_are_unchanged(self):
        self.assertEqual([s['n'] for s in SECTIONS], list(range(1, 21)))
        self.assertEqual(sum(1 for c in get_section('approvals')['children'] if c['url_name']), 0)
