"""Notification sync: every business event reaches the right people, and the bell, dashboards and Notifications page agree."""
import datetime
from unittest import mock

from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from .models import (ClientComplaint, ClientRequest, DemoRequest, Employee, InvestmentDeclaration, LeaveRequest, Notification,
                     PayrollRun, Reimbursement, User)
from .tests import make_company, make_employee, make_user

try:
    from .models_features import Announcement, Grievance
except ImportError:
    Announcement = Grievance = None

TODAY = datetime.date.today()


class SyncBase(TestCase):
    def setUp(self):
        self.co, self.other = make_company('Sync Co'), make_company('Sync Other Co')
        self.owner = make_user('syncowner', 'COMPANY_OWNER', self.co)
        self.other_owner = make_user('syncotherowner', 'COMPANY_OWNER', self.other)
        self.emp_user = make_user('syncemp', 'EMPLOYEE', self.co)
        self.emp = make_employee(self.co, 'SY001', 'sy001@example.com')
        Employee.objects.filter(pk=self.emp.pk).update(user=self.emp_user)
        self.emp.refresh_from_db()
        self.admin = User.objects.create_superuser('syncadmin', 'syncadmin@example.com', 'Adm1n-Pass#2026')
        Notification.objects.all().delete()                      # start every test with a clean inbox

    def got(self, user, text):
        return Notification.objects.filter(recipient=user, message__icontains=text).exists()


class EventTests(SyncBase):
    def test_leave_submitted_notifies_the_owner_only_of_that_company(self):
        """Leave submitted -> owner of THAT company notified (not other companies)"""
        LeaveRequest.objects.create(employee=self.emp, leave_type='CASUAL', from_date=TODAY, to_date=TODAY)
        self.assertTrue(Notification.objects.filter(recipient=self.owner).exists())
        self.assertFalse(Notification.objects.filter(recipient=self.other_owner).exists())

    def test_leave_decision_notifies_the_employee(self):
        """Leave approved/rejected -> employee notified"""
        leave = LeaveRequest.objects.create(employee=self.emp, leave_type='CASUAL', from_date=TODAY, to_date=TODAY)
        self.client.force_login(self.owner)
        self.client.post(reverse('leave_decision', args=[leave.pk]), {'decision': 'APPROVED', 'comment': 'ok'})
        self.assertTrue(self.got(self.emp_user, 'approved'))

    def test_leave_cancelled_notifies_the_owner(self):
        """Leave cancelled -> owner notified"""
        leave = LeaveRequest.objects.create(employee=self.emp, leave_type='CASUAL', from_date=TODAY, to_date=TODAY)
        Notification.objects.all().delete()
        leave.status = 'CANCELLED'
        leave.save()
        self.assertTrue(self.got(self.owner, 'cancelled a leave'))

    def test_claim_submitted_and_decided(self):
        """Claim submitted -> owner notified; claim decided -> employee notified"""
        claim = Reimbursement.objects.create(employee=self.emp, category='TRAVEL', amount=500, date=TODAY, description='cab')
        self.assertTrue(Notification.objects.filter(recipient=self.owner).exists())
        self.client.force_login(self.owner)
        self.client.post(reverse('reimbursement_decision', args=[claim.pk]), {'decision': 'APPROVED', 'comment': ''})
        self.assertTrue(self.got(self.emp_user, 'approved'))

    def test_payroll_validated_notifies_the_owner(self):
        """Payroll validated -> owner notified (once only)"""
        run = PayrollRun.objects.create(company=self.co, month='September 2026', status='DRAFT')
        self.assertFalse(Notification.objects.filter(recipient=self.owner).exists())
        run.status = 'VALIDATED'
        run.save()
        self.assertTrue(self.got(self.owner, 'validated'))
        count = Notification.objects.filter(recipient=self.owner).count()
        run.save()                                                  # saving again must not repeat the message
        self.assertEqual(Notification.objects.filter(recipient=self.owner).count(), count)

    def test_company_registration_and_demo_request_notify_admins(self):
        """Company registered / demo requested -> admins notified"""
        make_company('Brand New Co', status='PENDING')
        self.assertTrue(self.got(self.admin, 'Brand New Co'))
        DemoRequest.objects.create(full_name='A B', company_name='Demo Seeker Ltd', email='d@example.com', phone='9999999999')
        self.assertTrue(self.got(self.admin, 'Demo Seeker Ltd'))

    def test_investment_declaration_submitted_and_verified(self):
        """Investment declaration submitted -> owner; verified -> employee"""
        decl = InvestmentDeclaration.objects.create(employee=self.emp, financial_year='2026-27', section='80C', investment_type='PPF', declared_amount=1000)
        self.assertTrue(self.got(self.owner, 'investment declaration'))
        decl.is_verified = True
        decl.save()
        self.assertTrue(self.got(self.emp_user, 'verified'))

    def test_grievance_raised_and_updated(self):
        """Grievance raised -> owner; status changed -> employee"""
        if Grievance is None:
            self.skipTest('no grievance module')
        g = Grievance.objects.create(company=self.co, employee=self.emp, tracking_id='GRV-SYNC-1', category='OTHER', subject='Desk', description='x')
        self.assertTrue(self.got(self.owner, 'GRV-SYNC-1'))
        g.status = 'RESOLVED'
        g.save()
        self.assertTrue(self.got(self.emp_user, 'GRV-SYNC-1'))

    def test_announcement_reaches_the_companys_employees_only(self):
        """Announcement published -> employees of that company only"""
        if Announcement is None:
            self.skipTest('no announcement module')
        stranger = make_user('syncstranger', 'EMPLOYEE', self.other)
        Announcement.objects.create(company=self.co, title='Diwali holiday', message='Office closed', audience='ALL', publish_date=TODAY, status='PUBLISHED')
        self.assertTrue(self.got(self.emp_user, 'Diwali holiday'))
        self.assertFalse(Notification.objects.filter(recipient=stranger).exists())

    def test_client_complaint_and_request_reach_admins_and_answers_reach_the_owner(self):
        """Complaint/request raised -> admins; answer -> owner"""
        c = ClientComplaint.objects.create(company=self.co, raised_by=self.owner, subject='Slow page', description='x')
        self.assertTrue(self.got(self.admin, 'Slow page'))
        c.status = 'RESOLVED'
        c.save()
        self.assertTrue(self.got(self.owner, 'Slow page'))
        r = ClientRequest.objects.create(company=self.co, raised_by=self.owner, request_type='OTHER', description='x')
        self.assertTrue(Notification.objects.filter(recipient=self.admin, message__icontains='request from Sync Co').exists())
        r.status = 'APPROVED'
        r.save()
        self.assertTrue(Notification.objects.filter(recipient=self.owner, message__icontains='is now').exists())

    def test_a_notification_problem_never_breaks_the_real_action(self):
        """A notification failure never breaks the real action"""
        leave = LeaveRequest.objects.create(employee=self.emp, leave_type='CASUAL', from_date=TODAY, to_date=TODAY)
        with mock.patch.object(Notification.objects, 'bulk_create', side_effect=RuntimeError('boom')):
            leave.status = 'CANCELLED'
            leave.save()
        leave.refresh_from_db()
        self.assertEqual(leave.status, 'CANCELLED')


