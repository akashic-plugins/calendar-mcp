from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from plugins.tools.plugin import TOOLS, ToolCatalog
from calendar_test_plugin.tools import CALENDAR_TOOLS

import pytest

from calendar_test_plugin import plugin as calendar_module
from plugins.timer.timer import AsyncioOneShotTimer
from agent.plugin_composition import Context, CompositionRoot, Fiber, PluginRuntime
from plugins.managed_processes.contract import MANAGED_PROCESSES
from plugins.mcp.contract import MCP_SERVERS
from plugins.timer.contract import TIMERS
from agent.plugin_composition.tasks import TASKS, PluginTasks
from plugins.mcp.contract import McpServerDefinition
from plugins.managed_processes.contract import ManagedProcessDefinition
from agent.plugins.composable import ComposablePlugin
from agent.plugins.static_manifest import load_static_plugin_manifest
from calendar_test_plugin.plugin import CalendarConfig, CalendarContentApiError, CalendarSourceRuntime
from plugins.watch.contract import WATCH_EVENTS, WatchEvents


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 23, tzinfo=UTC)


class _RecordingProcessHandle:
    def __init__(self, definition: ManagedProcessDefinition) -> None:
        self.definition = definition

    def check(self, _ctx: object) -> None:
        return None

    def port(self, _ctx: object) -> int:
        return self.definition.formal_port


class _RecordingProcesses:
    """Capture declarations; manager integration tests exercise the real provider."""

    def __init__(self) -> None:
        self.definitions: dict[str, ManagedProcessDefinition] = {}

    async def register(
        self, _ctx: object, definition: ManagedProcessDefinition,
    ) -> _RecordingProcessHandle:
        self.definitions[definition.name] = definition
        return _RecordingProcessHandle(definition)


class _RecordingMcpServers:
    """Capture declarations; manager integration tests exercise the real provider."""

    def __init__(self) -> None:
        self.definitions: dict[str, McpServerDefinition] = {}

    async def register(self, _ctx: object, definition: McpServerDefinition) -> None:
        self.definitions[definition.name] = definition


async def _mount_tools(
    root: CompositionRoot, tmp_path: Path,
) -> tuple[PluginTasks, Fiber]:
    """Provide the real tools owner with its required task admission."""

    tasks = PluginTasks()
    await root.context.provide(TASKS, tasks)

    async def apply(ctx: Context) -> None:
        catalog = ToolCatalog(ctx, ctx.require(TASKS).open(ctx))
        _ = await ctx.provide(TOOLS, catalog)

    fiber = await root.mount(
        apply,
        name="tools-provider",
        inject=(TASKS,),
        runtime=PluginRuntime(
            plugin_id="tools-provider",
            generation_id="tools-provider:test",
            plugin_dir=ROOT,
            data_dir=tmp_path / "tools-data",
            workspace=tmp_path / "workspace",
            config={},
        ),
    )
    return tasks, fiber


class RecordingEvents:
    def __init__(self) -> None:
        self.emits: list[dict[str, object]] = []
        self.order: list[str] = []
        self.failure: Exception | None = None

    async def emit(
        self,
        source: str,
        *,
        title: str = "",
        text: str = "",
        url: str = "",
        at: datetime | None = None,
        state: bool = False,
        key: str = "",
    ) -> str:
        if self.failure is not None:
            raise self.failure
        self.order.append(f"emit:{key}")
        self.emits.append(
            {"source": source, "title": title, "text": text, "url": url,
             "at": at, "state": state, "key": key}
        )
        return f"watch-{len(self.emits)}"


class RecordingApi:
    def __init__(self, order: list[str] | None = None) -> None:
        self.order = order if order is not None else []
        self.batch: Mapping[str, object] = {
            "status": "batch",
            "batch_id": "calendar:0:stable",
            "items": [
                {
                    "item_id": "event-1",
                    "payload": {
                        "title": "Meeting soon",
                        "content": "calendar 有一条日程提醒：Meeting soon",
                        "url": "https://calendar.example/event-1",
                        "published_at": "2026-08-23T11:00:00+00:00",
                        "kind": "alert",
                    },
                }
            ],
        }
        self.pending_items: tuple[Mapping[str, object], ...] = ()
        self.polls = 0
        self.poll_failures = 0
        self.commits: list[str] = []
        self.acks: list[str] = []

    async def pending(self) -> tuple[Mapping[str, object], ...]:
        return self.pending_items

    async def poll(self) -> Mapping[str, object]:
        self.polls += 1
        if self.poll_failures:
            self.poll_failures -= 1
            raise CalendarContentApiError("recording Calendar poll failed")
        return self.batch

    async def commit(self, batch_id: str) -> Mapping[str, object]:
        self.order.append("commit")
        self.commits.append(batch_id)
        return {"committed": True}

    async def acknowledge(self, event_id: str) -> Mapping[str, object]:
        self.order.append(f"ack:{event_id}")
        self.acks.append(event_id)
        return {"acknowledged": True}


