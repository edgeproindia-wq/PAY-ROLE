"""Company Monitoring: admin-only, usage and health only, correct numbers, and never any payroll data or employee names."""
import datetime

from django.test import TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from .company_monitor import INACTIVE_DAYS, health_of, monitored_companies
from .models import AuditLog, Company, Employee, PayrollRun, User
from .tests import make_company, make_employee, make_user

NOW = timezone.now


def ago(days=0, hours=0):
    return NOW() - datetime.timedelta(days=days, hours=hours)


def logged(user, when):
    User.objects.filter(pk=user.pk).update(last_login=when)


def event(company, action='UPDATE', when=None, actor=None, details=''):
    e = AuditLog.objects.create(company=company, action=action, actor=actor, details=details, model_name='Employee')
    if when is not None:
        AuditLog.objects.filter(pk=e.pk).update(timestamp=when)
    return e


class MonitorBase(TestCase):
    def setUp(self):
        self.admin = make_user('mon_admin', 'ADMIN')
        self.active = make_company('Active Co')
        self.quiet = make_company('Quiet Co')
        self.never = make_company('Never Co')
        self.waiting = make_company('Waiting Co', status='PENDING_APPROVAL')
        self.blocked = make_company('Blocked Co', status='SUSPENDED')
        self.owner_active = make_user('own_active', 'COMPANY_OWNER', self.active)
        self.owner_quiet = make_user('own_quiet', 'COMPANY_OWNER', self.quiet)
        self.owner_never = make_user('own_never', 'COMPANY_OWNER', self.never)
        logged(self.owner_active, ago(days=2))
        logged(self.owner_quiet, ago(days=INACTIVE_DAYS + 6))
        self.client.force_login(self.admin)

    def rows(self, **params):
        r = self.client.get(reverse('admin_company_monitor'), params)
        self.assertEqual(r.status_code, 200)
        return {row['c'].name: row for row in r.context['rows']}, r


class AccessTests(MonitorBase):
    NAMES = ('admin_company_monitor',)

    def urls(self):
        return [reverse('admin_company_monitor'), reverse('admin_company_monitor_detail', args=[self.active.pk])]

    def test_platform_admins_and_superusers_can_open_it(self):
        for u in self.urls():
            self.assertEqual(self.client.get(u).status_code, 200, u)
        sup = make_user('mon_super', 'EMPLOYEE', is_superuser=True)
        self.client.force_login(sup)
        for u in self.urls():
            self.assertEqual(self.client.get(u).status_code, 200, u)

    def test_company_owners_and_employees_are_refused(self):
        emp_user = make_user('mon_emp', 'EMPLOYEE', self.active)
        emp = make_employee(self.active, 'M1', 'm1@mon.test')
        emp.user = emp_user
        emp.save()
        for user in (self.owner_active, emp_user):
            self.client.force_login(user)
            for u in self.urls() + [reverse('admin_company_monitor') + '?export=csv']:
                r = self.client.get(u)
                self.assertIn(r.status_code, (403, 302), (user.username, u))

    def test_a_visitor_who_is_not_logged_in_is_sent_to_the_login(self):
        self.client.logout()
        for u in self.urls():
            self.assertEqual(self.client.get(u).status_code, 302, u)

    def test_the_menu_link_is_for_admins_only(self):
        try:
            admin_page, owner_page = reverse('admin_dashboard'), reverse('dashboard')
        except NoReverseMatch:
            self.skipTest('this project has no admin_dashboard or dashboard page to look at')
        link = reverse('admin_company_monitor')
        self.assertContains(self.client.get(admin_page), link)
        self.client.force_login(self.owner_active)
        self.assertNotContains(self.client.get(owner_page), link)
        self.assertNotContains(self.client.get(owner_page), 'Company Monitoring')


