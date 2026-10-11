"""OpenTelemetry for the services: one trace across processes, and the metrics the SLOs are built from.

    traces   every request is a server span that continues the caller's W3C `traceparent`; outbound calls inject it,
             so a ticket is one trace across the ticket, gateway, identity and knowledge services. Spans from the
             libraries underneath (ADK, Requisite, the gateway's own `gateway.chat`) join it automatically.
    metrics  request counts and durations, plus the domain signals: ticket outcomes by release, gateway calls by
             route and tier, tokens, cost, circuit-breaker state, probe results, drift verdicts.
    logs     JSON lines carry the trace id (see logs.py), so a log line leads to its trace and back.

Everything is exported over OTLP/HTTP to a collector when `OTEL_EXPORTER_OTLP_ENDPOINT` is set; without it the
services run exactly as before and the instruments are no-ops.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional

from opentelemetry import metrics, trace
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

log = logging.getLogger("telemetry")

# Seconds. A ticket takes tens of seconds because it makes several model calls; the SLO thresholds sit on these edges.
DURATION_BUCKETS = (0.05, 0.25, 1, 2.5, 5, 10, 20, 30, 45, 60, 90, 120, 180, 300)

KNOWN_PATHS = {"/tickets", "/login", "/exchange", "/v1/chat", "/v1/embed", "/v1/status", "/.well-known/agent-card.json", "/"}


def path_label(path: str) -> str:
    """A bounded set of values for the `path` label: never put an id or a user-supplied string in a metric label."""
    if path in KNOWN_PATHS:
        return path
    return "/admin/releases" if path.startswith("/admin/releases") else "other"


class Telemetry:
    def __init__(self, service: str, version: str, tracer_provider: Optional[TracerProvider], meter_provider: Optional[MeterProvider]) -> None:
        self.service, self.version = service, version
        self.tracer_provider, self.meter_provider = tracer_provider, meter_provider
        self.tracer = (tracer_provider.get_tracer(service) if tracer_provider else trace.get_tracer(service))
        self.meter = (meter_provider.get_meter(service) if meter_provider else metrics.get_meter(service))
        self._counters: dict = {}
        self._histograms: dict = {}
        self._gauges: dict = {}

    def inc(self, name: str, value: float = 1, unit: str = "1", **attrs) -> None:
        if name not in self._counters:
            self._counters[name] = self.meter.create_counter(name, unit=unit)
        self._counters[name].add(value, attrs)

    def observe(self, name: str, value: float, unit: str = "s", **attrs) -> None:
        if name not in self._histograms:
            self._histograms[name] = self.meter.create_histogram(name, unit=unit)
        self._histograms[name].record(value, attrs)

    def gauge(self, name: str, read: Callable[[], Iterable[tuple]], unit: str = "") -> None:
        """An observable gauge: `read()` returns (value, {attrs}) pairs whenever the metrics are collected.
        The unit is empty on purpose: Prometheus conversion appends `_ratio` to a gauge whose unit is "1"."""
        def callback(_: CallbackOptions):
            try:
                return [Observation(v, a) for v, a in read()]
            except Exception:  # noqa: BLE001 - a broken gauge must never break collection
                log.exception("gauge %s failed", name)
                return []
        self._gauges[name] = self.meter.create_observable_gauge(name, callbacks=[callback], unit=unit)

    def shutdown(self) -> None:
        for provider in (self.tracer_provider, self.meter_provider):
            if provider is not None:
                try:
                    provider.shutdown()
                except Exception:  # noqa: BLE001
                    pass


_current: Optional[Telemetry] = None


def telemetry() -> Telemetry:
    """The process's telemetry, or a no-op one before `setup` ran."""
    global _current
    if _current is None:
        _current = Telemetry("unconfigured", "0", None, None)
    return _current


def setup(service: str, version: str, endpoint: str = "", environment: str = "local", *, span_exporter=None, metric_reader=None,
          set_global: bool = True, export_interval_ms: int = 5000) -> Telemetry:
    """Configure tracing and metrics for this process. Exporters can be injected (tests); otherwise OTLP/HTTP to `endpoint`."""
    global _current
    resource = Resource.create({"service.name": service, "service.version": version, "deployment.environment": environment})
    tracer_provider = meter_provider = None
    if span_exporter is not None or endpoint:
        tracer_provider = TracerProvider(resource=resource)
        if span_exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            span_exporter = OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter, schedule_delay_millis=1000))
    if metric_reader is not None or endpoint:
        if metric_reader is None:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint.rstrip("/") + "/v1/metrics"),
                                                          export_interval_millis=export_interval_ms)
        views = [View(instrument_name="*.duration", aggregation=ExplicitBucketHistogramAggregation(DURATION_BUCKETS))]
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader], views=views)
    if set_global:
        if tracer_provider is not None:
            trace.set_tracer_provider(tracer_provider)
        if meter_provider is not None:
            metrics.set_meter_provider(meter_provider)
    tel = Telemetry(service, version, tracer_provider, meter_provider)
    if set_global:
        _current = tel
    return tel
