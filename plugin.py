from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field

from agent.control.timer import TimerHandle, TimerStatus
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
)


logger = logging.getLogger(__name__)


class BoundContentSource(Protocol):
    def submit(
        self, batch_id: str, items: Sequence[Mapping[str, object]]
    ) -> Mapping[str, object]: ...

    def unsettled(self, limit: int = 100) -> tuple[Mapping[str, object], ...]: ...

    def ack(self, settlement_ref: str) -> Mapping[str, object]: ...


class ContentSourceServices(Protocol):
    def bind(self, source_id: str) -> BoundContentSource: ...


CONTENT_SOURCE = ServiceKey[ContentSourceServices]("content.source.v1")


class CalendarContentApiPort(Protocol):
    async def poll(self) -> Mapping[str, object]: ...

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
version = "3.1.0"
desc = "Google Calendar MCP and durable Content source plugin"
Config = CalendarConfig
inject = (MANAGED_PROCESSES, MCP_SERVERS, TIMERS, CONTENT_SOURCE)


class CalendarContentApi:
    """Call the plugin-private loopback Content API with explicit HTTP failures."""

    def __init__(self, port: int) -> None:
        self._base_url = f"http://127.0.0.1:{port}"

    async def poll(self) -> Mapping[str, object]:
        return await asyncio.to_thread(self._request, "POST", "/content/poll", None)

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
    """Compose Content settlement, frozen Calendar batches, and one-shot timers."""

    def __init__(
        self,
        timers: PluginTimers,
        content: BoundContentSource,
        api: CalendarContentApiPort,
        interval: timedelta,
    ) -> None:
        self._timers = timers
        self._content = content
        self._api = api
        self._interval = interval
        self._handle: TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self, ctx: Context) -> None:
        """Spawn exactly one Fiber-owned formal-runtime poll loop."""

        if self._closed:
            raise RuntimeError("calendar Content runtime 已关闭")
        if self._task is None:
            self._task = await ctx.spawn(
                self._run(), name="calendar-content-poll"
            )

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
        """Settle delivered items, then submit and commit one stable poll batch."""

        # 1. Provider acknowledgement precedes Content acknowledgement.
        for unsettled in self._content.unsettled():
            ref = unsettled["ref"]
            if not isinstance(ref, Mapping):
                raise RuntimeError("calendar Content unsettled ref 不是对象")
            await self._api.acknowledge(str(ref["item_id"]))
            _ = self._content.ack(str(unsettled["settlement_ref"]))

        # 2. Calendar freezes a batch; Core deduplicates replay by batch and revision.
        batch = await self._api.poll()
        status = batch.get("status")
        if status == "no_batch":
            return
        if status != "batch":
            raise RuntimeError(f"calendar Content batch status 无效: {status!r}")
        batch_id = str(batch["batch_id"])
        items = batch["items"]
        if not isinstance(items, list):
            raise RuntimeError("calendar Content batch items 不是列表")
        _ = self._content.submit(batch_id, items)

        # 3. Advance the Calendar cursor only after Content accepted the batch.
        _ = await self._api.commit(batch_id)


async def apply(ctx: Context, config: object) -> None:
    """Register Calendar capabilities and bind one formal-only Content runtime."""

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

    # 2. The ordinary Content source owns its lifecycle through existing hooks.
    runtime = CalendarSourceRuntime(
        ctx.require(TIMERS),
        ctx.require(CONTENT_SOURCE).bind("calendar"),
        CalendarContentApi(CALENDAR_PROCESS.formal_port),
        timedelta(seconds=config.content.poll_interval_seconds),
    )

    def setup() -> object:
        return runtime.close

    _ = await ctx.effect(setup, label="calendar-content-runtime")
    _ = await ctx.on(RUNTIME_STARTED, lambda _event: runtime.start(ctx))
    _ = await ctx.on(RUNTIME_STOPPING, lambda _event: runtime.close())
