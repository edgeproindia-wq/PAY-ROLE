"""Add Salary Structure: the script that fills HRA, Conveyance and Special Allowance from Basic is served, loaded on the app pages only, harmless, and calculates correctly."""
import json
import re
import shutil
import subprocess

from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from .tests import make_company, make_user

JS = 'js/np_salary_autofill.js'


class ScriptTests(TestCase):
    def setUp(self):
        self.owner = make_user('sal_owner', 'COMPANY_OWNER', make_company('Sal Co'))
        self.client.force_login(self.owner)

    def source(self):
        path = finders.find(JS)
        self.assertIsNotNone(path, 'the script file was not found by the static finders')
        return open(path, encoding='utf-8').read()

    def test_the_script_file_is_found_by_the_static_finders(self):
        self.assertIn('Add Salary Structure', self.source())

    def test_every_app_page_loads_the_script_exactly_once(self):
        html = self.client.get(reverse('dashboard')).content.decode()
        self.assertEqual(html.count('np_salary_autofill.js'), 1)

    def test_public_pages_do_not_load_it(self):
        self.client.logout()
        for url in ('/login/', '/welcome/'):
            self.assertNotIn('np_salary_autofill.js', self.client.get(url, follow=True).content.decode(), url)

    def test_the_salary_structure_page_still_opens(self):
        self.assertEqual(self.client.get(reverse('salary_structure')).status_code, 200)

    def test_the_script_sends_nothing_anywhere_and_runs_no_text_as_code(self):
        js = self.source()
        for banned in ('fetch(', 'XMLHttpRequest', 'sendBeacon', 'eval(', 'new Function', 'document.write', 'WebSocket'):
            self.assertNotIn(banned, js, banned)
        self.assertEqual(js.count('innerHTML'), 1)                                     # only the fixed rules box, built from numbers

    def test_the_rules_start_with_hra_50_conveyance_10_and_special_40_percent_of_basic(self):
        self.assertIn('DEFAULT_RULES = { hra: 50, conv: 10, special: 40 }', self.source())
        self.assertIn('np_salary_autofill_rules_v2', self.source())                    # older remembered percentages (all empty) are not used

    def test_it_only_touches_forms_that_have_no_effective_date(self):
        js = self.source()
        self.assertIn("!q('effective_from')", js)                                      # the Salary history form is left alone

    def test_the_calculation_is_right_when_node_is_available(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed on this computer; the calculation was checked in a browser simulation')
        path = finders.find(JS)
        code = ("const l=require(%s);console.log(JSON.stringify([l.compute(20000,{hra:50,conv:10,special:20}),l.compute(12501,{hra:50,conv:0,special:0}),l.compute('',{hra:50,conv:0,special:0})]));" % json.dumps(path))
        out = subprocess.run([node, '-e', code], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [{'hra': 10000, 'conv': 2000, 'special': 4000}, {'hra': 6251, 'conv': 0, 'special': 0}, None])
