"""
Django settings for the NZDWorld storage / uploader service (my.nzdworld.com).

Design rules
------------
* Every deployment-specific value comes from the environment (``.env`` next to
  ``manage.py``), never from hardcoded literals.
* ``DEBUG`` defaults to **False**: this service stores files and serves signed
  URLs, so an accidental debug deployment is a data-leak, not a cosmetic bug.
* This project is deliberately independent of ``djangoblog2``: no shared
  database, no shared session, no shared media root. The two apps talk over
  HTTPS only.

Verified hosting facts this file is written against (see PHASE1_AUDIT.md):
    Python 3.11.10, Django 5.2 LTS, MariaDB 10.11, LiteSpeed 6.3.7,
    Passenger apps under ~/virtualenv/repositories/<app>/3.11.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# .env is optional locally, mandatory in production (no secret fallbacks that
# could silently ship a known key to a live site).
load_dotenv(BASE_DIR / ".env")

RUNNING_TESTS = "test" in sys.argv or os.getenv("DJANGO_ENV") == "test"


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "true" if default else "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = env("SECRET_KEY")
if not SECRET_KEY:
    if env_bool("DEBUG") or RUNNING_TESTS:
        # Local development / test runs only. Never reached in production
        # because DJANGO_ENV=production and DEBUG=False there.
        SECRET_KEY = "dev-only-insecure-key-never-use-in-production"
    else:
        raise RuntimeError(
            "SECRET_KEY is not set. Create .env next to manage.py with a long "
            "random SECRET_KEY (and never commit it)."
        )

DEBUG = env_bool("DEBUG", False)

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "my.nzdworld.com")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", "https://my.nzdworld.com")

# The blog calls this service server-to-server; secrets live in env only.
BLOG_API_BASE_URL = env("BLOG_API_BASE_URL", "https://nzdworld.com").rstrip("/")
# Shared HMAC secret for blog <-> uploader API authentication.
API_SHARED_SECRET = env("API_SHARED_SECRET", "")
# Public origin of this service (used to build download-page / direct URLs).
SITE_URL = env("SITE_URL", "https://my.nzdworld.com").rstrip("/")

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "uploader",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
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

# ---------------------------------------------------------------------------
# Database — own database, never the blog's
# ---------------------------------------------------------------------------
if env("DB_NAME") and env("DB_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": env("DB_NAME"),
            "USER": env("DB_USER"),
            "PASSWORD": env("DB_PASSWORD"),
            "HOST": env("DB_HOST"),
            "PORT": env("DB_PORT", "3306"),
            "CONN_MAX_AGE": 60,
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------
# Video bytes never live inside a document root. On the server this is
# /home/jaraflix/videos (outside every docroot); locally it is ./storage.
VIDEO_ROOT = Path(env("VIDEO_ROOT", str(BASE_DIR / "storage" / "protected"))).resolve()
TEMP_ROOT = Path(env("TEMP_ROOT", str(BASE_DIR / "storage" / "tmp"))).resolve()
# Byte budget reported by the dashboard: the account's own usage ceiling, which
# is NOT the same as df() on a shared volume.
STORAGE_BUDGET_BYTES = env_int("STORAGE_BUDGET_BYTES", 50 * 1024**3)

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if not DEBUG
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}
WHITENOISE_MAX_AGE = 31536000
WHITENOISE_MANIFEST_STRICT = False

# ---------------------------------------------------------------------------
# Security — this service must never ship with these off
# ---------------------------------------------------------------------------
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", not DEBUG)
SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 0 if DEBUG else 31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_HTTPONLY = False  # Django admin needs it readable by JS.
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"

# Passenger/LiteSpeed terminate TLS in the web server. Set this only when the
# proxy is known to strip/override the client's own header.
if env_bool("TRUST_PROXY_HTTPS", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Cookies are host-only for my.nzdworld.com: this app's premium session must
# never be readable by nzdworld.com or any sibling subdomain.
SESSION_COOKIE_NAME = "nzdv_sessionid"
CSRF_COOKIE_NAME = "nzdv_csrftoken"

# This app never accepts large browser uploads (imports happen server-side).
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024

# ---------------------------------------------------------------------------
# Caching / logging
# ---------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "nzdworld-uploader",
        "TIMEOUT": 300,
    }
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "uploader": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO"), "propagate": False},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
}

# ---------------------------------------------------------------------------
# Uploader behaviour (defaults; live values come from uploader.SiteSetting)
# ---------------------------------------------------------------------------
UPLOADER = {
    "MAX_CONCURRENT_JOBS": env_int("UPLOADER_MAX_CONCURRENT_JOBS", 1),
    "QUEUE_MAX_ATTEMPTS": env_int("UPLOADER_QUEUE_MAX_ATTEMPTS", 3),
    "WORKER_STALE_SECONDS": env_int("UPLOADER_WORKER_STALE_SECONDS", 600),
    "JOB_HEARTBEAT_SECONDS": env_int("UPLOADER_JOB_HEARTBEAT_SECONDS", 10),
    "DEFAULT_TOKEN_TTL_MINUTES": env_int("UPLOADER_TOKEN_TTL_MINUTES", 30),
    "STORAGE_RESERVE_BYTES": env_int("UPLOADER_STORAGE_RESERVE_BYTES", 2 * 1024**3),
}

# ---------------------------------------------------------------------------
# Media tooling — FFmpeg 7.0.2-static, installed by the account owner
# ---------------------------------------------------------------------------
# Absolute paths only. A Passenger app does not inherit an interactive shell's
# PATH, so a bare "ffmpeg" can resolve differently (or not at all) under the web
# server. Verified on the server: /home/jaraflix/bin/{ffmpeg,ffprobe}.
FFMPEG_BINARY = env("FFMPEG_BINARY", "/home/jaraflix/bin/ffmpeg")
FFPROBE_BINARY = env("FFPROBE_BINARY", "/home/jaraflix/bin/ffprobe")

# When True an import fails if FFmpeg/FFprobe cannot run. Production sets it
# True: silently storing un-branded files is worse than a visible failure.
#
# During the test suite this is forced False *regardless of .env*. Tests must be
# hermetic: a machine with FFmpeg, a production .env and no FFmpeg must run the
# same suite and get the same result. (This was a real bug: the server's
# MEDIA_TOOLING_REQUIRED=True made the pipeline tests call real ffprobe on fake
# bytes and fail, while the same tests passed locally.)
MEDIA_TOOLING_REQUIRED = False if RUNNING_TESTS else env_bool("MEDIA_TOOLING_REQUIRED", True)

# Hard limits so one corrupt file cannot occupy a cron worker indefinitely.
FFPROBE_TIMEOUT_SECONDS = env_int("FFPROBE_TIMEOUT_SECONDS", 60)
FFMPEG_TIMEOUT_SECONDS = env_int("FFMPEG_TIMEOUT_SECONDS", 600)
THUMBNAIL_TIMEOUT_SECONDS = env_int("THUMBNAIL_TIMEOUT_SECONDS", 120)

# Poster frames.
THUMBNAIL_WIDTH = env_int("THUMBNAIL_WIDTH", 640)
THUMBNAIL_SUBDIR = env("THUMBNAIL_SUBDIR", "thumbs")

# Re-encoding is OFF unless an administrator explicitly enables it: normal
# imports stream-copy (-c copy) and never lose quality.
ALLOW_TRANSCODE = env_bool("ALLOW_TRANSCODE", False)
TRANSCODE_CRF = env_int("TRANSCODE_CRF", 20)
TRANSCODE_PRESET = env("TRANSCODE_PRESET", "veryfast")

# ---------------------------------------------------------------------------
# Delivery and premium
# ---------------------------------------------------------------------------
SITE_NAME = env("SITE_NAME", "NZDWorld")

# "django" streams through the app with full Range support (the default, and the
# only mode verified on this host). "litespeed" emits X-LiteSpeed-Location and is
# OFF unless LITESPEED_INTERNAL_REDIRECT is explicitly True, because that
# mechanism is unproven under the Python SAPI on shared LiteSpeed hosting.
DELIVERY_MODE = env("DELIVERY_MODE", "django")
LITESPEED_INTERNAL_REDIRECT = env_bool("LITESPEED_INTERNAL_REDIRECT", False)

# Blog-issued premium handoff (URL) and the uploader's own session cookie.
PREMIUM_HANDOFF_MAX_AGE_SECONDS = env_int("PREMIUM_HANDOFF_MAX_AGE_SECONDS", 300)
PREMIUM_SESSION_TTL_SECONDS = env_int("PREMIUM_SESSION_TTL_SECONDS", 1800)

# A crashed worker must not block the queue forever; the lock is taken over after this.
WORKER_LOCK_STALE_SECONDS = env_int("WORKER_LOCK_STALE_SECONDS", 1800)

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
