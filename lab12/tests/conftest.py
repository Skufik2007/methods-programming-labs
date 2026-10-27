"""Общие фикстуры тестов.

Переменные окружения задаются до импорта ``app``: engine создаётся при импорте
``app.db``, а настройки без DATABASE_URL и JWT_SECRET не загружаются.
"""

import itertools
import os
import tempfile
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, time
from pathlib import Path

# По умолчанию — временный файл SQLite; TEST_DATABASE_URL позволяет прогнать те же тесты
# на PostgreSQL (так делает CI): блокировки FOR UPDATE и частичные индексы там настоящие.
_DB_DIR = Path(tempfile.mkdtemp(prefix="clinic-tests-"))
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", f"sqlite+aiosqlite:///{(_DB_DIR / 'test.db').as_posix()}"
)
os.environ["JWT_SECRET"] = "tests-only-secret-0123456789abcdef"
os.environ["CLINIC_TIMEZONE"] = "Europe/Moscow"

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import db, models  # noqa: E402
from app.main import app  # noqa: E402
from app.routers.appointments import get_now  # noqa: E402
from app.security import create_access_token, hash_password  # noqa: E402

# Понедельник 12.10.2026, 09:00 по Москве: «текущий момент» во всех тестах.
FROZEN_NOW = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)
PASSWORD = "Password123"
_PASSWORD_HASH = hash_password(PASSWORD)  # bcrypt медленный: один хэш на все тесты
_email_seq = itertools.count(1)


@pytest.fixture(autouse=True)
async def _schema() -> AsyncIterator[None]:
    """Чистая схема БД на каждый тест.

    После теста пул соединений закрывается: у каждого теста свой event loop, а соединение
    asyncpg, открытое в одном loop, нельзя использовать в другом.
    """
    async with db.engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.drop_all)
        await conn.run_sync(models.Base.metadata.create_all)
    yield
    await db.engine.dispose()


@pytest.fixture
def now() -> datetime:
    return FROZEN_NOW


@pytest.fixture
async def client(now: datetime) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP-клиент к приложению; необработанная ошибка приходит как ответ 500, а не исключение."""
    app.dependency_overrides[get_now] = lambda: now
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def session() -> AsyncIterator[db.AsyncSession]:
    async with db.SessionFactory() as s:
        yield s


@pytest.fixture
def make_user(session: db.AsyncSession) -> Callable[..., object]:
    """Создаёт пользователя с ролью и возвращает его (пароль у всех — ``PASSWORD``)."""

    async def _make(role: models.UserRole, email: str | None = None, **fields: object) -> models.User:
        user = models.User(
            email=email or f"{role.value}{next(_email_seq)}@clinic.test",
            password_hash=_PASSWORD_HASH,
            full_name=f"Тест {role.value}",
            role=role,
            **fields,
        )
        session.add(user)
        await session.commit()
        return user

    return _make


@pytest.fixture
def make_doctor(session: db.AsyncSession, make_user: Callable[..., object]) -> Callable[..., object]:
    """Врач с графиком по умолчанию: пн-пт, 09:00-18:00 по Москве, слоты по 30 минут."""

    async def _make(**fields: object) -> models.Doctor:
        user = await make_user(models.UserRole.DOCTOR)
        params: dict[str, object] = {
            "specialty": "Терапевт",
            "cabinet": "101",
            "slot_minutes": 30,
            "work_start": time(9),
            "work_end": time(18),
            "work_days": [1, 2, 3, 4, 5],
        }
        params.update(fields)
        doctor = models.Doctor(user_id=user.id, **params)
        session.add(doctor)
        await session.commit()
        return doctor

    return _make


@pytest.fixture
def auth() -> Callable[[models.User], dict[str, str]]:
    """Заголовок Authorization для пользователя."""

    def _auth(user: models.User) -> dict[str, str]:
        return {"Authorization": f"Bearer {create_access_token(user.id, user.role, version=user.token_version)}"}

    return _auth
