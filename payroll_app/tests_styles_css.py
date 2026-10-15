"""styles.css: one project-wide stylesheet, linked last in every full-page layout, and harmless until you add rules to it."""
import re

from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from .tests import make_company, make_user


class StylesCssTests(TestCase):
    def setUp(self):
        self.co = make_company('Css Co')
        self.owner = make_user('cssowner', 'COMPANY_OWNER', self.co)

    def test_the_file_exists_and_only_uses_prefixed_names_so_nothing_existing_changes(self):
        path = finders.find('css/styles.css')
        self.assertIsNotNone(path)
        css = open(path, encoding='utf-8').read()
        self.assertTrue(css.strip())
        for var in re.findall(r'(--[A-Za-z0-9-]+)\s*:', css):
            self.assertTrue(var.startswith('--np-'), var)                      # never redefine a theme variable
        for cls in re.findall(r'^\s*\.([A-Za-z0-9_-]+)', css, re.M):
            self.assertTrue(cls.startswith('np-'), cls)                        # never restyle an existing class

    def test_the_app_layout_links_it_last_after_the_theme(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse('dashboard')).content.decode()
        self.assertIn('css/styles.css', html)
        self.assertLess(html.index('namma-theme.css'), html.index('css/styles.css'))
        self.assertLess(html.index('css/styles.css'), html.index('</head>'))

    def test_the_public_layouts_link_it_too(self):
        self.client.logout()
        for url in ('/login/', '/register/', '/welcome/'):
            resp = self.client.get(url, follow=True)
            self.assertEqual(resp.status_code, 200, url)
            self.assertIn('css/styles.css', resp.content.decode(), url)

    def test_the_error_pages_are_left_alone_on_purpose(self):
        resp = self.client.get('/this-page-does-not-exist/')
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn('css/styles.css', resp.content.decode())