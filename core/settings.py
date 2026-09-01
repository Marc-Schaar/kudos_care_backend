import os
from pathlib import Path
from celery.schedules import crontab
from dotenv import load_dotenv

load_dotenv()
if os.name == "nt":
    qgis_bin_path = r"C:\Program Files\QGIS 3.44.10\bin"

    os.environ["PATH"] = qgis_bin_path + os.pathsep + os.environ["PATH"]

    GDAL_LIBRARY_PATH = os.path.join(qgis_bin_path, "gdal312.dll")
    GEOS_LIBRARY_PATH = os.path.join(qgis_bin_path, "geos_c.dll")

STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET")
STRAVA_VERIFY_TOKEN = os.environ.get("STRAVA_VERIFY_TOKEN")

AI_PROVIDER = os.environ.get("AI_PROVIDER", "gemini")  # "gemini" | "groq"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# gemini-2.0-flash-lite wurde von Google abgeschaltet (HTTP 404 "no longer
# available", Google verweist selbst auf den Nachfolger) — nicht an einem
# ungueltigen Key gelegen, siehe `manage.py ai_diagnose`.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
# llama-3.3-70b-versatile ist aus Groqs Modell-Katalog verschwunden (HTTP 404
# "does not exist"). gpt-oss-20b bestaetigt per Diagnose: Freitext- und
# JSON-Modus laufen sauber innerhalb der bestehenden Token-Budgets (AI_MAX_
# OUTPUT_TOKENS=300 in ai_providers.py), inkl. seines Reasoning-Overheads.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")

# E-Mail-Versand (Benachrichtigungen, siehe app_notifications). Default = Brevo SMTP-Relay,
# aber vollstaendig env-gesteuert: ein spaeterer Wechsel auf einen eigenen Mailserver aendert
# nur .env-Werte, keinen Code.
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp-relay.brevo.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", 587))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "True") == "True"
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER)

# Basis-URL des Frontends fuer Links in E-Mails (z.B. "Zum Bike" in Warn-Mails).
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")

BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")

DEBUG = os.environ.get("DEBUG", "True") == "True"

ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost").split(",")

CORS_ALLOWED_ORIGINS = os.environ.get(
    "CORS_ALLOWED_ORIGINS", "http://localhost:3000"
).split(",")

CORS_ALLOW_CREDENTIALS = True

CSRF_TRUSTED_ORIGINS = os.environ.get(
    "CSRF_TRUSTED_ORIGINS", "http://localhost:3000"
).split(",")

CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SAMESITE = "Lax"

# Nur über HTTPS senden, wenn nicht im lokalen Dev-Modus – iOS/Safari (insb.
# Standalone-PWAs via "Zum Home-Bildschirm hinzufügen") verwirft nicht-Secure
# Cookies bei HTTPS-Origins teils sofort beim Neustart der App.
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SECURE = not DEBUG

# "Eingeloggt bleiben": Session überlebt Browser-/App-Neustart (Default eh
# False, hier explizit) und lebt lange; SAVE_EVERY_REQUEST lässt die
# Ablaufzeit bei Aktivität mitwandern statt starr 90 Tage ab Login abzulaufen.
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_AGE = 60 * 60 * 24 * 90  # 90 Tage
SESSION_SAVE_EVERY_REQUEST = True

CORS_ALLOW_HEADERS = [
    "accept",
    "authorization",
    "content-type",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]

# Application definition

INSTALLED_APPS = [
    "corsheaders",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "rest_framework",
    "app_auth",
    "app_dashboard",
    "app_strava_webhook",
    "app_maintenance",
    "app_notifications",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.contrib.gis.db.backends.postgis",
        "NAME": os.environ.get("DB_NAME"),
        "USER": os.environ.get("DB_USER"),
        "PASSWORD": os.environ.get("DB_PASSWORD"),
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": os.environ.get("DB_PORT", "5432"),
    }
}

# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = "static/"
STATIC_ROOT = os.path.join(BASE_DIR, "static")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
}


CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
# Sicherheitsnetz gegen hängende Tasks (z.B. durch Requests ohne Timeout):
# Soft-Limit wirft SoftTimeLimitExceeded (Task kann sauber aufräumen),
# Hard-Limit killt den Worker-Kindprozess danach unbedingt.
CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv("CELERY_TASK_SOFT_TIME_LIMIT", 480))
CELERY_TASK_TIME_LIMIT = int(os.getenv("CELERY_TASK_TIME_LIMIT", 600))

# Taegliche Wartungs-E-Mail-Checks (siehe app_notifications). Benoetigt einen laufenden
# "celery -A core beat"-Prozess zusaetzlich zum Worker.
MAINTENANCE_EMAIL_CHECK_HOUR = int(os.getenv("MAINTENANCE_EMAIL_CHECK_HOUR", 7))
CELERY_BEAT_SCHEDULE = {
    "check-component-warnings-daily": {
        "task": "app_notifications.tasks.check_component_warnings",
        "schedule": crontab(hour=MAINTENANCE_EMAIL_CHECK_HOUR, minute=0),
    },
    "check-bike-unsafe-predictions-daily": {
        "task": "app_notifications.tasks.check_bike_unsafe_predictions",
        "schedule": crontab(hour=MAINTENANCE_EMAIL_CHECK_HOUR, minute=15),
    },
}

STRAVA_SYNC_PAGE_SIZE = int(os.getenv("STRAVA_SYNC_PAGE_SIZE", 50))


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "file": {
            "level": "DEBUG",
            "class": "logging.FileHandler",
            "filename": os.path.join(BASE_DIR, "debug.log"),
        },
    },
    "loggers": {
        "my_app_debug": {
            "handlers": ["file"],
            "level": "DEBUG",
            "propagate": False,
        },
        "django.db.backends": {
            "level": "INFO",
            "handlers": ["file"],
        },
    },
}
