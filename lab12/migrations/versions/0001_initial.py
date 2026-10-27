"""Начальная схема клиники.

Revision ID: 0001
Revises:
Create Date: 2026-10-10

Таблицы: users, doctors, patients, appointments, prescriptions, medical_records.

Схема в точности повторяет ``app/models.py``: имена ограничений и индексов построены по
naming convention из ``Base.metadata`` (в миграции они записаны явно через ``op.f``, поэтому
не зависят от настроек env.py), enum-поля - это VARCHAR(16) + CHECK, моменты времени -
``TIMESTAMP WITH TIME ZONE``. Проверка соответствия: ``alembic upgrade head && alembic check``.

Только для PostgreSQL дополнительно создаётся то, чего нет в моделях (SQLite этого не умеет):

* расширение ``btree_gist``;
* ограничение EXCLUDE: у одного врача не может быть двух неотменённых записей с пересекающимися
  интервалами ``[starts_at, ends_at)``. Частичный уникальный индекс (doctor_id, starts_at)
  ловит только записи с одинаковым началом, EXCLUDE - любое пересечение. Нарушение даёт
  SQLSTATE 23P01, которое ``app.errors.is_unique_violation`` превращает в HTTP 409.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Идентификаторы ревизий, используемые Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Имя ограничения EXCLUDE; то же значение указано в DB_ONLY_OBJECT_NAMES в migrations/env.py.
EXCLUDE_NAME = "ex_appointments_doctor_no_overlap"

# Предикат частичного индекса и EXCLUDE: «неотменённая» запись (как _ACTIVE_APPOINTMENT в models.py).
ACTIVE_APPOINTMENT = "status <> 'cancelled'"


def _is_postgresql() -> bool:
    """Идёт ли миграция на PostgreSQL (работает и в offline-режиме ``--sql``)."""
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    """Создаёт таблицы, индексы и (на PostgreSQL) защиту от пересечения записей врача."""
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=128), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("token_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'registrar', 'doctor', 'patient')", name=op.f("ck_users_user_role")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    op.create_table(
        "doctors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("specialty", sa.String(length=100), nullable=False),
        sa.Column("cabinet", sa.String(length=20), nullable=False),
        sa.Column("slot_minutes", sa.SmallInteger(), server_default=sa.text("30"), nullable=False),
        sa.Column("work_start", sa.Time(), nullable=False),
        sa.Column("work_end", sa.Time(), nullable=False),
        sa.Column("work_days", sa.JSON(), nullable=False),
        sa.CheckConstraint("slot_minutes BETWEEN 5 AND 240", name=op.f("ck_doctors_slot_minutes_range")),
        sa.CheckConstraint("work_start < work_end", name=op.f("ck_doctors_shift_order")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_doctors_user_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_doctors")),
        sa.UniqueConstraint("user_id", name=op.f("uq_doctors_user_id")),
    )

    op.create_table(
        "patients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=False),
        sa.Column("phone", sa.String(length=16), nullable=True),
        sa.Column("policy_number", sa.String(length=16), nullable=False),
        sa.CheckConstraint("length(policy_number) = 16", name=op.f("ck_patients_policy_number_length")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_patients_user_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_patients")),
        sa.UniqueConstraint("policy_number", name=op.f("uq_patients_policy_number")),
        sa.UniqueConstraint("user_id", name=op.f("uq_patients_user_id")),
    )

    op.create_table(
        "appointments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("doctor_id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="scheduled", nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('scheduled', 'completed', 'cancelled', 'no_show')",
            name=op.f("ck_appointments_appointment_status"),
        ),
        sa.CheckConstraint("ends_at > starts_at", name=op.f("ck_appointments_ends_after_starts")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_appointments_created_by_users"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["doctor_id"], ["doctors.id"], name=op.f("fk_appointments_doctor_id_doctors"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], name=op.f("fk_appointments_patient_id_patients"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_appointments")),
    )
    op.create_index("ix_appointments_doctor_id_starts_at", "appointments", ["doctor_id", "starts_at"])
    op.create_index("ix_appointments_patient_id", "appointments", ["patient_id"])
    op.create_index("ix_appointments_status", "appointments", ["status"])
    # Врач не может иметь две неотменённые записи на одно и то же время начала.
    op.create_index(
        "uq_appointments_doctor_id_starts_at_active",
        "appointments",
        ["doctor_id", "starts_at"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_APPOINTMENT),
        sqlite_where=sa.text(ACTIVE_APPOINTMENT),
    )

    op.create_table(
        "prescriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=False),
        sa.Column("medication", sa.String(length=255), nullable=False),
        sa.Column("dosage", sa.String(length=100), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["appointment_id"],
            ["appointments.id"],
            name=op.f("fk_prescriptions_appointment_id_appointments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prescriptions")),
    )
    op.create_index("ix_prescriptions_appointment_id", "prescriptions", ["appointment_id"])

    op.create_table(
        "medical_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("doctor_id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=True),
        sa.Column("diagnosis_code", sa.String(length=6), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "length(diagnosis_code) BETWEEN 3 AND 6", name=op.f("ck_medical_records_diagnosis_code_length")
        ),
        sa.ForeignKeyConstraint(
            ["appointment_id"],
            ["appointments.id"],
            name=op.f("fk_medical_records_appointment_id_appointments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["doctor_id"], ["doctors.id"], name=op.f("fk_medical_records_doctor_id_doctors"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], name=op.f("fk_medical_records_patient_id_patients"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_medical_records")),
    )
    op.create_index("ix_medical_records_patient_id", "medical_records", ["patient_id"])
    op.create_index("ix_medical_records_doctor_id", "medical_records", ["doctor_id"])
    op.create_index("ix_medical_records_appointment_id", "medical_records", ["appointment_id"])

    if _is_postgresql():
        # btree_gist позволяет объединить в одном GiST-индексе равенство по скаляру (doctor_id)
        # и пересечение диапазонов. CREATE EXTENSION требует права CREATE на базу (в PostgreSQL 13+
        # btree_gist - «доверенное» расширение, суперпользователь не нужен).
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        op.execute(
            f"ALTER TABLE appointments ADD CONSTRAINT {EXCLUDE_NAME} "
            "EXCLUDE USING gist (doctor_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&) "
            f"WHERE ({ACTIVE_APPOINTMENT})"
        )


def downgrade() -> None:
    """Удаляет всё, что создал ``upgrade``: ограничение EXCLUDE, таблицы (вместе с индексами), расширение."""
    if _is_postgresql():
        op.execute(f"ALTER TABLE appointments DROP CONSTRAINT IF EXISTS {EXCLUDE_NAME}")

    # Порядок обратен созданию: сначала таблицы, ссылающиеся на другие. Индексы удаляются вместе с таблицами.
    op.drop_table("medical_records")
    op.drop_table("prescriptions")
    op.drop_table("appointments")
    op.drop_table("patients")
    op.drop_table("doctors")
    op.drop_table("users")

    if _is_postgresql():
        # Расширение можно удалить только после таблиц: от его классов операторов зависел EXCLUDE.
        op.execute("DROP EXTENSION IF EXISTS btree_gist")
