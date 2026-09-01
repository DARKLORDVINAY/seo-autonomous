"""AI Mode samples via the documented DataForSEO endpoint, when configured."""
from backend.app.contracts import ProviderUnavailable


class AISearchClient:
    is_fixture = False

    def __init__(self, serp_client=None):
        self.serp_client = serp_client

    def status(self):
        return {"supported": True, "configured": bool(self.serp_client and self.serp_client.enabled
                and self.serp_client.login and self.serp_client.password),
                "provider": "dataforseo:ai_mode", "visibility": None,
                "limitation": "One provider locale/device sample cannot establish universal AI visibility"}

    def search(self, keyword: str, location_code: int, language_code: str = "en"):
        if not self.serp_client:
            raise ProviderUnavailable("AI Mode requires configured, explicitly enabled DataForSEO credentials")
        return self.serp_client.search(keyword, location_code, language_code, mode="ai_mode")
