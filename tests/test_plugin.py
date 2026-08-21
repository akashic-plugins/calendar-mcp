import importlib.util
import sys
from pathlib import Path

import pytest

import plugin as calendar_module
from agent.plugin_composition import (
    MANAGED_PROCESSES,
    MCP_SERVERS,
    PROACTIVE_COMPONENTS,
    CompositionRoot,
    PluginProactiveComponents,
    PluginRuntime,
)
from agent.plugin_composition.mcp_slots import _freeze_plugin_mcp_servers
from agent.plugin_composition.mcp_slots import PluginMcpServers
from agent.plugin_composition.process_slots import (
    PluginManagedProcesses,
    _freeze_plugin_managed_processes,
)
from agent.plugin_composition.proactive import _freeze_plugin_proactive_components
from agent.plugins.composable import ComposablePlugin
from agent.plugins.static_manifest import load_static_plugin_manifest
from plugin import CalendarConfig, CalendarProactiveConfig
from src.mcp_bridge import (
    _acknowledge_proactive_events,
    _fetch_proactive_events,
    _proactive_ack_payload,
    _proactive_fetch_payload,
)


ROOT = Path(__file__).resolve().parents[1]


async def _mount(tmp_path: Path, *, proactive: bool = True):
    root = CompositionRoot("calendar:test")
    processes = PluginManagedProcesses(root.instance_token)
    servers = PluginMcpServers(root.instance_token)
    components = PluginProactiveComponents(root.instance_token)
    await root.context.provide(MANAGED_PROCESSES, processes)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(PROACTIVE_COMPONENTS, components)
    data_dir = tmp_path / "plugin-data"
    plugin = ComposablePlugin.from_module(calendar_module)
    await root.mount(
        plugin,
        name="calendar",
        runtime=PluginRuntime(
            plugin_id="calendar",
            plugin_dir=ROOT,
            data_dir=data_dir,
            workspace=tmp_path / "workspace",
            config=CalendarConfig(
                proactive=CalendarProactiveConfig(enabled=proactive)
            ),
        ),
    )
    return root, processes, servers, components, data_dir


@pytest.mark.asyncio
async def test_v3_apply_registers_exact_runtime_and_source_without_writes(
    tmp_path: Path,
) -> None:
    root, processes, servers, components, data_dir = await _mount(tmp_path)

    process_registry = _freeze_plugin_managed_processes(
        processes, root.instance_token
    )
    mcp_registry = _freeze_plugin_mcp_servers(servers, root.instance_token)
    proactive_catalog = _freeze_plugin_proactive_components(
        components,
        root.instance_token,
        {"calendar": "calendar:test"},
    )
    process = process_registry["calendar_api"].definition
    mcp = mcp_registry["calendar"].definition
    source = proactive_catalog.source("upcoming_events")

    assert process.command == ("python", "mcp/run_server.py")
    assert process.cwd == "."
    assert process.formal_port == 18000
    assert process.env == {
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
        "HOST": "127.0.0.1",
    }
    assert mcp.command == ("python", "mcp/run_mcp.py")
    assert mcp.required_tools == (
        "get_proactive_events",
        "acknowledge_events",
    )
    assert mcp.candidate_read_only_tools == ("get_proactive_events",)
    assert mcp.candidate_env == {
        "CALENDAR_BACKEND": "recording",
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
        "HOST": "127.0.0.1",
    }
    assert mcp.endpoint_env[0].process == "calendar_api"
    assert source is not None
    assert source.definition.channels == ("alert",)
    assert source.definition.mcp_server == "calendar"
    assert not data_dir.exists()
    await root.dispose()


@pytest.mark.asyncio
async def test_disabled_proactive_keeps_runtime_but_omits_source(tmp_path: Path) -> None:
    root, processes, servers, components, data_dir = await _mount(
        tmp_path,
        proactive=False,
    )

    assert len(_freeze_plugin_managed_processes(processes, root.instance_token)) == 1
    assert len(_freeze_plugin_mcp_servers(servers, root.instance_token)) == 1
    assert len(
        _freeze_plugin_proactive_components(
            components,
            root.instance_token,
            {"calendar": "calendar:test"},
        ).sources
    ) == 0
    assert not data_dir.exists()
    await root.dispose()


