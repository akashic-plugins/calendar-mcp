import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_mcp_rejects_missing_plugin_data_dir(monkeypatch) -> None:
    module_path = Path(__file__).resolve().parents[1] / "mcp/run_mcp.py"
    spec = importlib.util.spec_from_file_location("calendar_run_mcp_missing", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", "   ")

    with pytest.raises(RuntimeError, match="AKA_PLUGIN_DATA_DIR"):
        module._configure_environment()


def test_activate_rejects_missing_context_paths(tmp_path: Path) -> None:
    plugin = CalendarPlugin()
    plugin.context = SimpleNamespace(data_dir=None, workspace=tmp_path)
    with pytest.raises(RuntimeError, match="数据目录"):
        plugin.activate()

    plugin.context = SimpleNamespace(data_dir=tmp_path, workspace=None)
    with pytest.raises(RuntimeError, match="workspace"):
        plugin.activate()


@pytest.mark.parametrize(
    ("relative_path", "module_name"),
    (
        ("mcp/run_server.py", "calendar_server_missing"),
        ("mcp/src/auth.py", "calendar_auth_missing"),
    ),
)
def test_other_entrypoints_reject_missing_plugin_data_dir(
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    module_name: str,
) -> None:
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", "   ")

    with pytest.raises(RuntimeError, match="AKA_PLUGIN_DATA_DIR"):
        spec.loader.exec_module(module)
