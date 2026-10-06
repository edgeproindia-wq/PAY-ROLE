"""Safe editing when many people work at the same time.

* VersionedModel   - adds a `version` number that goes up by 1 every time an existing
                     record is saved again.
* remember_version - call when a form is SHOWN: notes which version this user is looking at.
* stale_warning    - call inside the locked part of a POST: returns None when nobody changed
                     the record since this user opened the form, otherwise the warning text.

Always use them together with  Model.objects.select_for_update()  inside
transaction.atomic().  On PostgreSQL the row is locked: the second person waits, then
sees the first person's saved values instead of silently overwriting them.
"""
from django.db import models
from django.utils import timezone

SESSION_KEY = 'record_versions'
MAX_TRACKED = 60          # forms remembered per browser session (oldest are forgotten)


class VersionedModel(models.Model):
    version = models.PositiveIntegerField(
        default=1, editable=False,
        help_text='Goes up by 1 every time the record is saved. Used to detect two people editing at once.',
    )

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:                       # existing row being updated
            self.version = (self.version or 0) + 1
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:                # save(update_fields=[...]) must still store the new version
                kwargs['update_fields'] = set(update_fields) | {'version'}
        super().save(*args, **kwargs)


def _key(obj):
    return f'{obj._meta.label_lower}:{obj.pk}'


def remember_version(request, obj):
    """Remember, in this user's session, the version of `obj` that is on screen."""
    seen = dict(request.session.get(SESSION_KEY, {}))
    key = _key(obj)
    seen.pop(key, None)
    seen[key] = obj.version
    while len(seen) > MAX_TRACKED:
        seen.pop(next(iter(seen)))
    request.session[SESSION_KEY] = seen


def stale_warning(request, locked_obj, noun='record'):
    """Compare the version this user saw with the locked, current row.

    Returns None if it is safe to save, or the warning text to show the user.
    The remembered version is used up either way; call remember_version() again
    when the form is shown again."""
    seen = dict(request.session.get(SESSION_KEY, {}))
    expected = seen.pop(_key(locked_obj), None)
    request.session[SESSION_KEY] = seen
    if expected is None:
        return (f'This {noun} form was opened too long ago, or in another browser tab, so nothing was saved. '
                'It has been reloaded with the latest values - please check them and save again.')
    if expected != locked_obj.version:
        stamp = getattr(locked_obj, 'updated_at', None)
        when = f'at {timezone.localtime(stamp):%d %b %Y %H:%M} ' if stamp else ''
        return (f'Someone else saved this {noun} {when}while you were editing it. Your changes were NOT saved. '
                'The form now shows the latest saved values - please check them and make your change again.')
    return None