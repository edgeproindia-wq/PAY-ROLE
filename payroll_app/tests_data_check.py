"""The Data check page shows the signed-in company's real numbers only."""
from django.test import TestCase
from django.urls import reverse

from .models import Employee, User
from .tests import make_company, make_employee, make_user


class DataCheckTests(TestCase):
    def setUp(self):
        self.a, self.b = make_company('Check Alpha'), make_company('Check Beta')
        self.owner = make_user('checkowner', 'COMPANY_OWNER', self.a)
        for i in range(3):
            make_employee(self.a, f'CA{i}', f'ca{i}@check.test')
        make_employee(self.b, 'CB0', 'cb0@check.test')

    def test_owner_sees_only_their_own_company(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse('data_check'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['total'], 3)
        html = resp.content.decode()
        self.assertIn('Check Alpha', html)
        self.assertNotIn('Check Beta', html)
        self.assertNotIn('CB0', html)

    def test_platform_admin_sees_every_company(self):
        admin = User.objects.create_superuser('checkadmin', 'checkadmin@check.test', 'Adm1n-Pass#2026')
        self.client.force_login(admin)
        resp = self.client.get(reverse('data_check'))
        names = {r['name']: r['emp_count'] for r in resp.context['rows']}
        self.assertEqual((names['Check Alpha'], names['Check Beta']), (3, 1))

    def test_employees_and_visitors_are_refused(self):
        emp_user = make_user('checkemp', 'EMPLOYEE', self.a)
        self.client.force_login(emp_user)
        self.assertEqual(self.client.get(reverse('data_check')).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('data_check')).status_code, 302)