class NumbersTests(MonitorBase):
    def test_health_of_each_company(self):
        rows, _ = self.rows()
        self.assertEqual({n: r['health'] for n, r in rows.items()}, {'Active Co': 'active', 'Quiet Co': 'inactive', 'Never Co': 'never', 'Waiting Co': 'waiting', 'Blocked Co': 'blocked'})
        self.assertEqual(rows['Quiet Co']['label'], 'Inactive %d days' % (INACTIVE_DAYS + 6))
        self.assertEqual(rows['Never Co']['label'], 'Never logged in')
        self.assertEqual(rows['Blocked Co']['label'], 'Suspended')
        self.assertRegex(rows['Waiting Co']['label'], r'^Waiting \d+ days?$')

    def test_the_inactive_limit_is_exact(self):
        logged(self.owner_active, ago(days=INACTIVE_DAYS, hours=-1))             # just inside the limit
        self.assertEqual(health_of(monitored_companies().get(pk=self.active.pk), NOW())[0], 'active')
        logged(self.owner_active, ago(days=INACTIVE_DAYS, hours=1))              # just outside
        self.assertEqual(health_of(monitored_companies().get(pk=self.active.pk), NOW())[0], 'inactive')

    def test_the_last_login_is_the_latest_of_all_the_company_users(self):
        second = make_user('own_active_2', 'COMPANY_OWNER', self.active)
        logged(second, ago(hours=3))
        rows, _ = self.rows()
        self.assertAlmostEqual(rows['Active Co']['c'].last_login.timestamp(), ago(hours=3).timestamp(), delta=5)
        self.assertEqual(rows['Active Co']['c'].active_users, 2)
        make_user('own_active_off', 'EMPLOYEE', self.active, is_active=False)
        self.assertEqual(self.rows()[0]['Active Co']['c'].active_users, 2)           # a switched-off login is not counted

    def test_employee_counts_only_count_active_and_on_leave(self):
        a, b, c = (make_employee(self.active, code, code.lower() + '@mon.test') for code in ('A1', 'A2', 'A3'))
        Employee.objects.filter(pk=b.pk).update(employment_status='ON_LEAVE')
        Employee.objects.filter(pk=c.pk).update(employment_status='RESIGNED')
        make_employee(self.quiet, 'Q1', 'q1@mon.test')
        rows, _ = self.rows()
        self.assertEqual((rows['Active Co']['c'].active_employees, rows['Quiet Co']['c'].active_employees, rows['Never Co']['c'].active_employees), (2, 1, 0))

    def test_activity_numbers_use_the_right_window_company_and_action(self):
        event(self.active, 'UPDATE', ago(days=1))
        event(self.active, 'PROCESS_PAYROLL', ago(days=3))
        event(self.active, 'PROCESS_PAYROLL', ago(days=40))
        event(self.active, 'LOGIN', ago(days=45))
        event(self.quiet, 'UPDATE', ago(days=2))
        rows, _ = self.rows()
        c = rows['Active Co']['c']
        self.assertEqual(c.actions_30d, 2)                                       # the two inside 30 days
        self.assertAlmostEqual(c.last_payroll.timestamp(), ago(days=3).timestamp(), delta=5)
        self.assertAlmostEqual(c.last_activity.timestamp(), ago(days=1).timestamp(), delta=5)
        self.assertIsNone(rows['Never Co']['c'].last_activity)
        self.assertIsNone(rows['Never Co']['c'].last_payroll)
        self.assertEqual(rows['Quiet Co']['c'].actions_30d, 1)

    def test_summary_cards(self):
        _, r = self.rows()
        cards = dict(r.context['cards'])
        self.assertEqual(cards['Companies'], 5)
        self.assertEqual((cards['Approved'], cards['Waiting for approval'], cards['Suspended or rejected']), (3, 1, 1))
        self.assertEqual((cards['Active in the last 7 days'], cards['Inactive (%d+ days)' % INACTIVE_DAYS], cards['Never logged in']), (1, 1, 1))


