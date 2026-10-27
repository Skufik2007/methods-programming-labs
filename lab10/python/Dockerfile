# syntax=docker/dockerfile:1

# Базовый образ закреплён по digest — сборка воспроизводима (см. go/Dockerfile).
ARG PYTHON_IMAGE=python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

# ---- сборка: зависимости в отдельном виртуальном окружении ----
FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /venv
COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip /venv/bin/pip install -r /tmp/requirements.txt

# ---- финальный образ: только venv и код, без pip-кеша и инструментов сборки ----
FROM ${PYTHON_IMAGE}
ENV PATH=/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000
COPY --from=build /venv /venv
WORKDIR /srv
COPY app ./app
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER app
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=2).status != 200)"]
# exec-форма: uvicorn — PID 1 и сам обрабатывает SIGTERM (graceful shutdown).
CMD ["python", "-m", "app"]
