"""Bank Payment Management in the payroll structure: all ten features are real pages the owner can open."""
from django.test import TestCase
from django.urls import reverse

from .payroll_structure import SECTIONS, get_section, overview_name
from .tests import make_company, make_user


class BankMenuTests(TestCase):
    def setUp(self):
        self.co = make_company('Bank Menu Co')
        self.owner = make_user('bank_menu_owner', 'COMPANY_OWNER', self.co)

    def test_all_ten_bank_features_have_a_real_page(self):
        section = get_section('bank-payments')
        self.assertEqual(len(section['children']), 10)
        for child in section['children']:
            with self.subTest(feature=child['label']):
                self.assertTrue(child['url_name'], f"{child['label']} is still marked planned")
                reverse(child['url_name'])

    def test_the_overview_says_ten_of_ten_are_available(self):
        self.client.force_login(self.owner)
        r = self.client.get(reverse(overview_name(get_section('bank-payments'))))
        self.assertContains(r, '10 of 10 features are available')

    def test_every_bank_page_opens_for_the_owner(self):
        self.client.force_login(self.owner)
        for child in get_section('bank-payments')['children']:
            with self.subTest(feature=child['label']):
                self.assertEqual(self.client.get(reverse(child['url_name'])).status_code, 200)

    def test_the_other_sections_are_unchanged(self):
        self.assertEqual([s['n'] for s in SECTIONS], list(range(1, 21)))
        self.assertEqual(sum(1 for c in get_section('accounting-integration')['children'] if c['url_name']), 0)
