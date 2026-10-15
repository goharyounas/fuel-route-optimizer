"""Django settings for the Fuel Route Optimizer API.

Every value that differs between environments is read from an environment
variable, with defaults that work out of the box for local development.
"""

import os
from pathlib import Path

from django.core.management.utils import get_random_secret_key

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


DEBUG = env_bool("DJANGO_DEBUG", False)
# The API keeps no sessions or signed data, so a missing key only matters for the admin.
# Fall back to a random per-process key rather than a well-known one.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or get_random_secret_key()
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0,[::1]")
# Send our origin (not the full URL) to other sites. OpenStreetMap's tile servers reject
# map tile requests that carry no Referer, which Django's default "same-origin" withholds.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "fuel_planner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

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

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_PATH", BASE_DIR / "db.sqlite3"),
    }
}

# Plans and upstream routing responses are cached so repeated requests are served
# without calling the free routing API again. Set REDIS_URL to share the cache
# between worker processes (requires the `redis` package).
if os.environ.get("REDIS_URL"):
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": os.environ["REDIS_URL"],
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "fuel-route-optimizer",
            "OPTIONS": {"MAX_ENTRIES": 2000},
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MAILERS = {
    "default": {
        "BACKEND": "django.core.mail.backends.console.EmailBackend",
    },
}

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        *(["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    ],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    # Public API without sessions, so POST clients never need a CSRF token.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "EXCEPTION_HANDLER": "fuel_planner.exceptions.api_exception_handler",
    # Protects the free upstream routing service from being hammered through this API.
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": os.environ.get("API_THROTTLE_RATE", "120/minute")},
}

FUEL_PLANNER = {
    # Vehicle model given by the assignment.
    "VEHICLE_RANGE_MILES": env_float("VEHICLE_RANGE_MILES", 500.0),
    "VEHICLE_MPG": env_float("VEHICLE_MPG", 10.0),
    # The trip is fuel neutral: it starts with this much range in the tank and must end
    # with the same, so the plan buys exactly the fuel the trip burns.
    "FUEL_RESERVE_MILES": env_float("FUEL_RESERVE_MILES", 50.0),
    # Cost charged per fuel stop when choosing stops (roughly 15 minutes of a driver's
    # time). It stops the optimiser from detouring to save a few cents. Clients can
    # override it per request; 0 gives the absolute cheapest fuel bill.
    "STOP_COST_USD": env_float("STOP_COST_USD", 15.0),
    # A station counts as "on the route" when its location is within this distance of
    # the route line. Station coordinates are city-level, so this absorbs the offset
    # between a town's centre and the highway exit the truck stop sits on.
    "ROUTE_CORRIDOR_MILES": env_float("ROUTE_CORRIDOR_MILES", 5.0),
    # Spacing used when sampling the route for the nearest-point search.
    "ROUTE_SAMPLE_SPACING_MILES": env_float("ROUTE_SAMPLE_SPACING_MILES", 0.5),
    # Douglas-Peucker tolerance for the route geometry returned to clients.
    "MAP_SIMPLIFY_TOLERANCE_METERS": env_float("MAP_SIMPLIFY_TOLERANCE_METERS", 25.0),
    # Free, key-less upstream services. Point these at self-hosted instances in production.
    "OSRM_BASE_URL": os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org"),
    "NOMINATIM_BASE_URL": os.environ.get("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org"),
    "HTTP_TIMEOUT_SECONDS": env_float("HTTP_TIMEOUT_SECONDS", 20.0),
    "HTTP_USER_AGENT": os.environ.get("HTTP_USER_AGENT", "fuel-route-optimizer/1.0 (Django take-home API)"),
    "ROUTE_CACHE_SECONDS": int(env_float("ROUTE_CACHE_SECONDS", 24 * 3600)),
    "GEOCODE_CACHE_SECONDS": int(env_float("GEOCODE_CACHE_SECONDS", 30 * 24 * 3600)),
    "PLAN_CACHE_SECONDS": int(env_float("PLAN_CACHE_SECONDS", 3600)),
    # Data files (see README "Data preparation").
    "FUEL_PRICES_CSV": DATA_DIR / "fuel_prices.csv",
    "STATION_LOCATIONS_CSV": DATA_DIR / "station_locations.csv",
    "PLACES_CSV": DATA_DIR / "us_places.csv",
    "US_BOUNDARY_GEOJSON": DATA_DIR / "us_boundary.geojson",
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "loggers": {
        "fuel_planner": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
    },
}
