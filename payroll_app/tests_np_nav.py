"""Sidebar with main items only, and one Previous / Next footer driven by one ordered list of every page."""
import html
import re
from types import SimpleNamespace
from unittest import mock

from django.template.loader import get_template
from django.test import RequestFactory, TestCase
from django.urls import reverse

from .templatetags.np_nav import SECTIONS, flat_pages, locate, np_nav, np_nav_css
from .tests import make_company, make_user

OWNER = 'COMPANY_OWNER'
ADDED_LATER = {'shift_assign', 'hr_day_hours'}          # pages from later packs: in the order when they exist, skipped when they do not
MAIN_ITEMS = ['Dashboard', 'Employee Management', 'Attendance', 'Leave Management', 'Salary & Payroll', 'Tax & Compliance',
              'Reports & Analytics', 'Communication', 'Settings']


def base_source():
    return open(get_template('base.html').origin.name, encoding='utf-8-sig').read().replace('\r\n', '\n')


def sidebar_groups():
    nav = base_source()
    nav = nav[nav.find('<nav class="sb-nav"'):]
    out = {}
    for m in re.finditer(r'<div class="sb-group" data-group="([^"]+)">(.*?)</div>\s*</div>', nav, re.S):
        out[m.group(1)] = re.findall(r"\{% url '([^']+)' as \w+ %\}\{% if \w+ %\}<a href=", m.group(2))
    return out


def names(role, section_key):
    return [p['name'] for p in flat_pages(role) if p['key'] == section_key]


class OrderTests(TestCase):
    def test_the_main_items_are_exactly_the_nine_asked_for_in_that_order(self):
        self.assertEqual([s[1] for s in SECTIONS[OWNER]], MAIN_ITEMS)
        self.assertIn('Tax &amp; Compliance', base_source())

    def test_every_group_of_the_old_sidebar_keeps_its_pages_in_the_same_order(self):
        groups = sidebar_groups()
        for key in ('people', 'attendance', 'compliance', 'engage', 'settings'):
            self.assertEqual([n for n in names(OWNER, key) if n not in ADDED_LATER], groups[key], key)
        self.assertEqual(names(OWNER, 'payroll'), groups['payroll'] + ['reimbursement'] + groups['loans'])   # pages that lost their own menu item move here

    def test_no_page_of_the_old_sidebar_can_become_unreachable(self):
        everything = set(names(OWNER, 'dashboard')) | {p['name'] for p in flat_pages(OWNER)}
        for key, pages in sidebar_groups().items():
            if key in ('mypay', 'selfservice', 'platform'):
                continue                                                          # those belong to the employee and admin menus
            for name in pages:
                self.assertIn(name, everything, f'{name} is in the sidebar group "{key}" but not in the page order')
        for name in ('dashboard', 'leave_management', 'reimbursement', 'reports_analytics'):
            self.assertIn(name, everything, name)

    def test_a_main_item_opens_the_first_page_of_its_section(self):
        groups = sidebar_groups()
        for key in ('people', 'attendance', 'payroll', 'compliance', 'engage', 'settings'):
            self.assertEqual(names(OWNER, key)[0], groups[key][0], key)           # the sidebar button goes to its first sub-link: that is the first page

    def test_pages_are_listed_once_and_all_are_real_addresses(self):
        for role in SECTIONS:
            listed = [p['name'] for p in flat_pages(role)]
            self.assertEqual(len(listed), len(set(listed)), role)
            self.assertTrue(listed, role)
            for p in flat_pages(role):
                self.assertEqual(p['url'], reverse(p['name']))

    def test_a_page_this_project_does_not_have_is_skipped_not_an_error(self):
        extra = SECTIONS[OWNER] + [('x', 'X', None, [('no_such_page', 'Nope')])]
        with mock.patch.dict(SECTIONS, {OWNER: extra}):
            self.assertNotIn('no_such_page', [p['name'] for p in flat_pages(OWNER)])

    def test_each_role_has_its_own_order_and_never_an_owners_page(self):
        emp = {p['name'] for p in flat_pages('EMPLOYEE')}
        self.assertTrue({'payslips', 'attendance', 'ess'} <= emp)
        self.assertFalse(emp & {'employee_master', 'salary_structure', 'payroll_processing', 'hr_shifts'})
        self.assertTrue({'admin_company_list', 'admin_audit_log'} <= {p['name'] for p in flat_pages('ADMIN')})
        self.assertFalse({p['name'] for p in flat_pages('ADMIN')} & {'employee_master', 'payroll_processing'})


class LocateTests(TestCase):
    def fake(self, path, name=None):
        return SimpleNamespace(path=path, resolver_match=SimpleNamespace(url_name=name) if name else None)

    def test_an_exact_page_and_a_page_under_a_listed_page(self):
        pages = flat_pages(OWNER)
        at = lambda req: pages[locate(pages, req)]['name']
        self.assertEqual(at(self.fake(reverse('employee_master'), 'employee_master')), 'employee_master')
        self.assertEqual(at(self.fake(reverse('employee_master') + '123/edit/', 'something_else')), 'employee_master')
        self.assertIsNone(locate(pages, self.fake('/nowhere/at/all/', 'nothing')))
        self.assertIsNone(locate(pages, self.fake('/', 'unknown')))                 # the root address is never "under" a page


