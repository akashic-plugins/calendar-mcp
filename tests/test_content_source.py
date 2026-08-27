from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from src.content_source import (
    CalendarContentConfig,
    CalendarContentStore,
    _to_event,
    acknowledge_content,
    pending_content,
)


NOW = datetime(2026, 8, 23, 10, tzinfo=UTC)


def _event(event_id: str, revision: str = "rev-1") -> dict[str, object]:
    return {
        "event_id": event_id,
        "revision": revision,
        "title": f"title-{event_id}",
        "content": f"content-{event_id}",
        "url": f"https://calendar.example/{event_id}",
        "severity": "high",
        "source_name": "configured-calendar",
        "source_type": "configured-alert",
        "kind": "alert",
        "published_at": (NOW + timedelta(hours=1)).isoformat(),
        "metrics": {"raw_event_id": event_id, "calendar_id": "primary"},
    }


def test_freeze_replays_same_batch_and_commit_advances_cursor_once(tmp_path) -> None:
    store = CalendarContentStore(tmp_path / "calendar_alerts.sqlite3")
    store.initialize()

    first = store.freeze([_event("event-1")], NOW)
    replay = store.freeze([_event("event-2")], NOW + timedelta(minutes=1))
    assert first is not None
    committed = store.commit(str(first["batch_id"]), NOW)
    duplicate = store.commit(str(first["batch_id"]), NOW)
    second = store.freeze([_event("event-2")], NOW + timedelta(minutes=2))
    assert second is not None

    assert replay == first
    first_items = cast(list[dict[str, object]], first["items"])
    assert [item["item_id"] for item in first_items] == ["event-1"]
    assert first_items[0]["payload"] == _event("event-1")
    assert committed == {"committed": True, "duplicate": False}
    assert duplicate == {"committed": True, "duplicate": True}
    assert second["cursor"] == 1
    second_items = cast(list[dict[str, object]], second["items"])
    assert [item["item_id"] for item in second_items] == ["event-2"]


def test_provider_ack_is_idempotent_and_preserves_legacy_fact_tables(tmp_path) -> None:
    store = CalendarContentStore(tmp_path / "calendar_alerts.sqlite3")
    store.initialize()
    batch = store.freeze([_event("event-1")], NOW)
    assert batch is not None
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


