"""Аутентификация: регистрация пациента, вход по паролю, текущий пользователь.

* ``POST /api/auth/register`` создаёт ``User(role=patient)`` и ``Patient`` в одной транзакции.
  Если пациент с таким полисом уже существует, аккаунт к нему НЕ привязывается (иначе любой,
  кто знает номер полиса, получил бы доступ к чужой медкарте): ответ 409, привязку выполняет
  регистратор.
* ``POST /api/auth/login`` отвечает одинаковым 401 на «нет пользователя», «неверный пароль» и
  «аккаунт деактивирован». Проверка bcrypt выполняется всегда (при отсутствии пользователя - по
  фиктивному хэшу той же стоимости), чтобы по времени ответа нельзя было определить, существует
  ли email.
"""

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.errors import ConflictError, is_unique_violation
from app.models import Patient, User, UserRole
from app.schemas import (
    BirthDate,
    Email,
    FullName,
    LoginIn,
    Password,
    PhoneE164,
    PolicyNumber,
    TokenOut,
    UserOut,
)
from app.security import (
    CurrentUser,
    create_access_token,
    hash_password,
    hash_password_async,
    verify_password_async,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Хэш случайного пароля: нужен только для выравнивания времени ответа, когда пользователь не
# найден. Считается один раз при импорте модуля (~0.25 с) с той же стоимостью bcrypt, что у
# настоящих хэшей, поскольку берётся из той же ``hash_password``.
_DUMMY_PASSWORD_HASH: str = hash_password(secrets.token_urlsafe(32))


class RegisterIn(BaseModel):
    """Тело самостоятельной регистрации пациента. Роль не принимается: всегда ``patient``."""

    model_config = ConfigDict(extra="forbid")

    email: Email
    password: Password
    full_name: FullName
    birth_date: BirthDate
    phone: PhoneE164 | None = None
    policy_number: PolicyNumber


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(data: RegisterIn, session: SessionDep) -> User:
    """Регистрирует пациента: создаёт ``User(role=patient)`` и ``Patient`` одной транзакцией."""
    if await session.scalar(select(User.id).where(User.email == data.email)) is not None:
        raise ConflictError("Email is already registered")
    if await session.scalar(select(Patient.id).where(Patient.policy_number == data.policy_number)) is not None:
        raise ConflictError(
            "A patient with this policy number already exists; please contact the clinic registrar to link your account"
        )

    # bcrypt - после проверок и до записи: ни лишней нагрузки на CPU для заведомо отказных
    # запросов, ни блокирующего вызова в event loop.
    password_hash = await hash_password_async(data.password.get_secret_value())

    user = User(
        email=data.email,
        password_hash=password_hash,
        full_name=data.full_name,
        role=UserRole.PATIENT,
        is_active=True,
    )
    session.add(user)
    try:
        await session.flush()  # нужен user.id для Patient.user_id
        session.add(
            Patient(
                user_id=user.id,
                full_name=data.full_name,
                birth_date=data.birth_date,
                phone=data.phone,
                policy_number=data.policy_number,
            )
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if is_unique_violation(exc):  # гонка: email или полис заняли между проверкой и записью
            raise ConflictError("Email or policy number is already registered") from exc
        raise
    return user


@router.post("/login", response_model=TokenOut)
async def login(data: LoginIn, session: SessionDep) -> TokenOut:
    """Выдаёт JWT по email и паролю. На любую причину отказа - один и тот же 401."""
    user = await session.scalar(select(User).where(User.email == data.email))
    password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_ok = await verify_password_async(data.password.get_secret_value(), password_hash)

    if user is None or not password_ok or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenOut(
        access_token=create_access_token(user.id, user.role, version=user.token_version),
        expires_in=get_settings().jwt_ttl_minutes * 60,
    )


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    """Текущий пользователь (по токену)."""
    return user
