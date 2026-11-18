from django.test import TestCase, override_settings
from django.urls import reverse

BANNED = ('preserve-3d', 'perspective', 'rotateX', 'rotateY', 'translateZ', 'translate3d')


class LandingV2Tests(TestCase):
    def body(self):
        r = self.client.get(reverse('landing'))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_the_new_page_has_its_sections_and_buttons(self):
        b = self.body()
        for text in ('Modern Payroll Management', 'Made Simple', 'Request a Demo', 'Get Started', 'Everything your payroll team needs', 'How it works', 'Built with security in mind', 'Ready to simplify your payroll?'):
            self.assertIn(text, b, text)
        for url in (reverse('login'), reverse('company_register'), reverse('request_demo')):
            self.assertIn('href="%s"' % url, b, url)

    def test_the_menu_links_point_to_sections_that_exist(self):
        b = self.body()
        for anchor in ('features', 'how', 'modules', 'security', 'faq'):
            self.assertIn('href="#%s"' % anchor, b)
            self.assertIn('id="%s"' % anchor, b)

    def test_sample_numbers_are_labelled_and_there_are_no_invented_customers(self):
        b = self.body()
        self.assertIn('Sample data for illustration', b)
        for word in ('Trusted by', 'testimonial', 'customers love'):
            self.assertNotIn(word, b)

    def test_no_3d_and_no_leftover_template_tags(self):
        b = self.body()
        for word in BANNED + ('{%', '{{'):
            self.assertNotIn(word, b, word)

    @override_settings(PUBLIC_CONTACT_EMAIL='hello@company.example', PUBLIC_CONTACT_PHONE='+91 80000 00000')
    def test_contact_details_come_from_settings(self):
        b = self.body()
        self.assertIn('hello@company.example', b)
        self.assertIn('+91 80000 00000', b)
