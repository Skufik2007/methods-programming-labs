# syntax=docker/dockerfile:1

# Базовые образы закреплены по digest: тег (1.27-alpine, nonroot) со временем указывает
# на другой образ, а digest — всегда на один и тот же, поэтому сборка воспроизводима.
# Обновить: docker buildx imagetools inspect <образ:тег> и заменить sha256.

# ---- сборка: статический бинарь без cgo ----
FROM golang:1.27-alpine@sha256:738d1cf061836894ff6bb8c33881080ac66de8cf0586615012a0c8f592649cfa AS build
WORKDIR /src

# Зависимости отдельным слоем: при изменении только кода слой берётся из кеша.
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download

COPY . .
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/server ./cmd/server \
    && mkdir -p /out/keys

# ---- финальный образ: distroless без shell и пакетного менеджера, пользователь nonroot ----
FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=build /out/server /server
# Каталог для ключа подписи JWT. Владелец — nonroot (65532): Docker копирует его
# в пустой именованный volume, и сервис сможет создать там ключ при первом запуске.
COPY --from=build --chown=65532:65532 /out/keys /keys
EXPOSE 8080
ENV ADDR=:8080
# В образе нет curl — проверку готовности выполняет сам бинарь.
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 CMD ["/server", "-healthcheck"]
# exec-форма: бинарь — PID 1 и сам получает SIGTERM от docker stop.
ENTRYPOINT ["/server"]
