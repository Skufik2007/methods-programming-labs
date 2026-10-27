"""Безопасность: пароли (bcrypt), JWT (PyJWT, HS256) и FastAPI-зависимости авторизации.

* Пароли хэшируются bcrypt напрямую (без passlib). bcrypt учитывает только первые
  72 байта пароля, поэтому более длинные пароли отвергаются, а не обрезаются молча.
* Токен содержит поля ``sub`` (id пользователя строкой - PyJWT требует строку),
  ``role``, ``ver`` (версия токенов пользователя) и ``exp``. Алгоритм при проверке жёстко ограничен HS256.
* Источник истины о правах - БД: роль из токена сверяется с текущей ролью пользователя,
  неактивные пользователи, токены с устаревшей ролью и токены, выданные до смены
  пароля (``ver`` меньше ``User.token_version``), получают 401.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import User, UserRole, utc_now
from app.schemas import PASSWORD_MAX_BYTES

JWT_ALGORITHM = "HS256"
BCRYPT_ROUNDS = 12

bearer_scheme = HTTPBearer(auto_error=False)  # ошибки формируем сами: всегда 401 + WWW-Authenticate


class TokenError(Exception):
    """Токен отсутствует в нужной форме, повреждён, просрочен или содержит неверные поля."""


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """Проверенное содержимое access-токена."""

    user_id: int
    role: UserRole
    version: int
    expires_at: datetime


# --------------------------------------------------------------------------- #
# Пароли
# --------------------------------------------------------------------------- #


def hash_password(password: str) -> str:
    """Возвращает bcrypt-хэш пароля (строка ``$2b$...``, соль внутри).

    Raises:
        ValueError: пароль длиннее 72 байт в UTF-8 (ограничение bcrypt).
    """
    raw = password.encode("utf-8")
    if len(raw) > PASSWORD_MAX_BYTES:
        raise ValueError(f"password must not exceed {PASSWORD_MAX_BYTES} bytes in UTF-8")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Проверяет пароль по хэшу. Любой некорректный ввод (в т.ч. битый хэш) даёт ``False``."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


async def hash_password_async(password: str) -> str:
    """``hash_password`` в пуле потоков: bcrypt нагружает CPU ~0.2-0.3 с и блокировал бы event loop."""
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, password_hash: str) -> bool:
    """``verify_password`` в пуле потоков (см. ``hash_password_async``)."""
    return await asyncio.to_thread(verify_password, password, password_hash)


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #


def create_access_token(user_id: int, role: UserRole, *, version: int, expires_delta: timedelta | None = None) -> str:
    """Создаёт подписанный JWT (HS256) с полями ``sub``, ``role``, ``ver``, ``exp``.

    ``version`` - текущее ``User.token_version``; параметр обязателен, чтобы его нельзя было забыть.

    ``expires_delta`` по умолчанию берётся из JWT_TTL_MINUTES; параметр нужен в основном тестам.
    """
    settings = get_settings()
    ttl = expires_delta if expires_delta is not None else timedelta(minutes=settings.jwt_ttl_minutes)
    claims = {
        "sub": str(user_id),
        "role": role.value,
        "ver": version,
        "exp": utc_now() + ttl,  # PyJWT сам превращает aware datetime в числовую метку времени
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)


def _strict_int(value: object) -> int:
    """Целое из JWT без неявных преобразований (``True`` и ``"1"`` не принимаются)."""
    if type(value) is not int:
        raise TypeError("expected an integer claim")
    return value


def decode_access_token(token: str) -> TokenPayload:
    """Проверяет подпись и срок действия токена и возвращает его содержимое.

    Допускается только HS256 (защита от подмены алгоритма, в т.ч. ``alg=none``);
    поля ``sub``, ``role``, ``exp`` обязательны.

    Raises:
        TokenError: токен недействителен по любой причине.
    """
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "role", "ver", "exp"]},
        )
        return TokenPayload(
            user_id=int(claims["sub"]),
            role=UserRole(claims["role"]),
            version=_strict_int(claims["ver"]),
            expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
        )
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
        raise TokenError("invalid token") from exc


# --------------------------------------------------------------------------- #
# Зависимости FastAPI
# --------------------------------------------------------------------------- #


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Возвращает пользователя по заголовку ``Authorization: Bearer <JWT>``.

    401, если токен отсутствует/недействителен, пользователь не найден или деактивирован,
    либо роль в токене не совпадает с текущей ролью в БД (роль изменили после выдачи токена).
    Сессия БД общая с остальными зависимостями того же запроса.
    """
    if credentials is None:
        raise _unauthorized("Not authenticated")
    try:
        payload = decode_access_token(credentials.credentials)
    except TokenError:
        raise _unauthorized("Invalid or expired token") from None

    user = await session.get(User, payload.user_id)
    if user is None or not user.is_active:
        raise _unauthorized("Invalid or expired token")
    if user.role != payload.role or user.token_version != payload.version:
        raise _unauthorized("Token is no longer valid")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole) -> Callable[..., Awaitable[User]]:
    """Фабрика зависимости: пропускает только пользователей с одной из перечисленных ролей.

    Пример: ``Depends(require_roles(UserRole.ADMIN, UserRole.REGISTRAR))``.
    Совпадение точное: администратор не получает доступ автоматически - его роль нужно
    указать явно. Неавторизованный получает 401, авторизованный с чужой ролью - 403.
    """
    if not roles:
        raise ValueError("require_roles() needs at least one role")
    allowed = frozenset(roles)

    async def dependency(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user

    return dependency
