import json
import os
import sqlite3
from pathlib import Path

import pytest

from bootstrap.workspace_lock import WorkspaceInstanceLock
from scripts import migrate_v2_data as migration


def _legacy_data(workspace: Path) -> Path:
    source = workspace / "mcp" / "calendar-mcp"
    source.mkdir(parents=True)
    (source / ".env").write_text("GOOGLE_CLIENT_ID=kept\n", encoding="utf-8")
    (source / "proactive_alerts.json").write_text(
        '{"enabled": true}\n',
        encoding="utf-8",
    )
    with sqlite3.connect(source / "calendar_alerts.sqlite3") as database:
        database.execute("CREATE TABLE receipts (value TEXT NOT NULL)")
        database.execute("INSERT INTO receipts VALUES ('kept')")
        database.commit()
    return source


def test_migration_preserves_source_and_publishes_verified_receipt(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = _legacy_data(workspace)

    receipt_path = migration.migrate_v2_data(
        workspace=workspace,
        marketplace="github",
    )

    target = workspace / "plugin-data" / "calendar-github"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == 2
    assert receipt["recovery"] == {
        "kind": "retained_source",
        "path": "mcp/calendar-mcp",
    }
    assert [item["name"] for item in receipt["files"]] == [
        ".env",
        ".gcp-saved-tokens.json",
        "content.json",
        "calendar_alerts.sqlite3",
    ]
    assert [item["status"] for item in receipt["files"]] == [
        "copied",
        "source_missing",
        "copied",
        "copied",
    ]
    assert receipt["files"][-1]["sqlite_integrity"] == "ok"
    assert (source / ".env").read_text(encoding="utf-8") == "GOOGLE_CLIENT_ID=kept\n"
    assert (target / ".env").read_text(encoding="utf-8") == "GOOGLE_CLIENT_ID=kept\n"
    assert json.loads((target / "content.json").read_text(encoding="utf-8")) == {}
    with sqlite3.connect(target / "calendar_alerts.sqlite3") as database:
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert database.execute("SELECT value FROM receipts").fetchone() == ("kept",)
    assert migration.migrate_v2_data(
        workspace=workspace,
        marketplace="github",
    ) == receipt_path


def test_migration_rejects_conflict_without_changing_either_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _legacy_data(workspace)
    target = workspace / "plugin-data" / "calendar-github"
    target.mkdir(parents=True)
    (target / ".env").write_text("GOOGLE_CLIENT_ID=current\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="内容不同"):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")

    assert (source / ".env").read_text(encoding="utf-8") == "GOOGLE_CLIENT_ID=kept\n"
    assert (target / ".env").read_text(encoding="utf-8") == "GOOGLE_CLIENT_ID=current\n"
    assert not (target / migration._RECEIPT).exists()
    assert list((workspace / "plugin-data").glob(".calendar-v2-migrate-*")) == []


def test_migration_rolls_back_files_published_before_in_process_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    _legacy_data(workspace)
    original_replace = os.replace
    calls = 0

    def fail_second_publish(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publish failure")
        original_replace(source, destination)

    monkeypatch.setattr(migration.os, "replace", fail_second_publish)
    with pytest.raises(OSError, match="injected publish failure"):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")

    assert not (workspace / "plugin-data" / "calendar-github").exists()
    assert list((workspace / "plugin-data").glob(".calendar-v2-migrate-*")) == []


def test_migration_recovers_matching_partial_publish_after_process_crash(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = _legacy_data(workspace)
    plugin_data = workspace / "plugin-data"
    target = plugin_data / "calendar-github"
    stale = plugin_data / ".calendar-v2-migrate-crashed"
    target.mkdir(parents=True)
    stale.mkdir()
    (target / ".env").write_bytes((source / ".env").read_bytes())
    (stale / "orphan").write_text("partial", encoding="utf-8")

    receipt = migration.migrate_v2_data(
        workspace=workspace,
        marketplace="github",
    )

    assert receipt.is_file()
    assert not stale.exists()
    assert (target / "content.json").is_file()
    assert (target / "calendar_alerts.sqlite3").is_file()
    statuses = {
        item["name"]: item["status"]
        for item in json.loads(receipt.read_text(encoding="utf-8"))["files"]
    }
    assert statuses[".env"] == "target_verified"
    assert statuses["content.json"] == "copied"


def test_migration_requires_idle_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _legacy_data(workspace)
    lock = WorkspaceInstanceLock(workspace)
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="其他 runtime 占用"):
            migration.migrate_v2_data(workspace=workspace, marketplace="github")
    finally:
        lock.release()

    assert not (workspace / "plugin-data").exists()


def test_migration_rejects_untrusted_existing_receipt(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _legacy_data(workspace)
    target = workspace / "plugin-data" / "calendar-github"
    target.mkdir(parents=True)
    (target / migration._RECEIPT).write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="receipt 无效"):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")


def test_existing_v1_receipt_is_verified_then_upgraded_to_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _legacy_data(workspace)
    target = workspace / "plugin-data" / "calendar-github"
    target.mkdir(parents=True)
    entries = []
    for name in (
        ".env",
        ".gcp-saved-tokens.json",
        "proactive_alerts.json",
        "calendar_alerts.sqlite3",
    ):
        source_file = source / name
        if not source_file.exists():
            entries.append({"name": name, "status": "source_missing"})
            continue
        target_file = target / name
        target_file.write_bytes(source_file.read_bytes())
        entries.append(
            {
                "name": name,
                "status": "copied",
                "sha256": migration._digest(target_file),
                "size": target_file.stat().st_size,
                **(
                    {"sqlite_integrity": "ok"}
                    if target_file.suffix == ".sqlite3"
                    else {}
                ),
            }
        )
    receipt = target / migration._RECEIPT
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "mcp/calendar-mcp",
                "target": "plugin-data/calendar-github",
                "recovery": {
                    "kind": "retained_source",
                    "path": "mcp/calendar-mcp",
                },
                "files": entries,
            }
        ),
        encoding="utf-8",
    )

    assert migration.migrate_v2_data(
        workspace=workspace, marketplace="github"
    ) == receipt
    upgraded = json.loads(receipt.read_text(encoding="utf-8"))
    assert upgraded["schema_version"] == 2
    assert upgraded["files"][2]["name"] == "content.json"
    assert not (target / "proactive_alerts.json").exists()
    assert json.loads((target / "content.json").read_text(encoding="utf-8")) == {}


