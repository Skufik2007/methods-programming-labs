"""Настройки приложения из переменных окружения (и необязательного файла ``.env``).

``DATABASE_URL`` и ``JWT_SECRET`` не имеют значений по умолчанию: если любая из
них не задана или некорректна, ``get_settings()`` бросает ``pydantic.ValidationError``,
и приложение не стартует. Секретов в коде нет.
"""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

ALLOWED_DB_DRIVERS: frozenset[str] = frozenset({"postgresql+asyncpg", "sqlite+aiosqlite"})
JWT_SECRET_MIN_LENGTH = 32  # HS256: ключ не короче 256 бит


class Settings(BaseSettings):
    """Конфигурация приложения. Имена переменных окружения - имена полей в верхнем регистре."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,  # значения секретов не попадают в текст ошибок валидации
    )

    database_url: SecretStr
    jwt_secret: SecretStr
    jwt_ttl_minutes: int = Field(default=60, ge=1, le=1440)
    clinic_timezone: str = "Europe/Moscow"

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr) -> SecretStr:
        """URL должен быть корректным и указывать на асинхронный драйвер."""
        try:
            driver = make_url(value.get_secret_value()).drivername
        except ArgumentError as exc:
            raise ValueError("DATABASE_URL is not a valid SQLAlchemy URL") from exc
        if driver not in ALLOWED_DB_DRIVERS:
            raise ValueError("DATABASE_URL must use an async driver: postgresql+asyncpg or sqlite+aiosqlite")
        return value

    @field_validator("jwt_secret")
    @classmethod
    def _check_jwt_secret(cls, value: SecretStr) -> SecretStr:
        """Отсекает заведомо слабые секреты."""
        if len(value.get_secret_value()) < JWT_SECRET_MIN_LENGTH:
            raise ValueError(f"JWT_SECRET must be at least {JWT_SECRET_MIN_LENGTH} characters long")
        return value

    @field_validator("clinic_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        """Название часового пояса должно существовать в базе IANA."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError(f"unknown IANA time zone: {value!r}") from exc
        return value

    @property
    def clinic_tz(self) -> ZoneInfo:
        """Часовой пояс клиники как объект ``ZoneInfo`` (для перевода UTC <-> местное время)."""
        return ZoneInfo(self.clinic_timezone)


@lru_cache
def get_settings() -> Settings:
    """Возвращает единственный (кешированный) экземпляр настроек.

    В тестах после смены переменных окружения вызовите ``get_settings.cache_clear()``.
    """
    return Settings()  # type: ignore[call-arg]
