#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _configure_environment() -> Path:
    script_dir = Path(__file__).resolve().parent
    data_dir = Path(os.environ.get("AKA_PLUGIN_DATA_DIR", "").strip() or script_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    load_dotenv(data_dir / ".env")
    os.environ["TOKEN_FILE_PATH"] = str(data_dir / ".gcp-saved-tokens.json")
    os.environ["CALENDAR_PROACTIVE_CONFIG_PATH"] = str(
        data_dir / "proactive_alerts.json"
    )
    os.environ["CALENDAR_LOG_PATH"] = str(data_dir / "calendar_mcp.log")
    os.environ.setdefault("RELOAD", "false")
    os.chdir(script_dir)
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    return data_dir


def main() -> None:
    _configure_environment()
    from src.mcp_bridge import create_mcp_server

    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
