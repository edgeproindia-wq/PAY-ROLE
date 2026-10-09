"""The page content must sit inside the main area. One stray </div> in base.html pushes it under the sidebar."""
from html.parser import HTMLParser

from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from .tests import make_company, make_user

VOID = {'meta', 'link', 'img', 'input', 'br', 'hr', 'source', 'area', 'base', 'col', 'embed', 'param', 'track', 'wbr'}


class Shell(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.content_in_main = [], None

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        cls = (dict(attrs).get('class') or '').split()
        if tag == 'div' and 'content' in cls and self.content_in_main is None:
            self.content_in_main = any(c == 'main' for _, k in self.stack for c in k)
        self.stack.append((tag, cls))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


class LayoutTests(TestCase):
    def test_content_is_inside_the_main_area_on_the_main_pages(self):
        owner = make_user('layoutowner', 'COMPANY_OWNER', make_company('Layout Co'))
        self.client.force_login(owner)
        checked = 0
        for name in ('dashboard', 'attendance', 'payroll_processing', 'pay_cycles', 'my_profile', 'employee_master', 'settings'):
            try:
                url = reverse(name)
            except NoReverseMatch:
                continue
            resp = self.client.get(url)
            if resp.status_code != 200:
                continue
            shell = Shell()
            shell.feed(resp.content.decode())
            self.assertTrue(shell.content_in_main, f'{name}: the page content is outside the main area (stray </div> in base.html)')
            checked += 1
        self.assertGreaterEqual(checked, 3)