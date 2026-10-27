# syntax=docker/dockerfile:1.7
# Образ Python-части: пакет vitals + колесо Rust-валидатора. Контекст — корень репозитория:
#   docker build -f deploy/python.Dockerfile -t vitals-py .
# Команда выбирается при запуске: python -m vitals.streaming | vitals.dashboard | vitals.analytics

ARG PYTHON_VERSION=3.12
ARG RUST_VERSION=1.99

# Готовый Rust-тулчейн берём из официального образа: на том же Debian bookworm, что и python:*-bookworm,
# поэтому glibc совпадает, и не нужно ставить rustup по сети
FROM rust:${RUST_VERSION}-slim-bookworm AS rust

# --- этап 1: сборка колеса maturin ---
FROM python:${PYTHON_VERSION}-slim-bookworm AS wheel
ENV CARGO_HOME=/usr/local/cargo RUSTUP_HOME=/usr/local/rustup PATH=/usr/local/cargo/bin:$PATH
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
# gcc нужен линковщику Rust
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "maturin>=1.5,<2"
WORKDIR /src/rust/vitals_validate
COPY rust/vitals_validate/ ./
# abi3-py312: колесо не привязано к минорной версии интерпретатора
RUN --mount=type=cache,target=/usr/local/cargo/registry --mount=type=cache,target=/src/rust/vitals_validate/target \
    maturin build --release --out /wheels

# --- этап 2: виртуальное окружение с пакетом и колесом ---
FROM python:${PYTHON_VERSION}-slim-bookworm AS venv
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
COPY pyproject.toml ./
COPY python/ ./python/
COPY --from=wheel /wheels /wheels
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install . /wheels/*.whl

# --- итоговый образ: без компиляторов и исходников ---
FROM python:${PYTHON_VERSION}-slim-bookworm
RUN groupadd -g 10001 app && useradd -u 10001 -g app -m -d /home/app -s /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data
COPY --from=venv /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DASHBOARD_HOST=0.0.0.0 DASHBOARD_PORT=8000
USER 10001:10001
WORKDIR /data
EXPOSE 8000
CMD ["python", "-m", "vitals.dashboard"]
