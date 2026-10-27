"""Общие зависимости и типы параметров для новых роутеров.

* ``get_now`` не определяется заново, а берётся из ``app.routers.appointments``: тогда один
  ``app.dependency_overrides[get_now]`` в тестах действует на все роутеры сразу;
* ``Annotated``-псевдонимы ролей (``AdminUser``, ``StaffUser`` ...) - грубый фильтр по роли;
  права на уровне объектов проверяет сервисный слой.
"""

from datetime import date, datetime
from typing import Annotated

from fastapi import Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User, UserRole
from app.routers.appointments import get_now
from app.security import require_roles

MAX_ID = 2_147_483_647  # граница INTEGER в PostgreSQL (как EntityId в схемах)
MIN_QUERY_DATE = date(1970, 1, 1)  # разумные границы дат в параметрах: защита от OverflowError
MAX_QUERY_DATE = date(2100, 12, 31)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
NowDep = Annotated[datetime, Depends(get_now)]

AdminUser = Annotated[User, Depends(require_roles(UserRole.ADMIN))]
StaffUser = Annotated[User, Depends(require_roles(UserRole.ADMIN, UserRole.REGISTRAR))]
DoctorUser = Annotated[User, Depends(require_roles(UserRole.DOCTOR))]
PatientUser = Annotated[User, Depends(require_roles(UserRole.PATIENT))]
# Медицинские данные: administrative staff (admin, registrar) получает 403 уже на этом уровне.
MedicalReaderUser = Annotated[User, Depends(require_roles(UserRole.DOCTOR, UserRole.PATIENT))]

IdPath = Annotated[int, Path(gt=0, le=MAX_ID)]
LimitQuery = Annotated[int, Query(ge=1, le=100)]
OffsetQuery = Annotated[int, Query(ge=0, le=MAX_ID)]
SearchQuery = Annotated[str | None, Query(min_length=1, max_length=100)]
RequiredDate = Annotated[date, Query(ge=MIN_QUERY_DATE, le=MAX_QUERY_DATE)]
