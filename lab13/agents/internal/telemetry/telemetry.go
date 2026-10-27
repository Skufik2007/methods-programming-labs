// Пакет telemetry настраивает OpenTelemetry: TracerProvider с OTLP/HTTP-экспортом.
package telemetry

import (
	"context"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// Setup ставит глобальные TracerProvider и propagator (W3C traceparent).
// Возвращает функцию остановки, которая сбрасывает буфер (с ограничением по времени).
// Конечная точка берётся из OTEL_EXPORTER_OTLP_ENDPOINT (разбирает сам экспортёр),
// иначе http://localhost:4318.
func Setup(ctx context.Context, service string, getenv func(string) string) (func(), error) {
	otel.SetTextMapPropagator(propagation.TraceContext{})
	if getenv("OTEL_SDK_DISABLED") == "true" {
		return func() {}, nil
	}

	opts := []otlptracehttp.Option{
		otlptracehttp.WithTimeout(3 * time.Second),
		otlptracehttp.WithRetry(otlptracehttp.RetryConfig{Enabled: false}),
	}
	if getenv("OTEL_EXPORTER_OTLP_ENDPOINT") == "" && getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") == "" {
		opts = append(opts, otlptracehttp.WithEndpoint("localhost:4318"), otlptracehttp.WithInsecure())
	}
	exp, err := otlptracehttp.New(ctx, opts...)
	if err != nil {
		return nil, err
	}
	res, err := resource.Merge(resource.Default(),
		resource.NewSchemaless(attribute.String("service.name", service)))
	if err != nil {
		return nil, err
	}
	// Пакетный экспорт: Start/End спанов не блокируются, при переполнении очереди спаны отбрасываются.
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exp,
			sdktrace.WithBatchTimeout(2*time.Second),
			sdktrace.WithMaxQueueSize(2048),
			sdktrace.WithExportTimeout(3*time.Second)),
		sdktrace.WithResource(res),
	)
	otel.SetTracerProvider(tp)
	return func() {
		sctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = tp.Shutdown(sctx)
	}, nil
}
