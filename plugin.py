from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from plugins.tools.contract import TOOLS
from .tools import register_tools

from pydantic import BaseModel, Field

from agent.plugin_composition import CREDENTIALS, RUNTIME_STARTED, RUNTIME_STOPPING, Context, CredentialRef
from agent.plugin_composition.plugin_config import PLUGIN_CONFIG
from plugins.managed_processes.contract import MANAGED_PROCESSES, ManagedProcessDefinition, ManagedProcessHandle
from plugins.onboarding.contract import ONBOARDING, Ability, PreviewLine
from plugins.plugin_settings.contract import (
    SETTINGS_FORMS, Authorize, SettingsField, SettingsForm, Setup, declare_settings, declare_setup,
)
from plugins.mcp.contract import MCP_SERVERS, EndpointEnv, McpServerDefinition
from plugins.timer.contract import TIMERS, OneShotTimer, TimerHandle, TimerStatus


logger = logging.getLogger(__name__)


from plugins.watch.contract import WATCH_EVENTS, WatchEvents


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
    # Google Cloud 的 OAuth 客户端；没填时服务回到数据目录里旧的 .env。
    google_client_id: str | None = None
    google_client_secret: CredentialRef | None = None


SETTINGS_FORM = SettingsForm(
    title="Google 日历", description="查询和安排 Google 日历里的日程，快开始的日程会交给值班提醒。",
    fields=(
        SettingsField("google_client_id", "Google OAuth 客户端 ID",
                      hint="Google Cloud 控制台新建「桌面应用」客户端，启用 Google Calendar API"),
        SettingsField("google_client_secret", "客户端密钥", kind="secret"),
    ),
    setup=Setup("calendar", "日程", Ability(
        pitch="帮你记着日程",
        benefit="接上 Google 日历后，聊天里就能查空档、加日程，快开始的事值班会提醒你。",
        preview=(PreviewLine("你", "周四下午三点帮我约个牙医"),
                 PreviewLine("Akashic", "加好了：周四 15:00–16:00「牙医」。那天上午还有个组会。"))),
    ),
)


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
    # 不挡宿主就绪；工具经 MCP 借用、Alert 轮询首轮前等就绪。
    background_ready=True,
)


api_version = 3
name = "calendar"
version = "3.4.0"
desc = "Google Calendar MCP and Watch event source plugin"
inject = (TOOLS, MANAGED_PROCESSES, MCP_SERVERS, TIMERS, CREDENTIALS)


# 日历服务私有回环接口上的就绪与授权；它自己保管进行中的授权和令牌文件。
class ServiceAuthorization:
    def __init__(self, ctx: Context, process: ManagedProcessHandle) -> None:
        self._ctx, self._process = ctx, process

    async def _call(self, method: str, path: str, body: Mapping[str, object] | None = None) -> Mapping[str, object]:
        async with self._process.borrow(self._ctx) as port:
            return await asyncio.to_thread(_service_json, method, f"http://127.0.0.1:{port}{path}", body)

    async def status(self) -> Mapping[str, object]:
        try:
            async with asyncio.timeout(5):
                health = await self._call("GET", "/health")
        except (OSError, ValueError, TimeoutError):
            return {"ready": False, "reason": "日历服务还没启动好，稍后刷新"}
        if not health.get("client_configured"):
            return {"ready": False, "reason": "还需要填写 Google OAuth 客户端 ID 和密钥"}
        if health.get("authentication") != "authenticated" and not health.get("token_saved"):
            return {"ready": False, "reason": "还没有授权 Google 日历：保存客户端后点下方「获取授权链接」"}
        return {"ready": True, "reason": ""}

    async def start(self) -> str:
        url = (await self._call("POST", "/auth/start")).get("url")
        if not isinstance(url, str) or not url.startswith("https://accounts.google.com/"):
            raise ValueError("日历服务没有给出 Google 授权地址")
        return url

    async def finish(self, callback: str) -> None:
        _ = await self._call("POST", "/auth/finish", {"callback": callback})


# 服务用 400 说明用户能改正的错误，这里原样变成 ValueError 交给设置页显示。
def _service_json(method: str, url: str, body: Mapping[str, object] | None) -> Mapping[str, object]:
    data = None if body is None else json.dumps(body).encode()
    request = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except HTTPError as error:
        detail = json.loads(error.read() or b"{}").get("detail") if error.code == 400 else None
        raise ValueError(detail if isinstance(detail, str) else f"日历服务返回 HTTP {error.code}") from error
    except URLError as error:
        raise ValueError(f"日历服务不可用：{error.reason}") from error
    if not isinstance(payload, dict):
        raise ValueError("日历服务返回的不是对象")
    return cast(Mapping[str, object], payload)


# 设置里填了客户端才覆盖进程环境；空字符串会让服务回到 .env。
async def _process_definition(ctx: Context, config: CalendarConfig) -> ManagedProcessDefinition:
    secret = config.google_client_secret
    if config.google_client_id is None or secret is None:
        return CALENDAR_PROCESS
    async with ctx.require(CREDENTIALS).open(ctx, {"google_client_secret": secret}) as client:
        env = {**CALENDAR_PROCESS.env, "GOOGLE_CLIENT_ID": config.google_client_id,
               "GOOGLE_CLIENT_SECRET": client.credential(secret)}
    return replace(CALENDAR_PROCESS, env=env)


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
        except HTTPError as error:
            detail = error.read(4096).decode("utf-8", errors="replace")
            raise CalendarContentApiError(
                f"calendar Content API {path} failed: HTTP {error.code}: {detail}"
            ) from error
        except (URLError, TimeoutError) as error:
            raise CalendarContentApiError(
                f"calendar Content API {path} failed: {error}"
            ) from error
        if not isinstance(result, dict):
            raise RuntimeError(f"calendar Content API {path} 返回非对象")
        return result


