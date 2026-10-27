# syntax=docker/dockerfile:1
#
# Образ приложения «Система управления клиникой» (FastAPI + uvicorn).
#
#   docker build -t clinic-app .
#   docker run --rm -p 8000:8000 -e DATABASE_URL=... -e JWT_SECRET=... clinic-app
#
# Полный стенд с PostgreSQL и миграциями: docker compose up -d --build (см. docker-compose.yml).

# --------------------------------------------------------------------------- #
# Этап 1. Виртуальное окружение с зависимостями приложения (без dev-зависимостей)
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# В слой попадает только pyproject.toml, поэтому зависимости пересобираются лишь при его изменении.
# Берётся список [project].dependencies; [project.optional-dependencies].dev (pytest, ruff, ...) не ставится.
# Сам проект через `pip install .` не устанавливается: в pyproject.toml нет [build-system] и настроек
# поиска пакетов, а в корне репозитория несколько каталогов верхнего уровня - автоопределение пакетов
# setuptools на такой раскладке не сработало бы. Код приложения копируется в рантайм-этапе.
COPY pyproject.toml ./
RUN python -c "import tomllib; print(*tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies'], sep='\n')" \
        > requirements.txt \
    && pip install -r requirements.txt

# --------------------------------------------------------------------------- #
# Этап 2. Рантайм: только окружение и код, без pip-кэша и инструментов сборки
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

# Непривилегированный пользователь с фиксированным числовым UID (нужно для runAsNonRoot в Kubernetes).
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app scripts ./scripts

USER 10001:10001
EXPOSE 8000

# curl в slim-образе нет, поэтому проверка - короткая программа на Python из окружения образа.
# urlopen бросает исключение при ответе 4xx/5xx и при недоступности порта -> код возврата 1.
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
