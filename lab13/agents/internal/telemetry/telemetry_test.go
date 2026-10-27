package telemetry

import (
	"context"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
)

// Экспорт выключен переменной OTEL_SDK_DISABLED: Setup не трогает провайдер и не падает.
func TestSetupDisabledIsNoop(t *testing.T) {
	t.Parallel()
	stop, err := Setup(context.Background(), "agent-test", func(k string) string {
		if k == "OTEL_SDK_DISABLED" {
			return "true"
		}
		return ""
	})
	if err != nil {
		t.Fatalf("Setup: %v", err)
	}
	stop() // не должно паниковать
}

// С явным адресом экспортёр создаётся без сетевых вызовов (соединение — лениво).
func TestSetupWithEndpointBuildsProvider(t *testing.T) {
	t.Parallel()
	stop, err := Setup(context.Background(), "agent-test", func(k string) string {
		if k == "OTEL_EXPORTER_OTLP_ENDPOINT" {
			return "http://127.0.0.1:1" // порт без слушателя: экспорт не должен блокировать
		}
		return ""
	})
	if err != nil {
		t.Fatalf("Setup: %v", err)
	}
	ctx, span := otel.Tracer("test").Start(context.Background(), "проверка")
	span.End()
	_ = ctx

	started := time.Now()
	stop() // сброс буфера ограничен 3 секундами, недоступный коллектор не блокирует
	if time.Since(started) > 5*time.Second {
		t.Fatalf("остановка заняла %v", time.Since(started))
	}
}

// Без адреса используется localhost:4318 по умолчанию; Setup всё равно возвращает функцию остановки.
func TestSetupDefaultEndpoint(t *testing.T) {
	t.Parallel()
	stop, err := Setup(context.Background(), "agent-test", func(string) string { return "" })
	if err != nil {
		t.Fatalf("Setup: %v", err)
	}
	if stop == nil {
		t.Fatal("функция остановки обязательна")
	}
	stop()
}
