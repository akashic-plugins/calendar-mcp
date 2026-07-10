from agent.plugins import ManagedServiceSpec, McpServerSpec

from plugin import CalendarPlugin


def test_calendar_runtime_ownership() -> None:
    assert CalendarPlugin.mcp_servers() == [
        McpServerSpec(name="calendar", command=("python", "mcp/run_mcp.py"))
    ]
    assert CalendarPlugin.managed_services() == [
        ManagedServiceSpec(
            id="calendar_api",
            command=("python", "mcp/run_server.py"),
            cwd="mcp",
            readiness_url="http://127.0.0.1:18000/health",
        )
    ]
