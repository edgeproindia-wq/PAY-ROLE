"""Database-driven announcements: priority, schedule (publish / expiry date), role visibility, company isolation,
the sidebar panel, and management by the company owner (and the platform admin through the Django admin)."""
import datetime

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Notification
from .models_features import Announcement
from .tests import make_company, make_employee, make_user

TODAY = timezone.localdate()
DAY = datetime.timedelta(days=1)


class Base(TestCase):
    def setUp(self):
        self.co, self.other_co = make_company('Ann Co'), make_company('Other Ann Co')
        self.owner = make_user('ann_owner', 'COMPANY_OWNER', self.co)
        self.emp = make_user('ann_emp', 'EMPLOYEE', self.co)
        profile = make_employee(self.co, 'AN001', 'an1@example.com')
        profile.user = self.emp
        profile.save()
        self.other_owner = make_user('ann_other_owner', 'COMPANY_OWNER', self.other_co)

    def make(self, title, company=None, **kw):
        kw.setdefault('message', 'Body of ' + title)
        return Announcement.objects.create(company=company or self.co, title=title, created_by=self.owner, **kw)

    def panel(self, user, page):
        self.client.force_login(user)
        html = self.client.get(reverse(page)).content.decode()
        return html.split('id="sbAnnouncements"')[1].split('</div>\n<button')[0] if 'id="sbAnnouncements"' in html else ''


class VisibilityTests(Base):
    def test_role_decides_who_sees_what(self):
        self.make('For all')
        self.make('For owners', audience='OWNERS')
        self.make('For employees', audience='EMPLOYEES')
        owner_titles = [a.title for a in Announcement.visible_to(self.owner)]
        emp_titles = [a.title for a in Announcement.visible_to(self.emp)]
        self.assertEqual(sorted(owner_titles), ['For all', 'For owners'])
        self.assertEqual(sorted(emp_titles), ['For all', 'For employees'])

    def test_other_companies_and_the_platform_admin_see_nothing_of_it(self):
        self.make('Mine')
        self.make('Theirs', company=self.other_co)
        self.assertEqual([a.title for a in Announcement.visible_to(self.owner)], ['Mine'])
        self.assertEqual([a.title for a in Announcement.visible_to(self.other_owner)], ['Theirs'])
        admin = make_user('ann_admin', 'ADMIN', is_superuser=True)
        self.assertEqual(Announcement.visible_to(admin).count(), 0)

    def test_schedule_scheduled_live_expired_and_archived(self):
        self.make('Later', publish_date=TODAY + DAY)
        self.make('Ends today', publish_date=TODAY - DAY, expiry_date=TODAY)
        self.make('Ended', publish_date=TODAY - 3 * DAY, expiry_date=TODAY - DAY)
        self.make('Archived', status='ARCHIVED')
        self.make('Live')
        self.assertEqual(sorted(a.title for a in Announcement.visible_to(self.emp)), ['Ends today', 'Live'])
        states = {a.title: a.schedule_state for a in Announcement.objects.all()}
        self.assertEqual(states, {'Later': 'Scheduled', 'Ends today': 'Live', 'Ended': 'Expired', 'Archived': 'Archived', 'Live': 'Live'})

    def test_priority_orders_the_list_then_newest_first(self):
        self.make('low', priority='LOW')
        self.make('normal old', priority='NORMAL', publish_date=TODAY - 2 * DAY)
        self.make('normal new', priority='NORMAL')
        self.make('high', priority='HIGH')
        self.make('urgent', priority='URGENT', publish_date=TODAY - 5 * DAY)
        self.assertEqual([a.title for a in Announcement.visible_to(self.emp)], ['urgent', 'high', 'normal new', 'normal old', 'low'])


