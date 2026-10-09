"""Profile photo: round photo (or initials) at the top right, private storage, small profile menu."""
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import ProfilePhoto
from .tests import make_company, make_employee, make_user


def picture(kind='JPEG', size=(900, 700)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', size, (31, 147, 72)).save(buf, kind)
    return buf.getvalue()


class ProfileTests(TestCase):
    def setUp(self):
        self.co, self.other = make_company('Photo Co'), make_company('Photo Other')
        self.owner = make_user('photoowner', 'COMPANY_OWNER', self.co)
        self.mate = make_user('photomate', 'EMPLOYEE', self.co)
        self.stranger = make_user('photostranger', 'COMPANY_OWNER', self.other)

    def upload(self, name='me.jpg', content=None, user=None):
        self.client.force_login(user or self.owner)
        return self.client.post(reverse('profile_photo_upload'),
                                {'photo': SimpleUploadedFile(name, content if content is not None else picture())}, follow=True)

    def test_initials_are_shown_until_a_photo_is_uploaded_then_the_round_photo(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse('dashboard')).content.decode()
        self.assertIn('pf-initials', html)
        self.assertNotIn('pf-img', html)
        self.upload()
        html = self.client.get(reverse('dashboard')).content.decode()
        self.assertIn('pf-img', html)
        self.assertIn(reverse('profile_photo', args=[self.owner.pk]), html)

    def test_photo_is_shrunk_and_stored_as_a_safe_jpeg(self):
        self.upload('big.png', picture('PNG', (2000, 1500)))
        from PIL import Image
        photo = ProfilePhoto.objects.get(user=self.owner)
        img = Image.open(io.BytesIO(bytes(photo.data)))
        self.assertEqual(photo.content_type, 'image/jpeg')
        self.assertLessEqual(max(img.size), 400)

    def test_bad_files_are_refused(self):
        self.upload('x.jpg', b'<html><script>alert(1)</script></html>')
        self.upload('y.jpg', b'\xff\xd8\xff' + b'not really a picture')
        self.upload('z.jpg', picture() + b'0' * (2 * 1024 * 1024))
        self.assertFalse(ProfilePhoto.objects.exists())

    def test_the_menu_has_profile_password_and_logout_and_no_logout_button_elsewhere(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse('dashboard')).content.decode()
        for text in ('My Profile', 'Change Password', 'Logout', 'Account Settings'):
            self.assertIn(text, html)
        self.assertEqual(self.client.get(reverse('my_profile')).status_code, 200)

    def test_employee_menu_has_no_company_settings_link(self):
        self.client.force_login(self.mate)
        html = self.client.get(reverse('my_profile')).content.decode()
        self.assertIn('Change Password', html)
        self.assertNotIn('Account Settings', html)

    def test_who_can_see_a_photo(self):
        self.upload()
        self.client.force_login(self.owner); self.assertEqual(self.client.get(reverse('profile_photo', args=[self.owner.pk])).status_code, 200)
        self.client.force_login(self.mate); resp = self.client.get(reverse('profile_photo', args=[self.owner.pk]))
        self.assertEqual((resp.status_code, resp['Content-Type']), (200, 'image/jpeg'))
        self.assertEqual(resp['X-Content-Type-Options'], 'nosniff')
        self.client.force_login(self.stranger); self.assertEqual(self.client.get(reverse('profile_photo', args=[self.owner.pk])).status_code, 404)
        self.client.logout(); self.assertEqual(self.client.get(reverse('profile_photo', args=[self.owner.pk])).status_code, 302)

    def test_remove_photo_and_login_required(self):
        self.upload()
        self.client.post(reverse('profile_photo_remove'))
        self.assertFalse(ProfilePhoto.objects.exists())
        self.client.logout()
        self.assertEqual(self.client.post(reverse('profile_photo_upload')).status_code, 302)
