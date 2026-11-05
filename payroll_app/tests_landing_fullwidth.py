"""The home page: the full-width layout is in place, it only applies to big screens, and nothing that was on the page is gone."""
from django.test import TestCase
from django.urls import reverse


class LandingFullWidthTests(TestCase):
    def html(self):
        r = self.client.get(reverse('landing'))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_the_page_keeps_all_its_content_and_links(self):
        html = self.html()
        for text in ('Modern Payroll Management', 'Made Simple', 'Request a Demo', 'Get Started', 'Everything Your Payroll Team Needs', 'Why Teams Choose Our Platform',
                     'How It Works', 'A Complete Payroll Suite', 'Built With Security in Mind', 'Reporting Built for Decision-Making', 'Employee Self Service', 'Payroll Processing'):
            self.assertIn(text, html)
        for name in ('login', 'company_register', 'request_demo'):
            self.assertIn(reverse(name), html)
        self.assertIn('front3d-start', html)                      # the 3D motion block is untouched
        self.assertEqual(html.count('<h1>'), 1)

    def test_the_full_width_rules_are_there_and_only_for_big_screens(self):
        html = self.html()
        self.assertEqual(html.count('id="fullwidth-landing"'), 1)
        start = html.index('id="fullwidth-landing"')
        block = html[start:html.index('</style>', start)]
        self.assertEqual(block.count('{'), block.count('}'))
        self.assertTrue(block.split('{', 1)[0].strip().endswith('@media (min-width:1200px)'))   # nothing is outside the media query
        self.assertGreater(start, html.index('.showcase-row{'))    # it comes after the page's own styles, so it wins on big screens
        self.assertLess(start, html.index('front3d-start'))
        for rule in ('.container{max-width:2200px', '.hero h1{font-size:clamp(48px', '.showcase-visual{justify-self:center'):
            self.assertIn(rule, block)

    def test_the_dashboard_cards_stay_inside_the_sizes_the_3d_script_looks_for(self):
        block = self.html().split('id="fullwidth-landing"', 1)[1]
        self.assertIn('max-width:740px', block)                      # the script tilts dark cards between 260 and 760 px wide
