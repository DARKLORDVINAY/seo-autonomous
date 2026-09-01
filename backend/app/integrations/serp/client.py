"""Explicitly opt-in paid SERP observation; never an SEO mutation capability."""
from __future__ import annotations

import re
import httpx

from backend.app.contracts import ProviderUnavailable
from backend.app.integrations.common import MalformedResponse, ObservationBatch, ProviderError, json_response, request, safe_client


class DataForSEOClient:
    is_fixture = False

    def __init__(self, login: str = "", password: str = "", *, client=None,
                 enabled: bool = False, max_depth: int = 10):
        self.login, self.password = login, password
        self.enabled = enabled
        if not 1 <= max_depth <= 100:
            raise ValueError("Invalid SERP depth budget")
        self.max_depth = max_depth
        self.client = client or safe_client()

    def search(self, keyword: str, location_code: int, language_code: str = "en", *, mode: str = "organic"):
        if not self.enabled:
            raise ProviderUnavailable("SERP paid observations disabled; configure explicit budget and enable provider")
        if not self.login or not self.password:
            raise ProviderUnavailable("DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD are required")
        if mode not in ("organic", "ai_mode"):
            raise ValueError("Unsupported SERP mode")
        if not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 500:
            raise ValueError("Keyword is empty or exceeds budget")
        if not isinstance(location_code, int) or location_code <= 0 or not re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", language_code):
            raise ValueError("Invalid locale")
        payload = {"keyword": keyword, "location_code": location_code, "language_code": language_code}
        if mode == "organic":
            payload["depth"] = self.max_depth
        # Live POST is read-like for the site, but can incur duplicate charges.
        # Only one attempt: the authoritative scheduler can choose a later new sample.
        response = request(self.client, "POST", retry_safe=True, url= f"https://api.dataforseo.com/v3/serp/google/{mode}/live/advanced",
                           json=[payload], auth=httpx.BasicAuth(self.login, self.password), attempts=1)
        data = json_response(response)
        if not isinstance(data, dict) or data.get("status_code") != 20000:
            raise ProviderError("DataForSEO envelope reports failure; no snapshot recorded")
        tasks = data.get("tasks")
        if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
            raise MalformedResponse("DataForSEO expected one completed task")
        task = tasks[0]
        if task.get("status_code") != 20000:
            raise ProviderError("DataForSEO task not completed successfully; no snapshot recorded")
        results = task.get("result")
        if not isinstance(results, list) or not results or any(not isinstance(item, dict) for item in results):
            raise MalformedResponse("DataForSEO successful task has no usable result")
        # Preserve provider result structure. Do not infer absent AI citations as zero visibility.
        return ObservationBatch(results, f"dataforseo:{mode}", ["single_locale_snapshot", "untrusted_external_content"],
            complete=False, metadata={"keyword": keyword, "location_code": location_code,
                "language_code": language_code, "task_id": task.get("id"), "cost": data.get("cost"),
                "mode": mode, "source_trust": "untrusted_external"})
