from __future__ import annotations

import os
import json
import shutil
import socket
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import pytest

from session.log import MessageLog
from agent.plugin_composition import MANAGED_PROCESSES, MCP_SERVERS
from agent.plugin_composition.bindings import Bindings
from agent.plugins.manager import PluginManager
from agent.plugins.selection import PluginSelection
from agent.plugins.snapshot import lease_runtime_snapshot
from collections.abc import Mapping

from calendar_test_plugin.tools import CALENDAR_TOOLS  # pyright: ignore[reportMissingImports]
from plugins.tools.plugin import TOOLS
from agent.plugins.python_environment import ENVIRONMENT_FILE, PythonEnvironments
from agent.plugins.static_manifest import load_static_plugin_manifest
from bus.event_bus import EventBus


ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get("AKASHIC_AGENT_ROOT", "")).resolve()
FORMAL_PORT = 18000
EXPECTED_TOOLS = frozenset(
    {
        "add_attendee",
        "analyze_busyness",
        "check_attendee_status",
        "create_calendar",
        "create_event",
        "delete_event",
        "find_events",
        "list_calendars",
        "query_free_busy",
        "quick_add_event",
        "schedule_mutual",
        "update_event",
    }
)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _fixture_runtime() -> Path:
    """Use the caller-selected artifact Python runtime for MCP subprocesses."""

    artifact_python = Path(os.environ["AKASHIC_PLUGIN_FIXTURE_PYTHON"])
    return artifact_python.parent.parent


def _stage_calendar(tmp_path: Path) -> Path:
    source = tmp_path / "calendar"
    (source / "mcp" / "src").mkdir(parents=True)
    for relative in (
        "plugin.py",
        "tools.py",
        "_tool_contract.py",
        "tool_catalog.json",
        "mcp/requirements.txt",
        "mcp/run_mcp.py",
        "mcp/run_server.py",
    ):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    shutil.copytree(ROOT / "mcp" / "src", source / "mcp" / "src", dirs_exist_ok=True)
    (source / "mcp" / ".venv").symlink_to(
        _fixture_runtime(), target_is_directory=True
    )
    return source


def _stage_content(tmp_path: Path) -> Path:
    source = tmp_path / "eventmail"
    shutil.copytree(CORE / "plugins" / "eventmail", source)
    return source


def _prepare_python_environment(source: Path, workspace: Path) -> None:
    """通过安装 owner 为测试 artifact 固定独立 Python 环境。"""

    manifest = load_static_plugin_manifest(source)
    environments = PythonEnvironments(workspace)
    refs = {
        item.runtime_root: environments.prepare(source, item)
        for item in manifest.python
    }
    (source / ENVIRONMENT_FILE).write_text(json.dumps(refs), encoding="utf-8")


@pytest.mark.asyncio
async def test_manager_boots_calendar_with_content_and_no_proactive_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot the real loader/process/MCP boundary without starting the poll lifecycle."""

    runtime = _fixture_runtime()
    runtime_python = runtime / "bin" / "python"
    if not runtime_python.is_file() or not os.access(runtime_python, os.X_OK):
        pytest.fail(f"Calendar MCP runtime is not staged: {runtime_python}")
    assert _port_free(FORMAL_PORT)
    monkeypatch.setenv(
        "PATH", f"{runtime / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    calendar = _stage_calendar(tmp_path)
    content = _stage_content(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    PluginSelection(workspace).initialize()
    _prepare_python_environment(calendar, workspace)
    log = MessageLog(tmp_path / "sessions.db")
    providers = tmp_path / "providers"
    for provider in ("content", "mcp", "managed_processes", "tools"):
        shutil.copytree(CORE / "plugins" / provider, providers / provider)
    manager = PluginManager(
        message_log=log,
        plugin_dirs=[content, calendar, providers],
        event_bus=EventBus(),
        workspace=workspace,
        installed_cache_root=tmp_path / "cache",
    )
    try:
        await manager.load_all()
        snapshot = manager.current_snapshot
        assert snapshot is not None and snapshot.composition_root is not None
        root = snapshot.composition_root
        generations = {
            item.plugin_id: item for item in snapshot.generations.values()
        }
        assert "calendar" in generations and "eventmail" in generations

        processes = root.context.require(MANAGED_PROCESSES)
        mcp = root.context.require(MCP_SERVERS)
        monitor = processes._entries[("calendar", "calendar_api")]  # pyright: ignore[reportPrivateUsage]
        endpoint = monitor._host.endpoint(monitor._id, "calendar_api")  # pyright: ignore[reportPrivateUsage]
        assert endpoint.port == FORMAL_PORT
        assert mcp._entries["calendar"].definition.required_tools == ()  # pyright: ignore[reportPrivateUsage]

        async with lease_runtime_snapshot(manager.snapshot_store) as leased:
            bound_root = leased.composition_root
            assert bound_root is not None
            bindings = Bindings(log, manager._archive, bound_root)  # pyright: ignore[reportPrivateUsage]
            tools = bound_root.context.require(TOOLS)
            view = bound_root.context.require(CALENDAR_TOOLS)
            names = {ref.name for ref in view.refs}
            assert {f"mcp_calendar__{name}" for name in EXPECTED_TOOLS} == names

            async def allow(_binding: str, _arguments: object) -> Mapping[str, object]:
                return {"allowed": True}

            execution = tools.execution(allow)
            binding = tools.bind(view.select("mcp_calendar__analyze_busyness"), bindings)
            call = await execution.execute("fixture-busyness", binding, {})
            assert call.outcome == "error"
            assert "validation error" in str(call.parts[0].value).lower()

        with build_opener(ProxyHandler({})).open(endpoint.readiness_url, timeout=3) as response:
            assert response.status == 200

        receipt = root.receipt()
        health = {item.name: item.healthy for item in receipt.health}
        assert health["process:calendar_api"]
        assert health["mcp:calendar"]
        assert not any(name.startswith("proactive:") for name in health)
        process_lines = list(
            monitor._host.logs(monitor._id, "calendar_api").lines  # pyright: ignore[reportPrivateUsage]
        )
        assert any("/health" in line and "200" in line for line in process_lines)
    finally:
        await manager.terminate_all()
        log.close()

    assert _port_free(FORMAL_PORT)
