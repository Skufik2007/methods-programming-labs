# syntax=docker/dockerfile:1

# Образ Go-агентов: универсальный агент /app/agent и автомасштабировщик /app/autoscaler.
# Контекст сборки — корень репозитория (нужны agents/ и roles/):
#   docker build -f deploy/agent.Dockerfile -t mas-agent:latest .
#
# Образец: Dockerfile ingest из ЛР №11 (D:\6 семестр\Методы программирования\lab11\ingest\Dockerfile):
# многоэтапная сборка, статический бинарь без cgo, cache mount для модулей и кеша компиляции,
# слой зависимостей отдельно от кода, запуск не от root. Отличия: итоговый образ alpine,
# а не scratch (нужны tzdata для CLINIC_TZ, CA-сертификаты и wget для healthcheck),
# два бинарника, роли внутри образа.

# ---- сборка ----
FROM golang:1.27-alpine AS build
WORKDIR /src/agents

# Слой зависимостей: меняется только вместе с go.mod/go.sum.
COPY agents/go.mod agents/go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download

COPY agents/ ./
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/agent ./cmd/agent \
    && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/autoscaler ./cmd/autoscaler

# ---- итоговый образ ----
FROM alpine:3.21
RUN apk add --no-cache ca-certificates tzdata \
    && addgroup -S -g 10001 app \
    && adduser -S -u 10001 -G app -H -h /app app
WORKDIR /app
COPY --from=build /out/agent /out/autoscaler /app/
COPY roles /app/roles
USER 10001:10001
# ENTRYPOINT намеренно нет: автомасштабировщик запускает контейнеры с Cmd=[/app/agent -role ...],
# а команды статических сервисов задаёт compose.yaml.
CMD ["/app/agent", "-role", "/app/roles/triage.md"]
