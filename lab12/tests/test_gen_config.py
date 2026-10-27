"""Unit-тесты app/config.py: Settings и его валидаторы, get_settings (без HTTP и БД)."""

from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.config import ALLOWED_DB_DRIVERS, JWT_SECRET_MIN_LENGTH, Settings, get_settings

GOOD_URL = "sqlite+aiosqlite:///test.db"
GOOD_SECRET = "s" * 32


def make(**override):
    """Settings только из переданных значений: .env не читается."""
    data = {"database_url": GOOD_URL, "jwt_secret": GOOD_SECRET}
    data.update(override)
    return Settings(_env_file=None, **data)


def fields_of(exc_info) -> list[str]:
    return [e["loc"][0] for e in exc_info.value.errors()]


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("DATABASE_URL", "JWT_SECRET", "JWT_TTL_MINUTES", "CLINIC_TIMEZONE"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def fresh_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# database_url
# --------------------------------------------------------------------------- #
# допустимо: postgresql+asyncpg, sqlite+aiosqlite; недопустимо: синхронные драйверы, другие СУБД, не URL, пусто


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://user:pw@localhost:5432/clinic",
        "sqlite+aiosqlite:///:memory:",
        "sqlite+aiosqlite:///C:/tmp/x.db",
        "postgresql+asyncpg://localhost/db",
    ],
    ids=["postgres", "sqlite_memory", "sqlite_file", "postgres_no_credentials"],
)
def test_database_url_async_driver_accepted(url):
    assert make(database_url=url).database_url.get_secret_value() == url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@h/db",
        "postgresql+psycopg2://u:p@h/db",
        "postgresql+psycopg://u:p@h/db",
        "sqlite://",
        "sqlite:///x.db",
        "mysql+aiomysql://u:p@h/db",
        "not a url",
        "",
        "postgresql+asyncpg",
        "http://example.com",
    ],
    ids=[
        "plain_postgres",
        "psycopg2",
        "psycopg3_sync",
        "sqlite_sync_memory",
        "sqlite_sync_file",
        "mysql",
        "garbage",
        "empty",
        "no_separator",
        "http",
    ],
)
def test_database_url_invalid_or_sync_driver_raises(url):
    with pytest.raises(ValidationError) as exc:
        make(database_url=url)
    assert fields_of(exc) == ["database_url"]


def test_database_url_error_message_for_sync_driver():
    with pytest.raises(ValidationError) as exc:
        make(database_url="postgresql://u:p@h/db")
    assert "must use an async driver" in exc.value.errors()[0]["msg"]


def test_database_url_error_message_for_malformed_url():
    with pytest.raises(ValidationError) as exc:
        make(database_url="not a url")
    assert "not a valid SQLAlchemy URL" in exc.value.errors()[0]["msg"]


def test_database_url_password_not_in_error_text():
    with pytest.raises(ValidationError) as exc:
        make(database_url="postgresql://user:SuperSecretPw@host/db")
    assert "SuperSecretPw" not in str(exc.value)


def test_allowed_drivers_constant():
    assert ALLOWED_DB_DRIVERS == {"postgresql+asyncpg", "sqlite+aiosqlite"}


# --------------------------------------------------------------------------- #
# jwt_secret: длина >= 32
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "secret", ["s" * 32, "s" * 33, "s" * 200, "ключ" * 8], ids=["32", "33", "200", "cyrillic_32_chars"]
)
def test_jwt_secret_long_enough_accepted(secret):
    assert make(jwt_secret=secret).jwt_secret.get_secret_value() == secret


@pytest.mark.parametrize("secret", ["", "s", "s" * 31], ids=["empty", "one", "31"])
def test_jwt_secret_too_short_raises(secret):
    with pytest.raises(ValidationError) as exc:
        make(jwt_secret=secret)
    assert fields_of(exc) == ["jwt_secret"]
    assert f"at least {JWT_SECRET_MIN_LENGTH} characters" in exc.value.errors()[0]["msg"]


def test_jwt_secret_value_not_in_error_text_or_repr():
    weak = "weak-secret-value"
    with pytest.raises(ValidationError) as exc:
        make(jwt_secret=weak)
    assert weak not in str(exc.value)
    settings = make()
    assert GOOD_SECRET not in repr(settings)
    assert GOOD_SECRET not in str(settings)


# --------------------------------------------------------------------------- #
# jwt_ttl_minutes: 1..1440, по умолчанию 60
# --------------------------------------------------------------------------- #


def test_jwt_ttl_default_is_60(clean_env):
    assert make().jwt_ttl_minutes == 60


@pytest.mark.parametrize("ttl", [1, 2, 60, 1439, 1440])
def test_jwt_ttl_in_range_accepted(ttl):
    assert make(jwt_ttl_minutes=ttl).jwt_ttl_minutes == ttl


@pytest.mark.parametrize(
    "ttl", [0, -1, 1441, 100_000, "abc", "60.5", None], ids=["0", "-1", "1441", "huge", "text", "float_text", "none"]
)
def test_jwt_ttl_out_of_range_or_invalid_raises(ttl):
    with pytest.raises(ValidationError) as exc:
        make(jwt_ttl_minutes=ttl)
    assert fields_of(exc) == ["jwt_ttl_minutes"]


# --------------------------------------------------------------------------- #
# clinic_timezone
# --------------------------------------------------------------------------- #


