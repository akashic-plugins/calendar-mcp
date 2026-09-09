from __future__ import annotations

import os
import json
import shutil
import socket
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import pytest

from session.log import MessageLog
from agent.plugins.manager import PluginManager
from agent.plugins.python_environment import ENVIRONMENT_FILE, PythonEnvironments
from agent.plugins.static_manifest import load_static_plugin_manifest
from bus.event_bus import EventBus


ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get("AKASHIC_AGENT_ROOT", "")).resolve()
MCP_RUNTIME = ROOT / "mcp" / ".venv"
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


def _stage_calendar(tmp_path: Path) -> Path:
    source = tmp_path / "calendar"
    (source / "mcp" / "src").mkdir(parents=True)
    for relative in (
        "plugin.py",
        "tools.py",
        "tool_catalog.json",
        "akashic.plugin.toml",
        "mcp/requirements.txt",
        "mcp/run_mcp.py",
        "mcp/run_server.py",
    ):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    shutil.copytree(ROOT / "mcp" / "src", source / "mcp" / "src", dirs_exist_ok=True)
    (source / "mcp" / ".venv").symlink_to(MCP_RUNTIME, target_is_directory=True)
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

    runtime_python = MCP_RUNTIME / "bin" / "python"
    if not runtime_python.is_file() or not os.access(runtime_python, os.X_OK):
        pytest.fail(f"Calendar MCP runtime is not staged: {runtime_python}")
    assert _port_free(FORMAL_PORT)
    monkeypatch.setenv(
        "PATH", f"{MCP_RUNTIME / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    calendar = _stage_calendar(tmp_path)
    content = _stage_content(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _prepare_python_environment(calendar, workspace)
    log = MessageLog(tmp_path / "sessions.db")
    manager = PluginManager(
        message_log=log,
        plugin_dirs=[content, calendar, CORE / "plugins" / "tools"],
        event_bus=EventBus(),
        workspace=workspace,
        installed_cache_root=tmp_path / "cache",
    )
    route = None
    generation_id = None
    try:
        await manager.load_all()
        snapshot = manager.current_snapshot
        assert snapshot is not None and snapshot.composition_root is not None
        generations = {
            item.plugin_id: item for item in snapshot.generations.values()
        }
        assert set(generations) == {"calendar", "eventmail", "tools"}
        generation_id = generations["calendar"].generation_id
        runtime = manager.composition_generation_host.get(generation_id)
        assert runtime is not None and runtime.mode == "formal"

        process_catalog = snapshot.managed_process_registry
        mcp_catalog = snapshot.mcp_server_registry
        assert process_catalog is not None and tuple(process_catalog) == ("calendar_api",)
        assert mcp_catalog is not None and tuple(mcp_catalog) == ("calendar",)
        assert mcp_catalog["calendar"].descriptor.required_tools == ()

        process = runtime.processes
        mcp = runtime.mcp
        assert process is not None and mcp is not None
        assert process.endpoint("calendar_api").port == FORMAL_PORT
        server = mcp.server("calendar")
        route = server.route()
        assert set(server.tool_names) == EXPECTED_TOOLS
        assert "get_proactive_events" not in server.tool_names
        assert "acknowledge_events" not in server.tool_names
        call = await route.call("analyze_busyness", {})
        assert not call.success
        assert "validation error" in call.output.lower()

        # 运行数据可能保留正式端口；归档调用必须使用 Core 分配的独立端口。
        data_dir = process_catalog["calendar_api"].runtime_data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / ".env").write_text(
            "PORT=18000\nGOOGLE_CLIENT_ID=fixture-client\nGOOGLE_CLIENT_SECRET=fixture-secret\n",
            encoding="utf-8",
        )
        async with manager.composition_generation_host.open_mcp(snapshot, "calendar") as scoped:
            bound = manager.composition_generation_host.get(scoped.generation_id)
            assert bound is not None and bound.processes is not None
            assert bound.processes.endpoint("calendar_api").port != FORMAL_PORT
            scoped_route = scoped.route()
            try:
                unavailable = await scoped_route.call("list_calendars", {})
                assert "503" in unavailable.output
                assert "/calendars" in "".join(bound.processes.logs("calendar_api").lines)
                assert "/calendars" not in "".join(process.logs("calendar_api").lines)
            finally:
                await scoped_route.aclose()

        with build_opener(ProxyHandler({})).open(process.endpoint("calendar_api").readiness_url, timeout=3) as response:
            assert response.status == 200

        receipt = snapshot.composition_root.receipt()
        health = {item.name: item.healthy for item in receipt.health}
        assert health["managed-process:calendar_api"]
        assert health["mcp:calendar"]
        assert not any(name.startswith("proactive:") for name in health)
        process_lines = list(process.logs("calendar_api").lines)
        assert any("/health" in line and "200" in line for line in process_lines)
        assert not any("/content/" in line for line in process_lines)
    finally:
        if route is not None:
            await route.aclose()
        await manager.terminate_all()
        log.close()

    assert _port_free(FORMAL_PORT)
    assert manager.composition_generation_host.get(generation_id) is None
