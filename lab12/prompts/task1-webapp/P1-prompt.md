# P1 — каркас: данные и безопасность

Модель: Claude Sonnet 5.5 (отдельный экземпляр, без доступа к проекту).

Почему так: после P0 стало видно, что без явных требований модель делает «прототип»
без аутентификации, на синхронной БД, с `create_all` вместо миграций и одним файлом
`main.py`. Здесь задача разбита на части (сначала данные и безопасность, затем
маршруты), заданы роль, стек с конкретными API, полная модель данных со связями и
ограничениями, требования к безопасности и формат ответа.

## Текст промпта

```text
Ты — senior Python backend-разработчик. Пишешь production-код для медицинской
информационной системы, где важны защита персональных данных и корректность расписания.

Задача: каркас веб-приложения «Система управления клиникой» — слой данных и
безопасности. Маршруты (роуты) будут в следующем запросе, сейчас их не пиши.

Стек — строго эти библиотеки и API:
- Python 3.12, FastAPI;
- SQLAlchemy 2.0 в асинхронном режиме: AsyncSession, DeclarativeBase, Mapped/mapped_column;
- драйверы: asyncpg (PostgreSQL в проде), aiosqlite (SQLite в тестах);
- Pydantic v2 и pydantic-settings;
- PyJWT для токенов, bcrypt напрямую для паролей (без passlib).

Сущности:
- User: id, email (уникальный), password_hash, full_name, role (enum: admin, registrar,
  doctor, patient), is_active, created_at.
- Doctor: id, user_id (1:1 → User), specialty, cabinet, slot_minutes (длительность приёма,
  по умолчанию 30), work_start и work_end (время начала и конца смены), work_days
  (рабочие дни недели).
- Patient: id, user_id (1:1 → User, может быть пустым — пациента заводит регистратор),
  full_name, birth_date, phone, policy_number (полис ОМС, ровно 16 цифр, уникальный).
- Appointment — основная сущность: id, doctor_id, patient_id, starts_at и ends_at (UTC,
  с часовым поясом), status (scheduled, completed, cancelled, no_show), reason,
  created_by (user_id), created_at.
- Prescription: id, appointment_id, medication, dosage, instructions, issued_at.
- MedicalRecord: id, patient_id, doctor_id, appointment_id (может быть пустым),
  diagnosis_code (код МКБ-10, формат вида J06.9), notes, created_at.

Связи: Doctor 1—N Appointment, Patient 1—N Appointment, Appointment 1—N Prescription,
Patient 1—N MedicalRecord, Doctor 1—N MedicalRecord.
Ограничения в БД: врач не может иметь две неотменённые записи на одно время (частичный
уникальный индекс по doctor_id, starts_at для статусов кроме cancelled); индексы по
полям фильтрации (doctor_id + starts_at, patient_id, status).

Нужны файлы:
1. app/config.py — настройки из переменных окружения: DATABASE_URL, JWT_SECRET (без
   значения по умолчанию — без него приложение не стартует), JWT_TTL_MINUTES,
   CLINIC_TIMEZONE (например Europe/Moscow).
2. app/db.py — async engine, фабрика сессий, зависимость get_session.
3. app/models.py — модели выше.
4. app/schemas.py — Pydantic-схемы Create / Update / Out для каждой сущности. Out-схемы
   никогда не содержат password_hash. Валидация: полис ОМС, телефон в формате E.164,
   код МКБ-10, email.
5. app/security.py — хеширование и проверка пароля (bcrypt), создание и проверка JWT
   (HS256, поля sub, role, exp), зависимости get_current_user и require_roles(*roles).

Требования к коду: type hints везде; docstring у модулей и неочевидных функций;
никаких секретов в коде; время — только timezone-aware UTC.

Формат ответа: для каждого файла — заголовок `### путь/к/файлу` и один блок кода с
полным содержимым файла. После файлов — короткий список допущений, которые ты сделал.
```

## Служебная часть (не влияет на содержание)

```text
Весь свой ответ сохрани в файл <...>/P1-response.md инструментом Write. Ничего
другого не читай и не создавай. Сообщением верни только слово «готово».
```
