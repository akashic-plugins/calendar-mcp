from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

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
from agent.plugin_composition.mcp_slots import (
    PluginMcpServers,
    _freeze_plugin_mcp_servers,
)
from agent.plugin_composition.process_slots import (
    PluginManagedProcesses,
    _freeze_plugin_managed_processes,
)
from agent.plugins.composable import ComposablePlugin
from agent.plugins.static_manifest import load_static_plugin_manifest
from plugin import CalendarConfig, CalendarContentApiError, CalendarSourceRuntime
from plugin import BoundAlertSource, EVENTMAIL_ALERT_SOURCE


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 23, tzinfo=UTC)


class RecordingAlerts:
    def __init__(self) -> None:
        self.reports: list[dict[str, object]] = []
        self.statuses: dict[str, str] = {}
        self.failure: Exception | None = None

    def report(self, **kwargs: object) -> Mapping[str, object]:
        if self.failure is not None:
            raise self.failure
        self.reports.append(dict(kwargs))
        return {"accepted": True}

    def status(self, *, event_id: str) -> str | None:
        return self.statuses.get(event_id)


class AlertSources:
    def __init__(self, alerts: RecordingAlerts) -> None:
        self.alerts = alerts

    def bind(self, source_id: str) -> BoundAlertSource:
        assert source_id == "calendar"
        return self.alerts


class RecordingApi:
    def __init__(self) -> None:
        self.batch: Mapping[str, object] = {
            "status": "batch",
            "batch_id": "calendar:0:stable",
            "items": [
                {
                    "item_id": "event-1",
                    "payload": {"title": "Meeting soon", "kind": "alert"},
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
        self.commits.append(batch_id)
        return {"committed": True}

    async def acknowledge(self, event_id: str) -> Mapping[str, object]:
        self.acks.append(event_id)
        return {"acknowledged": True}


@pytest.mark.asyncio
async def test_v3_apply_registers_calendar_process_and_alert_source(
    tmp_path: Path,
) -> None:
    root = CompositionRoot("calendar:test")
    processes = PluginManagedProcesses(root.instance_token)
    servers = PluginMcpServers(root.instance_token)
    await root.context.provide(MANAGED_PROCESSES, processes)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(TIMERS, PluginTimers.candidate_validation())
    _ = await root.context.provide(
        EVENTMAIL_ALERT_SOURCE, AlertSources(RecordingAlerts())
    )

    await root.mount(
        ComposablePlugin.from_module(calendar_module),
        name="calendar",
        runtime=PluginRuntime(
            plugin_id="calendar",
            generation_id="calendar:test",
            plugin_dir=ROOT,
            data_dir=tmp_path / "plugin-data",
            workspace=tmp_path / "workspace",
            config=CalendarConfig(),
        ),
    )

    process = _freeze_plugin_managed_processes(processes, root.instance_token)[
        "calendar_api"
    ].definition
    mcp = _freeze_plugin_mcp_servers(servers, root.instance_token)[
        "calendar"
    ].definition
    assert process == calendar_module.CALENDAR_PROCESS
    assert mcp.endpoint_env[0].process == process.name
    assert EVENTMAIL_ALERT_SOURCE not in calendar_module.inject
    await root.dispose()


@pytest.mark.asyncio
async def test_v3_apply_keeps_calendar_services_without_eventmail(tmp_path: Path) -> None:
    root = CompositionRoot("calendar:without-eventmail")
    processes = PluginManagedProcesses(root.instance_token)
    servers = PluginMcpServers(root.instance_token)
    await root.context.provide(MANAGED_PROCESSES, processes)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(TIMERS, PluginTimers.candidate_validation())
    await root.mount(
        ComposablePlugin.from_module(calendar_module),
        name="calendar",
        runtime=PluginRuntime(
            plugin_id="calendar",
            generation_id="calendar:without-eventmail",
            plugin_dir=ROOT,
            data_dir=tmp_path / "plugin-data",
            workspace=tmp_path / "workspace",
            config=CalendarConfig(),
        ),
    )

    assert "calendar" in _freeze_plugin_mcp_servers(servers, root.instance_token)
    await root.dispose()


def test_static_manifest_matches_v3_2_module() -> None:
    manifest = load_static_plugin_manifest(ROOT)
    assert manifest.version == calendar_module.version == "3.2.1"
    assert manifest.mcp_servers[0].required_tools == ()
    assert "PROACTIVE_COMPONENTS" not in (ROOT / "plugin.py").read_text()


@pytest.mark.asyncio
async def test_tick_reports_alert_before_committing_source_batch() -> None:
    alerts = RecordingAlerts()
    api = RecordingApi()
    runtime = CalendarSourceRuntime(
        PluginTimers.candidate_validation(),
        cast(BoundAlertSource, alerts),
        api,
        timedelta(minutes=5),
    )

    await runtime._tick()

    assert alerts.reports[0]["event_id"] == "event-1"
    assert api.commits == ["calendar:0:stable"]


@pytest.mark.asyncio
async def test_report_failure_does_not_commit_source_batch() -> None:
    alerts = RecordingAlerts()
    alerts.failure = RuntimeError("Wake Alert report failed")
    api = RecordingApi()
    runtime = CalendarSourceRuntime(
        PluginTimers.candidate_validation(),
        cast(BoundAlertSource, alerts),
        api,
        timedelta(minutes=5),
    )

    with pytest.raises(RuntimeError, match="report failed"):
        await runtime._tick()
    assert api.commits == []


@pytest.mark.parametrize("status", ["delivered", "skipped", "expired"])
@pytest.mark.asyncio
async def test_terminal_wake_alert_acks_calendar_source(status: str) -> None:
    alerts = RecordingAlerts()
    alerts.statuses["event-1"] = status
    api = RecordingApi()
    api.pending_items = ({"item_id": "event-1", "payload": {"title": "Meeting soon"}},)
    api.batch = {"status": "no_batch"}
    runtime = CalendarSourceRuntime(
        PluginTimers.candidate_validation(),
        cast(BoundAlertSource, alerts),
        api,
        timedelta(minutes=5),
    )

    await runtime._tick()
    assert api.acks == ["event-1"]


@pytest.mark.asyncio
async def test_transport_failure_is_rearmed(caplog: pytest.LogCaptureFixture) -> None:
    api = RecordingApi()
    api.poll_failures = 1
    runtime = CalendarSourceRuntime(
        PluginTimers(AsyncioOneShotTimer()),
        cast(BoundAlertSource, RecordingAlerts()),
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
