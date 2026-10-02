from __future__ import annotations

import os
import json
import shutil
import socket
import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

import agent.plugins.host as plugin_host_module
from agent.control.timer import TimerReceipt, TimerStatus
from session.log import MessageLog
from agent.plugin_composition import MCP_SERVERS
from agent.plugins.manager import PluginManager
from agent.plugins.python_environment import ENVIRONMENT_FILE, PythonEnvironments
from agent.plugins.selection import PluginSelection
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


class _TimerHandle:
    def __init__(self, timer_id: str, deadline: datetime, now: datetime) -> None:
        self._id = timer_id
        self.deadline = deadline
        self.now = now
        self.future: asyncio.Future[TimerReceipt] = (
            asyncio.get_running_loop().create_future()
        )

    @property
    def id(self) -> str:
        return self._id

    async def result(self) -> TimerReceipt:
        return await asyncio.shield(self.future)

    async def cancel(self) -> TimerReceipt:
        if not self.future.done():
            self.future.set_result(
                TimerReceipt(self.id, self.deadline, self.now, TimerStatus.CANCELLED)
            )
        return await self.future

    async def cleanup(self) -> None:
        _ = await self.cancel()


class _Timer:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.handles: list[_TimerHandle] = []

    def schedule(self, deadline: datetime) -> _TimerHandle:
        handle = _TimerHandle(f"timer:{len(self.handles)}", deadline, self.now)
        self.handles.append(handle)
        return handle


async def _eventually(predicate) -> None:
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not settle")


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
    """Boot the real loader, MCP session, Alert timer, and local replacement."""

    runtime = _fixture_runtime()
    runtime_python = runtime / "bin" / "python"
    if not runtime_python.is_file() or not os.access(runtime_python, os.X_OK):
        pytest.fail(f"Calendar MCP runtime is not staged: {runtime_python}")
    assert _port_free(FORMAL_PORT)
    monkeypatch.setenv(
        "PATH", f"{runtime / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    now = datetime(2026, 8, 23, tzinfo=UTC)
    timers: list[_Timer] = []

    def timer_factory() -> _Timer:
        timer = _Timer(now)
        timers.append(timer)
        return timer

    monkeypatch.setattr(plugin_host_module, "AsyncioOneShotTimer", timer_factory)
    calendar = _stage_calendar(tmp_path)
    content = _stage_content(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    PluginSelection(workspace).initialize()
    _prepare_python_environment(calendar, workspace)
    log = MessageLog(tmp_path / "sessions.db")
    manager = PluginManager(
        message_log=log,
        plugin_dirs=[
            content,
            calendar,
            *(CORE / "plugins" / name for name in (
                "content", "tools", "mcp", "managed_processes",
            )),
        ],
        event_bus=EventBus(),
        workspace=workspace,
        installed_cache_root=tmp_path / "cache",
    )
    try:
        await manager.load_all()
        root = manager.live_root
        calendar_generation = manager.generation("calendar")
        assert root is not None and calendar_generation is not None
        assert calendar_generation.fiber is not None
        await manager.start_runtime()
        await _eventually(lambda: sum(len(timer.handles) for timer in timers) == 1)
        formal_timer = next(timer for timer in timers if timer.handles)

        # Runtime input belongs to Calendar's live generation; the actual
        # MCP session borrows its endpoint process below.
        calendar_generation.data_dir.mkdir(parents=True, exist_ok=True)
        (calendar_generation.data_dir / ".env").write_text(
            "PORT=18000\nGOOGLE_CLIENT_ID=fixture-client\nGOOGLE_CLIENT_SECRET=fixture-secret\n",
            encoding="utf-8",
        )

        servers = root.context.require(MCP_SERVERS)
        assert servers.catalog() == [
            {"owner_id": "calendar", "name": "calendar", "status": "declared"}
        ]
        async with servers.open(calendar_generation.fiber.context, "calendar") as server:
            assert set(server.tool_names) == EXPECTED_TOOLS
            assert "get_proactive_events" not in server.tool_names
            assert "acknowledge_events" not in server.tool_names
            async with server.route() as route:
                call = await route.call("analyze_busyness", {})
                assert not call.success
                assert "validation error" in call.output.lower()
                unavailable = await route.call("list_calendars", {})
                assert "503" in unavailable.output

        receipt = root.receipt()
        health = {item.name: item.healthy for item in receipt.health}
        assert health["process:calendar_api"]
        assert health["mcp:calendar"]
        assert not any("proactive" in item.name for item in receipt.health)

        # The old Alert binding must release during local replacement before
        # the new Calendar source may own the same EventMail source ID.
        with (calendar / "plugin.py").open("a", encoding="utf-8") as handle:
            handle.write("\n# local replacement fixture revision\n")
        _prepare_python_environment(calendar, workspace)
        result = await manager.reconcile_changed()
        assert any(row["publication_state"] == "active" for row in result)
        assert manager.generation("calendar") is not calendar_generation
        assert calendar_generation.scope.closed
        assert manager.live_root is root
        await _eventually(lambda: len(formal_timer.handles) == 2)
        assert (await formal_timer.handles[0].result()).status is TimerStatus.CANCELLED
        active = [
            handle
            for timer in timers
            for handle in timer.handles
            if not handle.future.done()
        ]
        assert len(active) == 1
    finally:
        await manager.terminate_all()
        log.close()

    assert _port_free(FORMAL_PORT)
    assert all(handle.future.done() for timer in timers for handle in timer.handles)
