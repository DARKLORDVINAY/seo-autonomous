"""Metadata-only structured logs. Credentials and external page content are never logged."""
import logging
import re
import time
import uuid

import structlog
from fastapi import FastAPI, Request

_tracing_configured = False


def configure_tracing() -> None:
    """Export correlation metadata to structured logs; no external trace upload."""
    global _tracing_configured
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if _tracing_configured:
        return
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

    class MetadataExporter(SpanExporter):
        def export(self, spans):
            log = structlog.get_logger("seo.trace")
            for span in spans:
                # Deliberately omit URLs, headers, bodies, exception messages,
                # model input/output and arbitrary span attributes.
                log.info("trace_span", trace_id=format(span.context.trace_id, "032x"),
                         span_id=format(span.context.span_id, "016x"),
                         parent_id=format(span.parent.span_id, "016x") if span.parent else None,
                         status=span.status.status_code.name,
                         duration_ms=round((span.end_time - span.start_time) / 1_000_000, 2),
                         cycle_id=span.attributes.get("seo.cycle_id"))
            return SpanExportResult.SUCCESS

    provider = TracerProvider(resource=Resource.create({"service.name": "spiral-max-seo"}))
    provider.add_span_processor(SimpleSpanProcessor(MetadataExporter()))
    trace.set_tracer_provider(provider)
    _tracing_configured = True


def instrument(app: FastAPI, log_level: str = "INFO") -> None:
    structlog.configure(processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ], wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, log_level.upper(), logging.INFO)))
    log = structlog.get_logger("seo.http")

    @app.middleware("http")
    async def request_metadata(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if re.fullmatch(r"[A-Za-z0-9-]{1,64}", incoming) else str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        route = request.scope.get("route")
        log.info("http_request", request_id=request_id, method=request.method,
                 route=getattr(route, "path", "unmatched"), status=response.status_code,
                 latency_ms=round((time.monotonic() - started) * 1000, 2))
        return response

    configure_tracing()
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")
