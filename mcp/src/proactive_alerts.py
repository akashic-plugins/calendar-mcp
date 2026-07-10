"""
calendar proactive alerts

为 calendar-mcp 提供 proactive alert 轮询与 ack 存储。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import requests
from src.auth import get_credentials
import src.calendar_actions as calendar_actions

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = {
    "enabled": True,
    "lookahead_hours": 6,
    "max_results_per_calendar": 20,
    "calendar_ids": ["primary"],
    "calendar_labels": {"primary": "calendar"},
    "db_path": "./calendar_alerts.sqlite3",
    "source_name": "calendar",
    "source_type": "calendar_alert",
    "default_severity": "high",
}


@dataclass
class CalendarAlertConfig:
    enabled: bool
    lookahead_hours: int
    max_results_per_calendar: int
    calendar_ids: list[str]
    calendar_labels: dict[str, str]
    db_path: Path
    source_name: str
    source_type: str
    default_severity: str


def _config_path() -> Path:
    configured = os.environ.get("CALENDAR_PROACTIVE_CONFIG_PATH", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "proactive_alerts.json"


def load_config() -> CalendarAlertConfig:
    raw = dict(_DEFAULT_CONFIG)
    config_path = _config_path()
    if config_path.exists():
        raw.update(json.loads(config_path.read_text()))
    db_path = Path(str(raw["db_path"]))
    if not db_path.is_absolute():
        db_path = (config_path.parent / db_path).resolve()
    return CalendarAlertConfig(
        enabled=bool(raw["enabled"]),
        lookahead_hours=max(1, int(raw["lookahead_hours"])),
        max_results_per_calendar=max(1, int(raw["max_results_per_calendar"])),
        calendar_ids=[str(v).strip() for v in raw["calendar_ids"] if str(v).strip()],
        calendar_labels={str(k): str(v) for k, v in dict(raw["calendar_labels"]).items()},
        db_path=db_path,
        source_name=str(raw["source_name"]).strip() or "calendar",
        source_type=str(raw["source_type"]).strip() or "calendar_alert",
        default_severity=str(raw["default_severity"]).strip() or "high",
    )


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS acknowledged_alerts (
            event_id TEXT PRIMARY KEY,
            raw_event_id TEXT NOT NULL,
            calendar_id TEXT NOT NULL,
            starts_at TEXT,
            title TEXT,
            content TEXT,
            acked_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_alerts (
            event_id TEXT PRIMARY KEY,
            raw_event_id TEXT NOT NULL,
            calendar_id TEXT NOT NULL,
            starts_at TEXT,
            title TEXT,
            content TEXT,
            last_seen_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(acknowledged_alerts)").fetchall()
    }
    if "title" not in columns:
        conn.execute("ALTER TABLE acknowledged_alerts ADD COLUMN title TEXT")
    if "content" not in columns:
        conn.execute("ALTER TABLE acknowledged_alerts ADD COLUMN content TEXT")
    return conn


def _purge_expired(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute(
        "DELETE FROM acknowledged_alerts WHERE expires_at <= ?",
        (now.isoformat(),),
    )
    conn.execute(
        "DELETE FROM pending_alerts WHERE expires_at <= ?",
        (now.isoformat(),),
    )
    conn.commit()


def _is_acked(conn: sqlite3.Connection, event_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM acknowledged_alerts WHERE event_id = ? LIMIT 1",
        (event_id,),
    ).fetchone()
    return row is not None


def acknowledge_events(event_ids: list[str], config: CalendarAlertConfig) -> dict[str, list[str]]:
    now = datetime.now(UTC)
    acknowledged: list[str] = []
    failed: list[str] = []
    conn = _connect(config.db_path)
    try:
        _purge_expired(conn, now)
        for event_id in event_ids:
            try:
                meta = conn.execute(
                    """
                    SELECT raw_event_id, calendar_id, starts_at, title, content
                    FROM pending_alerts
                    WHERE event_id = ?
                    LIMIT 1
                    """,
                    (event_id,),
                ).fetchone()
                raw_event_id = meta[0] if meta else event_id
                calendar_id = meta[1] if meta else "unknown"
                starts_at = meta[2] if meta else None
                title = meta[3] if meta else None
                content = meta[4] if meta else None
                conn.execute(
                    """
                    INSERT INTO acknowledged_alerts (
                        event_id, raw_event_id, calendar_id, starts_at, title, content, acked_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        raw_event_id=excluded.raw_event_id,
                        calendar_id=excluded.calendar_id,
                        starts_at=excluded.starts_at,
                        title=excluded.title,
                        content=excluded.content,
                        acked_at=excluded.acked_at,
                        expires_at=excluded.expires_at
                    """,
                    (
                        event_id,
                        raw_event_id,
                        calendar_id,
                        starts_at,
                        title,
                        content,
                        now.isoformat(),
                        (now + timedelta(days=7)).isoformat(),
                    ),
                )
                conn.execute("DELETE FROM pending_alerts WHERE event_id = ?", (event_id,))
                acknowledged.append(event_id)
            except Exception:
                logger.exception("calendar proactive ack failed: %s", event_id)
                failed.append(event_id)
        conn.commit()
    finally:
        conn.close()
    return {"acknowledged": acknowledged, "failed": failed}


def _parse_event_start(raw_event: dict[str, Any]) -> datetime | None:
    start = raw_event.get("start") or {}
    date_time = start.get("dateTime")
    all_day = start.get("date")
    if date_time:
        dt = datetime.fromisoformat(str(date_time).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    if all_day:
        d = date.fromisoformat(str(all_day))
        return datetime.combine(d, time.min, tzinfo=UTC)
    return None


def _event_key(calendar_id: str, raw_event: dict[str, Any], start_at: datetime | None) -> str:
    raw = "|".join(
        [
            calendar_id,
            str(raw_event.get("id", "")).strip(),
            start_at.isoformat() if start_at else "",
            str(raw_event.get("updated", "")).strip(),
        ]
    )
    digest = hashlib.sha1(raw.encode()).hexdigest()[:16]
    return f"calalert_{digest}"


def _format_content(summary: str, start_at: datetime | None, calendar_label: str) -> str:
    when = start_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if start_at else "未知时间"
    return f"{calendar_label} 有一条日程提醒：{summary}，开始时间 {when}。"


def _to_proactive_event(
    raw_event: dict[str, Any],
    calendar_id: str,
    calendar_label: str,
    config: CalendarAlertConfig,
) -> dict[str, Any] | None:
    # 1. 先过滤取消事件和无开始时间事件，避免把无效日程送进 proactive。
    # 2. 再生成稳定 event_id，确保 ack 后不会重复出现同一版本。
    # 3. 最后转换成项目里的标准 alert schema。
    if str(raw_event.get("status", "")).lower() == "cancelled":
        return None
    start_at = _parse_event_start(raw_event)
    if start_at is None:
        return None
    summary = str(raw_event.get("summary", "")).strip() or "未命名日程"
    event_id = _event_key(calendar_id, raw_event, start_at)
    return {
        "event_id": event_id,
        "kind": "alert",
        "source_type": config.source_type,
        "source_name": config.source_name,
        "title": summary,
        "content": _format_content(summary, start_at, calendar_label),
        "url": raw_event.get("htmlLink"),
        "published_at": start_at.isoformat(),
        "severity": config.default_severity,
        "metrics": {
            "calendar_id": calendar_id,
            "raw_event_id": raw_event.get("id"),
        },
    }


def _fetch_calendar_events(
    calendar_id: str,
    config: CalendarAlertConfig,
    now: datetime,
) -> list[dict[str, Any]]:
    time_max = now + timedelta(hours=config.lookahead_hours)
    creds = get_credentials()
    if creds is None:
        raise RuntimeError("Google Calendar credentials unavailable")
    response = calendar_actions.find_events(
        credentials=creds,
        calendar_id=calendar_id,
        time_min=now,
        time_max=time_max,
        max_results=config.max_results_per_calendar,
    )
    if response is None:
        raise RuntimeError(f"find_events returned None for calendar {calendar_id}")
    items = response.items or []
    return [item.model_dump(by_alias=True) for item in items]


def fetch_proactive_events() -> list[dict[str, Any]]:
    config = load_config()
    if not config.enabled or not config.calendar_ids:
        return []
    now = datetime.now(UTC)
    result: list[dict[str, Any]] = []
    conn = _connect(config.db_path)
    try:
        _purge_expired(conn, now)
        for calendar_id in config.calendar_ids:
            calendar_label = config.calendar_labels.get(calendar_id, calendar_id)
            for raw_event in _fetch_calendar_events(calendar_id, config, now):
                event = _to_proactive_event(raw_event, calendar_id, calendar_label, config)
                if event is None or _is_acked(conn, event["event_id"]):
                    continue
                conn.execute(
                    """
                    INSERT INTO pending_alerts (
                        event_id, raw_event_id, calendar_id, starts_at, title, content, last_seen_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        raw_event_id=excluded.raw_event_id,
                        calendar_id=excluded.calendar_id,
                        starts_at=excluded.starts_at,
                        title=excluded.title,
                        content=excluded.content,
                        last_seen_at=excluded.last_seen_at,
                        expires_at=excluded.expires_at
                    """,
                    (
                        event["event_id"],
                        (event.get("metrics") or {}).get("raw_event_id"),
                        (event.get("metrics") or {}).get("calendar_id"),
                        event.get("published_at"),
                        event.get("title"),
                        event.get("content"),
                        now.isoformat(),
                        (now + timedelta(hours=config.lookahead_hours + 24)).isoformat(),
                    ),
                )
                result.append(event)
        conn.commit()
    finally:
        conn.close()
    return result
