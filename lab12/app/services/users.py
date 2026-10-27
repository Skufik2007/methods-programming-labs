"""Управление пользователями (только администратор): список, создание, изменение.

Защита от потери доступа к системе: администратор не может снять с себя роль ``admin`` и
деактивировать себя (403: тот же запрос от другого администратора допустим).

Согласованность роли и профилей: роль нельзя сменить, если у пользователя есть профиль
``Doctor`` или ``Patient``, не соответствующий новой роли (409). Строка пользователя
блокируется (``FOR UPDATE``), поэтому одновременное создание профиля врача/пациента и смена
роли выполняются по очереди и не оставляют «врача с ролью patient».

Токены уже выданных сессий после смены роли, пароля и деактивации отклоняются самим
``get_current_user`` (сверка роли, ``is_active`` и ``token_version`` с БД).
"""

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import BusinessRuleError, ConflictError, NotFoundError, PermissionDeniedError
from app.models import Doctor, Patient, User, UserRole
from app.schemas import UserCreate, UserUpdate
from app.security import hash_password_async
from app.services.dbutil import ci_contains, commit_or_conflict


@dataclass(frozen=True, slots=True)
class UserFilters:
    """Фильтры списка пользователей; ``q`` - подстрока email или ФИО без учёта регистра."""

    role: UserRole | None = None
    is_active: bool | None = None
    q: str | None = None


def ensure_admin(user: User) -> None:
    """Операции над пользователями и профилями врачей доступны только администратору."""
    if user.role != UserRole.ADMIN:
        raise PermissionDeniedError("Insufficient permissions: administrator role required")


async def lock_user(session: AsyncSession, user_id: int) -> User:
    """Читает пользователя и блокирует его строку до конца транзакции; нет такого - 404.

    ``populate_existing`` нужен, чтобы роль и профиль проверялись по свежим данным БД,
    а не по объекту, уже лежащему в сессии.
    """
    stmt = select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    user = await session.scalar(stmt)
    if user is None:
        raise NotFoundError("User not found")
    return user


async def list_users(
    session: AsyncSession, actor: User, filters: UserFilters, *, limit: int, offset: int
) -> list[User]:
    """Список пользователей с фильтрами и пагинацией; порядок - по ``id`` (стабильная пагинация)."""
    ensure_admin(actor)
    stmt = select(User)
    if filters.role is not None:
        stmt = stmt.where(User.role == filters.role)
    if filters.is_active is not None:
        stmt = stmt.where(User.is_active.is_(filters.is_active))
    term = (filters.q or "").strip()
    if term:
        stmt = stmt.where(or_(ci_contains(User.email, term), ci_contains(User.full_name, term)))
    stmt = stmt.order_by(User.id).limit(limit).offset(offset)
    return list((await session.scalars(stmt)).all())


async def create_user(session: AsyncSession, actor: User, data: UserCreate) -> User:
    """Создаёт активного пользователя с любой ролью. Профиль врача/пациента создаётся отдельно."""
    ensure_admin(actor)
    if await session.scalar(select(User.id).where(User.email == data.email)) is not None:
        raise ConflictError("Email is already registered")

    password_hash = await hash_password_async(data.password.get_secret_value())
    user = User(
        email=data.email,
        password_hash=password_hash,
        full_name=data.full_name,
        role=data.role,
        is_active=True,
    )
    session.add(user)
    await commit_or_conflict(session, "Email is already registered")
    return user


async def _ensure_role_fits_profiles(session: AsyncSession, user_id: int, new_role: UserRole) -> None:
    """Новая роль не должна противоречить существующим профилям пользователя (409)."""
    if new_role != UserRole.DOCTOR:
        if await session.scalar(select(Doctor.id).where(Doctor.user_id == user_id)) is not None:
            raise ConflictError(f"The user has a doctor profile; the role cannot be changed to '{new_role.value}'")
    if new_role != UserRole.PATIENT:
        if await session.scalar(select(Patient.id).where(Patient.user_id == user_id)) is not None:
            raise ConflictError(f"The user is linked to a patient; the role cannot be changed to '{new_role.value}'")


async def update_user(session: AsyncSession, actor: User, user_id: int, data: UserUpdate) -> User:
    """PATCH: ФИО, email, пароль, роль, ``is_active``.

    * пустое тело - 422; неизвестный пользователь - 404;
    * администратор не может снять роль ``admin`` с себя и деактивировать себя - 403;
    * смена роли при несоответствующем профиле ``Doctor`` / ``Patient`` - 409;
    * занятый email - 409.
    """
    ensure_admin(actor)
    if not data.model_fields_set:
        raise BusinessRuleError("No fields to update")

    user = await lock_user(session, user_id)

    if user.id == actor.id:
        if data.role is not None and data.role != UserRole.ADMIN:
            raise PermissionDeniedError("Administrators cannot remove their own admin role")
        if data.is_active is False:
            raise PermissionDeniedError("Administrators cannot deactivate themselves")

    if data.role is not None and data.role != user.role:
        await _ensure_role_fits_profiles(session, user.id, data.role)

    if data.email is not None and data.email != user.email:
        taken = await session.scalar(select(User.id).where(User.email == data.email, User.id != user.id))
        if taken is not None:
            raise ConflictError("Email is already registered")
        user.email = data.email
    if data.full_name is not None:
        user.full_name = data.full_name
    if data.role is not None:
        user.role = data.role
    if data.is_active is not None:
        user.is_active = data.is_active
    if data.password is not None:
        user.password_hash = await hash_password_async(data.password.get_secret_value())
        user.token_version += 1  # уже выданные токены (в том числе украденные) перестают действовать

    await commit_or_conflict(session, "Email is already registered")
    return user
