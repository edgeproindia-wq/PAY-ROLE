"""Phase-5 models: announcement read receipts."""
from django.conf import settings
from django.db import models


class AnnouncementRead(models.Model):
    announcement = models.ForeignKey('payroll_app.Announcement', on_delete=models.CASCADE, related_name='reads')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'payroll_app'
        constraints = [models.UniqueConstraint(fields=['announcement', 'user'], name='one_read_per_user')]