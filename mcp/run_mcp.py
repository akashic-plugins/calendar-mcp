#!/usr/bin/env python3
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv


def _wait_for_server(process: subprocess.Popen[bytes], host: str, port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Calendar HTTP 服务启动失败: exit={process.returncode}")
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("Calendar HTTP 服务启动超时")


def main() -> None:
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
    host = os.environ.setdefault("HOST", "127.0.0.1")
    port = int(os.environ.setdefault("PORT", "18000"))
    os.chdir(script_dir)
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))

    try:
        with socket.create_connection((host, port), timeout=0.2):
            raise RuntimeError(f"Calendar HTTP 端口已被占用: {host}:{port}")
    except OSError:
        pass

    http_process = subprocess.Popen(
        [sys.executable, str(script_dir / "run_server.py")],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ,
        cwd=script_dir,
    )
    try:
        _wait_for_server(http_process, host, port)
        from src.mcp_bridge import create_mcp_server

        create_mcp_server().run(transport="stdio")
    finally:
        http_process.terminate()
        try:
            http_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            http_process.kill()
            http_process.wait()


if __name__ == "__main__":
    main()
