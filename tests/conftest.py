from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


repo_root = Path(__file__).resolve().parents[1]
agent_root = Path(
    os.environ.get("AKASHIC_AGENT_ROOT", "").strip()
    or repo_root.parents[1] / "akasic-agent"
)
for path in (repo_root, repo_root / "mcp", agent_root):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_test_data_dir = tempfile.TemporaryDirectory(prefix="calendar-plugin-tests-")
if not os.environ.get("AKA_PLUGIN_DATA_DIR", "").strip():
    os.environ["AKA_PLUGIN_DATA_DIR"] = _test_data_dir.name
