"""GA4 organic reports with an explicit business-qualified outcome mapping."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.app.contracts import GA4Row, ProviderUnavailable, stable_hash, utcnow
from backend.app.integrations.common import (
    GoogleADCToken, MalformedResponse, ObservationBatch, ProviderError, dates, json_response, request, safe_client,
)


@dataclass
class _Report:
    rows: list[tuple[tuple[str, ...], tuple[int | float, ...]]]
    total: int
    metadata: list[dict]
    flags: set[str]

    @property
    def exhausted(self) -> bool:
        return len(self.rows) == self.total


class GA4Client:
    is_fixture = False
    DIMENSIONS = ("date", "landingPage", "sessionDefaultChannelGroup")
    METRICS = ("sessions", "keyEvents")
    QUALIFIED_DIMENSIONS = (*DIMENSIONS, "eventName")
    _REPORT_BLOCKERS = {
        "row_budget_reached", "thresholding", "sampled_report", "aggregated_other_rows",
        "landing_page_not_set", "schema_restrictions", "empty_report_reason",
        "report_timezone_unconfirmed", "timezone_disagreement",
    }

    def __init__(self, property_id: str, *, token_provider=None, client=None):
        property_id = str(property_id).removeprefix("properties/") if property_id else ""
        if not property_id:
            raise ProviderUnavailable("GA4_PROPERTY_ID is required")
        if not property_id.isdigit():
            raise ValueError("GA4 property ID must be numeric")
        self.property_id = property_id
        self.token_provider = token_provider or GoogleADCToken("https://www.googleapis.com/auth/analytics.readonly")
        self.client = client or safe_client()

    @staticmethod
    def _definition(definition: dict | None) -> dict | None:
        if definition is None or definition == {}:
            return None
        if not isinstance(definition, dict):
            raise ValueError("GA4 conversion_definition must be a dictionary")
        definition = deepcopy(definition)
        for field in ("verified", "tracking_verified", "qualification_verified", "deduplication_verified"):
            if definition.get(field) is not True:
                raise ValueError(f"GA4 conversion_definition requires {field}=true")
        events = definition.get("qualified_events")
        if (not isinstance(events, list) or not events
            or any(not isinstance(event, str) or not event.strip() or event != event.strip() for event in events)
            or len(set(events)) != len(events)):
            raise ValueError("GA4 qualified_events must contain distinct nonempty event names")
        for field in ("qualification_definition", "deduplication_method"):
            if not isinstance(definition.get(field), str) or not definition[field].strip():
                raise ValueError(f"GA4 conversion_definition requires {field}")
        currency = definition.get("currency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("GA4 conversion_definition requires a three-letter uppercase currency code")
        method = definition.get("value_method")
        if method == "fixed_per_qualified_conversion":
            value = definition.get("value_per_conversion")
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError("GA4 modeled lead value_per_conversion must be finite and nonnegative")
        elif method == "event_value":
            for field in ("currency_verified", "value_semantics_verified"):
                if definition.get(field) is not True:
                    raise ValueError(f"GA4 event_value requires {field}=true")
        else:
            raise ValueError("Unsupported GA4 qualified conversion value_method")
        return definition

    @staticmethod
    def _organic_filter() -> dict:
        return {"filter": {"fieldName": "sessionDefaultChannelGroup", "stringFilter": {
            "matchType": "EXACT", "value": "Organic Search", "caseSensitive": True}}}

    def _post(self, method: str, payload: dict):
        return json_response(request(self.client, "POST", retry_safe=True, url=
            f"https://analyticsdata.googleapis.com/v1beta/properties/{self.property_id}:{method}",
            json=payload, headers={"Authorization": f"Bearer {self.token_provider()}"}))

    def _check_compatibility(self, dimensions: tuple, metrics: tuple, dimension_filter: dict) -> None:
        data = self._post("checkCompatibility", {
            "dimensions": [{"name": name} for name in dimensions],
            "metrics": [{"name": name} for name in metrics],
            "dimensionFilter": dimension_filter, "compatibilityFilter": "COMPATIBLE",
        })
        try:
            for kind, required in (("dimension", dimensions), ("metric", metrics)):
                entries = data[kind + "Compatibilities"]
                if not isinstance(entries, list):
                    raise ValueError("Invalid compatibility list")
                found = {}
                for entry in entries:
                    name = entry[kind + "Metadata"]["apiName"]
                    if not isinstance(name, str) or name in found:
                        raise ValueError("Invalid or duplicate compatibility field")
                    found[name] = entry["compatibility"]
                if any(name not in found for name in required):
                    raise ValueError("Missing requested field compatibility")
                if any(found[name] != "COMPATIBLE" for name in required):
                    raise ProviderError("GA4 qualified outcome dimensions or metrics are incompatible")
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedResponse("Malformed GA4 compatibility response") from exc

    @staticmethod
    def _metric(value, name: str) -> int | float:
        if not isinstance(value, str):
            raise ValueError("GA4 metric values must be strings")
        if name in {"sessions", "eventCount"}:
            if not re.fullmatch(r"[0-9]+", value):
                raise ValueError("GA4 count must be a nonnegative integer")
            number = int(value)
            if not math.isfinite(number):
                raise ValueError("GA4 count must be finite")
            return number
        number = float(value)
        if not math.isfinite(number) or number < 0 or "_" in value:
            raise ValueError("GA4 metric must be finite and nonnegative")
        return number

    def _report(self, start, end, dimensions, metrics, dimension_filter, *, max_rows, page_size,
                qualified_events=(), currency=None) -> _Report:
        rows, seen, metadata_pages, flags = [], set(), [], set()
        total = None
        while len(rows) < max_rows:
            limit = min(page_size, max_rows - len(rows))
            payload = {
                "dateRanges": [{"startDate": str(start), "endDate": str(end)}],
                "dimensions": [{"name": d} for d in dimensions],
                "metrics": [{"name": m} for m in metrics],
                "dimensionFilter": dimension_filter,
                "orderBys": [{"dimension": {"dimensionName": d}} for d in dimensions],
                "limit": str(limit), "offset": str(len(rows)), "keepEmptyRows": False,
                "returnPropertyQuota": True,
            }
            if currency is not None:
                payload["currencyCode"] = currency
            data = self._post("runReport", payload)
            try:
                dims = [h["name"] for h in data["dimensionHeaders"]]
                metric_names = [h["name"] for h in data["metricHeaders"]]
                raw_count = data.get("rowCount", 0)
                if type(raw_count) not in (int, str) or not re.fullmatch(r"[0-9]+", str(raw_count)):
                    raise ValueError("Invalid report row count")
                count = int(raw_count)
                raw_rows = data.get("rows", [])
                if dims != list(dimensions) or metric_names != list(metrics):
                    raise ValueError("Unexpected headers")
                if not isinstance(raw_rows, list) or len(raw_rows) > limit or len(rows) + len(raw_rows) > count:
                    raise ValueError("Invalid row count")
                if total is not None and count != total:
                    raise ValueError("Dataset changed while paginating")
                total = count
                meta = data.get("metadata", {})
                if not isinstance(meta, dict):
                    raise ValueError("Invalid report metadata")
                metadata_pages.append(meta)
                for name, flag in (("subjectToThresholding", "thresholding"),
                                   ("dataLossFromOtherRow", "aggregated_other_rows")):
                    if name in meta and type(meta[name]) is not bool:
                        raise ValueError("Invalid report privacy flag")
                    if meta.get(name):
                        flags.update((name, flag))
                sampling = meta.get("samplingMetadatas", [])
                if not isinstance(sampling, list) or any(not isinstance(item, dict) for item in sampling):
                    raise ValueError("Invalid sampling metadata")
                if sampling:
                    flags.update(("samplingMetadatas", "sampled_report"))
                restrictions = meta.get("schemaRestrictionResponse", {})
                if not isinstance(restrictions, dict):
                    raise ValueError("Invalid schema restriction metadata")
                if restrictions.get("activeMetricRestrictions"):
                    flags.add("schema_restrictions")
                if meta.get("emptyReason"):
                    flags.add("empty_report_reason")
                if currency is not None and meta.get("currencyCode", currency) != currency:
                    raise ValueError("Qualified report currency differs from the requested currency")
                for raw in raw_rows:
                    ds = tuple(v["value"] for v in raw["dimensionValues"])
                    ms = [v["value"] for v in raw["metricValues"]]
                    if (len(ds) != len(dimensions) or len(ms) != len(metrics)
                        or any(not isinstance(value, str) for value in ds) or ds[2] != "Organic Search"):
                        raise ValueError("Unexpected GA4 row shape/channel")
                    if qualified_events and ds[3] not in qualified_events:
                        raise ValueError("Unexpected qualified event name")
                    if ds in seen:
                        raise ValueError("Repeated pagination")
                    if not re.fullmatch(r"[0-9]{8}", ds[0]):
                        raise ValueError("Invalid GA4 date")
                    row_date = datetime.strptime(ds[0], "%Y%m%d").date()
                    if not start <= row_date <= end:
                        raise ValueError("Out-of-range date")
                    if ds[1] in {"", "(not set)"}:
                        flags.add("landing_page_not_set")
                    if ds[1] == "(other)":
                        flags.add("aggregated_other_rows")
                    values = tuple(self._metric(value, name) for name, value in zip(metrics, ms, strict=True))
                    rows.append((ds, values))
                    seen.add(ds)
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise MalformedResponse("Malformed GA4 response or inconsistent pagination") from exc
            if len(rows) == total:
                break
            if not raw_rows:
                raise MalformedResponse("GA4 pagination ended before rowCount")
        if len(rows) < total:
            flags.add("row_budget_reached")
        return _Report(rows, total, metadata_pages, flags)

    def fetch(self, start, end, *, max_rows=50000, page_size=10000,
              conversion_definition: dict | None = None, outcome_holdback_days: int = 12) -> ObservationBatch[GA4Row]:
        """Fetch bounded reports; recent outcomes remain observations, not mature evidence.

        The holdback is an operator policy in the property's local calendar,
        not a Google guarantee of data finality. It never certifies empty rows.
        Row budgets apply independently to sessions and qualified event reports.
        """
        start, end = dates(start, end)
        if (type(page_size) is not int or type(max_rows) is not int
            or not 1 <= page_size <= 250000 or not 1 <= max_rows <= 250000):
            raise ValueError("Invalid GA4 row budget")
        if type(outcome_holdback_days) is not int or not 0 <= outcome_holdback_days <= 365:
            raise ValueError("GA4 outcome_holdback_days must be an integer within 0..365")
        definition = self._definition(conversion_definition)
        qualified_filter, qualified_metrics = None, None
        if definition is not None:
            qualified_filter = {"andGroup": {"expressions": [self._organic_filter(), {"filter": {
                "fieldName": "eventName", "inListFilter": {
                    "values": definition["qualified_events"], "caseSensitive": True}}}]}}
            qualified_metrics = ("eventCount", "eventValue") if definition["value_method"] == "event_value" else ("eventCount",)
            self._check_compatibility(self.QUALIFIED_DIMENSIONS, qualified_metrics, qualified_filter)
        sessions = self._report(start, end, self.DIMENSIONS, self.METRICS, self._organic_filter(),
                                max_rows=max_rows, page_size=page_size)
        rows = [GA4Row(date=datetime.strptime(ds[0], "%Y%m%d").date(), landing_page=ds[1],
                       sessions=values[0], key_events=values[1]) for ds, values in sessions.rows]
        flags = sessions.flags | {"omitted_dates_are_unknown"}
        present = {row.date for row in rows}
        requested = {start + timedelta(days=i) for i in range((end - start).days + 1)}
        if requested - present:
            flags.add("missing_dates")
        metadata = {
            "property": self.property_id, "start": str(start), "end": str(end),
            "scope": "all_organic_landing_pages", "reported_row_count": sessions.total,
            "report_metadata": sessions.metadata, "missing_dates": sorted(map(str, requested - present)),
            "pagination_exhausted": sessions.exhausted, "tracking_verified": definition is not None,
            "qualified_conversion_semantics_verified": definition is not None,
            "qualified_conversion_value_semantics_verified": definition is not None,
            "conversion_definition_hash": stable_hash(definition) if definition is not None else None,
            "complete_dates": [], "extraction_complete": False, "extraction_complete_dates": [],
        }
        held_dates = set()
        if definition is None:
            flags.add("business_conversion_definition_unconfirmed")
        else:
            qualified = self._report(start, end, self.QUALIFIED_DIMENSIONS, qualified_metrics, qualified_filter,
                max_rows=max_rows, page_size=page_size, qualified_events=definition["qualified_events"],
                currency=definition["currency"] if definition["value_method"] == "event_value" else None)
            flags.update(qualified.flags)
            metadata.update({
                "qualified_reported_row_count": qualified.total, "qualified_report_metadata": qualified.metadata,
                "sessions_pagination_exhausted": sessions.exhausted,
                "qualified_pagination_exhausted": qualified.exhausted,
                "pagination_exhausted": sessions.exhausted and qualified.exhausted,
                "qualified_report_compatibility_verified": True,
                "value_method": definition["value_method"], "currency": definition["currency"],
                "conversion_value_kind": "modeled_lead_value" if definition["value_method"] == "fixed_per_qualified_conversion"
                                         else "verified_event_value",
            })
            if definition["value_method"] == "fixed_per_qualified_conversion":
                flags.add("modeled_lead_value")
                metadata["value_per_conversion"] = definition["value_per_conversion"]
            report_zones = [page.get("timeZone") for page in sessions.metadata + qualified.metadata]
            report_zone = None
            if any(not isinstance(zone, str) or not zone for zone in report_zones):
                flags.add("report_timezone_unconfirmed")
            else:
                try:
                    zones = {zone: ZoneInfo(zone) for zone in report_zones}
                except (ValueError, ZoneInfoNotFoundError):
                    flags.add("report_timezone_unconfirmed")
                else:
                    if len(zones) != 1:
                        flags.add("timezone_disagreement")
                    else:
                        report_zone = next(iter(zones.values()))
            cutoff = (utcnow().astimezone(report_zone).date() - timedelta(days=outcome_holdback_days)
                      if report_zone is not None else None)
            held_dates = {day for day in requested if cutoff is not None and day >= cutoff}
            metadata.update({
                "report_timezone": report_zone.key if report_zone is not None else None,
                "report_timezone_verified": report_zone is not None,
                "outcome_holdback_days": outcome_holdback_days,
                "freshness_policy": "operator_calendar_day_holdback",
                "freshness_cutoff_exclusive": str(cutoff) if cutoff is not None else None,
                "held_back_dates": sorted(map(str, held_dates)), "data_finality_guaranteed": False,
            })
            by_key = {ds: row for (ds, _), row in zip(sessions.rows, rows, strict=True)}
            outcomes = {}
            unmatched = 0
            for ds, values in qualified.rows:
                key = ds[:3]
                if key not in by_key:
                    unmatched += 1
                    continue
                count, value = outcomes.get(key, (0, 0.0))
                try:
                    count += values[0]
                    value += (values[1] if definition["value_method"] == "event_value"
                              else values[0] * definition["value_per_conversion"])
                    if not math.isfinite(count) or not math.isfinite(value):
                        raise ValueError("Nonfinite qualified outcome total")
                except (OverflowError, ValueError) as exc:
                    raise MalformedResponse("GA4 qualified outcome total is not finite") from exc
                outcomes[key] = (count, value)
            metadata["unmatched_qualified_row_count"] = unmatched
            if unmatched:
                flags.add("api_disagreement")
            usable = not (flags & self._REPORT_BLOCKERS) and not unmatched
            if usable:
                for key, (count, value) in outcomes.items():
                    by_key[key].qualified_conversions = count
                    by_key[key].conversion_value = value
            missing = [row for row in rows if row.qualified_conversions is None]
            metadata["missing_qualified_row_count"] = len(missing)
            if missing or not rows:
                flags.add("qualified_outcome_missing")
            if usable:
                incomplete_dates = {row.date for row in missing}
                extraction_dates = present - incomplete_dates
                metadata["extraction_complete_dates"] = sorted(map(str, extraction_dates))
                metadata["extraction_complete"] = requested <= extraction_dates
                metadata["complete_dates"] = sorted(map(str, extraction_dates - held_dates))
        for row in rows:
            # A recent date must not contaminate mature dates in the same batch.
            row.quality_flags = sorted(flags | ({"data_not_final"} if row.date in held_dates else set()))
        complete = set(map(str, requested)) <= set(metadata["complete_dates"])
        return ObservationBatch(rows, "google_analytics_4", sorted(flags), complete=complete, metadata=metadata)