class FilterTests(MonitorBase):
    def test_search_status_and_health_filters(self):
        self.assertEqual(set(self.rows(q='quiet')[0]), {'Quiet Co'})
        self.assertEqual(set(self.rows(q='never co@example.com')[0]), {'Never Co'})              # the email is searched too
        self.assertEqual(set(self.rows(q='nobody-at-all')[0]), set())
        self.assertEqual(set(self.rows(q='never co')[0]), {'Never Co'})
        self.assertEqual(set(self.rows(status='SUSPENDED')[0]), {'Blocked Co'})
        for health, expect in (('active', {'Active Co'}), ('inactive', {'Quiet Co'}), ('never', {'Never Co'}), ('waiting', {'Waiting Co'}), ('blocked', {'Blocked Co'})):
            self.assertEqual(set(self.rows(health=health)[0]), expect, health)
        self.assertEqual(len(self.rows(health='nonsense')[0]), 5)                # an unknown value is ignored
        self.assertEqual(len(self.rows(status='NONSENSE')[0]), 5)

    def test_sorting_puts_the_most_recent_first_and_companies_without_activity_last(self):
        event(self.quiet, 'UPDATE', ago(days=1))
        event(self.active, 'UPDATE', ago(days=5))
        names = [r['c'].name for r in self.client.get(reverse('admin_company_monitor')).context['rows']]
        self.assertEqual(names[:2], ['Quiet Co', 'Active Co'])
        self.assertEqual(sorted(names[2:]), ['Blocked Co', 'Never Co', 'Waiting Co'])
        by_name = [r['c'].name for r in self.client.get(reverse('admin_company_monitor'), {'sort': 'name'}).context['rows']]
        self.assertEqual(by_name, sorted(by_name))
        by_login = [r['c'].name for r in self.client.get(reverse('admin_company_monitor'), {'sort': 'login'}).context['rows']]
        self.assertEqual(by_login[:2], ['Active Co', 'Quiet Co'])
        make_employee(self.never, 'N1', 'n1@mon.test')
        by_emp = [r['c'].name for r in self.client.get(reverse('admin_company_monitor'), {'sort': 'employees'}).context['rows']]
        self.assertEqual(by_emp[0], 'Never Co')

    def test_paging_keeps_the_filters(self):
        for i in range(30):
            Company.objects.create(name='Bulk %02d' % i, contact_email='b%d@x.test', status='APPROVED')
        r = self.client.get(reverse('admin_company_monitor'), {'q': 'Bulk', 'sort': 'name'})
        self.assertEqual((r.context['page'].paginator.count, len(r.context['rows'])), (30, 25))
        self.assertContains(r, 'page=2')
        self.assertContains(r, 'q=Bulk')
        r2 = self.client.get(reverse('admin_company_monitor'), {'q': 'Bulk', 'sort': 'name', 'page': 2})
        self.assertEqual(len(r2.context['rows']), 5)

    def test_the_page_does_not_run_a_query_for_every_company(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def queries():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(reverse('admin_company_monitor')).status_code, 200)
            return len(ctx)

        for i in range(10):
            Company.objects.create(name='Many %02d' % i, contact_email='m%d@x.test' % i, status='APPROVED')
        few = queries()
        for i in range(60):
            Company.objects.create(name='More %02d' % i, contact_email='z%d@x.test' % i, status='APPROVED')
        many = queries()
        self.assertEqual(few, many)                                               # six times as many companies, the same number of queries
        self.assertLess(many, 25)