class SidebarPanelTests(Base):
    def test_the_panel_is_read_from_the_database_and_limited_to_three(self):
        for i, p in enumerate(['LOW', 'NORMAL', 'HIGH', 'URGENT']):
            self.make('Notice %s' % p, priority=p)
        panel = self.panel(self.owner, 'dashboard')
        self.assertIn('Notice URGENT', panel)
        self.assertIn('Notice HIGH', panel)
        self.assertIn('Notice NORMAL', panel)
        self.assertNotIn('Notice LOW', panel)                       # only the 3 most important
        self.assertIn('View all 4', panel)

    def test_a_role_only_notice_is_not_in_the_other_roles_sidebar(self):
        self.make('Owners eyes only', audience='OWNERS')
        self.make('Staff notice', audience='EMPLOYEES')
        self.assertIn('Owners eyes only', self.panel(self.owner, 'dashboard'))
        self.assertNotIn('Staff notice', self.panel(self.owner, 'dashboard'))
        self.assertIn('Staff notice', self.panel(self.emp, 'employee_home'))
        self.assertNotIn('Owners eyes only', self.panel(self.emp, 'employee_home'))

    def test_no_panel_when_there_is_nothing_live_and_none_for_the_platform_admin(self):
        self.make('Future', publish_date=TODAY + DAY)
        self.assertEqual(self.panel(self.owner, 'dashboard'), '')
        self.make('Visible to nobody here', company=self.other_co)
        self.assertEqual(self.panel(self.owner, 'dashboard'), '')
        self.client.force_login(make_user('ann_admin2', 'ADMIN', is_superuser=True))
        self.assertEqual(self.client.get(reverse('admin_dashboard')).status_code, 200)

    def test_changing_the_database_row_changes_the_sidebar(self):
        a = self.make('First headline')
        self.assertIn('First headline', self.panel(self.emp, 'employee_home'))
        a.title = 'Edited headline'
        a.save()
        self.assertIn('Edited headline', self.panel(self.emp, 'employee_home'))
        a.status = 'ARCHIVED'
        a.save()
        self.assertEqual(self.panel(self.emp, 'employee_home'), '')

    def test_opening_the_announcements_page_marks_them_read_for_an_employee(self):
        a = self.make('Read me')
        self.assertIn('p-normal unread', self.panel(self.emp, 'employee_home'))
        self.client.get(reverse('announcements'))
        panel = self.panel(self.emp, 'employee_home')
        self.assertIn('Read me', panel)
        self.assertNotIn('p-normal unread', panel)


class ManagementTests(Base):
    def post(self, **over):
        data = {'title': 'Holiday notice', 'message': 'Office closed Friday', 'audience': 'EMPLOYEES', 'priority': 'HIGH',
                'publish_date': TODAY.isoformat(), 'expiry_date': (TODAY + 3 * DAY).isoformat()}
        data.update(over)
        self.client.force_login(self.owner)
        return self.client.post(reverse('announcements'), data)

    def test_owner_publishes_with_priority_audience_and_schedule_and_it_persists(self):
        self.assertEqual(self.post().status_code, 302)
        a = Announcement.objects.get(title='Holiday notice')
        self.assertEqual((a.company, a.priority, a.audience, a.created_by), (self.co, 'HIGH', 'EMPLOYEES', self.owner))
        self.assertEqual(a.expiry_date, TODAY + 3 * DAY)
        a.refresh_from_db()
        self.assertEqual(a.priority, 'HIGH')

    def test_a_form_posted_without_priority_still_publishes_as_normal(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('announcements'), {'title': 'Old form', 'message': 'm', 'audience': 'ALL', 'publish_date': TODAY.isoformat()})
        self.assertEqual(Announcement.objects.get(title='Old form').priority, 'NORMAL')

    def test_expiry_before_publish_is_refused_and_nothing_is_saved(self):
        r = self.post(expiry_date=(TODAY - DAY).isoformat())
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Announcement.objects.exists())

    def test_only_the_chosen_audience_is_notified_and_only_once(self):
        self.post()
        self.assertEqual(Notification.objects.filter(recipient=self.emp, message__contains='Holiday notice').count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.owner, message__contains='Holiday notice').count(), 0)
        self.post(title='Owners meeting', audience='OWNERS')
        self.assertEqual(Notification.objects.filter(recipient=self.emp, message__contains='Owners meeting').count(), 0)
        second_owner = make_user('ann_owner2', 'COMPANY_OWNER', self.co)
        self.post(title='Board note', audience='OWNERS')
        self.assertEqual(Notification.objects.filter(recipient=second_owner, message__contains='Board note').count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.owner, message__contains='Board note').count(), 0)   # not the author

    def test_the_management_page_shows_priority_and_schedule_state(self):
        self.make('Soon', publish_date=TODAY + DAY, priority='URGENT')
        self.client.force_login(self.owner)
        page = self.client.get(reverse('announcements')).content.decode()
        self.assertIn('Scheduled', page)
        self.assertIn('Urgent', page)

    def test_an_employee_cannot_publish_and_never_sees_an_owners_notice_on_the_page(self):
        self.make('Owners only', audience='OWNERS')
        self.client.force_login(self.emp)
        page = self.client.get(reverse('announcements'))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'Owners only')
        self.client.post(reverse('announcements'), {'title': 'x', 'message': 'y', 'audience': 'ALL', 'priority': 'LOW',
                                                    'publish_date': TODAY.isoformat()})
        self.assertFalse(Announcement.objects.filter(title='x').exists())           # an employee can never publish

    def test_an_owner_cannot_archive_another_companys_announcement(self):
        theirs = self.make('Theirs', company=self.other_co)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(reverse('announcements'), {'archive': theirs.pk}).status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, 'PUBLISHED')

    def test_the_platform_admin_manages_them_in_the_django_admin(self):
        self.make('Admin sees this')
        self.client.force_login(make_user('ann_super', 'ADMIN', is_superuser=True))
        page = self.client.get('/admin/payroll_app/announcement/')
        self.assertContains(page, 'Admin sees this')
