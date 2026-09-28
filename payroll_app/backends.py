"""Authentication backend: log in with username OR email (case-insensitive),
and refuse non-admin users whose company is not APPROVED (pending, rejected
or suspended) even if their account row is still active."""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


def company_allows_login(user):
    if user.is_superuser or user.role == 'ADMIN':
        return True
    return user.company is not None and user.company.status == 'APPROVED'


class EmailOrUsernameBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
        if username is None:
            username = kwargs.get(UserModel.USERNAME_FIELD)
        if not username or not password:
            return None
        ident = username.strip()
        candidates = list(UserModel._default_manager.filter(
            Q(username__iexact=ident) | Q(email__iexact=ident)
        )[:2])
        if len(candidates) != 1:
            # Unknown, or ambiguous (same email on two accounts): run the
            # hasher anyway to keep timing uniform, then fail.
            UserModel().set_password(password)
            return None
        user = candidates[0]
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

    def user_can_authenticate(self, user):
        return super().user_can_authenticate(user) and user.email_verified and company_allows_login(user)
