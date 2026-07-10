import importlib.util
from pathlib import Path

from agent.plugins import ManagedServiceSpec, McpServerSpec

from plugin import CalendarPlugin


def test_calendar_runtime_ownership() -> None:
    assert CalendarPlugin.mcp_servers() == [
        McpServerSpec(name="calendar", command=("python", "mcp/run_mcp.py"))
    ]


def test_mcp_uses_plugin_data_dir(tmp_path, monkeypatch) -> None:
    module_path = Path(__file__).resolve().parents[1] / "mcp/run_mcp.py"
    spec = importlib.util.spec_from_file_location("calendar_run_mcp", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path))
    original_cwd = Path.cwd()

    try:
        data_dir = module._configure_environment()

        assert data_dir == tmp_path
        assert module.os.environ["TOKEN_FILE_PATH"] == str(
            tmp_path / ".gcp-saved-tokens.json"
        )
        assert module.os.environ["CALENDAR_PROACTIVE_CONFIG_PATH"] == str(
            tmp_path / "proactive_alerts.json"
        )
    finally:
        module.os.chdir(original_cwd)
    assert CalendarPlugin.managed_services() == [
        ManagedServiceSpec(
            id="calendar_api",
            command=("python", "mcp/run_server.py"),
            cwd="mcp",
            readiness_url="http://127.0.0.1:18000/health",
        )
    ]
