"""The company owner's sidebar: four headings, every section once, in the same order as Previous / Next. The employee and admin menus are untouched."""
import re

from django.test import TestCase
from django.urls import reverse

from .templatetags.np_nav import OWNER_HEADINGS, SECTIONS, flat_pages
from .tests import make_company, make_user


def sidebar(html):
    return html.split('<nav class="sb-nav"', 1)[1].split('</nav>', 1)[0]


def tokens(side):
    """The sidebar as a list, in the order things appear: a heading, the Dashboard link, or a section (by its key)."""
    out = []
    for m in re.finditer(r'<div class="sb-label">(.*?)</div>|<div class="sb-group" data-group="([^"]+)">|<a class="sb-top"', side):
        if m.group(1) is not None:
            out.append(('heading', m.group(1).replace('&amp;', '&')))
        elif m.group(2):
            out.append(('section', m.group(2)))
        else:
            out.append(('dashboard', None))
    return out


class OwnerSidebarTests(TestCase):
    def setUp(self):
        self.co = make_company('Sections Co')
        self.owner = make_user('sections_owner', 'COMPANY_OWNER', self.co)
        self.client.force_login(self.owner)
        self.page = self.client.get(reverse('dashboard')).content.decode()

    def test_four_headings_each_followed_by_its_sections_in_order(self):
        expected = []
        for heading, keys in OWNER_HEADINGS:
            expected.append(('heading', heading))
            expected += [('dashboard', None) if k == 'dashboard' else ('section', k) for k in keys]
        self.assertEqual(tokens(sidebar(self.page)), expected)
        self.assertEqual([h for h, _ in OWNER_HEADINGS], ['Workspace', 'Attendance & Leave', 'Payroll', 'Reports & Settings'])

    def test_every_section_is_in_the_sidebar_exactly_once(self):
        keys = [v for kind, v in tokens(sidebar(self.page)) if kind == 'section']
        self.assertEqual(len(keys), 19)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), {s[2] for s in SECTIONS['COMPANY_OWNER'] if s[2]})

    def test_previous_and_next_follow_the_same_order_as_the_sidebar(self):
        side_order = ['dashboard'] + [v for kind, v in tokens(sidebar(self.page)) if kind == 'section']
        self.assertEqual([s[0] for s in SECTIONS['COMPANY_OWNER']], side_order)
        seen = []
        for page in flat_pages('COMPANY_OWNER'):
            if page['key'] not in seen:
                seen.append(page['key'])
        self.assertEqual(seen, [k for k in side_order if k in seen])

    def test_a_section_added_later_goes_to_the_end_and_is_never_lost(self):
        from .templatetags.np_nav import _by_headings
        extra = SECTIONS['COMPANY_OWNER'] + [('brand-new', 'Brand New', 'brand-new', [('dashboard', 'x')])]
        self.assertEqual([s[0] for s in _by_headings(extra)][-1], 'brand-new')
        self.assertEqual(len(_by_headings(extra)), len(extra))

    def test_the_headings_are_plain_labels_with_a_style(self):
        self.assertEqual(self.page.count('class="sb-label"'), 4)
        self.assertIn('id="np-sections"', self.page)
        self.assertIn('.sb-nav .sb-label{', self.page)
        self.assertNotIn('<a class="sb-label"', self.page)


class OtherMenusAreUntouchedTests(TestCase):
    def setUp(self):
        self.co = make_company('Others Co')

    def test_employee_and_admin_menus_have_no_headings(self):
        self.client.force_login(make_user('others_emp', 'EMPLOYEE', self.co))
        emp = sidebar(self.client.get(reverse('employee_home')).content.decode())
        self.client.force_login(make_user('others_admin', 'ADMIN'))
        adm = sidebar(self.client.get(reverse('admin_dashboard')).content.decode())
        for side in (emp, adm):
            self.assertNotIn('sb-label', side)
            self.assertNotIn('Organisation &amp; Payroll Configuration', side)
        self.assertIn('Client Management', adm)
        self.assertIn('My Pay', emp)
