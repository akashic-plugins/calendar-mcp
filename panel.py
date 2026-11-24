"""侧栏日历面板的数据层：90 天统一热缓存 + SWR 后台刷新，零卡顿支撑紧凑态、周/月/行动清单。"""

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
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from agent.plugin_composition import Context
from plugins.managed_processes.contract import ManagedProcessHandle

PALETTE_KEYS = ("sage", "sky", "lavender", "sand", "rose", "slate")
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

WINDOW_PAST_DAYS = 28
WINDOW_FUTURE_DAYS = 75
FRESH_TTL_SECONDS = 300.0  # 5 分钟内直接命中内存，0ms 返回


@dataclass
class _Snapshot:
    fetched_at: float
    window_start: date
    window_end: date
    calendars: list[dict[str, object]]
    events: list[dict[str, object]]


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _clip(text: str, limit: int) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[: max(1, limit - 1)] + "…"


def _friendly_calendar_name(raw_name: str, primary: bool) -> str:
    name = raw_name.strip()
    if not name:
        return "主日历" if primary else "未命名日历"
    if name.startswith(("http://", "https://")):
        parsed = urlparse(name)
        host = (parsed.hostname or "").lower()
        if "moodle" in host:
            return "CS Moodle" if "cs" in host else "Moodle"
        return host or name
    if primary and "@" in name:
        local_part = name.split("@", 1)[0]
        return f"主日历 ({local_part})" if len(local_part) <= 14 else "主日历"
    return name


