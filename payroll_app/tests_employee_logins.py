"""Owner bulk-creates employee logins; employees then sign in with their Employee ID."""
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from .models import Employee
from .tests_audit import PWD, company, employee, user


class EmployeeLoginsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.c1, self.c2 = company('Acme'), company('Beta')
        self.owner = user('own', 'COMPANY_OWNER', self.c1)
        self.owner2 = user('own2', 'COMPANY_OWNER', self.c2)
        self.e1 = employee(self.c1, 'EPRO0119', 'a@ex.com')
        self.e2 = employee(self.c1, 'EPRO003', 'b@ex.com')
        self.b1 = employee(self.c2, 'TR02', 'c@ex.com')

    def bulk(self, pwd='EPRO@2026', confirm=None):
        return self.client.post(reverse('hr_employee_logins'), {'action': 'bulk', 'bulk-password1': pwd,
                                                                'bulk-password2': confirm or pwd})

    def sign_in(self, code, pwd):
        self.client.logout()
        data = {'username': code, 'password': pwd}
        resp = self.client.post(reverse('login'), dict(data, login_as='employee'))
        return self.client.session.get('_auth_user_id')

    def test_bulk_creates_only_for_own_company_and_employee_id_login_works(self):
        self.client.force_login(self.owner)
        self.bulk()
        self.assertEqual(Employee.objects.filter(company=self.c1, user__isnull=False).count(), 2)
        self.assertIsNone(Employee.objects.get(pk=self.b1.pk).user)
        uid = self.sign_in('EPRO0119', 'EPRO@2026')
        self.assertEqual(str(uid), str(Employee.objects.get(pk=self.e1.pk).user_id))
        self.assertRedirects(self.client.get(reverse('post_login_redirect')), reverse('employee_home'),
                             fetch_redirect_response=False)

    def test_reset_password_for_existing_login(self):
        self.client.force_login(self.owner)
        self.bulk()
        self.client.post(reverse('hr_employee_logins'), {'action': 'reset', 'reset-employee_code': 'epro0119',
                                                         'reset-password1': 'New-Pass-2026', 'reset-password2': 'New-Pass-2026'})
        self.assertIsNotNone(self.sign_in('EPRO0119', 'New-Pass-2026'))

    def test_mismatch_weak_and_other_company_blocked(self):
        self.client.force_login(self.owner)
        self.assertContains(self.bulk('EPRO@2026', 'EPRO@2027'), 'do not match')
        self.bulk('123')
        self.assertFalse(Employee.objects.filter(user__isnull=False).exists())
        self.client.force_login(self.owner2)
        self.client.post(reverse('hr_employee_logins'), {'action': 'reset', 'reset-employee_code': 'EPRO0119',
                                                         'reset-password1': 'New-Pass-2026', 'reset-password2': 'New-Pass-2026'})
        self.assertIsNone(Employee.objects.get(pk=self.e1.pk).user)

    def test_only_owner(self):
        e = employee(self.c1, 'X1', 'x@ex.com', login='empx')
        self.client.force_login(e.user)
        self.assertEqual(self.client.get(reverse('hr_employee_logins')).status_code, 403)