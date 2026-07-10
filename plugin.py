from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field

from agent.plugins import (
    ManagedServiceSpec,
    McpServerSpec,
    Plugin,
    ProactiveSourceSpec,
)


class CalendarProactiveConfig(BaseModel):
    enabled: bool = True


class CalendarConfig(BaseModel):
    proactive: CalendarProactiveConfig = Field(default_factory=CalendarProactiveConfig)


class CalendarPlugin(Plugin):
    name = "calendar"
    version = "1.0.0"
    desc = "Google Calendar MCP plugin"
    ConfigModel = CalendarConfig

    @classmethod
    def mcp_servers(cls) -> list[McpServerSpec]:
        return [
            McpServerSpec(
                name="calendar",
                command=("python", "mcp/run_mcp.py"),
            )
        ]

    @classmethod
    def managed_services(cls) -> list[ManagedServiceSpec]:
        return [
            ManagedServiceSpec(
                id="calendar_api",
                command=("python", "mcp/run_server.py"),
                cwd="mcp",
                readiness_url="http://127.0.0.1:18000/health",
            )
        ]

    def proactive_sources(self) -> list[ProactiveSourceSpec]:
        config = cast(CalendarConfig, self.context.config)
        if not config.proactive.enabled:
            return []
        return [
            ProactiveSourceSpec(
                id="upcoming_events",
                channels=("alert",),
                server="calendar",
                fetch_tool="get_proactive_events",
                ack_tool="acknowledge_events",
            )
        ]

    async def initialize(self) -> None:
        data_dir = self.context.data_dir
        workspace = self.context.workspace
        if data_dir is None or workspace is None:
            return
        data_dir.mkdir(parents=True, exist_ok=True)
        legacy_dir = workspace / "mcp" / "calendar-mcp"
        for name in (
            ".env",
            ".gcp-saved-tokens.json",
            "proactive_alerts.json",
            "calendar_alerts.sqlite3",
        ):
            source = legacy_dir / name
            target = data_dir / name
            if source.exists() and not target.exists():
                shutil.copy2(source, target)
        config = data_dir / "proactive_alerts.json"
        if not config.exists():
            shutil.copy2(self.context.plugin_dir / "mcp" / "proactive_alerts.json", config)
