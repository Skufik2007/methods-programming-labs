# syntax=docker/dockerfile:1.7
# Образ с обоими Go-бинарниками: sensorsim и collector. Контекст сборки — корень репозитория:
#   docker build -f deploy/go.Dockerfile -t vitals-go .

ARG GO_VERSION=1.27
ARG ALPINE_VERSION=3.22

FROM golang:${GO_VERSION}-alpine AS build
WORKDIR /src/go
# сначала только манифесты модуля — слой с зависимостями кэшируется, пока go.mod/go.sum не менялись
COPY go/go.mod go/go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY go/ ./
# CGO выключен: статические бинарники работают на голом alpine; -s -w убирают символы и DWARF
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/ ./cmd/sensorsim ./cmd/collector

FROM alpine:${ALPINE_VERSION}
# ca-certificates — для TLS-клиентов, tzdata — для time.LoadLocation; wget (busybox) нужен healthcheck-ам
RUN apk add --no-cache ca-certificates tzdata \
    && addgroup -g 10001 -S app && adduser -u 10001 -S -G app -h /data app \
    && mkdir -p /data && chown app:app /data
COPY --from=build /out/sensorsim /out/collector /usr/local/bin/
USER 10001:10001
WORKDIR /data
# OUT_DIR сборщика по умолчанию — относительный "data"; в compose/k8s задаётся явно
ENV OUT_DIR=/data
EXPOSE 8080 9100 8815
CMD ["collector"]
