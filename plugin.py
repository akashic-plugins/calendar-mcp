from __future__ import annotations

import shutil
from pathlib import Path

from agent.plugins import Plugin


class CalendarPlugin(Plugin):
    name = "calendar"
    version = "0.1.0"
    desc = "Google Calendar MCP plugin"

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
