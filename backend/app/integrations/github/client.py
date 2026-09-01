"""Read-only GitHub repository metadata and commits; no deployment tool."""
import re

from backend.app.integrations.common import MalformedResponse, json_response, request, safe_client


class GitHubClient:
    is_fixture = False

    def __init__(self, repository: str, *, token: str = "", client=None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("GitHub repository must be owner/repo")
        self.repository, self.token = repository, token
        self.client = client or safe_client()

    def _get(self, suffix=""):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = request(self.client, "GET", f"https://api.github.com/repos/{self.repository}{suffix}", headers=headers)
        return json_response(response)

    def get_repository(self) -> dict:
        result = self._get()
        if not isinstance(result, dict):
            raise MalformedResponse("GitHub repository response must be an object")
        return {field: result.get(field) for field in (
            "full_name", "private", "html_url", "default_branch", "pushed_at", "archived")}

    def get_commits(self, *, limit=10) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("Commit limit must be 1..100")
        result = self._get(f"/commits?per_page={limit}")
        if not isinstance(result, list):
            raise MalformedResponse("GitHub commits response must be a list")
        return result
