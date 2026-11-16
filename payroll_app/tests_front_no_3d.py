"""The front page has no 3D effects (no floating cubes, spinning coin, tilting cards or 3D entrance), and the rest of the page is unchanged."""
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

HEADLINE = 'Modern Payroll Management'
BANNED = ('front3d', 'f3d', 'preserve-3d', 'perspective', 'rotateX', 'rotateY', 'rotate3d', 'translateZ', 'translate3d', 'backface-visibility', '3d=off')


class NoFront3dTests(TestCase):
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

    def test_the_front_page_has_no_3d_code(self):
        html = self.front()
        for word in BANNED:
            self.assertNotIn(word, html, word)

    def test_no_leftover_template_tags_or_markers(self):
        html = self.front()
        for word in ('{%', '{{', 'verbatim', '<!-- front3d'):
            self.assertNotIn(word, html, word)

    def test_the_rest_of_the_front_page_is_unchanged(self):
        html = self.front()
        for needle in (HEADLINE, 'Made Simple', 'Request a Demo', 'Get Started', 'Payroll Processing', 'Active Employees'):
            self.assertIn(needle, html, needle)
        self.assertIn('href="%s"' % reverse('login'), html)
        self.assertIn('href="%s"' % reverse('company_register'), html)
        self.assertEqual(html.count('</body>'), 1)
        self.assertEqual(html.count('</html>'), 1)
        self.assertEqual(html.count('<footer'), html.count('</footer>'))

    def test_the_original_scroll_reveal_and_hover_effects_are_kept(self):
        html = self.front()
        self.assertIn('.showcase-row.in-view', html)                          # the rule that fades the showcase rows in as you scroll
        self.assertGreaterEqual(html.count('in-view'), 2)                     # ... and the script that adds it
        self.assertIn('.card:hover', html)                                    # the plain 2D lift of the cards
        self.assertIn('translateY(-2px)', html)

    def test_the_other_public_pages_have_none_either(self):
        for name in ('login', 'company_register'):
            try:
                url = reverse(name)
            except NoReverseMatch:
                continue
            html = self.client.get(url, follow=True).content.decode()
            for word in ('front3d', 'f3d', 'preserve-3d'):
                self.assertNotIn(word, html, '%s %s' % (name, word))
