"""Search Analytics final-only, bounded pagination with explicit coverage caveats."""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import quote

from pydantic import ValidationError

from backend.app.contracts import GSCRow, ProviderUnavailable
from backend.app.integrations.common import (
    GoogleADCToken, MalformedResponse, ObservationBatch, dates, json_response, request, safe_client,
)


class GSCClient:
    is_fixture = False

    def __init__(self, property_url: str, *, token_provider=None, client=None):
        if not property_url:
            raise ProviderUnavailable("GSC_PROPERTY is required")
        if len(property_url) > 2048 or not property_url.startswith(("sc-domain:", "https://", "http://")):
            raise ValueError("Invalid Search Console property")
        self.property_url = property_url
        self.token_provider = token_provider or GoogleADCToken("https://www.googleapis.com/auth/webmasters.readonly")
        self.client = client or safe_client()

    def fetch(self, start: date | str, end: date | str, *, dimensions=("date", "page", "query"),
              max_rows: int = 50000, page_size: int = 25000) -> ObservationBatch[GSCRow]:
        start, end = dates(start, end)
        if not 1 <= page_size <= 25000 or not 1 <= max_rows <= 250000:
            raise ValueError("Invalid GSC row budget")
        if not {"date", "page"}.issubset(dimensions) or len(set(dimensions)) != len(dimensions):
            raise ValueError("Unique date and page dimensions are required")
        if set(dimensions) - {"date", "page", "query", "country", "device"}:
            raise ValueError("Unsupported GSC dimension")
        url = f"https://www.googleapis.com/webmasters/v3/sites/{quote(self.property_url, safe='')}/searchAnalytics/query"
        rows, seen = [], set()
        exhausted = False
        while len(rows) < max_rows:
            limit = min(page_size, max_rows - len(rows))
            payload = {"startDate": str(start), "endDate": str(end), "dimensions": list(dimensions),
                       "dataState": "final", "type": "web", "rowLimit": limit,
                       "startRow": len(rows), "aggregationType": "auto"}
            data = json_response(request(self.client, "POST", retry_safe=True, url= url, json=payload,
                                         headers={"Authorization": f"Bearer {self.token_provider()}"}))
            if not isinstance(data, dict) or ("rows" in data and not isinstance(data["rows"], list)):
                raise MalformedResponse("Malformed GSC response")
            raw_rows = data.get("rows", [])  # GSC's documented empty result omits rows.
            if len(raw_rows) > limit:
                raise MalformedResponse("GSC exceeded requested page size")
            for row in raw_rows:
                try:
                    if not isinstance(row, dict) or len(row["keys"]) != len(dimensions):
                        raise ValueError("Dimension mismatch")
                    identity = tuple(row["keys"])
                    if identity in seen:
                        raise ValueError("Duplicate or stalled pagination")
                    fields = dict(zip(dimensions, row["keys"], strict=True))
                    result = GSCRow(**fields, clicks=row["clicks"], impressions=row["impressions"],
                                    position=row["position"], data_state="final")
                    if not start <= result.date <= end:
                        raise ValueError("Out-of-range date")
                except (KeyError, TypeError, ValueError, ValidationError) as exc:
                    raise MalformedResponse("Malformed GSC row or repeated pagination") from exc
                seen.add(identity)
                rows.append(result)
            if len(raw_rows) < limit:
                exhausted = True
                break
        present = {row.date for row in rows}
        requested = {start + timedelta(days=i) for i in range((end - start).days + 1)}
        flags = ["gsc_top_rows_only", "omitted_dates_are_unknown", "gsc_dates_are_pacific_time"]
        if "query" in dimensions:
            flags.append("anonymised_queries_omitted_do_not_sum_as_page_totals")
        if not exhausted:
            flags.append("row_budget_reached")
        if requested - present:
            flags.append("missing_dates")
        return ObservationBatch(rows, "google_search_console", flags, complete=False, metadata={
            "property": self.property_url, "start": str(start), "end": str(end),
            "dimensions": list(dimensions), "pagination_exhausted": exhausted,
            "data_state": "final", "missing_dates": sorted(map(str, requested - present)),
            "full_dataset_guaranteed": False,
        })

    def fetch_page_totals(self, start, end, **kwargs) -> ObservationBatch[GSCRow]:
        return self.fetch(start, end, dimensions=("date", "page"), **kwargs)

    def inspect_url(self, url: str) -> ObservationBatch[dict]:
        """Read Google's stored index status, not a live indexability test."""
        from urllib.parse import urlsplit
        from backend.app.integrations.crawler.network import validate_url
        url = validate_url(url)
        if self.property_url.startswith("sc-domain:"):
            domain = self.property_url.removeprefix("sc-domain:").lower()
            host = urlsplit(url).hostname or ""
            if host != domain and not host.endswith("." + domain):
                raise ValueError("Inspection URL is outside the configured property")
        elif not url.startswith(self.property_url.rstrip("/") + "/"):
            raise ValueError("Inspection URL is outside the configured URL-prefix property")
        data = json_response(request(self.client, "POST",
            "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect", retry_safe=True,
            headers={"Authorization": f"Bearer {self.token_provider()}"},
            json={"inspectionUrl": url, "siteUrl": self.property_url, "languageCode": "en-US"}))
        if not isinstance(data, dict) or not isinstance(data.get("inspectionResult"), dict):
            raise MalformedResponse("Search Console inspection result missing")
        result = data["inspectionResult"]
        flags = ["google_stored_index_version_not_live_test"]
        if not isinstance(result.get("indexStatusResult"), dict):
            flags.append("index_status_missing")
        return ObservationBatch([result], "google_search_console:url_inspection", flags,
            complete=False, metadata={"url": url, "property": self.property_url, "live_test": False})
