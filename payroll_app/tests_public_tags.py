"""The public pages (home, login, register) carry the installed-app tags and the shared stylesheet link, and the login page links the privacy policy and the terms."""
import re

from django.test import TestCase
from django.urls import reverse

PAGES = ('/login/', '/register/', '/welcome/')


class PublicTagsTests(TestCase):
    def html(self, url):
        r = self.client.get(url, follow=True)
        self.assertEqual(r.status_code, 200, url)
        return r.content.decode()

    def test_every_public_page_carries_the_installed_app_tags(self):
        for url in PAGES:
            h = self.html(url)
            self.assertIn('rel="manifest" href="/manifest.webmanifest"', h, url)
            self.assertIn("serviceWorker.register('/sw.js'", h, url)
            self.assertIn('theme-color', h, url)

    def test_every_public_page_links_the_shared_stylesheet_once_and_after_its_own_styles(self):
        for url in PAGES:
            h = self.html(url)
            self.assertEqual(len(re.findall(r'href="[^"]*css/styles\.css', h)), 1, url)
            link = h.index('css/styles.css')
            self.assertLess(link, h.index('</head>'), url)                          # inside the head
            own_styles = [m.start() for m in re.finditer(r'<style', h[:h.index('</head>')])]
            self.assertTrue(all(link > s for s in own_styles), url)                  # linked after the page's own <style> blocks, as styles.css asks

    def test_the_tags_are_not_added_twice(self):
        for url in PAGES:
            h = self.html(url)
            self.assertEqual(h.count('rel="manifest"'), 1, url)
            self.assertEqual(h.count("serviceWorker.register('/sw.js'"), 1, url)

    def test_the_login_page_links_the_privacy_policy_and_the_terms(self):
        h = self.html('/login/')
        self.assertIn('href="%s"' % reverse('public_privacy'), h)
        self.assertIn('href="%s"' % reverse('public_terms'), h)
        self.assertEqual(self.client.get(reverse('public_privacy')).status_code, 200)

    def test_the_login_form_is_untouched(self):
        h = self.html('/login/')
        for needle in ('id="loginForm"', 'name="password"', 'Sign In', 'Forgot password?', 'Register here', 'login-light'):
            self.assertIn(needle, h, needle)

    def test_the_error_pages_are_left_alone_on_purpose(self):
        resp = self.client.get('/this-page-does-not-exist/')
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn('css/styles.css', resp.content.decode())