class CalendarSourceRuntime:
    """Emit frozen Calendar batches to Watch and own one-shot timers."""

    def __init__(
        self,
        timers: OneShotTimer,
        events: WatchEvents,
        api: CalendarContentApiPort,
        interval: timedelta,
        api_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._timers = timers
        self._events = events
        self._api = api
        self._interval = interval
        self._api_ready = api_ready
        self._handle: TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._drained = False

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

        # Calendar API 在后台就绪；首轮前等它可用，避免开机第一轮失败后等满间隔。
        if self._api_ready is not None:
            await self._api_ready()
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

    # 升级后第一轮：旧流程停在"未结算"的条目早已交过，直接确认掉，不再重交。
    async def _drain_legacy_pending(self) -> None:
        # 1. 已提交批次里的条目都交付过；尚未提交的活动批次会在 poll 中按冻结内容重新 emit。
        for item in await self._api.pending():
            event_id, _payload = _alert_item(item)
            _ = await self._api.acknowledge(event_id)
        self._drained = True

    # 取一个冻结批次，逐条 emit 并立刻确认，最后提交批次。
    async def _tick(self) -> None:
        # 1. 首轮清理旧积压。
        if not self._drained:
            await self._drain_legacy_pending()

        # 2. 取冻结批次。
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

        # 3. emit 成功即交付完成，立刻向 Calendar API 确认。
        for item in items:
            if not isinstance(item, Mapping):
                raise RuntimeError("calendar Alert batch item 不是对象")
            event_id, payload = _alert_item(item)
            await self._emit(event_id, payload)
            _ = await self._api.acknowledge(event_id)

        # 4. 全部交付后才推进批次游标。
        _ = await self._api.commit(batch_id)

    # 把 Calendar payload 映射成一条 Watch 事件，key 用稳定的 event_id。
    async def _emit(self, event_id: str, payload: Mapping[str, object]) -> None:
        url = payload.get("url")
        _ = await self._events.emit(
            "calendar",
            title=_text(payload.get("title")),
            text=_text(payload.get("content")),
            url=url if isinstance(url, str) else "",
            at=_aware_time(payload.get("published_at")),
            key=event_id,
        )


async def apply(ctx: Context) -> None:
    """Register Calendar capabilities and bind one formal-only Alert runtime."""
    config = CalendarConfig.model_validate(ctx.config)

    # 1. One process declaration owns both the launched port and loopback client fact.
    calendar_process = await ctx.require(MANAGED_PROCESSES).register(ctx, await _process_definition(ctx, config))
    await ctx.require(MCP_SERVERS).register(
        ctx,
        McpServerDefinition(
            name="calendar",
            command=("python", "mcp/run_mcp.py"),
            endpoint_env=(EndpointEnv("PORT", calendar_process),),
            candidate_env={
                "CALENDAR_BACKEND": "recording",
                "GOOGLE_CLIENT_ID": "",
                "GOOGLE_CLIENT_SECRET": "",
                "HOST": "127.0.0.1",
            },
        ),
    )

    await register_tools(ctx, description=desc)

    # 设置与引导是可选依赖；就绪看服务是否已有客户端和有效令牌，授权也经它完成。
    authorization = ServiceAuthorization(ctx, calendar_process)
    authorize = Authorize("Google 日历授权", "保存客户端后授权一次；之后令牌自动续期",
                          authorization.start, authorization.finish)

    async def settings(child: Context) -> None:
        _ = await declare_settings(child, CalendarConfig, SETTINGS_FORM, status=authorization.status, authorize=authorize)

    async def setup(child: Context) -> None:
        await declare_setup(child, CalendarConfig, SETTINGS_FORM, status=authorization.status)

    _ = await ctx.inject((SETTINGS_FORMS, PLUGIN_CONFIG), settings, name="settings")
    _ = await ctx.inject((SETTINGS_FORMS, PLUGIN_CONFIG, ONBOARDING), setup, name="onboarding")

    async def api_ready() -> None:
        async with calendar_process.borrow(ctx):
            return

    # 2. WATCH_EVENTS 存在时，独立子 Fiber 才启动日程来源。
    async def apply_watch(source_ctx: Context) -> None:
        runtime = CalendarSourceRuntime(
            source_ctx.require(TIMERS),
            source_ctx.require(WATCH_EVENTS),
            CalendarContentApi(CALENDAR_PROCESS.formal_port),
            timedelta(seconds=config.content.poll_interval_seconds),
            api_ready=api_ready,
        )
        _ = await source_ctx.effect(
            lambda: runtime.close,
            label="calendar-watch-runtime",
        )
        _ = await source_ctx.on(
            RUNTIME_STARTED, lambda _event: runtime.start(source_ctx)
        )
        _ = await source_ctx.on(RUNTIME_STOPPING, lambda _event: runtime.close())

    _ = await ctx.inject(
        (TIMERS, WATCH_EVENTS),
        apply_watch,
        name="calendar-watch-source",
    )


def _alert_item(item: Mapping[str, object]) -> tuple[str, Mapping[str, object]]:
    event_id = item.get("item_id")
    payload = item.get("payload")
    if not isinstance(event_id, str) or not event_id:
        raise RuntimeError("calendar Alert item_id 必须是非空字符串")
    if not isinstance(payload, Mapping):
        raise RuntimeError("calendar Alert payload 必须是对象")
    return event_id, cast(Mapping[str, object], payload)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


# 日程开始时间转成带时区时间；缺失或无时区则不带 at。
def _aware_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else None
