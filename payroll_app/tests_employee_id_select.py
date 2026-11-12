"""Other allowances & deductions: the Employee box lists IDs only and shows the chosen name by itself."""
import re

from django import forms
from django.test import TestCase
from django.urls import reverse

from .employee_id_select import EmployeeIdSelect, use_employee_id_select
from .models import Employee, PayComponent
from .tests import make_company, make_user


def person(company, code, first, last='Person'):
    return Employee.objects.create(employee_code=code, first_name=first, last_name=last, email='%s@eid.test' % code.lower(), date_of_joining='2020-01-01',
                                   department='Eng', designation='Dev', company=company)


class Base(TestCase):
    def setUp(self):
        self.co, self.other = make_company('Eid Co'), make_company('Eid Other')
        self.owner = make_user('eid_owner', 'COMPANY_OWNER', self.co)
        self.a = person(self.co, 'EPRO0010', 'Pradeeparaddi', 'K')
        self.b = person(self.co, 'EPRO0011', 'Bala', 'Kumar')
        self.stranger = person(self.other, 'ZZ0001', 'Stranger', 'Person')
        self.client.force_login(self.owner)
        self.url = reverse('hr_pay_components')

    def options(self, html):
        return re.findall(r'<option value="(\d+)"([^>]*)>([^<]*)</option>', html)


class PageTests(Base):
    def test_the_dropdown_lists_ids_only_and_each_option_carries_the_name(self):
        html = self.client.get(self.url).content.decode()
        opts = {pk: (attrs, label) for pk, attrs, label in self.options(html)}
        self.assertEqual({label for _a, label in opts.values()}, {'EPRO0010', 'EPRO0011'})            # the ID and nothing else
        self.assertNotIn(' - ', ''.join(label for _a, label in opts.values()))
        self.assertIn('data-name="Pradeeparaddi K"', opts[str(self.a.pk)][0])
        self.assertIn('data-name="Bala Kumar"', opts[str(self.b.pk)][0])

    def test_the_name_line_and_its_script_are_on_the_page(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('id="id_employee_name"', html)
        self.assertIn('class="np-empname"', html)
        self.assertIn('getAttribute("data-name")', html)
        self.assertIn('textContent', html)
        self.assertNotIn('innerHTML', html.split('np-empname')[1][:600])                              # a name is never written as HTML

    def test_the_value_that_is_saved_is_still_the_employee(self):
        r = self.client.post(self.url, {'employee': self.b.pk, 'name': 'Shift allowance', 'kind': 'EARNING', 'calc_type': 'FIXED', 'percent': '0', 'amount': '500', 'taxable': 'on', 'effective_from': '2026-09-01'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(PayComponent.objects.get(name='Shift allowance').employee, self.b)

    def test_another_companys_employees_never_appear_in_the_list_or_as_names(self):
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('ZZ0001', html)
        self.assertNotIn('Stranger', html)

    def test_an_employee_login_still_cannot_open_the_page(self):
        emp_user = make_user('eid_emp', 'EMPLOYEE', self.co)
        Employee.objects.filter(pk=self.a.pk).update(user=emp_user)
        self.client.force_login(emp_user)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_a_name_with_html_or_quotes_is_escaped(self):
        person(self.co, 'EPRO0012', '<b>"Bold"</b>', "O'Neil")
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('<b>"Bold"</b>', html)
        self.assertIn('data-name="&lt;b&gt;&quot;Bold&quot;&lt;/b&gt; O&#x27;Neil"', html)

    def test_after_a_failed_save_the_chosen_employee_stays_selected(self):
        r = self.client.post(self.url, {'employee': self.b.pk, 'name': '', 'kind': 'EARNING', 'calc_type': 'FIXED', 'percent': '0', 'amount': '5', 'effective_from': '2026-09-01'})
        self.assertEqual(r.status_code, 200)
        self.assertRegex(r.content.decode(), r'<option value="%d"[^>]*selected[^>]*>EPRO0011</option>' % self.b.pk)


class WidgetTests(TestCase):
    def test_the_widget_handles_the_ids_it_is_given_and_escapes_the_field_id(self):
        class F(forms.Form):
            employee = forms.ChoiceField(choices=[('1', 'EPRO1'), ('2', 'EPRO2')], widget=EmployeeIdSelect(names={'1': 'One', '2': 'Two'}))
        html = F().as_p()
        self.assertIn('data-name="One"', html)
        self.assertIn('"id_employee_name"', html)

    def test_a_field_that_does_not_exist_is_ignored(self):
        class F(forms.Form):
            other = forms.CharField()
        self.assertIsNotNone(use_employee_id_select(F(), 'employee'))
