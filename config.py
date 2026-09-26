import os

DEV_SECRET_KEY = "dev-secret-change-me-in-production"


class Config:
    # Connect as an ordinary, non-superuser role — Postgres Row-Level
    # Security is bypassed entirely by superusers and by roles with
    # BYPASSRLS, so RLS has zero effect if the app connects as postgres.
    # Some managed Postgres hosts normalize the URL scheme to
    # "postgres://" (SQLAlchemy/psycopg2 want "postgresql://") -- handled
    # below rather than assuming the platform gives the right one.
    _raw_db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://acquiral_app:acquiral_app_pw@localhost/acquiral_dev",
    )
    if _raw_db_url.startswith("postgres://"):
        _raw_db_url = _raw_db_url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif _raw_db_url.startswith("postgresql://"):
        _raw_db_url = _raw_db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    SQLALCHEMY_DATABASE_URI = _raw_db_url

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET_KEY)
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Behind a cloud platform's reverse proxy/load balancer, the request
    # Flask sees is plain HTTP even though the browser used HTTPS --
    # ProxyFix (wired up in app/__init__.py when this is True) restores
    # the real scheme from X-Forwarded-Proto so url_for(..., _external=True),
    # secure cookies, and Flask-Login redirects all still work correctly.
    BEHIND_PROXY = os.environ.get("BEHIND_PROXY", "false").lower() == "true"


class ProductionConfig(Config):
    """Selected via APP_ENV=production (see app/__init__.py)."""
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    BEHIND_PROXY = os.environ.get("BEHIND_PROXY", "true").lower() == "true"


CONFIG_BY_ENV = {"production": ProductionConfig, "development": Config}


def get_config():
    """Picks the config class for APP_ENV (defaults to development, same
    as always running locally). Refuses to return ProductionConfig with
    the default dev secret key rather than silently running an
    internet-facing deployment with a publicly-known session secret --
    every session/login/CSRF token would otherwise be forgeable by
    anyone who has read this file on GitHub."""
    cls = CONFIG_BY_ENV.get(os.environ.get("APP_ENV", "development").lower(), Config)
    if cls is ProductionConfig and cls.SECRET_KEY == DEV_SECRET_KEY:
        raise RuntimeError(
            "Refusing to start with APP_ENV=production and the default dev SECRET_KEY. "
            "Set a real SECRET_KEY environment variable, e.g.: "
            "python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return cls
