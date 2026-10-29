"""The 20-section payroll structure: its data, the overview page of each section, and who can see the numbered menu."""
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from .payroll_structure import SECTIONS, overview_name, owner_sections
from .tests import make_company, make_user

LATER_PACKS = {'shift_assign', 'hr_day_hours'}              # pages from later packs: listed in the structure, skipped until their routes exist


class StructureDataTests(TestCase):
    def test_there_are_twenty_numbered_sections_in_order_with_unique_keys(self):
        self.assertEqual([s['n'] for s in SECTIONS], list(range(1, 21)))
        self.assertEqual(len({s['key'] for s in SECTIONS}), 20)
        self.assertEqual(SECTIONS[6]['title'], 'Monthly Payroll Processing')

    def test_every_url_name_in_the_structure_is_a_real_address(self):
        for s in SECTIONS:
            for c in s['children']:
                if c['url_name'] and c['url_name'] not in LATER_PACKS:
                    with self.subTest(section=s['key'], page=c['url_name']):
                        reverse(c['url_name'])                       # a typo in the structure shows up here

    def test_every_real_page_belongs_to_one_section_only(self):
        seen = []
        for _key, _label, _group, pages in owner_sections():
            seen += [name for name, _ in pages]
        self.assertEqual(len(seen), len(set(seen)))

    def test_a_section_with_no_real_page_opens_its_overview(self):
        for s in SECTIONS:
            pages = dict(owner_sections_by_key()[s['key']])
            if not any(c['url_name'] for c in s['children']) and s['children']:
                self.assertEqual(list(pages), [overview_name(s)], s['key'])
                reverse(overview_name(s))


def owner_sections_by_key():
    return {key: pages for key, _label, _group, pages in owner_sections()}


class OverviewPageTests(TestCase):
    def setUp(self):
        self.co = make_company('Struct Co')
        self.other = make_company('Other Struct Co')
        self.owner = make_user('struct_owner', 'COMPANY_OWNER', self.co)

    def url(self, key):
        return reverse(overview_name(next(s for s in SECTIONS if s['key'] == key)))

    def test_owner_sees_available_and_planned_features(self):
        self.client.force_login(self.owner)
        r = self.client.get(self.url('bank-payments'))
        self.assertContains(r, 'Bank Payment Management')
        self.assertContains(r, 'Available')
        self.assertContains(r, 'Planned')
        self.assertContains(r, reverse('bank_transfer'))

    def test_every_section_with_features_has_a_working_overview_for_the_owner(self):
        self.client.force_login(self.owner)
        for s in SECTIONS:
            if s['children']:
                with self.subTest(section=s['key']):
                    self.assertEqual(self.client.get(self.url(s['key'])).status_code, 200)

    def test_an_empty_section_overview_lists_every_planned_feature(self):
        self.client.force_login(self.owner)
        r = self.client.get(self.url('accounting-integration'))
        for label in ('Salary Expense Journal', 'General Ledger Mapping', 'Accounting Export'):
            self.assertContains(r, label)
        self.assertNotContains(r, 'Open first page')                # nothing is built yet: no button to open

    def test_employee_admin_and_anonymous_are_refused(self):
        url = self.url('bank-payments')
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(make_user('struct_emp', 'EMPLOYEE', self.co))
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(make_user('struct_admin', 'ADMIN'))
        self.assertEqual(self.client.get(url).status_code, 403)    # the platform admin has no payroll menu

    def test_unknown_section_gives_404(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get('/module/no-such-section/').status_code, 404)


class MenuVisibilityTests(TestCase):
    def setUp(self):
        self.co = make_company('Menu Co')

    def sidebar(self, user, name):
        self.client.force_login(user)
        return self.client.get(reverse(name)).content.decode().split('<div class="content">')[0]

    def test_owner_sees_the_numbered_structure(self):
        side = self.sidebar(make_user('menu_owner', 'COMPANY_OWNER', self.co), 'dashboard')
        for s in SECTIONS:
            self.assertIn('<span class="tx">%s</span>' % s['title'].replace('&', '&amp;'), side)

    def test_employee_and_admin_keep_their_own_menus(self):
        emp = self.sidebar(make_user('menu_emp', 'EMPLOYEE', self.co), 'employee_home')
        self.assertIn('My Pay', emp)
        self.assertNotIn('Organisation &amp; Payroll Configuration', emp)
        adm = self.sidebar(make_user('menu_admin', 'ADMIN'), 'admin_dashboard')
        self.assertIn('Client Management', adm)
        self.assertNotIn('Organisation &amp; Payroll Configuration', adm)
