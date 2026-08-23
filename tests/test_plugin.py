from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import plugin as calendar_module
from agent.control.timer import AsyncioOneShotTimer
from agent.plugin_composition import (
    MANAGED_PROCESSES,
    MCP_SERVERS,
    TIMERS,
    CompositionRoot,
    PluginRuntime,
    PluginTimers,
)
from agent.plugin_composition.mcp_slots import PluginMcpServers, _freeze_plugin_mcp_servers
from agent.plugin_composition.process_slots import (
    PluginManagedProcesses,
    _freeze_plugin_managed_processes,
)
from agent.plugins.composable import ComposablePlugin
from agent.plugins.static_manifest import load_static_plugin_manifest
from plugin import (
    CONTENT_SOURCE,
    CalendarConfig,
    CalendarContentApiError,
    CalendarContentConfig,
    CalendarSourceRuntime,
)


ROOT = Path(__file__).resolve().parents[1]


class RecordingContent:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, list[dict[str, object]]]] = []
        self.unsettled_rows: tuple[dict[str, object], ...] = ()
        self.acks: list[str] = []
        self.after_submit: Exception | None = None
        self.ack_failures = 0

    def submit(self, batch_id, items):
        self.submissions.append((batch_id, list(items)))
        if self.after_submit is not None:
            raise self.after_submit
        return {"batch_id": batch_id}

    def unsettled(self, limit=100):
        return self.unsettled_rows

    def ack(self, settlement_ref):
        self.acks.append(settlement_ref)
        if self.ack_failures:
            self.ack_failures -= 1
            raise RuntimeError("recording Content ack failed")
        return {"settled": True}


class RecordingApi:
    def __init__(self) -> None:
        self.batch = {
            "batch_id": "calendar:0:stable",
            "items": [
                {
                    "item_id": "event-1",
                    "revision": "rev-1",
                    "payload": {"kind": "alert"},
                    "not_before": datetime(2026, 8, 23, tzinfo=UTC).isoformat(),
                    "requires_ack": True,
                }
            ],
        }
        self.polls = 0
        self.commits: list[str] = []
        self.acks: list[str] = []
        self.ack_failures = 0
        self.poll_failures = 0

    async def poll(self):
        self.polls += 1
        if self.poll_failures:
            self.poll_failures -= 1
            raise CalendarContentApiError("recording Calendar poll failed")
        return self.batch

    async def commit(self, batch_id):
        self.commits.append(batch_id)
        return {"committed": True}

    async def acknowledge(self, event_id):
        self.acks.append(event_id)
        if self.ack_failures:
            self.ack_failures -= 1
            raise RuntimeError("recording Calendar ack failed")
        return {"acknowledged": True}


class RecordingSourceServices:
    def __init__(self, bound: RecordingContent) -> None:
        self.bound = bound
        self.source_ids: list[str] = []

    def bind(self, source_id: str):
        self.source_ids.append(source_id)
        return self.bound


async def _mount(tmp_path: Path):
    root = CompositionRoot("calendar:test")
    processes = PluginManagedProcesses(root.instance_token)
    servers = PluginMcpServers(root.instance_token)
    timers = PluginTimers(AsyncioOneShotTimer())
    content = RecordingContent()
    sources = RecordingSourceServices(content)
    await root.context.provide(MANAGED_PROCESSES, processes)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(TIMERS, timers)
    await root.context.provide(CONTENT_SOURCE, sources)
    await root.mount(
        ComposablePlugin.from_module(calendar_module),
        name="calendar",
        runtime=PluginRuntime(
            plugin_id="calendar",
            plugin_dir=ROOT,
            data_dir=tmp_path / "plugin-data",
            workspace=tmp_path / "workspace",
            config=CalendarConfig(content=CalendarContentConfig()),
        ),
    )
    return root, processes, servers, sources


@pytest.mark.asyncio
async def test_v3_apply_registers_ordinary_content_capabilities_without_writes(
    tmp_path: Path,
) -> None:
    root, processes, servers, sources = await _mount(tmp_path)

    process = _freeze_plugin_managed_processes(
        processes, root.instance_token
    )["calendar_api"].definition
    mcp = _freeze_plugin_mcp_servers(
        servers, root.instance_token
    )["calendar"].definition
    assert process == calendar_module.CALENDAR_PROCESS
    assert process.formal_port == 18000
    assert mcp.required_tools == ()
    assert mcp.candidate_read_only_tools == ()
    assert mcp.endpoint_env[0].process == process.name
    assert sources.source_ids == ["calendar"]
    assert calendar_module.inject == (
        MANAGED_PROCESSES,
        MCP_SERVERS,
        TIMERS,
        CONTENT_SOURCE,
    )
    assert not (tmp_path / "plugin-data").exists()
    await root.dispose()