def test_pending_endpoint_roundtrip_survives_commit_until_terminal_ack(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "content.json"
    config_path.write_text(
        json.dumps({"db_path": "calendar_alerts.sqlite3"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CALENDAR_CONTENT_CONFIG_PATH", str(config_path))
    store = CalendarContentStore(tmp_path / "calendar_alerts.sqlite3")
    store.initialize()
    batch = store.freeze([_event("event-1")], NOW)
    assert batch is not None
    _ = store.commit(str(batch["batch_id"]), NOW)

    assert [item["item_id"] for item in pending_content()["items"]] == ["event-1"]
    assert acknowledge_content("event-1")["acknowledged"] is True
    assert pending_content() == {"items": []}


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
            """
            SELECT event_id, revision, submitted_batch_id, payload_origin,
                   payload_json FROM pending_alerts
            """
        ).fetchone()[:4] == ("legacy", "1", None, "legacy_fallback")
        payload = json.loads(
            connection.execute("SELECT payload_json FROM pending_alerts").fetchone()[0]
        )
        assert payload["title"] == "legacy title"
        assert payload["metrics"] == {
            "calendar_id": "primary",
            "raw_event_id": "raw",
        }
        assert "url" not in payload


def test_malformed_fallback_config_leaves_legacy_db_atomic_and_replays(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "calendar_alerts.sqlite3"
    config_path = tmp_path / "content.json"
    config_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("CALENDAR_CONTENT_CONFIG_PATH", str(config_path))
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
                'pending-legacy', 'pending-raw', 'primary',
                '2026-08-23T11:00:00+00:00', 'pending title', 'pending content',
                '2026-08-23T10:00:00+00:00', '2026-08-25T10:00:00+00:00'
            );
            INSERT INTO acknowledged_alerts VALUES(
                'acked-legacy', 'acked-raw', 'primary',
                '2026-08-22T11:00:00+00:00', 'acked title', 'acked content',
                '2026-08-22T12:00:00+00:00', '2026-08-29T12:00:00+00:00'
            );
            """)

    store = CalendarContentStore(path)
    with pytest.raises(json.JSONDecodeError):
        store.initialize()

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert tables == {"acknowledged_alerts", "pending_alerts"}
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert connection.execute(
            "SELECT event_id FROM pending_alerts"
        ).fetchall() == [("pending-legacy",)]
        assert connection.execute(
            "SELECT event_id FROM acknowledged_alerts"
        ).fetchall() == [("acked-legacy",)]

    config_path.write_text(
        json.dumps(
            {
                "source_name": "restored-calendar",
                "source_type": "restored-alert",
            }
        ),
        encoding="utf-8",
    )
    store.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute(
            "SELECT event_id FROM acknowledged_alerts"
        ).fetchall() == [("acked-legacy",)]
        pending = connection.execute(
            "SELECT event_id, payload_origin, payload_json FROM pending_alerts"
        ).fetchone()
        assert pending[:2] == ("pending-legacy", "legacy_fallback")
        payload = json.loads(pending[2])
        assert payload["source_name"] == "restored-calendar"
        assert payload["source_type"] == "restored-alert"


def test_same_version_malformed_schema_is_rejected(tmp_path) -> None:
    path = tmp_path / "calendar_alerts.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.execute("PRAGMA user_version = 1")

    with pytest.raises(RuntimeError, match="schema 不匹配"):
        CalendarContentStore(path).initialize()


def test_empty_poll_creates_no_batch_cursor_or_row(tmp_path) -> None:
    store = CalendarContentStore(tmp_path / "calendar_alerts.sqlite3")
    store.initialize()

    assert store.freeze([], NOW) is None

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT cursor FROM source_state").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM poll_batches").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM pending_alerts").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM acknowledged_alerts"
        ).fetchone() == (0,)


def test_committed_batches_and_ack_history_are_never_purged(tmp_path) -> None:
    store = CalendarContentStore(tmp_path / "calendar_alerts.sqlite3")
    store.initialize()
    first = store.freeze([_event("event-1")], NOW)
    assert first is not None
    _ = store.commit(str(first["batch_id"]), NOW)
    _ = store.acknowledge("event-1", NOW)
    second = store.freeze([_event("event-2")], NOW + timedelta(minutes=1))
    assert second is not None
    _ = store.commit(str(second["batch_id"]), NOW)
    _ = store.acknowledge("event-2", NOW)

    assert store.freeze([], NOW + timedelta(days=30)) is None

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM poll_batches").fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM acknowledged_alerts"
        ).fetchone() == (2,)


def test_repoll_of_submitted_revision_is_a_database_no_op(tmp_path) -> None:
    store = CalendarContentStore(tmp_path / "calendar_alerts.sqlite3")
    store.initialize()
    first = store.freeze([_event("event-1")], NOW)
    assert first is not None
    _ = store.commit(str(first["batch_id"]), NOW)
    with sqlite3.connect(store.path) as connection:
        before = connection.execute(
            "SELECT cursor, (SELECT count(*) FROM poll_batches), "
            "(SELECT last_seen_at FROM pending_alerts WHERE event_id='event-1') "
            "FROM source_state"
        ).fetchone()

    assert store.freeze([_event("event-1")], NOW + timedelta(hours=1)) is None

    with sqlite3.connect(store.path) as connection:
        after = connection.execute(
            "SELECT cursor, (SELECT count(*) FROM poll_batches), "
            "(SELECT last_seen_at FROM pending_alerts WHERE event_id='event-1') "
            "FROM source_state"
        ).fetchone()
    assert after == before


def test_revised_event_reuses_identity_and_replaces_pending_payload(tmp_path) -> None:
    store = CalendarContentStore(tmp_path / "calendar_alerts.sqlite3")
    store.initialize()
    first = store.freeze([_event("event-1")], NOW)
    assert first is not None
    _ = store.commit(str(first["batch_id"]), NOW)

    revised = _event("event-1", "rev-2")
    revised["title"] = "revised title"
    revised["content"] = "revised content"
    second = store.freeze([revised], NOW + timedelta(minutes=1))

    assert second is not None
    items = cast(list[dict[str, object]], second["items"])
    assert len(items) == 1
    assert items[0]["item_id"] == "event-1"
    assert items[0]["revision"] == "rev-2"
    assert cast(dict[str, object], items[0]["payload"])["title"] == "revised title"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM pending_alerts").fetchone() == (1,)


def test_google_revision_and_start_changes_keep_one_alert_identity(tmp_path) -> None:
    config = CalendarContentConfig(
        lookahead_hours=6,
        max_results_per_calendar=20,
        calendar_ids=("primary",),
        calendar_labels={"primary": "calendar"},
        db_path=tmp_path / "calendar_alerts.sqlite3",
        source_name="calendar",
        source_type="calendar_alert",
        default_severity="high",
    )
    first = _to_event(
        {
            "id": "google-event-1",
            "summary": "First title",
            "updated": "2026-08-23T10:00:00Z",
            "start": {"dateTime": "2026-08-23T11:00:00Z"},
        },
        "primary",
        "calendar",
        config,
    )
    revised = _to_event(
        {
            "id": "google-event-1",
            "summary": "Revised title",
            "updated": "2026-08-23T10:05:00Z",
            "start": {"dateTime": "2026-08-23T11:30:00Z"},
        },
        "primary",
        "calendar",
        config,
    )

    assert first is not None and revised is not None
    assert first["event_id"] == revised["event_id"]
    assert first["revision"] != revised["revision"]
    assert first["content"] != revised["content"]
