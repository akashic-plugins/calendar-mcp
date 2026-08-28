from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field

from agent.plugin_composition import (
    MANAGED_PROCESSES,
    MCP_SERVERS,
    RUNTIME_STARTED,
    RUNTIME_STOPPING,
    TIMERS,
    Context,
    EndpointEnv,
    ManagedProcessDefinition,
    McpServerDefinition,
    PluginTimers,
    ServiceKey,
    TimerHandle,
    TimerStatus,
)


logger = logging.getLogger(__name__)


class BoundAlertSource(Protocol):
    def report(
        self,
        *,
        event_id: str,
        payload: Mapping[str, object],
        observed_at: datetime,
        expires_at: datetime | None = None,
    ) -> Mapping[str, object]: ...

    def status(self, *, event_id: str) -> str | None: ...


class AlertSourceServices(Protocol):
    def bind(self, source_id: str) -> BoundAlertSource: ...


EVENTMAIL_ALERT_SOURCE = ServiceKey[AlertSourceServices](
    "eventmail.alert_source.v1"
)


class CalendarContentApiPort(Protocol):
    async def poll(self) -> Mapping[str, object]: ...

    async def pending(self) -> tuple[Mapping[str, object], ...]: ...

    async def commit(self, batch_id: str) -> Mapping[str, object]: ...

    async def acknowledge(self, event_id: str) -> Mapping[str, object]: ...


class CalendarContentApiError(RuntimeError):
    """Represent a retryable failure at the private Calendar HTTP boundary."""


class CalendarContentConfig(BaseModel):
    poll_interval_seconds: int = Field(default=300, ge=1)


class CalendarConfig(BaseModel):
    content: CalendarContentConfig = Field(default_factory=CalendarContentConfig)


CALENDAR_PROCESS = ManagedProcessDefinition(
    name="calendar_api",
    command=("python", "mcp/run_server.py"),
    env={
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
        "HOST": "127.0.0.1",
    },
    port_env="PORT",
    formal_port=18000,
    readiness_path="/health",
    startup_timeout_seconds=15.0,
)


api_version = 3
name = "calendar"
version = "3.2.1"
desc = "Google Calendar MCP and durable Alert source plugin"
Config = CalendarConfig
inject = (MANAGED_PROCESSES, MCP_SERVERS, TIMERS)


class CalendarContentApi:
    """Call the plugin-private loopback Content API with explicit HTTP failures."""

    def __init__(self, port: int) -> None:
        self._base_url = f"http://127.0.0.1:{port}"

    async def poll(self) -> Mapping[str, object]:
        return await asyncio.to_thread(self._request, "POST", "/content/poll", None)

    async def pending(self) -> tuple[Mapping[str, object], ...]:
        result = await asyncio.to_thread(
            self._request, "POST", "/content/pending", None
        )
        items = result.get("items")
        if not isinstance(items, list) or any(
            not isinstance(item, Mapping) for item in items
        ):
            raise RuntimeError("calendar pending items 不是对象列表")
        return tuple(cast(list[Mapping[str, object]], items))

    async def commit(self, batch_id: str) -> Mapping[str, object]:
        return await asyncio.to_thread(
            self._request, "POST", "/content/commit", {"batch_id": batch_id}
        )

    async def acknowledge(self, event_id: str) -> Mapping[str, object]:
        return await asyncio.to_thread(
            self._request, "POST", "/content/ack", {"event_id": event_id}
        )

    def _request(
        self, method: str, path: str, body: Mapping[str, object] | None
    ) -> Mapping[str, object]:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            self._base_url + path,
            data=payload,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as error:
            raise CalendarContentApiError(
                f"calendar Content API {path} failed: {error}"
            ) from error
        if not isinstance(result, dict):
            raise RuntimeError(f"calendar Content API {path} 返回非对象")
        return result


