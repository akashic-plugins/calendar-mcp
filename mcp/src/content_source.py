"""Calendar-owned durable Content polling and acknowledgement boundary."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Generator, cast

_SCHEMA_VERSION = 1
_DEFAULT_CONFIG = {
    "lookahead_hours": 6,
    "max_results_per_calendar": 20,
    "calendar_ids": ["primary"],
    "calendar_labels": {"primary": "calendar"},
    "db_path": "./calendar_alerts.sqlite3",
    "source_name": "calendar",
    "source_type": "calendar_alert",
    "default_severity": "high",
}


@dataclass(frozen=True, slots=True)
class CalendarContentConfig:
    lookahead_hours: int
    max_results_per_calendar: int
    calendar_ids: tuple[str, ...]
    calendar_labels: dict[str, str]
    db_path: Path
    source_name: str
    source_type: str
    default_severity: str


def _config_path() -> Path:
    configured = os.environ.get("CALENDAR_CONTENT_CONFIG_PATH", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "content.json"


def load_config() -> CalendarContentConfig:
    raw = dict(_DEFAULT_CONFIG)
    config_path = _config_path()
    if config_path.exists():
        raw.update(json.loads(config_path.read_text(encoding="utf-8")))
    db_path = Path(str(raw["db_path"]))
    if not db_path.is_absolute():
        db_path = (config_path.parent / db_path).resolve()
    return CalendarContentConfig(
        lookahead_hours=max(1, int(raw["lookahead_hours"])),
        max_results_per_calendar=max(1, int(raw["max_results_per_calendar"])),
        calendar_ids=tuple(
            str(value).strip()
            for value in raw["calendar_ids"]
            if str(value).strip()
        ),
        calendar_labels={
            str(key): str(value)
            for key, value in dict(raw["calendar_labels"]).items()
        },
        db_path=db_path,
        source_name=str(raw["source_name"]).strip() or "calendar",
        source_type=str(raw["source_type"]).strip() or "calendar_alert",
        default_severity=str(raw["default_severity"]).strip() or "high",
    )


class CalendarContentStore:
    """Own frozen poll batches, source cursor, and provider acknowledgement facts."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        """Migrate the legacy alert database once, then verify the complete schema."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        with self._connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            if existed:
                backup = self.path.with_suffix(self.path.suffix + ".pre-content-v1.bak")
                if not backup.exists():
                    shutil.copy2(self.path, backup)
            self._migrate_v0()
        elif version != _SCHEMA_VERSION:
            raise RuntimeError(f"calendar Content schema version 不支持: {version}")
        self._verify_schema()

    def active_batch(self) -> dict[str, object] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT batch_id, cursor, next_cursor, items_json
                FROM poll_batches WHERE status = 'pending' LIMIT 1
                """
            ).fetchone()
            return _batch_row(row) if row is not None else None

    def freeze(self, events: list[dict[str, object]], now: datetime) -> dict[str, object]:
        """Freeze one immutable batch without advancing the source cursor."""

        now_text = _utc(now)
        with self._transaction() as connection:
            active = connection.execute(
                """
                SELECT batch_id, cursor, next_cursor, items_json
                FROM poll_batches WHERE status = 'pending' LIMIT 1
                """
            ).fetchone()
            if active is not None:
                return _batch_row(active)

            self._purge_expired(connection, now_text)
            for event in events:
                self._upsert_pending(connection, event, now)
            cursor = int(
                connection.execute(
                    "SELECT cursor FROM source_state WHERE singleton = 1"
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT event_id, revision, starts_at, title, content,
                       raw_event_id, calendar_id
                FROM pending_alerts
                WHERE submitted_batch_id IS NULL
                ORDER BY event_id
                """
            ).fetchall()
            items = [_content_item(row) for row in rows]
            fingerprint = json.dumps(items, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(f"{cursor}\0{fingerprint}".encode()).hexdigest()[:20]
            batch_id = f"calendar:{cursor}:{digest}"
            next_cursor = cursor + 1
            connection.execute(
                """
                INSERT INTO poll_batches(
                    batch_id, cursor, next_cursor, items_json, status, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (batch_id, cursor, next_cursor, fingerprint, now_text),
            )
            return {
                "batch_id": batch_id,
                "cursor": cursor,
                "next_cursor": next_cursor,
                "items": items,
            }

    def commit(self, batch_id: str, now: datetime) -> dict[str, object]:
        """Advance the cursor exactly once after Core accepted the frozen batch."""

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT cursor, next_cursor, status FROM poll_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(batch_id)
            if row["status"] == "committed":
                return {"committed": True, "duplicate": True}
            changed = connection.execute(
                """
                UPDATE source_state SET cursor = ?
                WHERE singleton = 1 AND cursor = ?
                """,
                (row["next_cursor"], row["cursor"]),
            )
            if changed.rowcount != 1:
                raise RuntimeError("calendar Content cursor CAS failed")
            connection.execute(
                """
                UPDATE poll_batches SET status = 'committed', committed_at = ?
                WHERE batch_id = ? AND status = 'pending'
                """,
                (_utc(now), batch_id),
            )
            connection.execute(
                """
                UPDATE pending_alerts SET submitted_batch_id = ?
                WHERE submitted_batch_id IS NULL
                  AND event_id IN (
                    SELECT json_extract(value, '$.item_id')
                    FROM json_each((SELECT items_json FROM poll_batches WHERE batch_id = ?))
                  )
                """,
                (batch_id, batch_id),
            )
            return {"committed": True, "duplicate": False}

    def acknowledge(self, event_id: str, now: datetime) -> dict[str, object]:
        """Record provider acknowledgement idempotently and clear its pending fact."""

        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM acknowledged_alerts WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                return {"acknowledged": True, "duplicate": True}
            row = connection.execute(
                """
                SELECT raw_event_id, calendar_id, starts_at, title, content
                FROM pending_alerts WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            connection.execute(
                """
                INSERT INTO acknowledged_alerts(
                    event_id, raw_event_id, calendar_id, starts_at, title,
                    content, acked_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    row["raw_event_id"],
                    row["calendar_id"],
                    row["starts_at"],
                    row["title"],
                    row["content"],
                    _utc(now),
                    _utc(now + timedelta(days=7)),
                ),
            )
            connection.execute("DELETE FROM pending_alerts WHERE event_id = ?", (event_id,))
            return {"acknowledged": True, "duplicate": False}

    def _migrate_v0(self) -> None:
        with self._transaction() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            acknowledged = self._legacy_rows(connection, "acknowledged_alerts", tables)
            pending = self._legacy_rows(connection, "pending_alerts", tables)
            if "acknowledged_alerts" in tables:
                connection.execute(
                    "ALTER TABLE acknowledged_alerts RENAME TO legacy_acknowledged_alerts"
                )
            if "pending_alerts" in tables:
                connection.execute(
                    "ALTER TABLE pending_alerts RENAME TO legacy_pending_alerts"
                )
            connection.executescript(_V1_SCHEMA)
            for row in acknowledged:
                connection.execute(
                    """
                    INSERT INTO acknowledged_alerts VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["event_id"], row["raw_event_id"], row["calendar_id"],
                        row.get("starts_at"), row.get("title"), row.get("content"),
                        row["acked_at"], row["expires_at"],
                    ),
                )
            for row in pending:
                connection.execute(
                    """
                    INSERT INTO pending_alerts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["event_id"], row["raw_event_id"], row["calendar_id"],
                        row.get("starts_at"), row.get("title"), row.get("content"),
                        row["last_seen_at"], row["expires_at"],
                        row.get("revision", "1"), row.get("submitted_batch_id"),
                    ),
                )
            connection.execute("DROP TABLE IF EXISTS legacy_acknowledged_alerts")
            connection.execute("DROP TABLE IF EXISTS legacy_pending_alerts")
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @staticmethod
    def _legacy_rows(
        connection: sqlite3.Connection,
        table: str,
        tables: set[str],
    ) -> list[dict[str, object]]:
        if table not in tables:
            return []
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]

    def _verify_schema(self) -> None:
        with self._connection() as connection:
            actual = {
                (row[0], row[1]): _normalize_sql(row[2])
                for row in connection.execute(
                    """
                    SELECT type, name, sql FROM sqlite_master
                    WHERE type IN ('table', 'index') AND sql IS NOT NULL
                    """
                )
            }
        with sqlite3.connect(":memory:") as expected_connection:
            expected_connection.executescript(_V1_SCHEMA)
            expected = {
                (row[0], row[1]): _normalize_sql(row[2])
                for row in expected_connection.execute(
                    """
                    SELECT type, name, sql FROM sqlite_master
                    WHERE type IN ('table', 'index') AND sql IS NOT NULL
                    """
                )
            }
        if actual != expected:
            raise RuntimeError(
                "calendar Content schema 不匹配: "
                f"actual={sorted(actual)} expected={sorted(expected)}"
            )

    def _upsert_pending(
        self,
        connection: sqlite3.Connection,
        event: dict[str, object],
        now: datetime,
    ) -> None:
        event_id = str(event["event_id"])
        if connection.execute(
            "SELECT 1 FROM acknowledged_alerts WHERE event_id = ?", (event_id,)
        ).fetchone():
            return
        metrics = cast(dict[str, object], event["metrics"])
        connection.execute(
            """
            INSERT INTO pending_alerts(
                event_id, raw_event_id, calendar_id, starts_at, title, content,
                last_seen_at, expires_at, revision, submitted_batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(event_id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                expires_at = excluded.expires_at
            """,
            (
                event_id,
                metrics["raw_event_id"],
                metrics["calendar_id"],
                event["published_at"],
                event["title"],
                event["content"],
                _utc(now),
                _utc(now + timedelta(hours=load_config().lookahead_hours + 24)),
                event["revision"],
            ),
        )

    @staticmethod
    def _purge_expired(connection: sqlite3.Connection, now: str) -> None:
        connection.execute("DELETE FROM acknowledged_alerts WHERE expires_at <= ?", (now,))
        connection.execute(
            "DELETE FROM pending_alerts WHERE expires_at <= ? AND submitted_batch_id IS NULL",
            (now,),
        )

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection, None, None]:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise


def poll_content() -> dict[str, object]:
    """Return the active batch or freeze one fresh Google Calendar snapshot."""

    config = load_config()
    store = CalendarContentStore(config.db_path)
    store.initialize()
    active = store.active_batch()
    if active is not None:
        return active
    now = datetime.now(UTC)
    events: list[dict[str, object]] = []
    for calendar_id in config.calendar_ids:
        label = config.calendar_labels.get(calendar_id, calendar_id)
        events.extend(_fetch_events(calendar_id, label, config, now))
    return store.freeze(events, now)


def commit_content_batch(batch_id: str) -> dict[str, object]:
    config = load_config()
    store = CalendarContentStore(config.db_path)
    store.initialize()
    return store.commit(batch_id, datetime.now(UTC))


def acknowledge_content(event_id: str) -> dict[str, object]:
    config = load_config()
    store = CalendarContentStore(config.db_path)
    store.initialize()
    return store.acknowledge(event_id, datetime.now(UTC))


def _fetch_events(
    calendar_id: str,
    label: str,
    config: CalendarContentConfig,
    now: datetime,
) -> list[dict[str, object]]:
    from src.auth import get_credentials
    import src.calendar_actions as calendar_actions

    credentials = get_credentials()
    if credentials is None:
        raise RuntimeError("Google Calendar credentials unavailable")
    response = calendar_actions.find_events(
        credentials=credentials,
        calendar_id=calendar_id,
        time_min=now,
        time_max=now + timedelta(hours=config.lookahead_hours),
        max_results=config.max_results_per_calendar,
    )
    if response is None:
        raise RuntimeError(f"find_events returned None for calendar {calendar_id}")
    result: list[dict[str, object]] = []
    for model in response.items or []:
        event = _to_event(model.model_dump(by_alias=True), calendar_id, label, config)
        if event is not None:
            result.append(event)
    return result


def _to_event(
    raw: dict[str, Any],
    calendar_id: str,
    label: str,
    config: CalendarContentConfig,
) -> dict[str, object] | None:
    if str(raw.get("status", "")).lower() == "cancelled":
        return None
    starts_at = _parse_start(raw)
    if starts_at is None:
        return None
    summary = str(raw.get("summary", "")).strip() or "未命名日程"
    revision = str(raw.get("updated", "")).strip() or starts_at.isoformat()
    raw_id = str(raw.get("id", "")).strip()
    digest = hashlib.sha1(
        f"{calendar_id}|{raw_id}|{starts_at.isoformat()}|{revision}".encode()
    ).hexdigest()[:16]
    return {
        "event_id": f"calalert_{digest}",
        "revision": revision,
        "kind": "alert",
        "source_type": config.source_type,
        "source_name": config.source_name,
        "title": summary,
        "content": f"{label} 有一条日程提醒：{summary}，开始时间 {starts_at.astimezone(UTC):%Y-%m-%d %H:%M UTC}。",
        "url": raw.get("htmlLink"),
        "published_at": starts_at.isoformat(),
        "severity": config.default_severity,
        "metrics": {"calendar_id": calendar_id, "raw_event_id": raw_id},
    }


def _parse_start(raw: dict[str, Any]) -> datetime | None:
    start = raw.get("start") or {}
    if start.get("dateTime"):
        value = datetime.fromisoformat(str(start["dateTime"]).replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if start.get("date"):
        return datetime.combine(date.fromisoformat(str(start["date"])), time.min, tzinfo=UTC)
    return None


def _content_item(row: sqlite3.Row) -> dict[str, object]:
    return {
        "item_id": row["event_id"],
        "revision": row["revision"],
        "payload": {
            "kind": "alert",
            "source_type": "calendar_alert",
            "source_name": "calendar",
            "title": row["title"],
            "content": row["content"],
            "published_at": row["starts_at"],
            "metrics": {
                "calendar_id": row["calendar_id"],
                "raw_event_id": row["raw_event_id"],
            },
        },
        "not_before": row["starts_at"],
        "requires_ack": True,
    }


def _batch_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "batch_id": row["batch_id"],
        "cursor": int(row["cursor"]),
        "next_cursor": int(row["next_cursor"]),
        "items": json.loads(row["items_json"]),
    }


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("calendar Content 时间必须带时区")
    return value.astimezone(UTC).isoformat()


def _normalize_sql(value: str) -> str:
    return " ".join(value.lower().split())


_V1_SCHEMA = """
CREATE TABLE acknowledged_alerts(
    event_id TEXT PRIMARY KEY,
    raw_event_id TEXT NOT NULL,
    calendar_id TEXT NOT NULL,
    starts_at TEXT,
    title TEXT,
    content TEXT,
    acked_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE pending_alerts(
    event_id TEXT PRIMARY KEY,
    raw_event_id TEXT NOT NULL,
    calendar_id TEXT NOT NULL,
    starts_at TEXT,
    title TEXT,
    content TEXT,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revision TEXT NOT NULL,
    submitted_batch_id TEXT
);
CREATE TABLE source_state(
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    cursor INTEGER NOT NULL
);
INSERT OR IGNORE INTO source_state(singleton, cursor) VALUES(1, 0);
CREATE TABLE poll_batches(
    batch_id TEXT PRIMARY KEY,
    cursor INTEGER NOT NULL,
    next_cursor INTEGER NOT NULL,
    items_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'committed')),
    created_at TEXT NOT NULL,
    committed_at TEXT
);
CREATE UNIQUE INDEX one_pending_calendar_batch
ON poll_batches(status) WHERE status = 'pending';
CREATE INDEX pending_calendar_submission
ON pending_alerts(submitted_batch_id, event_id);
"""
