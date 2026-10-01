"""
Django settings for the EdgePro Payroll project.

Rewritten for production-readiness: every environment-sensitive value is
read from an environment variable with a safe local-dev default, so the
exact same settings module works unchanged on Render (or any host) once
the corresponding env vars are set.
"""
import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ('1', 'true', 'yes', 'on')


# ---------------------------------------------------------------------------
# Core / security
# ---------------------------------------------------------------------------

# Render sets RENDER=true on every service. Locally (no RENDER variable) we
# default to DEBUG=True so http://127.0.0.1:8000 works: previously DEBUG
# defaulted to False locally, which forced Secure-only CSRF/session cookies
# over plain http and caused "CSRF verification failed" on login.
ON_RENDER = bool(os.environ.get('RENDER'))
DEBUG = env_bool('DJANGO_DEBUG', default=not ON_RENDER)

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured('DJANGO_SECRET_KEY must be set when DEBUG is off (production).')
    SECRET_KEY = 'django-insecure-local-dev-only-do-not-use-in-production'

ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h.strip()
]

RENDER_EXTERNAL_HOSTNAME = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
if RENDER_EXTERNAL_HOSTNAME:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)

CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
]
if RENDER_EXTERNAL_HOSTNAME:
    CSRF_TRUSTED_ORIGINS.append(f'https://{RENDER_EXTERNAL_HOSTNAME}')

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'payroll_app',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'payroll_app.context_processors.role_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# Uses DATABASE_URL when present (e.g. MySQL/Postgres on Render):
#   mysql://user:password@host:3306/dbname
#   postgres://user:password@host:5432/dbname
# Falls back to local SQLite for development, matching the pre-existing setup.

DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL:
    DATABASES = {
        'default': dj_database_url.parse(DATABASE_URL, conn_max_age=600, conn_health_checks=True)
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

AUTH_USER_MODEL = 'payroll_app.User'

AUTHENTICATION_BACKENDS = ['payroll_app.backends.EmailOrUsernameBackend']

# Password-reset links expire after 1 hour (Django default is 3 days) and are
# single-use: the token is bound to the password hash, so it dies once used.
PASSWORD_RESET_TIMEOUT = 60 * 60

# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'post_login_redirect'
LOGOUT_REDIRECT_URL = 'login'

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static / media files
# ---------------------------------------------------------------------------

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static'] if (BASE_DIR / 'static').exists() else []
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        # Manifest storage needs `collectstatic`; locally (DEBUG) use the plain
        # backend so runserver works without collecting first.
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage' if DEBUG
        else 'config.storage.TolerantManifestStaticFilesStorage',
    },
}

MEDIA_URL = 'media/'
# On Render set MEDIA_ROOT to a persistent disk mount (e.g. /var/data/media);
# the default container filesystem is wiped on every deploy.
MEDIA_ROOT = Path(os.environ.get('MEDIA_ROOT', BASE_DIR / 'media'))

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
# Falls back to the console backend so the app never crashes trying to send
# mail when SMTP isn't configured locally — notifications are logged instead
# of raising an unhandled exception.

EMAIL_BACKEND = os.environ.get(
    'DJANGO_EMAIL_BACKEND',
    'django.core.mail.backends.console.EmailBackend' if DEBUG else 'django.core.mail.backends.smtp.EmailBackend',
)
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', default=True)
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'no-reply@edgepro-payroll.local')

# Company sign-up email OTP. OFF by default: registration is one step and no
# email is needed. Set REQUIRE_EMAIL_VERIFICATION=true once real email works.
REQUIRE_EMAIL_VERIFICATION = env_bool('REQUIRE_EMAIL_VERIFICATION', default=False)
EMAIL_TIMEOUT = int(os.environ.get('EMAIL_TIMEOUT', '10'))  # never hang a request on a dead SMTP server

