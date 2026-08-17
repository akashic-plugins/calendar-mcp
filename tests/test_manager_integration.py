from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path

import pytest

from agent.plugins.generation_activity_host import ActivityHost
from agent.plugins.generation_proactive_host import ProactiveActivityAdapter
from agent.plugins.manager import PluginManager
from bus.event_bus import EventBus


ROOT = Path(__file__).resolve().parents[1]
MCP_RUNTIME = ROOT / "mcp" / ".venv"
FORMAL_PORT = 18000
EXPECTED_TOOLS = frozenset(
    {
        "acknowledge_events",
        "add_attendee",
        "analyze_busyness",
        "check_attendee_status",
        "create_calendar",
        "create_event",
        "delete_event",
        "find_events",
        "get_proactive_events",
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


def _pending_task_names() -> list[str]:
    import asyncio

    current = asyncio.current_task()
    return sorted(
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    )


def _stage_plugin_source(tmp_path: Path) -> Path:
    """Copy the executable Calendar artifact while reusing only the local runtime."""

    source = tmp_path / "plugin-source"
    (source / "mcp" / "src").mkdir(parents=True)
    for relative in (
        "plugin.py",
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


def _state_text(value: object) -> str:
    return str(getattr(value, "value", value))


@pytest.mark.asyncio
async def test_manager_boots_calendar_v3_and_drains_every_runtime(tmp_path, monkeypatch) -> None:
    """Boot the real v3 process/MCP boundary once, then prove exact cleanup."""

    runtime_python = MCP_RUNTIME / "bin" / "python"
    if not runtime_python.is_file() or not os.access(runtime_python, os.X_OK):
        pytest.fail(
            "Calendar MCP runtime is not staged: "
            f"expected executable {runtime_python}; run the plugin runtime staging first"
        )
    assert _port_free(FORMAL_PORT), "port 18000 must be free before the probe"
    monkeypatch.setenv(
        "PATH",
        f"{MCP_RUNTIME / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
    )
    plugin_source = _stage_plugin_source(tmp_path)
    workspace = tmp_path / "workspace"
    plugin_data = tmp_path / "plugin-data"
    workspace.mkdir()
    plugin_data.mkdir()

    manager = PluginManager(
        plugin_dirs=[plugin_source],
        event_bus=EventBus(),
        tool_registry=None,
        workspace=workspace,
        installed_cache_root=plugin_data,
    )
    adapter = ProactiveActivityAdapter(manager.composition_generation_host)
    activity = ActivityHost((adapter,))
    manager.bind_activity_host(activity)
    snapshot = None
    generation_id = None
    route = None
    try:
        await manager.load_all()
        snapshot = manager.current_snapshot
        assert snapshot is not None
        assert _state_text(snapshot.state) == "committed"
        assert snapshot.accepting_leases
        root = snapshot.composition_root
        assert root is not None
        generation = next(
            item
            for item in snapshot.generations.values()
            if item.plugin_id == "calendar"
        )
        generation_id = generation.generation_id
        runtime = manager.composition_generation_host.get(generation_id)
        assert runtime is not None
        assert runtime.mode == "formal"

        active = activity.active
        assert active is not None
        assert active.snapshot_id == snapshot.snapshot_id
        assert set(active.child_bindings) == {"proactive_components"}

        process_catalog = snapshot.managed_process_registry
        mcp_catalog = snapshot.mcp_server_registry
        proactive_catalog = snapshot.proactive_component_catalog
        assert process_catalog is not None
        assert mcp_catalog is not None
        assert proactive_catalog is not None
        assert snapshot.managed_process_registry_identity == process_catalog.identity
        assert snapshot.mcp_server_registry_identity == mcp_catalog.identity
        assert snapshot.proactive_component_catalog_identity == proactive_catalog.identity
        assert process_catalog.root_instance_token is root.instance_token
        assert mcp_catalog.root_instance_token is root.instance_token
        assert proactive_catalog.root_instance_token is root.instance_token
        assert tuple(process_catalog) == ("calendar_api",)
        assert tuple(mcp_catalog) == ("calendar",)
        assert tuple(proactive_catalog.sources) == ("calendar:upcoming_events",)
        assert tuple(proactive_catalog.modules) == ()

        process_binding = process_catalog["calendar_api"]
        mcp_binding = mcp_catalog["calendar"]
        source_binding = proactive_catalog.sources["calendar:upcoming_events"]
        assert process_binding.descriptor.owner == "calendar"
        assert process_binding.descriptor.formal_port == FORMAL_PORT
        assert process_binding.descriptor.readiness_path == "/health"
        assert mcp_binding.descriptor.owner == "calendar"
        assert mcp_binding.descriptor.required_tools == (
            "get_proactive_events",
            "acknowledge_events",
        )
        assert source_binding.descriptor.owner == "calendar"
        assert source_binding.descriptor.mcp_server == "calendar"
        assert source_binding.descriptor.fetch_tool == "get_proactive_events"
        assert source_binding.descriptor.ack_tool == "acknowledge_events"
        assert source_binding.generation_id == generation_id
        assert source_binding.is_live()

        process_generation = runtime.processes
        mcp_generation = runtime.mcp
        assert process_generation is not None
        assert mcp_generation is not None
        process_endpoint = process_generation.endpoint("calendar_api")
        mcp_server = mcp_generation.server("calendar")
        assert process_generation.mode == "formal"
        assert process_generation.state == "ready"
        assert process_endpoint.process_name == "calendar_api"
        assert process_endpoint.mode == "formal"
        assert process_endpoint.port == FORMAL_PORT
        assert process_endpoint.readiness_url == "http://127.0.0.1:18000/health"
        assert mcp_generation.mode == "formal"
        assert mcp_generation.state == "ready"
        assert mcp_server.mode == "formal"

        route = mcp_server.route()
        assert route.mode == "formal"
        assert route.generation_id == generation_id
        assert route.server_name == "calendar"
        assert set(route.tool_names) == EXPECTED_TOOLS
        assert set(mcp_server.tool_names) == EXPECTED_TOOLS
        assert set(mcp_binding.descriptor.required_tools).issubset(route.tool_names)

        receipt = root.receipt()
        health = {item.name: item.healthy for item in receipt.health}
        assert health["managed-process:calendar_api"]
        assert health["mcp:calendar"]
        assert health["proactive:upcoming_events"]
        process_log = process_generation.logs("calendar_api")
        mcp_log = mcp_server.logs()
        process_lines = list(process_log.lines)
        mcp_lines = list(mcp_log.stdout) + list(mcp_log.stderr)
        assert any('/health' in line and '200' in line for line in process_lines)
        assert not any("CallToolRequest" in line or '"tools/call"' in line for line in mcp_lines)
        assert not any("/calendars" in line or "/events" in line for line in process_lines)
        assert adapter.source_fetch_invocations == 0
        assert adapter.ack_invocations == 0
        assert adapter.module_invocations == 0
        assert adapter.handler_resolution_count == 0
    finally:
        if route is not None:
            await route.aclose()
        await manager.terminate_all()

    assert _port_free(FORMAL_PORT)
    assert activity.active is None
    assert manager.composition_generation_host.get(generation_id) is None
    assert manager.composition_generation_host.failure(generation_id) is None
    assert snapshot is not None and snapshot.composition_root is not None
    root_after = snapshot.composition_root
    receipt_after = root_after.receipt()
    assert _state_text(root_after.root_fiber.state) == "disposed"
    assert list(receipt_after.effects) == []
    assert list(receipt_after.external_effects) == []
    assert list(root_after.topology_view().listeners) == []
    assert _pending_task_names() == []