def test_migration_receipt_rejects_missing_published_target(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _legacy_data(workspace)
    receipt = migration.migrate_v2_data(workspace=workspace, marketplace="github")
    target = receipt.parent
    (target / ".env").unlink()

    with pytest.raises(ValueError, match="目标缺失"):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")


def test_migration_receipt_rejects_target_content_drift(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _legacy_data(workspace)
    receipt = migration.migrate_v2_data(workspace=workspace, marketplace="github")
    target = receipt.parent / ".env"
    target.write_bytes(b"X" * target.stat().st_size)

    with pytest.raises(ValueError, match="内容漂移"):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")


def test_migration_receipt_rechecks_sqlite_integrity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _legacy_data(workspace)
    receipt_path = migration.migrate_v2_data(
        workspace=workspace,
        marketplace="github",
    )
    database = receipt_path.parent / "calendar_alerts.sqlite3"
    database.write_bytes(b"not-a-sqlite-database")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    database_entry = next(
        item for item in receipt["files"] if item["name"] == database.name
    )
    database_entry["size"] = database.stat().st_size
    database_entry["sha256"] = migration._digest(database)
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(sqlite3.DatabaseError):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")


def test_migration_records_target_only_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _legacy_data(workspace)
    target = workspace / "plugin-data" / "calendar-github"
    target.mkdir(parents=True)
    (target / ".gcp-saved-tokens.json").write_text("{}\n", encoding="utf-8")

    receipt = migration.migrate_v2_data(workspace=workspace, marketplace="github")
    entries = {
        item["name"]: item
        for item in json.loads(receipt.read_text(encoding="utf-8"))["files"]
    }
    assert entries[".gcp-saved-tokens.json"]["status"] == "target_only"
    assert entries[".gcp-saved-tokens.json"]["size"] == 3


def test_migration_rejects_legacy_root_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    _legacy_data(outside)
    workspace.mkdir()
    (workspace / "mcp").symlink_to(outside / "mcp", target_is_directory=True)

    with pytest.raises(FileNotFoundError, match="不安全"):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")

    assert not (workspace / "plugin-data").exists()
