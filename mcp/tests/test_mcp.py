import httpx
import pytest

from seo_mcp.server import ControlClient, create_server


@pytest.mark.asyncio
async def test_mcp_registry_has_no_unrestricted_or_approval_tools():
    server = create_server(ControlClient(token="fixture-only", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "ok"}))))
    tools = await server.list_tools()
    names = {tool.name for tool in tools}
    assert {"health", "get_site_state", "create_metadata_draft", "execute_approved_revision", "detect_content_decay"} <= names
    assert not any(x in names for x in {"execute_arbitrary_sql", "run_arbitrary_shell", "approve_revision", "publish_page", "change_robots"})
    assert len(tools) >= 30
    assert next(x for x in tools if x.name == "get_site_state").annotations.readOnlyHint is True
    assert next(x for x in tools if x.name == "execute_approved_revision").annotations.destructiveHint is True


@pytest.mark.asyncio
async def test_health_tool_reaches_control_plane():
    server = create_server(ControlClient(token="test", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "ok"}))))
    result = await server.call_tool("health", {})
    assert "ok" in str(result)


@pytest.mark.asyncio
async def test_tool_id_cannot_escape_fixed_paths():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})
    server = create_server(ControlClient(token="test", transport=httpx.MockTransport(handler)))
    with pytest.raises(Exception):
        await server.call_tool("get_site_state", {"site_id": "../../admin"})
    assert calls == []


def test_remote_mcp_refuses_unconfigured_oauth(monkeypatch):
    monkeypatch.delenv("MCP_OAUTH_ISSUER", raising=False)
    with pytest.raises(KeyError):
        create_server(remote=True)


def test_control_client_blocks_credential_bearing_or_insecure_origins():
    for url in ["http://remote.example", "https://user:pass@example.test", "https://example.test?token=secret"]:
        with pytest.raises(ValueError):
            ControlClient(url, "test")
