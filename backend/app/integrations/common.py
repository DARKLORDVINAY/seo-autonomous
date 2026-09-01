"""Bounded HTTP, provider errors, and explicit observation coverage."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Generic, TypeVar

import httpx

from backend.app.contracts import ProviderUnavailable, utcnow

T = TypeVar("T")


class ProviderError(RuntimeError):
    """Provider failed; the observation is unknown, never a zero."""


class MalformedResponse(ProviderError):
    pass


class AmbiguousWriteError(ProviderError):
    """The remote write may have happened. Reconcile state before any retry."""


@dataclass
class ObservationBatch(Generic[T]):
    rows: list[T]
    source: str
    quality_flags: list[str] = field(default_factory=list)
    complete: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=utcnow)

    @property
    def is_fixture(self) -> bool:
        return self.source.startswith("fixture:")


def dates(start: date | str, end: date | str) -> tuple[date, date]:
    start, end = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    if start > end:
        raise ValueError("start must be no later than end")
    if (end - start).days > 500:
        raise ValueError("Date range exceeds 501-day per-run budget")
    return start, end


def json_response(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (ValueError, UnicodeError) as exc:
        raise MalformedResponse("Provider returned malformed JSON") from exc


def request(
    client: httpx.Client, method: str, url: str, *, retry_safe: bool | None = None,
    attempts: int = 3, max_bytes: int = 10_000_000,
    sleep: Callable[[float], None] = time.sleep, **kwargs: Any,
) -> httpx.Response:
    """Retry only reads (including read-style POST). No credential-bearing errors."""
    if attempts < 1 or attempts > 5:
        raise ValueError("attempts must be within 1..5")
    if retry_safe is None:
        retry_safe = method.upper() in {"GET", "HEAD", "OPTIONS"}
    tries = attempts if retry_safe else 1
    for attempt in range(tries):
        try:
            with client.stream(method, url, follow_redirects=False, **kwargs) as response:
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        if not retry_safe:
                            raise AmbiguousWriteError("Write response exceeded limit; reconcile remote state")
                        raise ProviderError("Provider response exceeded byte limit")
                # A detached bounded response avoids consuming a potentially unbounded stream.
                result = httpx.Response(response.status_code, headers=response.headers,
                                        content=bytes(content), request=response.request)
        except httpx.HTTPError as exc:
            if not retry_safe:
                raise AmbiguousWriteError("Write transport failed; reconcile remote state before retry") from exc
            if attempt + 1 == tries:
                raise ProviderError("Provider transport failed after bounded retries") from exc
            sleep(min(2 ** attempt, 4))
            continue
        status = result.status_code
        if 200 <= status < 300:
            return result
        if not retry_safe:
            if status >= 500 or status in (408, 429):
                raise AmbiguousWriteError("Write response inconclusive; reconcile remote state before retry")
            raise ProviderError(f"Provider rejected write (HTTP {status})")
        if status in (429, 500, 502, 503, 504) and attempt + 1 < tries:
            value = result.headers.get("retry-after", "")
            try:
                delay = float(value)
            except ValueError:
                try:
                    delay = (parsedate_to_datetime(value) - utcnow()).total_seconds()
                except (ValueError, TypeError):
                    delay = 2 ** attempt
            # Long provider backoff goes to the operational scheduler, not a busy loop.
            if delay > 10:
                raise ProviderError(f"Provider rate limited/unavailable (HTTP {status}); retry later")
            sleep(max(0, min(delay, 10)))
            continue
        if status in (401, 403):
            raise ProviderUnavailable(f"Provider access denied (HTTP {status}); verify scoped credentials")
        raise ProviderError(f"Provider request failed (HTTP {status})")
    raise ProviderError("Provider retry budget exhausted")


class GoogleADCToken:
    """Load read-only application default credentials without accepting chat secrets."""

    def __init__(self, scope: str) -> None:
        if scope not in {
            "https://www.googleapis.com/auth/webmasters.readonly",
            "https://www.googleapis.com/auth/analytics.readonly",
        }:
            raise ValueError("Only supported read-only scopes are allowed")
        self.scope = scope
        self._credentials: Any = None

    def __call__(self) -> str:
        import google.auth
        from google.auth.transport.requests import Request
        try:
            if self._credentials is None:
                self._credentials, _ = google.auth.default(scopes=[self.scope])
            if not self._credentials.valid:
                self._credentials.refresh(Request())
            if not self._credentials.token:
                raise ProviderUnavailable("Google ADC returned no token")
            return self._credentials.token
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable(
                "Google ADC unavailable; mount a credential file or use workload identity with read-only access"
            ) from exc


def safe_client() -> httpx.Client:
    from backend.app.integrations.crawler.network import PublicHTTPTransport
    return httpx.Client(transport=PublicHTTPTransport(), timeout=20, follow_redirects=False, trust_env=False)
