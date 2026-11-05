"""Front page 3D motion: it is on the front page exactly once, it is on no other page, and the page itself is unchanged."""
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

START = '<!-- front3d-start -->'
END = '<!-- front3d-end -->'
HEADLINE = 'Modern Payroll Management'


class Front3dTests(TestCase):
    def front(self):
        """The front page as a visitor gets it: the address '/' (following any redirect), then the usual page names."""
        tried = []
        candidates = ['/']
        for name in ('landing', 'home', 'public_home', 'index', 'landing_page', 'home_page'):
            try:
                candidates.append(reverse(name))
            except NoReverseMatch:
                pass
        for url in candidates:
            response = self.client.get(url, follow=True)
            tried.append('%s -> %s' % (url, response.status_code))
            if response.status_code == 200 and HEADLINE in response.content.decode():
                return response.content.decode()
        self.fail('Could not reach the front page (it should show %r). Tried: %s' % (HEADLINE, '; '.join(tried)))

    def test_the_front_page_has_the_3d_layer_exactly_once(self):
        html = self.front()
        self.assertEqual(html.count(START), 1)
        self.assertEqual(html.count(END), 1)
        self.assertLess(html.index(START), html.index(END))

    def test_the_script_and_styles_arrive_intact_and_are_not_run_through_the_template_engine(self):
        html = self.front()
        block = html[html.index(START):html.index(END)]
        self.assertNotIn('{% verbatim', block)
        self.assertIn('.f3d-tilt', block)
        self.assertIn('(function () {', block)
        self.assertIn("'use strict';", block)
        self.assertIn('prefers-reduced-motion', block)
        self.assertEqual(block.count('<script>'), 1)
        self.assertEqual(block.count('</script>'), 1)

    def test_nothing_of_the_front_page_was_taken_away(self):
        html = self.front()
        self.assertIn(HEADLINE, html)
        self.assertEqual(html.count('</body>'), 1)
        self.assertGreater(html.index('</body>'), html.index(END))              # the layer sits just before the page ends

    def test_the_other_pages_do_not_get_it(self):
        for name in ('login', 'company_register'):
            try:
                url = reverse(name)
            except NoReverseMatch:
                continue
            response = self.client.get(url, follow=True)
            self.assertEqual(response.status_code, 200, name)
            self.assertNotIn(START, response.content.decode(), name)
