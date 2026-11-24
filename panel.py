"""侧栏日历面板的数据层：经托管日历服务读取日程，汇总「此刻/下一步」与周视图网格。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from agent.plugin_composition import Context
from plugins.managed_processes.contract import ManagedProcessHandle

PALETTE_KEYS = ("sage", "sky", "lavender", "sand", "rose", "slate")
# Google Calendar event/calendar colorId 到柔和粉彩色系的稳定映射。
_GOOGLE_COLOR_MAP: dict[str, str] = {
    "1": "lavender",
    "2": "sage",
    "3": "lavender",
    "4": "rose",
    "5": "sand",
    "6": "sand",
    "7": "sky",
    "8": "slate",
    "9": "sky",
    "10": "sage",
    "11": "rose",
    "12": "sage",
    "13": "sage",
    "14": "sky",
    "15": "sky",
    "16": "lavender",
    "17": "lavender",
    "18": "rose",
    "19": "sand",
    "20": "sand",
    "21": "slate",
    "22": "rose",
    "23": "lavender",
    "24": "slate",
}


@dataclass
class _CacheEntry:
    expires_at: float
    value: object


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _clip(text: str, limit: int) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[: max(1, limit - 1)] + "…"


def _color_key(calendar_id: str, color_id: str | None, primary: bool, fallback_index: int) -> str:
    if color_id and color_id in _GOOGLE_COLOR_MAP:
        return _GOOGLE_COLOR_MAP[color_id]
    if primary:
        return "sage"
    digest = hashlib.sha1(calendar_id.encode("utf-8")).digest()[0]
    return PALETTE_KEYS[(digest + fallback_index) % len(PALETTE_KEYS)]


def _http_json(url: str, *, timeout: float = 20.0) -> Mapping[str, object]:
    request = Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read(2048).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {detail}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise RuntimeError(str(error)) from error
    if not isinstance(payload, dict):
        raise RuntimeError("日历服务返回格式异常")
    return cast(Mapping[str, object], payload)


def _parse_iso(value: str, local_tz) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(local_tz)


def _normalize_event(
    raw: Mapping[str, object],
    *,
    cal_id: str,
    cal_name: str,
    cal_color: str,
    local_tz,
) -> dict[str, object] | None:
    if raw.get("status") == "cancelled":
        return None
    start_obj = raw.get("start")
    end_obj = raw.get("end")
    if not isinstance(start_obj, Mapping):
        return None

    start_date_str = start_obj.get("date")
    start_dt_str = start_obj.get("dateTime")
    end_date_str = end_obj.get("date") if isinstance(end_obj, Mapping) else None
    end_dt_str = end_obj.get("dateTime") if isinstance(end_obj, Mapping) else None

    all_day = False
    if isinstance(start_date_str, str) and not start_dt_str:
        all_day = True
        start_day = date.fromisoformat(start_date_str[:10])
        if isinstance(end_date_str, str):
            end_day = date.fromisoformat(end_date_str[:10])
        else:
            end_day = start_day + timedelta(days=1)
        if end_day <= start_day:
            end_day = start_day + timedelta(days=1)
        start_dt = datetime(start_day.year, start_day.month, start_day.day, tzinfo=local_tz)
        end_dt = datetime(end_day.year, end_day.month, end_day.day, tzinfo=local_tz)
        start_str = start_day.isoformat()
        end_str = end_day.isoformat()
    elif isinstance(start_dt_str, str):
        start_dt = _parse_iso(start_dt_str, local_tz)
        if isinstance(end_dt_str, str):
            end_dt = _parse_iso(end_dt_str, local_tz)
        else:
            end_dt = start_dt + timedelta(minutes=30)
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(minutes=15)
        start_str = start_dt.isoformat()
        end_str = end_dt.isoformat()
    else:
        return None

    event_color_id = raw.get("colorId") or raw.get("color_id")
    color = _GOOGLE_COLOR_MAP.get(str(event_color_id), cal_color) if event_color_id else cal_color

    # 检查受邀回复状态：未回复/待定走虚线样式
    response_status = ""
    attendees = raw.get("attendees")
    if isinstance(attendees, list):
        for person in attendees:
            if isinstance(person, Mapping) and person.get("self"):
                rs = person.get("responseStatus") or person.get("response_status")
                if isinstance(rs, str):
                    response_status = rs
                break

    tentative = raw.get("status") == "tentative" or response_status in ("needsAction", "tentative")
    summary = str(raw.get("summary") or "（无标题日程）").strip() or "（无标题日程）"
    location = str(raw.get("location") or "").strip()
    description = str(raw.get("description") or "").strip()
    html_link = str(raw.get("htmlLink") or raw.get("html_link") or "").strip()

    return {
        "id": str(raw.get("id") or f"{cal_id}:{start_str}:{summary}"),
        "calendar_id": cal_id,
        "calendar_name": cal_name,
        "color": color,
        "summary": summary,
        "location": location,
        "description": description[:1500],
        "html_link": html_link,
        "all_day": all_day,
        "start": start_str,
        "end": end_str,
        "start_ts": start_dt.timestamp(),
        "end_ts": end_dt.timestamp(),
        "tentative": tentative,
        "status": str(raw.get("status") or "confirmed"),
    }


def _compute_action_state(events: list[dict[str, object]], now: datetime) -> dict[str, object]:
    now_ts = now.timestamp()
    today_iso = now.date().isoformat()
    tomorrow_iso = (now.date() + timedelta(days=1)).isoformat()

    current: list[dict[str, object]] = []
    upcoming: list[dict[str, object]] = []
    today_all_day: list[dict[str, object]] = []
    today_timed: list[dict[str, object]] = []
    tomorrow_events: list[dict[str, object]] = []

    for ev in events:
        if ev["all_day"]:
            s_day = str(ev["start"])[:10]
            e_day = str(ev["end"])[:10]
            if s_day <= today_iso < e_day:
                today_all_day.append(ev)
            if s_day <= tomorrow_iso < e_day:
                tomorrow_events.append(ev)
            continue

        s_ts = float(ev["start_ts"])
        e_ts = float(ev["end_ts"])
        ev_start_day = str(ev["start"])[:10]
        if ev_start_day == today_iso:
            today_timed.append(ev)
        elif ev_start_day == tomorrow_iso:
            tomorrow_events.append(ev)

        if s_ts <= now_ts < e_ts:
            rem = max(1, int((e_ts - now_ts + 59) // 60))
            current.append({**ev, "remaining_minutes": rem})
        elif s_ts > now_ts:
            mins = max(1, int((s_ts - now_ts + 59) // 60))
            upcoming.append({**ev, "starts_in_minutes": mins})

    current.sort(key=lambda item: float(item["end_ts"]))
    upcoming.sort(key=lambda item: float(item["start_ts"]))
    today_timed.sort(key=lambda item: float(item["start_ts"]))
    tomorrow_events.sort(key=lambda item: (0 if item["all_day"] else 1, float(item["start_ts"])))

    next_event = upcoming[0] if upcoming else None
    today_remaining = [ev for ev in today_timed if float(ev["end_ts"]) > now_ts]

    # 生成侧栏状态栏的一句话摘要与是否亮提醒点
    badge: dict[str, object]
    if current:
        top = current[0]
        badge = {"text": f"进行中 · {_clip(str(top['summary']), 12)}", "alert": True}
    elif next_event and int(next_event["starts_in_minutes"]) <= 60:
        mins = int(next_event["starts_in_minutes"])
        badge = {"text": f"{mins} 分钟后 · {_clip(str(next_event['summary']), 10)}", "alert": True}
    elif today_remaining:
        if len(today_remaining) == 1:
            only = today_remaining[0]
            hhmm = str(only["start"])[11:16]
            badge = {"text": f"今天 {hhmm} · {_clip(str(only['summary']), 10)}", "alert": False}
        else:
            badge = {"text": f"今天还有 {len(today_remaining)} 项", "alert": False}
    elif today_all_day:
        badge = {"text": f"今日 · {_clip(str(today_all_day[0]['summary']), 12)}", "alert": False}
    elif tomorrow_events:
        first = tomorrow_events[0]
        if first["all_day"]:
            badge = {"text": f"明天 · {_clip(str(first['summary']), 11)}", "alert": False}
        else:
            hhmm = str(first["start"])[11:16]
            badge = {"text": f"明天 {hhmm} · {_clip(str(first['summary']), 9)}", "alert": False}
    elif next_event:
        mmdd = str(next_event["start"])[5:10]
        badge = {"text": f"{mmdd} · {_clip(str(next_event['summary']), 11)}", "alert": False}
    else:
        badge = {"text": "近期空闲", "alert": False}

    return {
        "badge": badge,
        "current": current,
        "next": next_event,
        "today_all_day": today_all_day,
        "today": today_timed,
        "tomorrow": tomorrow_events[:5],
    }


def _expand_busy_days(events: list[dict[str, object]], month_prefix: str) -> list[str]:
    days: set[str] = set()
    for ev in events:
        if ev["all_day"]:
            cur = date.fromisoformat(str(ev["start"])[:10])
            end = date.fromisoformat(str(ev["end"])[:10])
            steps = 0
            while cur < end and steps < 62:
                iso = cur.isoformat()
                if not month_prefix or iso.startswith(month_prefix):
                    days.add(iso)
                cur += timedelta(days=1)
                steps += 1
        else:
            iso = str(ev["start"])[:10]
            if not month_prefix or iso.startswith(month_prefix):
                days.add(iso)
    return sorted(days)


class CalendarPanelService:
    """面向 sidebar.panels 的只读聚合服务，带短缓存与优雅降级。"""

    def __init__(self, ctx: Context, process: ManagedProcessHandle) -> None:
        self._ctx = ctx
        self._process = process
        self._cache: dict[str, _CacheEntry] = {}

    def _get_cached(self, key: str) -> object | None:
        entry = self._cache.get(key)
        if entry is None or time.monotonic() >= entry.expires_at:
            return None
        return entry.value

    def _set_cached(self, key: str, value: object, ttl: float) -> None:
        self._cache[key] = _CacheEntry(time.monotonic() + ttl, value)

    async def _check_ready(self, port: int) -> tuple[bool, str]:
        try:
            health = await asyncio.to_thread(_http_json, f"http://127.0.0.1:{port}/health", timeout=6.0)
        except Exception:
            return False, "日历服务正在启动，稍后再试"
        if not health.get("client_configured"):
            return False, "还没有配置 Google 日历 OAuth 客户端，请在插件设置里填写"
        if health.get("authentication") != "authenticated" and not health.get("token_saved"):
            return False, "还没有完成 Google 日历授权，请在插件设置里授权"
        return True, ""

    async def _list_calendars(self, port: int, *, refresh: bool = False) -> list[dict[str, object]]:
        if not refresh:
            cached = self._get_cached("calendars")
            if isinstance(cached, list):
                return cast(list[dict[str, object]], cached)

        payload = await asyncio.to_thread(_http_json, f"http://127.0.0.1:{port}/calendars", timeout=20.0)
        raw_items = payload.get("items")
        calendars: list[dict[str, object]] = []
        if isinstance(raw_items, list):
            for idx, item in enumerate(raw_items):
                if not isinstance(item, Mapping):
                    continue
                if item.get("deleted") is True or item.get("hidden") is True:
                    continue
                cal_id = str(item.get("id") or "").strip()
                if not cal_id:
                    continue
                primary = bool(item.get("primary"))
                summary = str(item.get("summaryOverride") or item.get("summary") or cal_id).strip()
                selected = item.get("selected")
                color = _color_key(cal_id, str(item.get("colorId") or "") or None, primary, idx)
                calendars.append({
                    "id": cal_id,
                    "name": summary,
                    "primary": primary,
                    "selected": True if selected is None else bool(selected),
                    "color": color,
                    "access_role": str(item.get("accessRole") or ""),
                })
        calendars.sort(key=lambda c: (0 if c["primary"] else 1, str(c["name"]).lower()))
        self._set_cached("calendars", calendars, ttl=120.0)
        return calendars

    async def _fetch_range_events(
        self,
        port: int,
        calendars: list[dict[str, object]],
        start_dt: datetime,
        end_dt: datetime,
        *,
        refresh: bool = False,
    ) -> list[dict[str, object]]:
        cache_key = f"events:{start_dt.isoformat()}:{end_dt.isoformat()}"
        if not refresh:
            cached = self._get_cached(cache_key)
            if isinstance(cached, list):
                return cast(list[dict[str, object]], cached)

        local_tz = start_dt.tzinfo
        params = urlencode({
            "time_min": start_dt.isoformat(),
            "time_max": end_dt.isoformat(),
            "single_events": "true",
            "order_by": "startTime",
            "max_results": "250",
        })

        async def fetch_one(cal: dict[str, object]) -> list[dict[str, object]]:
            cal_id = str(cal["id"])
            url = f"http://127.0.0.1:{port}/calendars/{quote(cal_id, safe='')}/events?{params}"
            try:
                payload = await asyncio.to_thread(_http_json, url, timeout=20.0)
            except Exception:
                return []
            items = payload.get("items")
            if not isinstance(items, list):
                return []
            out: list[dict[str, object]] = []
            for raw in items:
                if not isinstance(raw, Mapping):
                    continue
                ev = _normalize_event(
                    raw,
                    cal_id=cal_id,
                    cal_name=str(cal["name"]),
                    cal_color=str(cal["color"]),
                    local_tz=local_tz,
                )
                if ev is not None:
                    out.append(ev)
            return out

        nested = await asyncio.gather(*(fetch_one(cal) for cal in calendars))
        merged = [ev for group in nested for ev in group]
        merged.sort(key=lambda ev: (0 if ev["all_day"] else 1, float(ev["start_ts"]), str(ev["summary"])))
        self._set_cached(cache_key, merged, ttl=30.0)
        return merged

    async def summary(self, *, refresh: bool = False) -> dict[str, object]:
        now = _local_now()
        try:
            async with asyncio.timeout(25), self._process.borrow(self._ctx) as port:
                ready, reason = await self._check_ready(port)
                if not ready:
                    return {"ready": False, "reason": reason, "now": now.isoformat(), "badge": None}
                calendars = await self._list_calendars(port, refresh=refresh)
                selected_cals = [c for c in calendars if c["selected"]] or calendars
                day_start = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
                range_start = day_start - timedelta(days=1)
                range_end = day_start + timedelta(days=7)
                events = await self._fetch_range_events(port, selected_cals, range_start, range_end, refresh=refresh)
        except Exception as error:
            return {"ready": False, "reason": f"暂时无法读取日程：{error}", "now": now.isoformat(), "badge": None}

        action = _compute_action_state(events, now)
        return {
            "ready": True,
            "reason": "",
            "now": now.isoformat(),
            "today_date": now.date().isoformat(),
            "timezone": str(now.tzinfo or "Local"),
            "calendars": calendars,
            **action,
        }

    async def range_view(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        month: str | None = None,
        refresh: bool = False,
    ) -> dict[str, object]:
        now = _local_now()
        local_tz = now.tzinfo

        if start and end:
            s_day = date.fromisoformat(start[:10])
            e_day = date.fromisoformat(end[:10])
        else:
            # 默认本周一到下周一（7 天）
            monday = now.date() - timedelta(days=now.weekday())
            s_day = monday
            e_day = monday + timedelta(days=7)

        if (e_day - s_day).days > 45 or e_day <= s_day:
            e_day = s_day + timedelta(days=7)

        # 同时覆盖当前月份用于迷你月历标点 + 覆盖未来 7 天用于左侧「此刻/下一步」行动卡
        month_prefix = month[:7] if isinstance(month, str) and len(month) >= 7 else s_day.isoformat()[:7]
        m_year, m_mon = int(month_prefix[:4]), int(month_prefix[5:7])
        month_first = date(m_year, m_mon, 1)
        month_next = date(m_year + (1 if m_mon == 12 else 0), 1 if m_mon == 12 else m_mon + 1, 1)

        today_day = now.date()
        fetch_start_day = min(s_day, month_first, today_day - timedelta(days=1))
        fetch_end_day = max(e_day, month_next, today_day + timedelta(days=7))
        fetch_start = datetime(fetch_start_day.year, fetch_start_day.month, fetch_start_day.day, tzinfo=local_tz)
        fetch_end = datetime(fetch_end_day.year, fetch_end_day.month, fetch_end_day.day, tzinfo=local_tz)

        try:
            async with asyncio.timeout(25), self._process.borrow(self._ctx) as port:
                ready, reason = await self._check_ready(port)
                if not ready:
                    return {"ready": False, "reason": reason, "now": now.isoformat()}
                calendars = await self._list_calendars(port, refresh=refresh)
                all_events = await self._fetch_range_events(port, calendars, fetch_start, fetch_end, refresh=refresh)
        except Exception as error:
            return {"ready": False, "reason": f"暂时无法读取日程：{error}", "now": now.isoformat()}

        s_ts = datetime(s_day.year, s_day.month, s_day.day, tzinfo=local_tz).timestamp()
        e_ts = datetime(e_day.year, e_day.month, e_day.day, tzinfo=local_tz).timestamp()
        visible_events = [ev for ev in all_events if float(ev["end_ts"]) > s_ts and float(ev["start_ts"]) < e_ts]

        selected_ids = {str(c["id"]) for c in calendars if c["selected"]} or {str(c["id"]) for c in calendars}
        action_events = [ev for ev in all_events if str(ev["calendar_id"]) in selected_ids]
        action = _compute_action_state(action_events, now)
        busy_days = _expand_busy_days(action_events, month_prefix)

        return {
            "ready": True,
            "reason": "",
            "now": now.isoformat(),
            "today_date": now.date().isoformat(),
            "timezone": str(now.tzinfo or "Local"),
            "range": {"start": s_day.isoformat(), "end": e_day.isoformat()},
            "calendars": calendars,
            "events": visible_events,
            "busy_days": busy_days,
            **action,
        }
