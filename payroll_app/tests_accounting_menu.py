"""Accounting Integration in the payroll structure: all nine features are real pages the owner can open."""
from django.test import TestCase
from django.urls import reverse

from .payroll_structure import SECTIONS, get_section, overview_name
from .tests import make_company, make_user


class AccountingMenuTests(TestCase):
    def setUp(self):
        self.co = make_company('Acc Menu Co')
        self.owner = make_user('acc_menu_owner', 'COMPANY_OWNER', self.co)

    def test_all_nine_accounting_features_have_a_real_page(self):
        section = get_section('accounting-integration')
        self.assertEqual(len(section['children']), 9)
        for child in section['children']:
            with self.subTest(feature=child['label']):
                self.assertTrue(child['url_name'], f"{child['label']} is still marked planned")
                reverse(child['url_name'])

    def test_the_overview_says_nine_of_nine_are_available(self):
        self.client.force_login(self.owner)
        r = self.client.get(reverse(overview_name(get_section('accounting-integration'))))
        self.assertContains(r, '9 of 9 features are available')

    def test_every_accounting_page_opens_for_the_owner(self):
        self.client.force_login(self.owner)
        for child in get_section('accounting-integration')['children']:
            with self.subTest(feature=child['label']):
                self.assertEqual(self.client.get(reverse(child['url_name'])).status_code, 200)

    def test_the_other_sections_are_unchanged(self):
        self.assertEqual([s['n'] for s in SECTIONS], list(range(1, 21)))
        self.assertEqual(sum(1 for c in get_section('approvals')['children'] if c['url_name']), 0)
