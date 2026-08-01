from __future__ import annotations

import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from src import mcp_bridge


@pytest.mark.asyncio
async def test_get_proactive_events_returns_event_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [{"kind": "alert", "event_id": "event-1"}]
    monkeypatch.setattr(
        mcp_bridge.proactive_alerts,
        "fetch_proactive_events",
        lambda: events,
    )
    server = mcp_bridge.create_mcp_server()

    result = await server._tool_manager.call_tool("get_proactive_events", {})

    assert json.loads(result) == events


@pytest.mark.asyncio
async def test_get_proactive_events_exposes_fetch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_fetch() -> list[dict[str, object]]:
        raise RuntimeError("calendar fetch failed")

    monkeypatch.setattr(
        mcp_bridge.proactive_alerts,
        "fetch_proactive_events",
        fail_fetch,
    )
    server = mcp_bridge.create_mcp_server()

    with pytest.raises(ToolError, match="calendar fetch failed"):
        await server._tool_manager.call_tool("get_proactive_events", {})
