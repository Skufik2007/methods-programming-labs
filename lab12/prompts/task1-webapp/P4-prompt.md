# P4 — миграции, демо-данные, Docker и CI

Модель: Claude Sonnet 5.5 (отдельный экземпляр). Доступ: только чтение `app/`,
`tests/conftest.py` и `pyproject.toml`.

Почему так: приложение уже работает, но схему создаёт `create_all` из тестов, и
запускать его можно только вручную. Здесь нужны воспроизводимые миграции, демо-данные
для показа аналитики, контейнер и CI. По итогам ревью P2 (находка №4) защиту от
пересечения записей стоит перенести в саму БД: в PostgreSQL для этого есть
ограничение `EXCLUDE`. Новое ограничение даёт новый вид ошибки, поэтому в промпте
отдельно оговорено, что она должна превращаться в 409, а не в 500. Секреты (пароль БД,
JWT_SECRET, пароль демо-пользователей) не должны появиться ни в одном файле.

## Текст промпта

```text
Ты — senior Python/DevOps-инженер. Проект «Система управления клиникой» (FastAPI,
SQLAlchemy 2.0 async, PostgreSQL в проде, SQLite в тестах) готов на уровне кода,
теперь нужна инфраструктура.

Контекст: прочитай каталог app/, файл tests/conftest.py и pyproject.toml. Код
приложения не переписывай; если без изменения файла из app/ не обойтись — приведи
его новую версию целиком и объясни причину.

Нужны файлы:

1. Alembic (асинхронный):
   - alembic.ini, migrations/env.py, migrations/script.py.mako;
   - env.py берёт URL из app.config.get_settings(), target_metadata = Base.metadata,
     compare_type=True; для SQLite — render_as_batch=True;
   - migrations/versions/0001_initial.py — схема, точно совпадающая с app/models.py
     (имена ограничений и индексов — по naming convention из models.py, частичный
     уникальный индекс, CHECK-ограничения, server_default). Проверка: после
     `alembic upgrade head` команда `alembic check` не должна находить отличий от
     моделей ни в PostgreSQL, ни в SQLite. downgrade удаляет всё.
   - только для PostgreSQL в той же миграции: расширение btree_gist и ограничение
     EXCLUDE USING gist (doctor_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&)
     WHERE (status <> 'cancelled') — запрет пересечения неотменённых записей врача на
     уровне БД. В models.py его не добавляй (SQLite его не поддерживает).
   - Нарушение EXCLUDE в PostgreSQL — это SQLSTATE 23P01, а app/errors.py
     превращает в 409 только 23505. Сделай так, чтобы гонка, пойманная EXCLUDE,
     тоже давала 409, а не 500.

2. scripts/seed.py — демо-данные для показа (запуск: python -m scripts.seed):
   - идемпотентно: повторный запуск ничего не дублирует;
   - пароль всех демо-пользователей — из переменной окружения SEED_PASSWORD, без
     значения по умолчанию; в конце печатается список логинов (без пароля);
   - admin, registrar, 4 врача разных специальностей с разными графиками и длиной
     слота, 20 пациентов (реалистичные русские ФИО, полисы — 16 цифр), записи за
     последние 30 дней (completed, no_show, cancelled в правдоподобных долях) и на
     14 дней вперёд (scheduled), записи медкарты с кодами МКБ-10 и рецепты к части
     completed-приёмов;
   - все записи соблюдают правила расписания (рабочие дни, смена, сетка слотов, без
     пересечений у врача и у пациента) — используй функции app/services/scheduling.py;
   - детерминированно (фиксированный seed генератора случайных чисел).

3. Docker:
   - Dockerfile: многоэтапная сборка на python:3.12-slim, зависимости ставятся из
     pyproject.toml без dev-зависимостей, запуск от непривилегированного пользователя,
     HEALTHCHECK на GET /api/health без curl (в slim-образе его нет), uvicorn на
     0.0.0.0:8000;
   - .dockerignore;
   - docker-compose.yml: postgres:16 (healthcheck pg_isready, именованный том),
     одноразовый сервис migrate (alembic upgrade head), app стартует после успешного
     migrate; все секреты — из .env через ${VAR:?сообщение}, ни одного значения
     секрета в файле;
   - .env.example с заглушками и комментариями, как сгенерировать JWT_SECRET.

4. .github/workflows/ci.yml (push и pull_request):
   - lint: ruff check и ruff format --check;
   - tests: pytest с покрытием на SQLite и отдельно на PostgreSQL 16 (service
     container; tests/conftest.py берёт URL из TEST_DATABASE_URL);
   - migrations: на PostgreSQL — upgrade head, alembic check, downgrade base,
     снова upgrade head;
   - docker: сборка образа и запуск docker compose с проверкой /api/health;
   - версии actions зафиксированы; права GITHUB_TOKEN минимальные (contents: read).

Требования: type hints и docstring в Python-файлах; никаких секретов в репозитории;
команды в комментариях — рабочие.

Формат ответа: для каждого файла — заголовок `### путь/к/файлу` и один блок кода с
полным содержимым файла. После файлов — короткий список допущений и отдельный список
мест, которые стоит особенно внимательно проверить на ревью.
```

## Служебная часть (не влияет на содержание)

```text
Кроме app/, tests/conftest.py и pyproject.toml, ничего не читай. Весь свой ответ
сохрани в файл <...>/P4-response.md инструментом Write, других файлов не создавай.
Сообщением верни только слово «готово».
```
