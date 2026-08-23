from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from src.content_source import CalendarContentStore


NOW = datetime(2026, 8, 23, 10, tzinfo=UTC)


def _event(event_id: str, revision: str = "rev-1") -> dict[str, object]:
    return {
        "event_id": event_id,
        "revision": revision,
        "title": f"title-{event_id}",
        "content": f"content-{event_id}",
        "published_at": (NOW + timedelta(hours=1)).isoformat(),
        "metrics": {"raw_event_id": event_id, "calendar_id": "primary"},
    }


def test_freeze_replays_same_batch_and_commit_advances_cursor_once(tmp_path) -> None:
    store = CalendarContentStore(tmp_path / "calendar_alerts.sqlite3")
    store.initialize()

    first = store.freeze([_event("event-1")], NOW)
    replay = store.freeze([_event("event-2")], NOW + timedelta(minutes=1))
    committed = store.commit(str(first["batch_id"]), NOW)
    duplicate = store.commit(str(first["batch_id"]), NOW)
    second = store.freeze([_event("event-2")], NOW + timedelta(minutes=2))

    assert replay == first
    first_items = cast(list[dict[str, object]], first["items"])
    assert [item["item_id"] for item in first_items] == ["event-1"]
    assert committed == {"committed": True, "duplicate": False}
    assert duplicate == {"committed": True, "duplicate": True}
    assert second["cursor"] == 1
    second_items = cast(list[dict[str, object]], second["items"])
    assert [item["item_id"] for item in second_items] == ["event-2"]


def test_provider_ack_is_idempotent_and_preserves_legacy_fact_tables(tmp_path) -> None:
    store = CalendarContentStore(tmp_path / "calendar_alerts.sqlite3")
    store.initialize()
    batch = store.freeze([_event("event-1")], NOW)
    _ = store.commit(str(batch["batch_id"]), NOW)

    first = store.acknowledge("event-1", NOW)
    replay = store.acknowledge("event-1", NOW + timedelta(minutes=1))

    assert first == {"acknowledged": True, "duplicate": False}
    assert replay == {"acknowledged": True, "duplicate": True}
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM pending_alerts").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM acknowledged_alerts"
        ).fetchone() == (1,)


def test_legacy_schema_migrates_with_recovery_copy_and_exact_v1(tmp_path) -> None:
    path = tmp_path / "calendar_alerts.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE acknowledged_alerts(
                event_id TEXT PRIMARY KEY, raw_event_id TEXT NOT NULL,
                calendar_id TEXT NOT NULL, starts_at TEXT, title TEXT,
                content TEXT, acked_at TEXT NOT NULL, expires_at TEXT NOT NULL
            );
            CREATE TABLE pending_alerts(
                event_id TEXT PRIMARY KEY, raw_event_id TEXT NOT NULL,
                calendar_id TEXT NOT NULL, starts_at TEXT, title TEXT,
                content TEXT, last_seen_at TEXT NOT NULL, expires_at TEXT NOT NULL
            );
            INSERT INTO pending_alerts VALUES(
                'legacy', 'raw', 'primary', '2026-08-23T11:00:00+00:00',
                'legacy title', 'legacy content', '2026-08-23T10:00:00+00:00',
                '2026-08-25T10:00:00+00:00'
            );
            """)

    store = CalendarContentStore(path)
    store.initialize()

    assert path.with_suffix(".sqlite3.pre-content-v1.bak").is_file()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute(
            "SELECT event_id, revision, submitted_batch_id FROM pending_alerts"
        ).fetchone() == ("legacy", "1", None)


def test_same_version_malformed_schema_is_rejected(tmp_path) -> None:
    path = tmp_path / "calendar_alerts.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.execute("PRAGMA user_version = 1")

    with pytest.raises(RuntimeError, match="schema 不匹配"):
        CalendarContentStore(path).initialize()