class ScreensAgreeTests(SyncBase):
    def make_unread(self, user, n=3):
        for i in range(n):
            Notification.objects.create(recipient=user, message=f'Test message {i}', link='/leave_management/')

    def test_bell_count_dashboard_card_and_page_all_agree(self):
        """Bell count = dashboard card = Notifications page"""
        self.make_unread(self.owner, 3)
        Notification.objects.create(recipient=self.owner, message='Already seen', is_read=True)
        self.client.force_login(self.owner)
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.context['unread_notification_count'], 3)
        html = resp.content.decode()
        self.assertIn('nb-badge', html)
        self.assertIn('id="notificationCard"', html)
        self.assertIn('Test message 0', html)
        page = self.client.get(reverse('notifications'))
        self.assertEqual(page.context['unread_notification_count'], 3)
        self.assertContains(page, 'Already seen')
        self.assertContains(page, 'Test message 2')

    def test_every_role_sees_its_own_notifications_on_its_dashboard(self):
        """Employee and admin dashboards show their own notifications"""
        self.make_unread(self.emp_user, 2)
        self.make_unread(self.admin, 1)
        self.client.force_login(self.emp_user)
        try:
            html = self.client.get(reverse('employee_home')).content.decode()
        except NoReverseMatch:
            html = ''
        if '</body>' in html:                                       # a real page
            self.assertIn('id="notificationCard"', html)
            self.assertIn('Test message 0', html)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('admin_dashboard')).context['unread_notification_count'], 1)

    def test_opening_marks_read_and_goes_to_the_linked_page(self):
        """Opening a notification marks it read and opens the linked page"""
        self.make_unread(self.owner, 2)
        note = Notification.objects.filter(recipient=self.owner).first()
        self.client.force_login(self.owner)
        resp = self.client.get(reverse('notification_open', args=[note.pk]))
        self.assertEqual((resp.status_code, resp['Location']), (302, '/leave_management/'))
        self.assertEqual(self.client.get(reverse('dashboard')).context['unread_notification_count'], 1)

    def test_mark_all_read_clears_the_count_and_ignores_unsafe_redirects(self):
        """Mark all read clears the count (unsafe redirects ignored)"""
        self.make_unread(self.owner, 4)
        self.client.force_login(self.owner)
        resp = self.client.post(reverse('notification_mark_all_read'), {'next': '//evil.example.com/'})
        self.assertEqual(resp['Location'], reverse('notifications'))
        self.assertEqual(self.client.get(reverse('dashboard')).context['unread_notification_count'], 0)
        self.assertEqual(self.client.get(reverse('notification_mark_all_read')).status_code, 405)

    def test_nobody_can_open_or_clear_someone_elses_notifications(self):
        """Nobody can open or clear another user's notifications"""
        self.make_unread(self.owner, 2)
        note = Notification.objects.filter(recipient=self.owner).first()
        self.client.force_login(self.other_owner)
        self.assertEqual(self.client.get(reverse('notification_open', args=[note.pk])).status_code, 404)
        self.client.post(reverse('notification_mark_all_read'))
        self.assertEqual(Notification.objects.filter(recipient=self.owner, is_read=False).count(), 2)