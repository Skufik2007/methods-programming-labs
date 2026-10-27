# syntax=docker/dockerfile:1

# Образ Python-части: пакет mas (оркестратор + дашборд, LLM-агент). Один образ для всех
# Python-сервисов, команда задаётся в compose.yaml:
#   python -m mas.dashboard
#   python -m mas.llm_agent --role /app/roles/intake.md
# Контекст сборки — корень репозитория:  docker build -f deploy/python.Dockerfile -t mas-python:latest .

# ---- сборка: зависимости и пакет ставятся в venv, который затем копируется целиком ----
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
RUN python -m venv /opt/venv
COPY pyproject.toml ./
COPY mas ./mas
RUN /opt/venv/bin/pip install .

# ---- итоговый образ ----
FROM python:3.12-slim
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /app app
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY roles /app/roles
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "mas.dashboard"]
