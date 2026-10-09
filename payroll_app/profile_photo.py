"""My Profile page, photo upload/removal and the permission-checked photo download."""
import io

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import path
from django.views.decorators.http import require_POST

from .audit import log_action
from .profile_photo_models import ProfilePhoto

MAX_BYTES = 2 * 1024 * 1024
SIGNATURES = {'image/jpeg': (b'\xff\xd8\xff',), 'image/png': (b'\x89PNG\r\n\x1a\n',)}


def _sniff(head):
    for ctype, sigs in SIGNATURES.items():
        if any(head.startswith(s) for s in sigs):
            return ctype
    return None


def _prepare(data):
    """Return (bytes, content_type) of a safe, small picture, or raise ValueError with a message for the user."""
    ctype = _sniff(data[:16])
    if ctype is None:
        raise ValueError('Please choose a JPG or PNG picture.')
    try:
        from PIL import Image, ImageOps
    except ImportError:                                  # no image library: keep the verified original
        return data, ctype
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert('RGB')
        img.thumbnail((400, 400))
        out = io.BytesIO()
        img.save(out, 'JPEG', quality=88)
        return out.getvalue(), 'image/jpeg'
    except Exception:
        raise ValueError('That file is not a valid picture. Please choose another JPG or PNG.')


def _may_view(viewer, owner):
    if viewer.pk == owner.pk or viewer.is_superuser or getattr(viewer, 'role', '') == 'ADMIN':
        return True
    return bool(viewer.company_id and viewer.company_id == owner.company_id)


@login_required
def my_profile(request):
    return render(request, 'profile/my_profile.html', {'max_mb': MAX_BYTES // (1024 * 1024)})


@login_required
@require_POST
def profile_photo_upload(request):
    f = request.FILES.get('photo')
    if f is None:
        messages.error(request, 'Choose a picture first.')
    elif f.size > MAX_BYTES:
        messages.error(request, f'The picture must be {MAX_BYTES // (1024 * 1024)} MB or smaller.')
    else:
        try:
            data, ctype = _prepare(f.read())
        except ValueError as e:
            messages.error(request, str(e))
        else:
            ProfilePhoto.objects.update_or_create(user=request.user, defaults={'data': data, 'content_type': ctype})
            log_action(request, 'UPDATE', request.user, details='Profile photo changed')
            messages.success(request, 'Your profile photo was updated.')
    return redirect('my_profile')


@login_required
@require_POST
def profile_photo_remove(request):
    ProfilePhoto.objects.filter(user=request.user).delete()
    log_action(request, 'UPDATE', request.user, details='Profile photo removed')
    messages.success(request, 'Your profile photo was removed.')
    return redirect('my_profile')


@login_required
def profile_photo(request, user_id):
    photo = ProfilePhoto.objects.select_related('user').filter(user_id=user_id).first()
    if photo is None or not _may_view(request.user, photo.user):
        raise Http404('No photo')                        # same answer whether it is missing or not yours
    resp = HttpResponse(bytes(photo.data), content_type=photo.content_type)
    resp['Cache-Control'] = 'private, max-age=300'
    resp['X-Content-Type-Options'] = 'nosniff'
    return resp


urlpatterns = [
    path('profile/', my_profile, name='my_profile'),
    path('profile/photo/upload/', profile_photo_upload, name='profile_photo_upload'),
    path('profile/photo/remove/', profile_photo_remove, name='profile_photo_remove'),
    path('profile/photo/<int:user_id>/', profile_photo, name='profile_photo'),
]
