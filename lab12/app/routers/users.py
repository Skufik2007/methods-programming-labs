"""Роуты пользователей: список, создание, изменение. Только администратор.

Роль проверяется через ``AdminUser`` (403 остальным), запреты на уровне объектов
(нельзя снять с себя роль admin, несоответствие профилям) - в ``app.services.users``.
"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.models import User, UserRole
from app.routers.common import AdminUser, IdPath, LimitQuery, OffsetQuery, SearchQuery, SessionDep
from app.schemas import UserCreate, UserOut, UserUpdate
from app.services import users as service
from app.services.users import UserFilters

router = APIRouter(prefix="/api", tags=["users"])


@router.get("/users", response_model=list[UserOut])
async def list_users(
    user: AdminUser,
    session: SessionDep,
    role: Annotated[UserRole | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    q: SearchQuery = None,
    limit: LimitQuery = 20,
    offset: OffsetQuery = 0,
) -> list[User]:
    """Список пользователей; ``q`` - подстрока email или ФИО без учёта регистра."""
    filters = UserFilters(role=role, is_active=is_active, q=q)
    return await service.list_users(session, user, filters, limit=limit, offset=offset)


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(data: UserCreate, user: AdminUser, session: SessionDep) -> User:
    """Создаёт пользователя с любой ролью."""
    return await service.create_user(session, user, data)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(user_id: IdPath, data: UserUpdate, user: AdminUser, session: SessionDep) -> User:
    """Меняет ФИО, email, пароль, роль, ``is_active``."""
    return await service.update_user(session, user, user_id, data)
