from __future__ import annotations

import logging.config
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


def _runtime_dir() -> Path:
    raw = os.environ.get("AKA_PLUGIN_DATA_DIR", "").strip()
    if not raw:
        raise RuntimeError("calendar service 缺少 AKA_PLUGIN_DATA_DIR")
    path = Path(raw).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


RUNTIME_DIR = _runtime_dir()
load_dotenv(RUNTIME_DIR / ".env", override=True)
os.environ["TOKEN_FILE_PATH"] = str(RUNTIME_DIR / ".gcp-saved-tokens.json")
os.environ["CALENDAR_CONTENT_CONFIG_PATH"] = str(RUNTIME_DIR / "content.json")
os.environ.setdefault("RELOAD", "false")

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
    },
    "root": {"handlers": ["default"], "level": "INFO"},
}


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    os.chdir(script_dir)
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    uvicorn.run(
        "src.server:app",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "18000")),
        reload=False,
        log_config=LOGGING_CONFIG,
    )


if __name__ == "__main__":
    main()
