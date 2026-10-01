"""python manage.py lock_user <username>

Credential rotation helper (BUG-01): makes the account's current password unusable
and signs it out everywhere. The owner then sets a new password with "Forgot password"
(emailed link). Never prints or stores any password."""
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = 'Invalidate a user password and end all of their sessions.'

    def add_arguments(self, parser):
        parser.add_argument('username')

    def handle(self, *args, **opts):
        User = get_user_model()
        try:
            user = User.objects.get(username=opts['username'])
        except User.DoesNotExist:
            raise CommandError('No such user.')
        user.set_unusable_password()
        user.save(update_fields=['password'])
        ended = 0
        for s in Session.objects.filter(expire_date__gt=timezone.now()):
            if str(s.get_decoded().get('_auth_user_id')) == str(user.pk):
                s.delete()
                ended += 1
        self.stdout.write(self.style.SUCCESS(
            f'Password for "{user.username}" disabled and {ended} session(s) ended. '
            'Set a new one with "Forgot password" on the login page.'))