class PrivacyTests(MonitorBase):
    """The platform admin never sees payroll data or employee names here."""

    def setUp(self):
        super().setUp()
        self.emp = make_employee(self.active, 'SECRETCODE1', 'secret@mon.test')
        Employee.objects.filter(pk=self.emp.pk).update(first_name='Zebediah', last_name='Quillfeather', bank_account_no='9988776655443', pan_number='ABCDE1234F')
        PayrollRun.objects.create(company=self.active, month='March 2031', status='RELEASED')
        event(self.active, 'CREATE', ago(hours=2), actor=self.owner_active, details='ESS login created for SECRETCODE1 Zebediah Quillfeather')

    def test_nothing_private_is_on_the_list_the_detail_page_or_the_export(self):
        pages = [self.client.get(reverse('admin_company_monitor')), self.client.get(reverse('admin_company_monitor_detail', args=[self.active.pk])),
                 self.client.get(reverse('admin_company_monitor'), {'export': 'csv'})]
        for r in pages:
            self.assertEqual(r.status_code, 200)
            body = r.content.decode()
            for secret in ('Zebediah', 'Quillfeather', 'SECRETCODE1', '9988776655443', 'ABCDE1234F', 'March 2031', 'secret@mon.test'):
                self.assertNotIn(secret, body, secret)
        self.assertContains(pages[1], 'Employee')                                  # the kind of record is shown, never which one

    def test_only_the_date_of_the_last_payroll_is_shown(self):
        event(self.active, 'PROCESS_PAYROLL', ago(days=3), details='Processed 117 employees, net 9,99,999.00')
        body = self.client.get(reverse('admin_company_monitor_detail', args=[self.active.pk])).content.decode()
        self.assertNotIn('9,99,999', body)
        self.assertNotIn('117 employees', body)


class DetailAndExportTests(MonitorBase):
    def test_the_detail_page_shows_users_headcount_activity_and_events(self):
        make_employee(self.active, 'D1', 'd1@mon.test')
        for i in range(25):
            event(self.active, 'UPDATE', ago(hours=i + 1), actor=self.owner_active)
        event(self.active, 'LOGIN', ago(days=60))
        event(self.quiet, 'UPDATE', ago(hours=1))
        r = self.client.get(reverse('admin_company_monitor_detail', args=[self.active.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'own_active')
        self.assertNotContains(r, 'own_quiet')
        self.assertEqual(dict(r.context['headcount'])['Active'], 1)
        self.assertEqual(len(r.context['events']), 20)                              # only the last 20
        self.assertEqual(r.context['action_counts'], [{'name': 'Update', 'n': 25}])  # the 60-day-old login is outside the window and the quiet company's event is not counted
        self.assertEqual(self.client.get(reverse('admin_company_monitor_detail', args=[999999])).status_code, 404)

    def test_biometric_devices_are_shown_when_the_company_has_them(self):
        try:
            from .biometric_models import BiometricDevice
        except Exception:
            self.skipTest('the biometric module is not installed')
        BiometricDevice.objects.create(company=self.active, name='Hosur door', key_hash='h' * 64, key_prefix='bio_abcd', last_seen=ago(hours=1))
        self.assertContains(self.client.get(reverse('admin_company_monitor_detail', args=[self.active.pk])), 'Hosur door')
        self.assertContains(self.client.get(reverse('admin_company_monitor_detail', args=[self.quiet.pk])), 'No biometric device set up.')

    def test_the_csv_export_matches_the_filters_and_is_recorded(self):
        import csv
        import io
        r = self.client.get(reverse('admin_company_monitor'), {'export': 'csv', 'health': 'inactive'})
        self.assertIn('company_monitoring_', r['Content-Disposition'])
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        self.assertEqual(rows[0][0], 'Company')
        self.assertEqual([x[0] for x in rows[1:]], ['Quiet Co'])
        self.assertEqual(rows[1][1:3], ['Approved', 'Inactive %d days' % (INACTIVE_DAYS + 6)])
        self.assertTrue(AuditLog.objects.filter(action='EXPORT', details__startswith='Company monitoring exported (1 companies)').exists())

    def test_a_company_name_that_looks_like_a_formula_is_made_safe_in_the_export(self):
        Company.objects.create(name='=HYPERLINK("http://x")', contact_email='f@x.test', status='APPROVED')
        body = self.client.get(reverse('admin_company_monitor'), {'export': 'csv'}).content.decode()
        self.assertIn("'=HYPERLINK", body)
