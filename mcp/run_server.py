from __future__ import annotations

import logging.config
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


RUNTIME_DIR = Path(os.environ.get("AKA_PLUGIN_DATA_DIR", "").strip() or Path.cwd())
load_dotenv(RUNTIME_DIR / ".env")
LOG_PATH = os.environ.get("CALENDAR_LOG_PATH", str(RUNTIME_DIR / "calendar_mcp.log"))

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {"format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"}
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "file": {
            "formatter": "default",
            "class": "logging.FileHandler",
            "filename": LOG_PATH,
            "mode": "a",
        },
    },
    "root": {"handlers": ["default", "file"], "level": "INFO"},
}


def main() -> None:
    uvicorn.run(
        "src.server:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "18000")),
        reload=False,
        log_config=LOGGING_CONFIG,
    )


if __name__ == "__main__":
    main()
