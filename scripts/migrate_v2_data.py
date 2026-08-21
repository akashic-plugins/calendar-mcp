#!/usr/bin/env python3
"""把 Calendar v2 workspace 数据非破坏迁移到 v3 plugin-data。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from agent.plugins.manifest import (
    ensure_workspace_plugin_data_dir,
    validate_workspace_plugin_data_path,
)
from bootstrap.workspace_lock import WorkspaceInstanceLock


_DATA_FILES = (
    ".env",
    ".gcp-saved-tokens.json",
    "proactive_alerts.json",
    "calendar_alerts.sqlite3",
)
_RECEIPT = ".calendar-v2-migration.json"


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _sqlite_integrity(path: Path) -> str:
    """读取 SQLite integrity receipt，不修改数据库。"""

    uri = f"file:{path}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as database:
        result = database.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise sqlite3.DatabaseError(f"Calendar SQLite 完整性检查失败: {path} ({result})")
    return "ok"


def _copy_sqlite(source: Path, destination: Path) -> str:
    """用 SQLite 在线备份生成一致副本并校验。"""

    uri = f"file:{source}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source_db:
        with closing(sqlite3.connect(destination)) as destination_db:
            source_db.backup(destination_db, pages=256, sleep=0.1)
            destination_db.commit()
    return _sqlite_integrity(destination)


def _stage_files(source: Path, staging: Path) -> tuple[dict[str, object], ...]:
    """复制全部现存 v2 文件并返回内容 receipt。"""

    entries: list[dict[str, object]] = []
    for name in _DATA_FILES:
        source_file = source / name
        if not source_file.exists():
            entries.append({"name": name, "status": "source_missing"})
            continue
        if source_file.is_symlink() or not source_file.is_file():
            raise ValueError(f"Calendar v2 数据不是普通文件: {source_file}")
        staged_file = staging / name
        integrity = None
        if source_file.suffix == ".sqlite3":
            integrity = _copy_sqlite(source_file, staged_file)
        else:
            shutil.copy2(source_file, staged_file)
        entry: dict[str, object] = {
            "name": name,
            "status": "staged",
            "sha256": _digest(staged_file),
            "size": staged_file.stat().st_size,
        }
        if integrity is not None:
            entry["sqlite_integrity"] = integrity
        entries.append(entry)
    return tuple(entries)


def _validate_targets(target: Path, entries: tuple[dict[str, object], ...]) -> None:
    """发布前拒绝覆盖任何不同内容的正式数据。"""

    for entry in entries:
        name = str(entry["name"])
        destination = target / name
        if destination.is_symlink():
            raise ValueError(f"Calendar v3 目标不得是符号链接: {destination}")
        if entry["status"] == "source_missing":
            if not destination.exists():
                continue
            if not destination.is_file():
                raise FileExistsError(f"Calendar v3 目标不是普通文件: {destination}")
            entry.update(
                status="target_only",
                sha256=_digest(destination),
                size=destination.stat().st_size,
            )
            if destination.suffix == ".sqlite3":
                entry["sqlite_integrity"] = _sqlite_integrity(destination)
            continue
        if not destination.exists():
            entry["status"] = "copied"
            continue
        if not destination.is_file() or _digest(destination) != entry["sha256"]:
            raise FileExistsError(f"Calendar v3 目标已存在且内容不同: {destination}")
        entry["status"] = "target_verified"

    if all(entry["status"] == "source_missing" for entry in entries):
        raise FileNotFoundError("Calendar v2 与 v3 数据目录都没有可迁移文件")


def _publish(
    staging: Path,
    target: Path,
    entries: tuple[dict[str, object], ...],
    receipt: dict[str, object],
) -> None:
    """发布本事务创建的文件，失败时回滚且不覆盖旧数据。"""

    published: list[Path] = []
    receipt_path = target / _RECEIPT
    try:
        for entry in entries:
            name = str(entry["name"])
            destination = target / name
            if entry["status"] != "copied":
                continue
            os.replace(staging / name, destination)
            published.append(destination)
        staged_receipt = staging / _RECEIPT
        staged_receipt.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(staged_receipt, receipt_path)
        published.append(receipt_path)
    except BaseException:
        for path in reversed(published):
            path.unlink(missing_ok=True)
        raise


def _remove_stale_staging(workspace: Path) -> None:
    """清理上次进程崩溃遗留且未发布的 Calendar staging。"""

    root = workspace / "plugin-data"
    if not root.is_dir():
        return
    for path in root.glob(".calendar-v2-migrate-*"):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def _has_valid_receipt(path: Path, *, target: Path, marketplace: str) -> bool:
    """在持久化边界校验已有迁移 receipt。"""

    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Calendar migration receipt 不是普通文件: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    expected_target = f"plugin-data/calendar-{marketplace}"
    files = value.get("files") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("source") != "mcp/calendar-mcp"
        or value.get("target") != expected_target
        or value.get("recovery")
        != {"kind": "retained_source", "path": "mcp/calendar-mcp"}
        or not isinstance(files, list)
        or [item.get("name") for item in files if isinstance(item, dict)]
        != list(_DATA_FILES)
    ):
        raise ValueError(f"Calendar migration receipt 无效: {path}")
    for item in files:
        if not isinstance(item, dict) or item.get("status") not in {
            "source_missing",
            "target_only",
            "target_verified",
            "copied",
        }:
            raise ValueError(f"Calendar migration receipt 无效: {path}")
        if item["status"] == "source_missing":
            continue
        digest = item.get("sha256")
        size = item.get("size")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise ValueError(f"Calendar migration receipt 文件证据无效: {path}")
        target_file = target / str(item["name"])
        if not target_file.is_file() or target_file.is_symlink():
            raise ValueError(f"Calendar migration receipt 目标缺失: {target_file}")
        if target_file.stat().st_size != size or _digest(target_file) != digest:
            raise ValueError(f"Calendar migration receipt 目标内容漂移: {target_file}")
        if target_file.suffix == ".sqlite3":
            if item.get("sqlite_integrity") != "ok":
                raise ValueError(f"Calendar migration receipt 缺少 SQLite integrity: {path}")
            _sqlite_integrity(target_file)
    return True


def migrate_v2_data(*, workspace: Path, marketplace: str) -> Path:
    """持有 workspace 独占锁迁移 Calendar 数据并写最终 receipt。"""

    workspace = workspace.expanduser().resolve()
    lock = WorkspaceInstanceLock(workspace)
    lock.acquire()
    try:
        return _migrate_locked(workspace=workspace, marketplace=marketplace)
    finally:
        lock.release()


def _migrate_locked(*, workspace: Path, marketplace: str) -> Path:
    """在 workspace 独占区间准备、校验并发布一次迁移。"""

    if not marketplace or not marketplace.replace("-", "").isalnum():
        raise ValueError(f"Calendar marketplace 无效: {marketplace!r}")
    legacy_root = workspace / "mcp"
    source = legacy_root / "calendar-mcp"
    if (
        legacy_root.is_symlink()
        or source.is_symlink()
        or not source.is_dir()
        or not source.resolve().is_relative_to(workspace)
    ):
        raise FileNotFoundError(f"Calendar v2 数据目录不存在或不安全: {source}")
    target = workspace / "plugin-data" / f"calendar-{marketplace}"
    validate_workspace_plugin_data_path(target, workspace)
    _remove_stale_staging(workspace)
    receipt_path = target / _RECEIPT
    if _has_valid_receipt(receipt_path, target=target, marketplace=marketplace):
        return receipt_path

    staging = workspace / "plugin-data" / f".calendar-v2-migrate-{uuid.uuid4().hex}"
    created_target = not target.exists()
    ensure_workspace_plugin_data_dir(staging, workspace)
    try:
        entries = _stage_files(source, staging)
        ensure_workspace_plugin_data_dir(target, workspace)
        _validate_targets(target, entries)
        receipt: dict[str, object] = {
            "schema_version": 1,
            "source": "mcp/calendar-mcp",
            "target": f"plugin-data/calendar-{marketplace}",
            "recovery": {
                "kind": "retained_source",
                "path": "mcp/calendar-mcp",
            },
            "files": entries,
        }
        _publish(staging, target, entries, receipt)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if created_target and target.is_dir() and not any(target.iterdir()):
            target.rmdir()
    return receipt_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--marketplace", default="github")
    args = parser.parse_args()
    print(migrate_v2_data(workspace=args.workspace, marketplace=args.marketplace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