def _color_key(calendar_id: str, color_id: str | None, primary: bool, fallback_index: int) -> str:
    if color_id and color_id in _GOOGLE_COLOR_MAP:
        return _GOOGLE_COLOR_MAP[color_id]
    if primary:
        return "sky"
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
        end_day = date.fromisoformat(end_date_str[:10]) if isinstance(end_date_str, str) else start_day + timedelta(days=1)
        if end_day <= start_day:
            end_day = start_day + timedelta(days=1)
        start_dt = datetime(start_day.year, start_day.month, start_day.day, tzinfo=local_tz)
        end_dt = datetime(end_day.year, end_day.month, end_day.day, tzinfo=local_tz)
        start_str = start_day.isoformat()
        end_str = end_day.isoformat()
    elif isinstance(start_dt_str, str):
        start_dt = _parse_iso(start_dt_str, local_tz)
        end_dt = _parse_iso(end_dt_str, local_tz) if isinstance(end_dt_str, str) else start_dt + timedelta(minutes=30)
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(minutes=15)
        start_str = start_dt.isoformat()
        end_str = end_dt.isoformat()
    else:
        return None

    event_color_id = raw.get("colorId") or raw.get("color_id")
    color = _GOOGLE_COLOR_MAP.get(str(event_color_id), cal_color) if event_color_id else cal_color

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
    upcoming_all: list[dict[str, object]] = []

    for ev in events:
        if ev["all_day"]:
            s_day = str(ev["start"])[:10]
            e_day = str(ev["end"])[:10]
            if s_day <= today_iso < e_day:
                today_all_day.append(ev)
                upcoming_all.append({**ev, "starts_in_minutes": 0})
            elif s_day > today_iso:
                delta_days = (date.fromisoformat(s_day) - now.date()).days
                upcoming_all.append({**ev, "starts_in_minutes": delta_days * 1440})
            continue

        s_ts = float(ev["start_ts"])
        e_ts = float(ev["end_ts"])
        ev_start_day = str(ev["start"])[:10]
        if ev_start_day == today_iso:
            today_timed.append(ev)

        if s_ts <= now_ts < e_ts:
            rem = max(1, int((e_ts - now_ts + 59) // 60))
            item = {**ev, "remaining_minutes": rem}
            current.append(item)
            upcoming_all.append(item)
        elif s_ts > now_ts:
            mins = max(1, int((s_ts - now_ts + 59) // 60))
            item = {**ev, "starts_in_minutes": mins}
            upcoming.append(item)
            upcoming_all.append(item)

    current.sort(key=lambda item: float(item["end_ts"]))
    upcoming.sort(key=lambda item: float(item["start_ts"]))
    today_timed.sort(key=lambda item: float(item["start_ts"]))
    upcoming_all.sort(key=lambda item: (str(item["start"])[:10], 0 if item["all_day"] else 1, float(item["start_ts"])))

    next_event = upcoming[0] if upcoming else None
    today_remaining = [ev for ev in today_timed if float(ev["end_ts"]) > now_ts]

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
    elif next_event:
        n_day = str(next_event["start"])[:10]
        hhmm = str(next_event["start"])[11:16]
        if n_day == tomorrow_iso:
            badge = {"text": f"明天 {hhmm} · {_clip(str(next_event['summary']), 9)}", "alert": False}
        else:
            badge = {"text": f"{n_day[5:10]} · {_clip(str(next_event['summary']), 11)}", "alert": False}
    else:
        badge = {"text": "近期空闲", "alert": False}

    return {
        "badge": badge,
        "current": current,
        "next": next_event,
        "today_all_day": today_all_day,
        "today": today_timed,
        "upcoming": upcoming_all[:40],
    }


class CalendarPanelService:
    """90 天单窗口热缓存 + SWR 后台刷新；同一份快照服务所有视图，切周/切月/切清单 0ms。"""

    def __init__(self, ctx: Context, process: ManagedProcessHandle) -> None:
        self._ctx = ctx
        self._process = process
        self._snapshot: _Snapshot | None = None
        self._inflight: asyncio.Task[_Snapshot] | None = None
        self._last_error: str = ""

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

    async def _fetch_snapshot(self, extra_start: date | None = None, extra_end: date | None = None) -> _Snapshot:
        now = _local_now()
        today = now.date()
        w_start = today - timedelta(days=WINDOW_PAST_DAYS)
        w_end = today + timedelta(days=WINDOW_FUTURE_DAYS)
        if extra_start and extra_start < w_start:
            w_start = extra_start - timedelta(days=7)
        if extra_end and extra_end > w_end:
            w_end = extra_end + timedelta(days=7)

        start_dt = datetime(w_start.year, w_start.month, w_start.day, tzinfo=now.tzinfo)
        end_dt = datetime(w_end.year, w_end.month, w_end.day, tzinfo=now.tzinfo)

        async with asyncio.timeout(25), self._process.borrow(self._ctx) as port:
            ready, reason = await self._check_ready(port)
            if not ready:
                raise PermissionError(reason)

            cal_payload = await asyncio.to_thread(_http_json, f"http://127.0.0.1:{port}/calendars", timeout=20.0)
            raw_cals = cal_payload.get("items")
            calendars: list[dict[str, object]] = []
            if isinstance(raw_cals, list):
                for idx, item in enumerate(raw_cals):
                    if not isinstance(item, Mapping):
                        continue
                    if item.get("deleted") is True or item.get("hidden") is True:
                        continue
                    cal_id = str(item.get("id") or "").strip()
                    if not cal_id:
                        continue
                    primary = bool(item.get("primary"))
                    raw_summary = str(item.get("summaryOverride") or item.get("summary") or cal_id).strip()
                    selected = item.get("selected")
                    color = _color_key(cal_id, str(item.get("colorId") or "") or None, primary, idx)
                    calendars.append({
                        "id": cal_id,
                        "name": _friendly_calendar_name(raw_summary, primary),
                        "raw_name": raw_summary,
                        "primary": primary,
                        "selected": True if selected is None else bool(selected),
                        "color": color,
                    })
            calendars.sort(key=lambda c: (0 if c["primary"] else 1, str(c["name"]).lower()))

            params = urlencode({
                "time_min": start_dt.isoformat(),
                "time_max": end_dt.isoformat(),
                "single_events": "true",
                "order_by": "startTime",
                "max_results": "500",
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
                        local_tz=now.tzinfo,
                    )
                    if ev is not None:
                        out.append(ev)
                return out

            nested = await asyncio.gather(*(fetch_one(cal) for cal in calendars))
            merged = [ev for group in nested for ev in group]
            merged.sort(key=lambda ev: (0 if ev["all_day"] else 1, float(ev["start_ts"]), str(ev["summary"])))

        snap = _Snapshot(
            fetched_at=time.monotonic(),
            window_start=w_start,
            window_end=w_end,
            calendars=calendars,
            events=merged,
        )
        self._snapshot = snap
        self._last_error = ""
        return snap

    def _trigger_background_refresh(self, extra_start: date | None = None, extra_end: date | None = None) -> None:
        if self._inflight is not None and not self._inflight.done():
            return
        task = asyncio.create_task(self._fetch_snapshot(extra_start, extra_end))
        self._inflight = task

        def _done(t: asyncio.Task[_Snapshot]) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                self._last_error = str(exc)

        task.add_done_callback(_done)

    async def _ensure_snapshot(
        self,
        *,
        refresh: bool = False,
        need_start: date | None = None,
        need_end: date | None = None,
    ) -> _Snapshot:
        snap = self._snapshot
        now_mono = time.monotonic()

        covers = (
            snap is not None
            and (need_start is None or snap.window_start <= need_start)
            and (need_end is None or snap.window_end >= need_end)
        )

        if not refresh and snap is not None and covers:
            if now_mono - snap.fetched_at > FRESH_TTL_SECONDS:
                self._trigger_background_refresh(need_start, need_end)
            return snap

        # 需要强制刷新或首次加载/超出当前缓存窗口
        if self._inflight is not None and not self._inflight.done() and not refresh:
            return await self._inflight

        task = asyncio.create_task(self._fetch_snapshot(need_start, need_end))
        self._inflight = task
        return await task

    def _pack_response(self, snap: _Snapshot, *, include_events: bool) -> dict[str, object]:
        now = _local_now()
        selected_ids = {str(c["id"]) for c in snap.calendars if c["selected"]} or {str(c["id"]) for c in snap.calendars}
        action_events = [ev for ev in snap.events if str(ev["calendar_id"]) in selected_ids]
        action = _compute_action_state(action_events, now)

        res: dict[str, object] = {
            "ready": True,
            "reason": "",
            "now": now.isoformat(),
            "today_date": now.date().isoformat(),
            "timezone": str(now.tzinfo or "Local"),
            "window": {"start": snap.window_start.isoformat(), "end": snap.window_end.isoformat()},
            "calendars": snap.calendars,
            **action,
        }
        if include_events:
            res["events"] = snap.events
        return res

    async def summary(self, *, refresh: bool = False) -> dict[str, object]:
        now = _local_now()
        try:
            snap = await self._ensure_snapshot(refresh=refresh)
        except PermissionError as err:
            return {"ready": False, "reason": str(err), "now": now.isoformat(), "badge": None}
        except Exception as err:
            return {"ready": False, "reason": f"暂时无法读取日程：{err}", "now": now.isoformat(), "badge": None}
        return self._pack_response(snap, include_events=False)

    async def snapshot(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        refresh: bool = False,
    ) -> dict[str, object]:
        now = _local_now()
        need_start = date.fromisoformat(start[:10]) if isinstance(start, str) and len(start) >= 10 else None
        need_end = date.fromisoformat(end[:10]) if isinstance(end, str) and len(end) >= 10 else None
        try:
            snap = await self._ensure_snapshot(refresh=refresh, need_start=need_start, need_end=need_end)
        except PermissionError as err:
            return {"ready": False, "reason": str(err), "now": now.isoformat()}
        except Exception as err:
            return {"ready": False, "reason": f"暂时无法读取日程：{err}", "now": now.isoformat()}
        return self._pack_response(snap, include_events=True)
