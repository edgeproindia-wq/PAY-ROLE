"""Payroll Reports & Analytics in the payroll structure: every feature is a real page the owner can open."""
from django.test import TestCase
from django.urls import reverse

from .payroll_structure import SECTIONS, get_section, overview_name
from .tests import make_company, make_user


class ReportsMenuTests(TestCase):
    def setUp(self):
        self.co = make_company('Reports Menu Co')
        self.owner = make_user('reports_menu_owner', 'COMPANY_OWNER', self.co)

    def test_every_report_feature_has_a_real_page(self):
        section = get_section('reports-analytics')
        self.assertEqual(len(section['children']), 22)
        for child in section['children']:
            with self.subTest(feature=child['label']):
                self.assertTrue(child['url_name'], f"{child['label']} is still marked planned")
                reverse(child['url_name'])

    def test_the_overview_says_all_are_available(self):
        self.client.force_login(self.owner)
        r = self.client.get(reverse(overview_name(get_section('reports-analytics'))))
        self.assertContains(r, '22 of 22 features are available')

    def test_every_report_page_opens_for_the_owner(self):
        self.client.force_login(self.owner)
        for child in get_section('reports-analytics')['children']:
            with self.subTest(feature=child['label']):
                self.assertEqual(self.client.get(reverse(child['url_name'])).status_code, 200)

    def test_the_other_sections_are_unchanged(self):
        self.assertEqual([s['n'] for s in SECTIONS], list(range(1, 21)))
        self.assertEqual(sum(1 for c in get_section('approvals')['children'] if c['url_name']), 0)
