"""Live notifications: the bell and popup are fed by a small endpoint that only ever reads the signed-in user's own notifications."""
import json

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import Notification
from .tests import make_company, make_user


class PollTests(TestCase):
    def setUp(self):
        self.co = make_company('Live Co')
        self.me = make_user('liveowner', 'COMPANY_OWNER', self.co)
        self.other = make_user('liveother', 'COMPANY_OWNER', make_company('Other Live Co'))
        self.client.force_login(self.me)
        self.url = reverse('notification_poll')

    def note(self, user=None, msg='Hello', read=False):
        return Notification.objects.create(recipient=user or self.me, message=msg, link='/leave_management/', is_read=read)

    def get(self, **params):
        resp = self.client.get(self.url, params)
        self.assertEqual(resp.status_code, 200)
        return json.loads(resp.content)

    def test_the_first_answer_gives_the_count_and_a_starting_point_but_no_popups(self):
        a, b = self.note(msg='old 1'), self.note(msg='old 2')
        d = self.get()
        self.assertEqual((d['unread'], d['latest'], d['items'], d['new_total']), (2, b.pk, [], 0))

    def test_later_answers_give_only_newer_unread_ones_newest_first_with_a_link_that_opens_and_marks_read(self):
        old = self.note(msg='old')
        n1, n2 = self.note(msg='first new'), self.note(msg='second new')
        d = self.get(after=old.pk)
        self.assertEqual([i['message'] for i in d['items']], ['second new', 'first new'])
        self.assertEqual(d['new_total'], 2)
        self.assertEqual(d['items'][0]['url'], reverse('notification_open', args=[n2.pk]))
        self.assertEqual(d['unread'], 3)
        self.client.get(d['items'][0]['url'])                                         # opening it uses the existing route
        self.assertTrue(Notification.objects.get(pk=n2.pk).is_read)

    def test_nothing_new_means_no_items(self):
        n = self.note()
        d = self.get(after=n.pk)
        self.assertEqual((d['items'], d['new_total'], d['latest']), ([], 0, n.pk))

    def test_a_new_one_that_is_already_read_is_not_a_popup_but_the_starting_point_moves_on(self):
        old = self.note()
        gone = self.note(msg='read already', read=True)
        d = self.get(after=old.pk)
        self.assertEqual(d['items'], [])
        self.assertEqual(d['latest'], gone.pk)

    def test_at_most_five_popups_and_the_rest_are_counted(self):
        start = self.note(msg='start')
        for i in range(8): self.note(msg=f'n{i}')
        d = self.get(after=start.pk)
        self.assertEqual(len(d['items']), 5)
        self.assertEqual(d['new_total'], 8)
        self.assertEqual(d['items'][0]['message'], 'n7')

    def test_polling_never_marks_anything_read(self):
        self.note(); self.note()
        self.get(); self.get(after=0)
        self.assertEqual(Notification.objects.filter(recipient=self.me, is_read=False).count(), 2)

    def test_someone_elses_notifications_never_appear(self):
        mine = self.note(msg='mine')
        theirs = self.note(user=self.other, msg='SECRET for the other user')
        d = self.get(after=0)
        self.assertEqual([i['message'] for i in d['items']], ['mine'])
        self.assertEqual((d['unread'], d['latest']), (1, mine.pk))
        self.assertNotIn('SECRET', json.dumps(d))
        self.client.force_login(self.other)
        d2 = self.get(after=0)
        self.assertEqual([i['message'] for i in d2['items']], ['SECRET for the other user'])
        self.assertEqual(d2['latest'], theirs.pk)

    def test_a_bad_or_negative_after_is_treated_like_the_first_answer(self):
        self.note(); self.note()
        for bad in ('abc', '-5', ''):
            d = self.get(after=bad)
            self.assertEqual(d['items'], [], bad)

    def test_the_answer_is_never_cached_and_only_get_is_allowed(self):
        resp = self.client.get(self.url)
        self.assertIn('no-store', resp.headers['Cache-Control'])
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_a_visitor_who_is_not_signed_in_is_sent_to_the_login_page(self):
        self.client.logout()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])

    def test_every_role_can_use_it(self):
        for role, co in (('EMPLOYEE', self.co), ('ADMIN', None)):
            self.client.force_login(make_user(f'live{role.lower()}', role, co))
            self.assertEqual(self.client.get(self.url).status_code, 200, role)

    def test_a_message_with_html_in_it_is_sent_as_plain_text(self):
        n = self.note(msg='<img src=x onerror=alert(1)> & more')
        d = self.get(after=n.pk - 1)
        self.assertEqual(d['items'][0]['message'], '<img src=x onerror=alert(1)> & more')

    def test_the_work_does_not_grow_with_the_number_of_notifications(self):
        self.note()
        with CaptureQueriesContext(connection) as few:
            self.get(after=0)
        Notification.objects.bulk_create([Notification(recipient=self.me, message=f'm{i}', link='') for i in range(300)])
        with CaptureQueriesContext(connection) as many:
            self.get(after=0)
        self.assertLessEqual(len(many) - len(few), 1, (len(few), len(many)))          # one extra count for the "N more" total, nothing per notification
        self.assertLessEqual(len(many), 12)                                            # including the session and sign-in lookups every request makes


class PageTests(TestCase):
    def setUp(self):
        self.co = make_company('Live Page Co')
        self.owner = make_user('livepageowner', 'COMPANY_OWNER', self.co)

    def test_a_signed_in_page_carries_the_live_script_and_the_popup_area(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse('hr_shifts')).content.decode()
        for piece in ('__npLive', 'id="npToasts"', "'" + reverse('notification_poll') + "'", 'textContent = n.message', 'a.nb-wrap', 'visibilitychange'):
            self.assertIn(piece, html, piece)

    def test_the_existing_bell_is_unchanged_on_the_server_side(self):
        Notification.objects.create(recipient=self.owner, message='x', link='')
        self.client.force_login(self.owner)
        html = self.client.get(reverse('hr_shifts')).content.decode()
        self.assertIn('class="nb-badge">1</span>', html)
        self.assertIn('Notifications, 1 unread', html)

    def test_the_sign_in_page_does_not_poll(self):
        html = self.client.get(reverse('login')).content.decode()
        self.assertNotIn('__npLive', html)