class CalendarSourceRuntime:
    """Report frozen Calendar Alert batches and own one-shot timers."""

    def __init__(
        self,
        timers: PluginTimers,
        alerts: BoundAlertSource,
        api: CalendarContentApiPort,
        interval: timedelta,
    ) -> None:
        self._timers = timers
        self._alerts = alerts
        self._api = api
        self._interval = interval
        self._handle: TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self, ctx: Context) -> None:
        """Spawn exactly one Fiber-owned formal-runtime poll loop."""

        if self._closed:
            raise RuntimeError("calendar Alert runtime 已关闭")
        if self._task is None:
            self._task = await ctx.spawn(self._run(), name="calendar-alert-poll")

    async def close(self) -> None:
        """Cancel the owned timer/task without changing durable progress."""

        self._closed = True
        handle = self._handle
        task = self._task
        self._handle = None
        self._task = None
        if handle is not None:
            _ = await handle.cancel()
        if task is not None and task is not asyncio.current_task():
            _ = await asyncio.gather(task, return_exceptions=True)
        if handle is not None:
            await handle.cleanup()

    async def _run(self) -> None:
        """Wait, poll, and re-arm while Fiber ownership remains healthy."""

        deadline = datetime.now(UTC)
        while not self._closed:
            handle = self._timers.schedule(deadline)
            self._handle = handle
            try:
                receipt = await handle.result()
                if receipt.status is TimerStatus.CANCELLED or self._closed:
                    return
                try:
                    await self._tick()
                except CalendarContentApiError:
                    logger.exception(
                        "calendar Content transport failed; next timer will retry"
                    )
            finally:
                self._handle = None
                await handle.cleanup()
            deadline = datetime.now(UTC) + self._interval

    async def _tick(self) -> None:
        """Report current alerts, ACK terminal identities, and commit the source batch."""

        # 1. Submitted Calendar rows remain queryable until Wake reaches a terminal state.
        for item in await self._api.pending():
            event_id, payload, _expires_at = _alert_item(item)
            if self._alerts.status(event_id=event_id) in {
                "delivered",
                "skipped",
                "expired",
            }:
                _ = await self._api.acknowledge(event_id)

        # 2. Freeze one new batch and report each stable Alert identity.
        batch = await self._api.poll()
        status = batch.get("status")
        if status == "no_batch":
            return
        if status != "batch":
            raise RuntimeError(f"calendar Content batch status 无效: {status!r}")
        batch_id = str(batch["batch_id"])
        items = batch["items"]
        if not isinstance(items, list):
            raise RuntimeError("calendar Alert batch items 不是列表")
        now = datetime.now(UTC)
        for item in items:
            if not isinstance(item, Mapping):
                raise RuntimeError("calendar Alert batch item 不是对象")
            event_id, payload, expires_at = _alert_item(item)
            _ = self._alerts.report(
                event_id=event_id,
                payload=payload,
                observed_at=now,
                expires_at=expires_at,
            )

        # 3. Advance only after Wake accepted every Alert report.
        _ = await self._api.commit(batch_id)


async def apply(ctx: Context, config: object) -> None:
    """Register Calendar capabilities and bind one formal-only Alert runtime."""

    if not isinstance(config, CalendarConfig):
        raise TypeError("calendar config 必须是 CalendarConfig")

    # 1. One process declaration owns both the launched port and loopback client fact.
    await ctx.require(MANAGED_PROCESSES).register(ctx, CALENDAR_PROCESS)
    await ctx.require(MCP_SERVERS).register(
        ctx,
        McpServerDefinition(
            name="calendar",
            command=("python", "mcp/run_mcp.py"),
            endpoint_env=(EndpointEnv("PORT", CALENDAR_PROCESS.name),),
            candidate_env={
                "CALENDAR_BACKEND": "recording",
                "GOOGLE_CLIENT_ID": "",
                "GOOGLE_CLIENT_SECRET": "",
                "HOST": "127.0.0.1",
            },
        ),
    )

    # 2. EventMail 存在时，独立子 Fiber 才启动 Alert 来源。
    async def apply_eventmail(source_ctx: Context) -> None:
        runtime = CalendarSourceRuntime(
            source_ctx.require(TIMERS),
            source_ctx.require(EVENTMAIL_ALERT_SOURCE).bind("calendar"),
            CalendarContentApi(CALENDAR_PROCESS.formal_port),
            timedelta(seconds=config.content.poll_interval_seconds),
        )

        def setup() -> object:
            return runtime.close

        _ = await source_ctx.effect(setup, label="calendar-alert-runtime")
        _ = await source_ctx.on(
            RUNTIME_STARTED, lambda _event: runtime.start(source_ctx)
        )
        _ = await source_ctx.on(RUNTIME_STOPPING, lambda _event: runtime.close())

    _ = await ctx.inject(
        (TIMERS, EVENTMAIL_ALERT_SOURCE),
        apply_eventmail,
        name="calendar-eventmail-source",
    )


def _alert_item(
    item: Mapping[str, object],
) -> tuple[str, Mapping[str, object], datetime | None]:
    event_id = item.get("item_id")
    payload = item.get("payload")
    if not isinstance(event_id, str) or not event_id:
        raise RuntimeError("calendar Alert item_id 必须是非空字符串")
    if not isinstance(payload, Mapping):
        raise RuntimeError("calendar Alert payload 必须是对象")
    raw_expiry = item.get("expires_at")
    if raw_expiry is None:
        expires_at = None
    elif isinstance(raw_expiry, str):
        expires_at = datetime.fromisoformat(raw_expiry)
        if expires_at.tzinfo is None:
            raise RuntimeError("calendar Alert expires_at 必须带时区")
    else:
        raise RuntimeError("calendar Alert expires_at 必须是 ISO 时间")
    return event_id, cast(Mapping[str, object], payload), expires_at