class FooterTests(TestCase):
    def setUp(self):
        self.co = make_company('Pager Co')
        self.owner = make_user('pagerowner', 'COMPANY_OWNER', self.co)
        self.client.force_login(self.owner)
        self.pages = flat_pages(OWNER)
        self.total = len(self.pages)

    def get(self, name):
        return self.client.get(reverse(name)).content.decode()

    def test_the_footer_shows_the_section_and_page_x_of_n(self):
        i = [p['name'] for p in self.pages].index('attendance_bulk')
        h = self.get('attendance_bulk')
        self.assertIn('<b>Attendance</b> &middot; Page %d of %d' % (i + 1, self.total), h)
        self.assertLess(h.index('class="np-meta"'), h.index('data-np-go'))              # the text is above the buttons
        self.assertIn('Previous', h)
        self.assertIn('Next &raquo;', h)

    def test_the_first_page_has_no_previous_and_next_goes_to_the_next_page(self):
        h = self.get('dashboard')
        self.assertIn('Page 1 of %d' % self.total, h)
        self.assertIn('np-off', h)
        self.assertNotIn('<a class="btn btn-outline" href', h.split('class="np-pager"')[1].split('</footer>')[0])     # Previous is not a link here
        self.assertIn(f'href="{reverse("employee_master")}" data-np-go>Next', h)

    def test_next_continues_into_the_next_section_and_previous_goes_back_across_it(self):
        last_att = names(OWNER, 'attendance')[-1]
        self.assertIn(f'href="{reverse("leave_management")}" data-np-go>Next', self.get(last_att))
        self.assertIn(f'href="{reverse(last_att)}" data-np-go>&laquo; Previous', self.get('leave_management'))
        first_pay = names(OWNER, 'payroll')[0]
        self.assertIn(f'href="{reverse("leave_management")}" data-np-go>&laquo; Previous', self.get(first_pay))

    def test_the_last_page_says_finish_and_goes_back_to_the_start(self):
        last = self.pages[-1]['name']
        h = self.get(last)
        self.assertIn('Page %d of %d' % (self.total, self.total), h)
        self.assertIn(f'href="{reverse("dashboard")}" data-np-go>Finish', h)
        self.assertNotIn('Next &raquo;', h)

    def test_walking_next_visits_every_page_once_in_order(self):
        order = [p['name'] for p in self.pages]
        seen, name = [], order[0]
        while True:
            seen.append(name)
            h = self.get(name)
            m = re.search(r'href="([^"]+)" data-np-go>(Next|Finish)', h)
            if m.group(2) == 'Finish':
                break
            name = next(p['name'] for p in self.pages if p['url'] == m.group(1))
        self.assertEqual(seen, order)

    def test_the_footer_belongs_to_logged_in_pages_only(self):
        self.client.logout()
        for url in ('/login/', '/welcome/'):
            h = self.client.get(url, follow=True).content.decode()
            self.assertNotIn('np-pager', h, url)
            self.assertNotIn('data-np', h, url)

    def test_the_sidebar_script_and_style_are_on_every_logged_in_page_even_without_a_footer(self):
        request = RequestFactory().get('/somewhere-not-in-the-menu/')
        request.user, request.resolver_match = self.owner, None
        self.assertEqual(np_nav(dict(request=request)), {'on': True, 'show': False})
        self.assertTrue(np_nav_css(dict(request=request))['on'])
        anon = RequestFactory().get('/x/')
        anon.user = SimpleNamespace(is_authenticated=False)
        self.assertEqual(np_nav(dict(request=anon)), {'on': False})

    def test_the_sidebar_style_hides_the_sub_menus_and_arrows_and_the_pages_that_lost_their_item(self):
        h = self.get('hr_shifts')
        self.assertIn('id="np-nav-css"', h)
        self.assertIn('.sb-group > .sb-sub{display:none !important}', h)
        self.assertIn('.sb-caret{display:none !important}', h)
        self.assertIn('data-group="loans"]{display:none', h)
        self.assertIn(f'a.sb-top[href="{reverse("reimbursement")}"]{{display:none', h)

    def test_existing_page_content_is_still_there(self):
        h = self.get('employee_master')
        self.assertIn('Import Excel', h)
        self.assertIn('id="empImportModal"', h)
        self.assertEqual(h.count('<footer class="np-pager"'), 1)

    def test_the_employee_and_the_admin_get_their_own_footer(self):
        emp_user = make_user('pageremp', 'EMPLOYEE', self.co)
        request = RequestFactory().get(reverse('payslips'))
        request.user, request.resolver_match = emp_user, SimpleNamespace(url_name='payslips')
        ctx = np_nav(dict(request=request))
        self.assertEqual((ctx['section'], ctx['total']), ('My Pay', len(flat_pages('EMPLOYEE'))))
        self.assertEqual(ctx['next']['name'], 'investment_declaration')
        self.client.force_login(make_user('pageradmin', 'ADMIN'))
        h = self.client.get(reverse('admin_company_approvals')).content.decode()
        self.assertIn('<b>Platform</b> &middot; Page', h)
