"""OpenTelemetry: провайдер с OTLP/HTTP и перенос `traceparent` через заголовки NATS."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from mas.common.settings import Settings

_PROPAGATOR = TraceContextTextMapPropagator()


def create_tracer_provider(service_name: str, settings: Settings) -> TracerProvider:
    """Провайдер без глобальной регистрации. Экспорт пакетный, в фоновом потоке:
    недоступный Jaeger не блокирует обработку. При OTEL_SDK_DISABLED экспортёра нет,
    но спаны и trace_id остаются (нужны для traceparent и журнала)."""
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if not settings.otel_sdk_disabled:
        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=3)))
    return provider


def inject_traceparent(headers: MutableMapping[str, str]) -> None:
    _PROPAGATOR.inject(headers)


def extract_context(headers: Mapping[str, str] | None) -> Context:
    carrier = {k.lower(): v for k, v in (headers or {}).items()}
    return _PROPAGATOR.extract(carrier)


def current_ids() -> tuple[str, str]:
    """(trace_id hex32, span_id hex16) текущего спана; пустые строки, если спана нет."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return "", ""
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