def test_clinic_timezone_default_is_moscow(clean_env):
    settings = make()
    assert settings.clinic_timezone == "Europe/Moscow"
    assert settings.clinic_tz == ZoneInfo("Europe/Moscow")


@pytest.mark.parametrize(
    "name", ["Europe/Moscow", "UTC", "Asia/Kolkata", "America/New_York", "Europe/Berlin", "Asia/Vladivostok"]
)
def test_clinic_timezone_known_zone_accepted(name):
    settings = make(clinic_timezone=name)
    assert settings.clinic_timezone == name
    assert settings.clinic_tz.key == name


@pytest.mark.parametrize(
    "name",
    ["Mars/Phobos", "", "../etc/passwd", "/etc/localtime", "Europe/Moscow\n", "Moscow", "Europe/Moskva", " "],
    ids=["unknown", "empty", "path_traversal", "absolute_path", "trailing_newline", "city_only", "typo", "blank"],
)
def test_clinic_timezone_unknown_raises(name):
    with pytest.raises(ValidationError) as exc:
        make(clinic_timezone=name)
    assert fields_of(exc) == ["clinic_timezone"]
    assert "unknown IANA time zone" in exc.value.errors()[0]["msg"]


# --------------------------------------------------------------------------- #
# обязательные поля, окружение, .env, extra
# --------------------------------------------------------------------------- #


def test_missing_database_url_raises(clean_env):
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, jwt_secret=GOOD_SECRET)
    assert fields_of(exc) == ["database_url"]
    assert exc.value.errors()[0]["type"] == "missing"


def test_missing_jwt_secret_raises(clean_env):
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, database_url=GOOD_URL)
    assert fields_of(exc) == ["jwt_secret"]
    assert exc.value.errors()[0]["type"] == "missing"


def test_missing_both_required_fields_reports_both(clean_env):
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert sorted(fields_of(exc)) == ["database_url", "jwt_secret"]


def test_all_errors_reported_together(clean_env):
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None, database_url="mysql://x", jwt_secret="x", jwt_ttl_minutes=0, clinic_timezone="Nowhere/Land"
        )
    assert sorted(fields_of(exc)) == ["clinic_timezone", "database_url", "jwt_secret", "jwt_ttl_minutes"]


def test_values_are_read_from_environment(clean_env):
    clean_env.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    clean_env.setenv("JWT_SECRET", "e" * 40)
    clean_env.setenv("JWT_TTL_MINUTES", "15")
    clean_env.setenv("CLINIC_TIMEZONE", "Asia/Kolkata")
    settings = Settings(_env_file=None)
    assert settings.database_url.get_secret_value() == "postgresql+asyncpg://u:p@h/db"
    assert settings.jwt_secret.get_secret_value() == "e" * 40
    assert settings.jwt_ttl_minutes == 15
    assert settings.clinic_timezone == "Asia/Kolkata"


def test_environment_value_is_validated(clean_env):
    clean_env.setenv("DATABASE_URL", GOOD_URL)
    clean_env.setenv("JWT_SECRET", "short")
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert fields_of(exc) == ["jwt_secret"]


def test_values_are_read_from_env_file(clean_env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={GOOD_URL}\nJWT_SECRET={'f' * 36}\nJWT_TTL_MINUTES=30\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)
    assert settings.jwt_secret.get_secret_value() == "f" * 36
    assert settings.jwt_ttl_minutes == 30


def test_environment_overrides_env_file(clean_env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={GOOD_URL}\nJWT_SECRET={'f' * 36}\nJWT_TTL_MINUTES=30\n", encoding="utf-8")
    clean_env.setenv("JWT_TTL_MINUTES", "45")
    assert Settings(_env_file=env_file).jwt_ttl_minutes == 45


def test_explicit_arguments_override_environment(clean_env):
    clean_env.setenv("JWT_TTL_MINUTES", "45")
    assert make(jwt_ttl_minutes=10).jwt_ttl_minutes == 10


def test_unknown_settings_are_ignored(clean_env):
    clean_env.setenv("SOMETHING_ELSE", "1")
    assert make(unexpected_option="x").jwt_ttl_minutes == 60


def test_clinic_tz_returns_zoneinfo_object():
    tz = make(clinic_timezone="Europe/Berlin").clinic_tz
    assert isinstance(tz, ZoneInfo)
    assert tz.key == "Europe/Berlin"


# --------------------------------------------------------------------------- #
# get_settings
# --------------------------------------------------------------------------- #


def test_get_settings_is_cached(fresh_cache):
    assert get_settings() is get_settings()


def test_get_settings_cache_clear_picks_up_new_environment(fresh_cache, monkeypatch):
    monkeypatch.setenv("JWT_TTL_MINUTES", "11")
    get_settings.cache_clear()
    assert get_settings().jwt_ttl_minutes == 11
    monkeypatch.setenv("JWT_TTL_MINUTES", "12")
    assert get_settings().jwt_ttl_minutes == 11  # из кеша
    get_settings.cache_clear()
    assert get_settings().jwt_ttl_minutes == 12


def test_get_settings_invalid_environment_raises(fresh_cache, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "too-short")
    with pytest.raises(ValidationError) as exc:
        get_settings()
    assert fields_of(exc) == ["jwt_secret"]


def test_get_settings_failure_is_not_cached(fresh_cache, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "too-short")
    with pytest.raises(ValidationError):
        get_settings()
    monkeypatch.setenv("JWT_SECRET", "z" * 40)
    assert get_settings().jwt_secret.get_secret_value() == "z" * 40
