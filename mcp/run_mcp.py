#!/usr/bin/env python3
from __future__ import annotations

def main() -> None:
    from src.mcp_bridge import create_mcp_server

    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