def test_static_manifest_matches_v3_module_and_runtime() -> None:
    manifest = load_static_plugin_manifest(ROOT)

    assert manifest.name == calendar_module.name == "calendar"
    assert manifest.version == calendar_module.version == "3.0.0"
    assert manifest.api_version == calendar_module.api_version == 3
    assert manifest.entrypoint == "plugin.py"
    assert manifest.exclude_data_paths == (
        ".env",
        ".gcp-saved-tokens.json",
        ".calendar-v2-migration.json",
    )
    assert manifest.managed_processes[0].python_runtime == "mcp"
    assert manifest.mcp_servers[0].python_runtime == "mcp"
    assert manifest.managed_processes[0].env == (
        ("GOOGLE_CLIENT_ID", ""),
        ("GOOGLE_CLIENT_SECRET", ""),
        ("HOST", "127.0.0.1"),
    )
    assert manifest.mcp_servers[0].candidate_env == (
        ("CALENDAR_BACKEND", "recording"),
        ("GOOGLE_CLIENT_ID", ""),
        ("GOOGLE_CLIENT_SECRET", ""),
        ("HOST", "127.0.0.1"),
    )
    assert not hasattr(calendar_module, "CalendarPlugin")


def test_proactive_results_are_explicit_and_partial_ack_never_commits() -> None:
    event = {"id": "event-1"}
    assert _proactive_fetch_payload([]) == {"status": "empty"}
    assert _proactive_fetch_payload([event]) == {
        "status": "items",
        "items": [event],
    }


def test_recording_backend_is_local_and_rejects_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CALENDAR_BACKEND", "recording")
    sys.modules.pop("src.proactive_alerts", None)

    assert _fetch_proactive_events() == {"status": "empty"}
    assert "src.proactive_alerts" not in sys.modules
    with pytest.raises(RuntimeError, match="不允许确认"):
        _acknowledge_proactive_events(["event-1"])
    assert "src.proactive_alerts" not in sys.modules
    assert _proactive_ack_payload(
        ["event-1"],
        {"acknowledged": ["event-1"], "failed": []},
    ) == {"status": "committed", "ids": ["event-1"]}
    assert _proactive_ack_payload(
        ["event-1", "event-2"],
        {"acknowledged": ["event-1"], "failed": ["event-2"]},
    ) == {
        "status": "failure",
        "error": "calendar ack 未完整提交",
        "retryable": True,
        "failed_ids": ["event-2"],
    }


def test_mcp_uses_plugin_data_dir(tmp_path, monkeypatch) -> None:
    module_path = ROOT / "mcp/run_mcp.py"
    spec = importlib.util.spec_from_file_location("calendar_run_mcp", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / ".env").write_text(
        "GOOGLE_CLIENT_ID=formal-id\nGOOGLE_CLIENT_SECRET=formal-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "ambient-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "ambient-secret")
    monkeypatch.setenv("HOST", "0.0.0.0")
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
        assert module.os.environ["GOOGLE_CLIENT_ID"] == "formal-id"
        assert module.os.environ["GOOGLE_CLIENT_SECRET"] == "formal-secret"
        assert module.os.environ["HOST"] == "127.0.0.1"
    finally:
        module.os.chdir(original_cwd)


def test_managed_server_uses_stderr_only_and_fixed_loopback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_path = ROOT / "mcp/run_server.py"
    spec = importlib.util.spec_from_file_location("calendar_server_logging", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CALENDAR_LOG_PATH", "/must/not/be/opened")
    monkeypatch.setenv("HOST", "0.0.0.0")
    spec.loader.exec_module(module)
    invocation: dict[str, object] = {}

    def capture_run(_app: str, **kwargs: object) -> None:
        invocation.update(kwargs)

    monkeypatch.setattr(module.uvicorn, "run", capture_run)
    original_cwd = Path.cwd()
    try:
        module.main()
    finally:
        module.os.chdir(original_cwd)

    assert tuple(module.LOGGING_CONFIG["handlers"]) == ("default",)
    assert module.LOGGING_CONFIG["root"]["handlers"] == ["default"]
    assert "FileHandler" not in repr(module.LOGGING_CONFIG)
    assert invocation["host"] == "127.0.0.1"


def test_mcp_rejects_missing_plugin_data_dir(monkeypatch) -> None:
    module_path = ROOT / "mcp/run_mcp.py"
    spec = importlib.util.spec_from_file_location("calendar_run_mcp_missing", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", "   ")

    with pytest.raises(RuntimeError, match="AKA_PLUGIN_DATA_DIR"):
        module._configure_environment()


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
    module_path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", "   ")

    with pytest.raises(RuntimeError, match="AKA_PLUGIN_DATA_DIR"):
        spec.loader.exec_module(module)