# Demo-request alert recipients (not secrets; override per environment).
# Admin/host address for demo-request and registration alerts (set on Render).
DEMO_NOTIFY_EMAIL = os.environ.get('ADMIN_NOTIFICATION_EMAIL') or os.environ.get('DEMO_NOTIFY_EMAIL', '')
DEMO_NOTIFY_MOBILE = os.environ.get('DEMO_NOTIFY_MOBILE', '')

# SMS: 'fast2sms' or 'twilio'. Empty = SMS disabled (logged, never crashes).
SMS_PROVIDER = os.environ.get('SMS_PROVIDER', '')
FAST2SMS_API_KEY = os.environ.get('FAST2SMS_API_KEY', '')
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_FROM_NUMBER = os.environ.get('TWILIO_FROM_NUMBER', '')

# If SMTP is required but not configured, fall back to console so the
# request completes instead of raising a 500.
if EMAIL_BACKEND.endswith('smtp.EmailBackend') and not EMAIL_HOST:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# ---------------------------------------------------------------------------
# Session / cookie / transport security
# ---------------------------------------------------------------------------

SESSION_COOKIE_AGE = 60 * 60 * 8  # 8 hours
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False  # must remain readable by the CSRF JS if used; token itself isn't the session
X_FRAME_OPTIONS = 'DENY'

SECURE_SSL_REDIRECT = env_bool('DJANGO_SECURE_SSL_REDIRECT', default=not DEBUG)
SECURE_REFERRER_POLICY = 'same-origin'
SESSION_COOKIE_SECURE = env_bool('DJANGO_SESSION_COOKIE_SECURE', default=not DEBUG)
CSRF_COOKIE_SECURE = env_bool('DJANGO_CSRF_COOKIE_SECURE', default=not DEBUG)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') if not DEBUG else None
SECURE_HSTS_SECONDS = 0 if DEBUG else 60 * 60 * 24 * 30
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = False  # opt in deliberately once the domain is final
SECURE_CONTENT_TYPE_NOSNIFF = True

# ---------------------------------------------------------------------------
# File upload validation
# ---------------------------------------------------------------------------

FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024  # 5 MB
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = ['.pdf', '.png', '.jpg', '.jpeg']

# ---------------------------------------------------------------------------
# Logging — errors are recorded rather than silently swallowed or crashing
# the process with no trace.
# ---------------------------------------------------------------------------

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {'format': '[{asctime}] {levelname} {name}: {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'},
    },
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {
        'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False},
        'payroll_app': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
    },
}


# ---------------------------------------------------------------------------
# Private object storage for uploads (BUG-07)
# Render's disk is wiped on every deploy, so uploaded files (receipts, proofs,
# documents) go to a PRIVATE S3-compatible bucket (Cloudflare R2 or AWS S3)
# when STORAGE_BUCKET is set. Credentials come only from environment variables.
# ---------------------------------------------------------------------------
STORAGE_BUCKET = os.environ.get('STORAGE_BUCKET', '')
if STORAGE_BUCKET:
    STORAGES['default'] = {
        'BACKEND': 'storages.backends.s3.S3Storage',
        'OPTIONS': {
            'bucket_name': STORAGE_BUCKET,
            'endpoint_url': os.environ.get('STORAGE_ENDPOINT_URL') or None,   # R2: https://<account-id>.r2.cloudflarestorage.com
            'access_key': os.environ.get('STORAGE_ACCESS_KEY_ID', ''),
            'secret_key': os.environ.get('STORAGE_SECRET_ACCESS_KEY', ''),
            'region_name': os.environ.get('STORAGE_REGION', 'auto'),
            'default_acl': None,          # objects stay private (no public-read)
            'querystring_auth': True,     # any generated URL is signed ...
            'querystring_expire': 300,    # ... and expires after 5 minutes
            'file_overwrite': False,
            'location': 'private',
            'signature_version': 's3v4',  # required by Cloudflare R2, standard for AWS
        },
    }
    if os.environ.get('STORAGE_ENDPOINT_URL'):
        STORAGES['default']['OPTIONS']['addressing_style'] = 'path'   # R2 endpoints use path-style URLs
