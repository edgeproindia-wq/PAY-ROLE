"""The /version/ address: shows only the short commit code, works without a login, and is never cached."""
from unittest import mock

from django.test import TestCase


class VersionTests(TestCase):
    def test_it_shows_only_the_first_seven_characters_of_the_commit(self):
        with mock.patch.dict('os.environ', {'RENDER_GIT_COMMIT': 'ed53234abcdef0123456789'}):
            resp = self.client.get('/version/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'commit': 'ed53234'})
        self.assertIn('no-cache', resp['Cache-Control'])

    def test_it_is_empty_when_the_host_gives_no_commit_and_needs_no_login(self):
        with mock.patch.dict('os.environ', {'RENDER_GIT_COMMIT': ''}):
            resp = self.client.get('/version/')
        self.assertEqual(resp.json(), {'commit': ''})

    def test_nothing_else_is_exposed(self):
        with mock.patch.dict('os.environ', {'RENDER_GIT_COMMIT': 'abc1234567', 'DATABASE_URL': 'postgres://secret', 'DJANGO_SECRET_KEY': 'topsecret'}):
            text = self.client.get('/version/').content.decode()
        self.assertNotIn('secret', text.lower())
        self.assertNotIn('postgres', text.lower())