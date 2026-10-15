import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-local-development-key" if DEBUG else "")
if not SECRET_KEY:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be supplied through server Secrets")
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
CSRF_TRUSTED_ORIGINS = list(filter(None, os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")))
INSTALLED_APPS = [
    "unfold",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.github",
    "allauth.socialaccount.providers.gitlab",
    "providers",
    "providers.gitee",
    "django_celery_beat",
    "core",
    "repositories",
    "bulletins",
    "directory",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "core.middleware.ProxyHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "core.middleware.AccountReadinessMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.site_context",
                "bulletins.context_processors.announcements",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "namelist"),
        "USER": os.getenv("POSTGRES_USER", "namelist"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
        "HOST": os.getenv("POSTGRES_HOST", "db"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}
if os.getenv("DATABASE_SQLITE", "0") == "1":
    (BASE_DIR / "var").mkdir(exist_ok=True)
    DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "var" / "local.sqlite3"}
    }
AUTH_USER_MODEL = "core.User"
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
SITE_ID = 1
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "username*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_ADAPTER = "core.adapters.AccountAdapter"
ACCOUNT_FORMS = {"signup": "core.forms.SignupForm", "login": "core.forms.LoginForm"}
ACCOUNT_LOGOUT_ON_GET = False
ACCOUNT_RATE_LIMITS = {"login": "10/m/ip", "login_failed": "5/m/ip,5/5m/key", "signup": "5/h/ip"}
# Enable only when a controlled external entry overwrites these headers and
# direct application access is restricted. Forwarded address chains are never trusted.
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "0") == "1"
ALLAUTH_TRUSTED_PROXY_COUNT = 0
ALLAUTH_TRUSTED_CLIENT_IP_HEADER = "x-real-ip" if TRUST_PROXY_HEADERS else None
RATELIMIT_IP_META_KEY = "HTTP_X_REAL_IP" if TRUST_PROXY_HEADERS else "REMOTE_ADDR"
SOCIALACCOUNT_ADAPTER = "providers.adapters.SocialAdapter"
SOCIALACCOUNT_STORE_TOKENS = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = False
SOCIALACCOUNT_LOGIN_ON_GET = False
SOCIALACCOUNT_AUTO_SIGNUP = False
SOCIALACCOUNT_EMAIL_VERIFICATION = "mandatory"
SOCIALACCOUNT_FORMS = {"signup": "core.forms.SocialSignupForm"}
SOCIALACCOUNT_PROVIDERS = {
    "github": {"SCOPE": ["read:user", "user:email"]},
    "gitlab": {"SCOPE": ["read_user"]},
}
LOGIN_REDIRECT_URL = "/account/profile/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/"
EMAIL_BACKEND = "core.mail.RuntimeEmailBackend"
DEFAULT_FROM_EMAIL = "noreply@localhost"
LANGUAGE_CODE = "zh-hans"
LANGUAGES = [("zh-hans", "简体中文"), ("en", "English")]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if TRUST_PROXY_HEADERS else None
SESSION_COOKIE_SAMESITE = "Lax"
APP_ENCRYPTION_KEY = os.getenv("APP_ENCRYPTION_KEY", "")
BOOTSTRAP_SECRET = os.getenv("BOOTSTRAP_SECRET", "")
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "http://localhost:8000")
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "https" if SITE_BASE_URL.startswith("https://") else "http"
SOURCE_REPOSITORY_URL = os.getenv("SOURCE_REPOSITORY_URL", "")
WORK_ROOT = Path(os.getenv("WORK_ROOT", str(BASE_DIR / "var" / "work")))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL.replace("/0", "/1"),
    }
}
if os.getenv("LOCAL_MEMORY_CACHE", "0") == "1":
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL.replace("/0", "/2")
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_ANNOTATIONS = {
    "repositories.tasks.audit_submission": {"soft_time_limit": 6600, "time_limit": 7200},
    "core.tasks.sync_license_policy": {"soft_time_limit": 6600, "time_limit": 7200},
    "core.tasks.restore_drill": {"soft_time_limit": 18030, "time_limit": 18300},
}
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 21600}
CELERY_RESULT_EXPIRES = 86400
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_TASK_ROUTES = {
    "repositories.tasks.audit_submission": {"queue": "scan"},
    "core.tasks.sync_license_policy": {"queue": "scan"},
    "core.tasks.restore_drill": {"queue": "maintenance"},
}
CELERY_TASK_ALWAYS_EAGER = os.getenv("CELERY_TASK_ALWAYS_EAGER", "0") == "1"
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_PUBLISH_RETRY = False
CELERY_TIMEZONE = TIME_ZONE
UNFOLD = {"SITE_TITLE": "南京工业大学开源软件仓库名录", "SITE_HEADER": "开源软件仓库名录"}