def test_static_manifest_matches_module_and_has_no_proactive_tools() -> None:
    manifest = load_static_plugin_manifest(ROOT)

    assert manifest.version == calendar_module.version == "3.1.0"
    assert manifest.mcp_servers[0].required_tools == ()
    assert manifest.mcp_servers[0].candidate_read_only_tools == ()
    text = (ROOT / "plugin.py").read_text(encoding="utf-8")
    assert "PROACTIVE_COMPONENTS" not in text
    assert "ProactiveSourceDefinition" not in text


@pytest.mark.asyncio
async def test_tick_submits_before_cursor_commit_and_replays_stable_batch() -> None:
    content = RecordingContent()
    api = RecordingApi()
    runtime = CalendarSourceRuntime(
        PluginTimers(AsyncioOneShotTimer()), content, api, timedelta(minutes=5)
    )

    await runtime._tick()
    await runtime._tick()

    assert [batch_id for batch_id, _items in content.submissions] == [
        "calendar:0:stable",
        "calendar:0:stable",
    ]
    assert api.commits == ["calendar:0:stable", "calendar:0:stable"]


@pytest.mark.asyncio
async def test_submit_failure_never_commits_calendar_cursor() -> None:
    content = RecordingContent()
    content.after_submit = RuntimeError("submit committed but hint failed")
    api = RecordingApi()
    runtime = CalendarSourceRuntime(
        PluginTimers(AsyncioOneShotTimer()), content, api, timedelta(minutes=5)
    )

    with pytest.raises(RuntimeError, match="hint failed"):
        await runtime._tick()

    assert api.commits == []


@pytest.mark.asyncio
async def test_provider_ack_precedes_content_ack_and_failure_replays() -> None:
    content = RecordingContent()
    content.unsettled_rows = (
        {
            "ref": {"source_id": "calendar", "item_id": "event-1", "revision": "1"},
            "settlement_ref": "delivery-1",
            "payload": {},
        },
    )
    api = RecordingApi()
    api.ack_failures = 1
    runtime = CalendarSourceRuntime(
        PluginTimers(AsyncioOneShotTimer()), content, api, timedelta(minutes=5)
    )

    with pytest.raises(RuntimeError, match="ack failed"):
        await runtime._tick()
    assert content.acks == []

    await runtime._tick()
    assert api.acks == ["event-1", "event-1"]
    assert content.acks == ["delivery-1"]


@pytest.mark.asyncio
async def test_crash_after_provider_ack_before_content_ack_replays_both() -> None:
    content = RecordingContent()
    content.unsettled_rows = (
        {
            "ref": {"source_id": "calendar", "item_id": "event-1", "revision": "1"},
            "settlement_ref": "delivery-1",
            "payload": {},
        },
    )
    content.ack_failures = 1
    api = RecordingApi()
    runtime = CalendarSourceRuntime(
        PluginTimers(AsyncioOneShotTimer()), content, api, timedelta(minutes=5)
    )

    with pytest.raises(RuntimeError, match="Content ack failed"):
        await runtime._tick()
    await runtime._tick()

    assert api.acks == ["event-1", "event-1"]
    assert content.acks == ["delivery-1", "delivery-1"]


@pytest.mark.asyncio
async def test_start_and_reload_own_exactly_one_real_timer() -> None:
    content = RecordingContent()
    api = RecordingApi()
    runtime = CalendarSourceRuntime(
        PluginTimers(AsyncioOneShotTimer()), content, api, timedelta(hours=1)
    )

    await runtime.start()
    await runtime.start()
    for _ in range(100):
        if api.polls == 1:
            break
        await asyncio.sleep(0.001)
    assert api.polls == 1
    assert runtime._handle is not None
    await runtime.close()
    assert runtime._handle is None
    assert runtime._task is None


@pytest.mark.asyncio
async def test_http_boundary_failure_is_logged_and_rearmed() -> None:
    content = RecordingContent()
    api = RecordingApi()
    api.poll_failures = 1
    runtime = CalendarSourceRuntime(
        PluginTimers(AsyncioOneShotTimer()), content, api, timedelta(hours=1)
    )

    await runtime.start()
    first_task = runtime._task
    assert first_task is not None
    await first_task

    assert api.polls == 1
    assert runtime._handle is not None
    assert runtime._task is not None
    await runtime.close()


@pytest.mark.asyncio
async def test_content_contract_error_remains_fail_loud_after_cleanup() -> None:
    content = RecordingContent()
    content.after_submit = RuntimeError("Content contract broken")
    api = RecordingApi()
    runtime = CalendarSourceRuntime(
        PluginTimers(AsyncioOneShotTimer()), content, api, timedelta(hours=1)
    )

    await runtime.start()
    first_task = runtime._task
    assert first_task is not None
    with pytest.raises(RuntimeError, match="Content contract broken"):
        await first_task

    assert api.commits == []
    assert runtime._handle is not None
    await runtime.close()
