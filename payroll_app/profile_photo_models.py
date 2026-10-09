"""Profile photo of a signed-in user (kept in the database; served only through a permission-checked view)."""
from django.conf import settings
from django.db import models


class ProfilePhoto(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile_photo')
    content_type = models.CharField(max_length=40, default='image/jpeg')
    data = models.BinaryField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'payroll_app'

    def __str__(self):
        return f'Photo of {self.user_id}'
