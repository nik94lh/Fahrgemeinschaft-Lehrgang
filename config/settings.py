"""Django settings for the isolated BMS development and test instances."""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.environ.get("BMS_DEBUG") == "1"
HTTPS = os.environ.get("BMS_HTTPS") == "1"
SECRET_KEY = os.environ.get("BMS_SECRET_KEY", "")
if HTTPS and (DEBUG or len(SECRET_KEY) < 50 or SECRET_KEY.startswith("local-")):
    raise ImproperlyConfigured("HTTPS requires debug off and a non-default secret of at least 50 characters.")
if not SECRET_KEY:
    SECRET_KEY = "local-insecure-development-only"

ALLOWED_HOSTS = os.environ.get("BMS_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
CSRF_TRUSTED_ORIGINS = [
    origin for origin in os.environ.get("BMS_CSRF_TRUSTED_ORIGINS", "").split(",") if origin
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": [
            "django.template.context_processors.request",
            "config.version.version_context",
        ]},
    },
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "bookings",
    "mailimport",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "config.middleware.ContentPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if os.environ.get("DB_HOST"):
    db_password = os.environ["DB_PASSWORD"]
    if HTTPS and (not db_password or db_password == "local-bms-password"):
        raise ImproperlyConfigured("HTTPS requires a non-default database password.")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["DB_NAME"],
            "USER": os.environ["DB_USER"],
            "PASSWORD": db_password,
            "HOST": os.environ["DB_HOST"],
            "PORT": os.environ.get("DB_PORT", "5432"),
        }
    }
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "de-de"
TIME_ZONE = "Europe/Berlin"
USE_I18N = True
USE_TZ = True
EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"

if HTTPS:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 3600
