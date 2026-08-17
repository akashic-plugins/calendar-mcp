from __future__ import annotations

from pydantic import BaseModel, Field

from agent.plugin_composition import (
    MANAGED_PROCESSES,
    MCP_SERVERS,
    PROACTIVE_COMPONENTS,
    Context,
    EndpointEnv,
    ManagedProcessDefinition,
    McpServerDefinition,
    ProactiveSourceDefinition,
)


class CalendarProactiveConfig(BaseModel):
    enabled: bool = True


class CalendarConfig(BaseModel):
    proactive: CalendarProactiveConfig = Field(default_factory=CalendarProactiveConfig)


api_version = 3
name = "calendar"
version = "3.0.0"
desc = "Google Calendar MCP plugin"
Config = CalendarConfig
inject = (MANAGED_PROCESSES, MCP_SERVERS, PROACTIVE_COMPONENTS)


async def apply(ctx: Context, config: object) -> None:
    """注册 Calendar 运行时与可选的主动事件源。"""

    if not isinstance(config, CalendarConfig):
        raise TypeError("calendar config 必须是 CalendarConfig")

    # 1. 声明后端进程和 MCP；apply 本身不启动运行时
    await ctx.require(MANAGED_PROCESSES).register(
        ctx,
        ManagedProcessDefinition(
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
        ),
    )
    await ctx.require(MCP_SERVERS).register(
        ctx,
        McpServerDefinition(
            name="calendar",
            command=("python", "mcp/run_mcp.py"),
            required_tools=("get_proactive_events", "acknowledge_events"),
            candidate_read_only_tools=("get_proactive_events",),
            endpoint_env=(EndpointEnv("PORT", "calendar_api"),),
            candidate_env={
                "CALENDAR_BACKEND": "recording",
                "GOOGLE_CLIENT_ID": "",
                "GOOGLE_CLIENT_SECRET": "",
                "HOST": "127.0.0.1",
            },
        ),
    )

    # 2. 仅由用户配置决定是否发布主动事件源
    if config.proactive.enabled:
        await ctx.require(PROACTIVE_COMPONENTS).register(
            ctx,
            ProactiveSourceDefinition(
                name="upcoming_events",
                channels=("alert",),
                mcp_server="calendar",
                fetch_tool="get_proactive_events",
                ack_tool="acknowledge_events",
            ),
        )