@pytest.mark.asyncio
async def test_v3_apply_registers_calendar_process_and_watch_source(
    tmp_path: Path,
) -> None:
    root = CompositionRoot("calendar:test")
    processes = _RecordingProcesses()
    servers = _RecordingMcpServers()
    await root.context.provide(MANAGED_PROCESSES, processes)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(TIMERS, AsyncioOneShotTimer())
    events = RecordingEvents()
    _ = await root.context.provide(WATCH_EVENTS, cast(WatchEvents, events))

    tasks, tools_fiber = await _mount_tools(root, tmp_path)
    try:
        plugin = ComposablePlugin.from_module(
            calendar_module, load_static_plugin_manifest(ROOT)
        )
        await root.mount(
            plugin.apply,
            name="calendar",
            inject=plugin.inject,
            runtime=PluginRuntime(
                plugin_id="calendar",
                generation_id="calendar:test",
                plugin_dir=ROOT,
                data_dir=tmp_path / "plugin-data",
                workspace=tmp_path / "workspace",
                config=CalendarConfig(),
            ),
        )

        process = processes.definitions["calendar_api"]
        mcp = servers.definitions["calendar"]
        assert process == calendar_module.CALENDAR_PROCESS
        assert mcp.endpoint_env[0].process.port(root.context) == process.formal_port
        assert WATCH_EVENTS not in calendar_module.inject
        assert any(item["name"].startswith("mcp_calendar__") for item in (ref.description for ref in root.context.require(CALENDAR_TOOLS).refs))
    finally:
        await tasks.close()
        await tools_fiber.dispose()
        await root.dispose()


@pytest.mark.asyncio
async def test_v3_apply_keeps_calendar_services_without_watch(tmp_path: Path) -> None:
    root = CompositionRoot("calendar:without-watch")
    processes = _RecordingProcesses()
    servers = _RecordingMcpServers()
    await root.context.provide(MANAGED_PROCESSES, processes)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(TIMERS, AsyncioOneShotTimer())
    tasks, tools_fiber = await _mount_tools(root, tmp_path)
    try:
        plugin = ComposablePlugin.from_module(
            calendar_module, load_static_plugin_manifest(ROOT)
        )
        await root.mount(
            plugin.apply,
            name="calendar",
            inject=plugin.inject,
            runtime=PluginRuntime(
                plugin_id="calendar",
                generation_id="calendar:without-watch",
                plugin_dir=ROOT,
                data_dir=tmp_path / "plugin-data",
                workspace=tmp_path / "workspace",
                config=CalendarConfig(),
            ),
        )

        assert "calendar" in servers.definitions
        assert any(item["name"].startswith("mcp_calendar__") for item in (ref.description for ref in root.context.require(CALENDAR_TOOLS).refs))
    finally:
        await tasks.close()
        await tools_fiber.dispose()
        await root.dispose()


def test_static_manifest_matches_v3_3_module() -> None:
    manifest = load_static_plugin_manifest(ROOT)
    assert manifest.name == "calendar"
    assert manifest.version == calendar_module.version == "3.3.0"
    assert manifest.api_version == 3
    assert manifest.requirements == ("mcp/requirements.txt",)
    assert "PROACTIVE_COMPONENTS" not in (ROOT / "plugin.py").read_text()


def _runtime(events: RecordingEvents, api: RecordingApi) -> CalendarSourceRuntime:
    return CalendarSourceRuntime(
        AsyncioOneShotTimer(),
        cast(WatchEvents, events),
        api,
        timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_tick_emits_acks_then_commits_source_batch() -> None:
    events = RecordingEvents()
    api = RecordingApi(events.order)

    await _runtime(events, api)._tick()

    assert events.emits == [
        {
            "source": "calendar",
            "title": "Meeting soon",
            "text": "calendar 有一条日程提醒：Meeting soon",
            "url": "https://calendar.example/event-1",
            "at": datetime(2026, 8, 23, 11, tzinfo=UTC),
            "state": False,
            "key": "event-1",
        }
    ]
    assert events.order == ["emit:event-1", "ack:event-1", "commit"]


@pytest.mark.asyncio
async def test_emit_failure_does_not_ack_or_commit_source_batch() -> None:
    events = RecordingEvents()
    events.failure = RuntimeError("Watch emit failed")
    api = RecordingApi()

    with pytest.raises(RuntimeError, match="emit failed"):
        await _runtime(events, api)._tick()
    assert api.acks == []
    assert api.commits == []


@pytest.mark.asyncio
async def test_first_tick_acks_legacy_pending_without_re_emitting() -> None:
    events = RecordingEvents()
    api = RecordingApi()
    api.pending_items = (
        {"item_id": "old-1", "payload": {"title": "old"}},
        {"item_id": "old-2", "payload": {"title": "old"}},
    )
    api.batch = {"status": "no_batch"}
    runtime = _runtime(events, api)

    await runtime._tick()
    api.pending_items = ({"item_id": "late", "payload": {"title": "late"}},)
    await runtime._tick()

    assert api.acks == ["old-1", "old-2"]
    assert events.emits == []


@pytest.mark.asyncio
async def test_transport_failure_is_rearmed(caplog: pytest.LogCaptureFixture) -> None:
    api = RecordingApi()
    api.poll_failures = 1
    runtime = CalendarSourceRuntime(
        AsyncioOneShotTimer(),
        cast(WatchEvents, RecordingEvents()),
        api,
        timedelta(hours=1),
    )
    root = CompositionRoot("calendar-retry")

    await runtime.start(root.context)
    for _ in range(100):
        if api.polls == 1 and runtime._handle is not None:
            break
        await asyncio.sleep(0.001)

    assert api.polls == 1
    assert runtime._task is not None and not runtime._task.done()
    assert sum("transport failed" in row.message for row in caplog.records) == 1
    await runtime.close()
    await root.dispose()
