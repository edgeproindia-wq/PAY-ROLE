"""Import popup layout: after a check the chooser hides, the Import button sits under the results, and ticking the gap box re-checks."""
from django.test import TestCase
from django.urls import reverse

from .tests import make_company, make_user


class ImportPopupLayoutTests(TestCase):
    def setUp(self):
        self.co = make_company('Layout Co')
        self.owner = make_user('layoutowner', 'COMPANY_OWNER', self.co)
        self.client.force_login(self.owner)

    def popup(self):
        html = self.client.get(reverse('employee_master')).content.decode()
        start = html.index('id="empImportModal"')
        return html[start:html.index('</script>', start)]

    def test_the_new_layout_pieces_are_present(self):
        p = self.popup()
        for piece in ('id="empStepA"', 'id="empActionBox"', 'id="empFileLine"', 'Change file or options'):
            self.assertIn(piece, p, piece)
        self.assertIn("'Import ' + d.ready + ' employees'", p)                 # the big button says how many will be imported
        self.assertIn("el('empStepA').style.display = 'none'", p)              # the chooser hides after a successful check

    def test_the_import_button_is_in_the_body_not_hidden_in_the_footer(self):
        p = self.popup()
        self.assertLess(p.index('id="empConfirmBtn"'), p.index('class="modal-foot"'))
        foot = p[p.index('class="modal-foot"'):]
        self.assertNotIn('empConfirmBtn', foot.split('</div>')[0])

    def test_ticking_the_gap_box_checks_the_file_again(self):
        p = self.popup()
        self.assertIn("el('empFill').addEventListener('change'", p)
        self.assertIn("window.checkEmpFile()", p)