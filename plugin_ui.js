// 侧栏日历面板：Calendarcn 透亮极简视觉 + 90 天本地瞬切 + 本周/本月/清单三视图。

const ICON_SVG = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2.25" y="3.25" width="11.5" height="10.5" rx="2"/><path d="M5.25 1.75v2.5M10.75 1.75v2.5M2.25 6.5h11.5"/></svg>';
const DOW_ZH = ["一", "二", "三", "四", "五", "六", "日"];
const HOUR_H = 52;
const HIDDEN_CALS_KEY = "akashic.calendar.hidden-cals.v1";

// 模块级热缓存：重新打开或从紧凑切到展开时 0ms 瞬开，不白屏
let memSnapshot = null;

function h(tag, className, text, children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  for (const child of children ?? []) if (child) node.append(child);
  return node;
}

function btn(className, text, onClick, attrs = {}) {
  const node = h("button", className, text);
  node.type = "button";
  for (const [k, v] of Object.entries(attrs)) {
    if (v != null) node.setAttribute(k, String(v));
  }
  node.addEventListener("click", onClick);
  return node;
}

function pad2(n) { return String(n).padStart(2, "0"); }
function toIsoDate(d) { return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`; }

function parseIsoDate(iso) {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(y, m - 1, d);
}

function addDays(iso, delta) {
  const d = parseIsoDate(iso);
  d.setDate(d.getDate() + delta);
  return toIsoDate(d);
}

function mondayOf(iso) {
  const d = parseIsoDate(iso);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return toIsoDate(d);
}

function isoWeekNumber(iso) {
  const d = parseIsoDate(iso);
  const utc = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const dayNum = utc.getUTCDay() || 7;
  utc.setUTCDate(utc.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(utc.getUTCFullYear(), 0, 1));
  return Math.ceil((((utc - yearStart) / 86400000) + 1) / 7);
}

function parseLocalMinutes(isoStr) {
  const hh = Number(isoStr.slice(11, 13)) || 0;
  const mm = Number(isoStr.slice(14, 16)) || 0;
  return hh * 60 + mm;
}

function fmtMinutesAhead(mins) {
  if (mins <= 1) return "即将开始";
  if (mins < 60) return `${mins} 分钟后`;
  const hrs = Math.floor(mins / 60);
  const rem = mins % 60;
  if (hrs < 24) return rem ? `${hrs} 小时 ${rem} 分后` : `${hrs} 小时后`;
  const days = Math.floor(hrs / 24);
  const remH = hrs % 24;
  return remH ? `${days} 天 ${remH} 小时后` : `${days} 天后`;
}

function fmtRemaining(mins) {
  if (mins <= 1) return "即将结束";
  if (mins < 60) return `还剩 ${mins} 分钟`;
  const hrs = Math.floor(mins / 60);
  const rem = mins % 60;
  return rem ? `还剩 ${hrs} 小时 ${rem} 分` : `还剩 ${hrs} 小时`;
}

function fmtDayFriendly(iso, todayIso) {
  if (iso === todayIso) return "今天";
  if (iso === addDays(todayIso, 1)) return "明天";
  if (iso === addDays(todayIso, 2)) return "后天";
  const d = parseIsoDate(iso);
  const dow = DOW_ZH[(d.getDay() + 6) % 7];
  return `${d.getMonth() + 1}月${d.getDate()}日 周${dow}`;
}

function fmtTimeRange(ev) {
  if (ev.all_day) {
    const s = ev.start.slice(5, 10);
    const e = addDays(ev.end.slice(0, 10), -1).slice(5, 10);
    return s === e ? "全天" : `${s} – ${e} 全天`;
  }
  return `${ev.start.slice(11, 16)} – ${ev.end.slice(11, 16)}`;
}

function fmtFullTime(ev) {
  if (ev.all_day) {
    const s = ev.start.slice(0, 10);
    const e = addDays(ev.end.slice(0, 10), -1);
    return s === e ? `${s} · 全天` : `${s} 至 ${e} · 全天`;
  }
  const day = ev.start.slice(0, 10);
  const endDay = ev.end.slice(0, 10);
  const dow = DOW_ZH[(parseIsoDate(day).getDay() + 6) % 7];
  return day === endDay
    ? `${day} 周${dow}  ${ev.start.slice(11, 16)} – ${ev.end.slice(11, 16)}`
    : `${day} ${ev.start.slice(11, 16)} – ${endDay} ${ev.end.slice(11, 16)}`;
}

function colorClass(ev) {
  return `cal-c-${ev?.color || "sky"}`;
}

function readHiddenCalendars() {
  try {
    const raw = JSON.parse(localStorage.getItem(HIDDEN_CALS_KEY) || "[]");
    return new Set(Array.isArray(raw) ? raw.filter((v) => typeof v === "string") : []);
  } catch {
    return new Set();
  }
}

function writeHiddenCalendars(set) {
  try { localStorage.setItem(HIDDEN_CALS_KEY, JSON.stringify([...set])); } catch {}
}

// 统计每一天有哪些事件（返回 Map<iso, Event[]>），用于迷你月历多色圆点、列头提示与月视图
function indexEventsByDay(events) {
  const map = new Map();
  const push = (iso, ev) => {
    let arr = map.get(iso);
    if (!arr) { arr = []; map.set(iso, arr); }
    arr.push(ev);
  };
  for (const ev of events) {
    if (ev.all_day) {
      let cur = ev.start.slice(0, 10);
      const end = ev.end.slice(0, 10);
      for (let i = 0; i < 45 && cur < end; i++) {
        push(cur, ev);
        cur = addDays(cur, 1);
      }
    } else {
      const sDay = ev.start.slice(0, 10);
      const eDay = ev.end.slice(0, 10);
      push(sDay, ev);
      if (eDay > sDay && parseLocalMinutes(ev.end) > 0) push(eDay, ev);
    }
  }
  return map;
}

// 重叠事件精准分列
function layoutDayEvents(items) {
  const sorted = [...items].sort((a, b) => a.startMin - b.startMin || b.endMin - a.endMin);
  const placed = [];
  let cluster = [];
  let clusterEnd = -1;
  const flush = () => {
    if (!cluster.length) return;
    const laneEnds = [];
    for (const item of cluster) {
      let lane = laneEnds.findIndex((end) => end <= item.startMin);
      if (lane === -1) { lane = laneEnds.length; laneEnds.push(item.endMin); }
      else laneEnds[lane] = item.endMin;
      item.lane = lane;
    }
    const cols = laneEnds.length;
    for (const item of cluster) { item.cols = cols; placed.push(item); }
    cluster = [];
    clusterEnd = -1;
  };
  for (const item of sorted) {
    if (cluster.length && item.startMin >= clusterEnd) flush();
    cluster.push(item);
    clusterEnd = Math.max(clusterEnd, item.endMin);
  }
  flush();
  return placed;
}

function renderPopover(ev, onClose) {
  const backdrop = h("div", "cal-popover-backdrop");
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) onClose(); });
  const rows = [
    h("div", "cal-popover__row", null, [h("strong", null, "时间："), document.createTextNode(fmtFullTime(ev))]),
    ev.location ? h("div", "cal-popover__row", null, [h("strong", null, "地点："), document.createTextNode(ev.location)]) : null,
    ev.tentative ? h("div", "cal-popover__row", null, [h("strong", null, "状态："), document.createTextNode("待确认")]) : null,
  ];
  const card = h("div", `cal-popover ${colorClass(ev)}`, null, [
    h("div", "cal-popover__head", null, [
      h("div", null, null, [
        h("span", "cal-popover__badge", ev.calendar_name || "日程"),
        h("h3", "cal-popover__title", ev.summary),
      ]),
      btn("cal-btn cal-btn--ghost cal-btn--icon", "×", onClose, { "aria-label": "关闭" }),
    ]),
    h("div", "cal-popover__rows", null, rows),
    ev.description ? h("div", "cal-popover__desc", ev.description) : null,
    ev.html_link ? (() => {
      const a = h("a", "cal-popover__link", "在 Google 日历中打开");
      a.href = ev.html_link; a.target = "_blank"; a.rel = "noopener noreferrer";
      return a;
    })() : null,
  ]);
  backdrop.append(card);
  return backdrop;
}

// 紧凑态：顶部透亮主焦点 + 按天展示「接下来要做什么」
function renderCompact(data, panel, onRefresh, onSelectEvent) {
  const todayIso = data.today_date || toIsoDate(new Date());
  const live = data.current?.[0];
  const next = data.next;
  const allDay = data.today_all_day?.[0];

  let hero;
  if (live) {
    hero = h("div", `cal-hero is-live ${colorClass(live)}`, null, [
      h("div", "cal-hero__top", null, [
        h("span", "cal-hero__tag", null, [h("span", "cal-hero__dot"), h("span", null, `正在进行 · ${fmtRemaining(live.remaining_minutes)}`)]),
      ]),
      h("p", "cal-hero__title", live.summary),
      h("div", "cal-hero__meta", null, [h("span", null, fmtTimeRange(live)), live.location ? h("span", null, live.location) : null]),
    ]);
    hero.addEventListener("click", () => onSelectEvent(live));
  } else if (next) {
    hero = h("div", `cal-hero ${colorClass(next)}`, null, [
      h("div", "cal-hero__top", null, [
        h("span", "cal-hero__tag", null, [h("span", "cal-hero__dot"), h("span", null, `下一步 · ${fmtMinutesAhead(next.starts_in_minutes)}`)]),
      ]),
      h("p", "cal-hero__title", next.summary),
      h("div", "cal-hero__meta", null, [
        h("span", null, `${fmtDayFriendly(next.start.slice(0, 10), todayIso)} ${fmtTimeRange(next)}`),
        next.location ? h("span", null, next.location) : null,
      ]),
    ]);
    hero.addEventListener("click", () => onSelectEvent(next));
  } else if (allDay) {
    hero = h("div", `cal-hero ${colorClass(allDay)}`, null, [
      h("div", "cal-hero__top", null, [h("span", "cal-hero__tag", null, [h("span", "cal-hero__dot"), h("span", null, "今日全天")])]),
      h("p", "cal-hero__title", allDay.summary),
    ]);
    hero.addEventListener("click", () => onSelectEvent(allDay));
  } else {
    hero = h("div", "cal-hero is-idle", null, [
      h("div", "cal-hero__top", null, [h("span", "cal-hero__tag", null, [h("span", "cal-hero__dot"), h("span", null, "此刻状态")])]),
      h("p", "cal-hero__title", "近期没有紧迫日程"),
    ]);
  }

  // 把 upcoming 按天分组，最多展示 6 条，让人一眼看到未来几天要干嘛
  const upcoming = (data.upcoming || []).slice(0, 6);
  const groups = new Map();
  for (const ev of upcoming) {
    const day = ev.start.slice(0, 10);
    if (!groups.has(day)) groups.set(day, []);
    groups.get(day).push(ev);
  }

  const sections = [hero];
  for (const [day, list] of groups.entries()) {
    sections.push(h("div", "cal-group", null, [
      h("div", "cal-group__head", null, [h("span", null, fmtDayFriendly(day, todayIso)), h("span", null, `${list.length} 项`)]),
      ...list.map((ev) => {
        const node = h("button", `cal-row ${colorClass(ev)}${ev.tentative ? " is-tentative" : ""}`, null, [
          h("span", "cal-row__time", ev.all_day ? "全天" : ev.start.slice(11, 16)),
          h("span", null, null, [
            h("span", "cal-row__title", ev.summary),
            ev.location ? h("span", "cal-row__loc", ev.location) : null,
          ]),
        ]);
        node.type = "button";
        node.addEventListener("click", () => onSelectEvent(ev));
        return node;
      }),
    ]));
  }

  sections.push(h("div", "cal-compact__foot", null, [
    btn("cal-btn cal-btn--primary", "展开完整日历", () => panel.setSize("expanded")),
    btn("cal-btn cal-btn--icon", "↻", onRefresh, { "aria-label": "刷新", title: "刷新" }),
  ]));

  return h("div", "cal-compact cal-anim", null, sections);
}

// 左侧迷你月历（多色圆点标出哪些天有事情）
function renderMiniMonth(state, byDay, todayIso, actions) {
  const [y, m] = state.miniMonth.split("-").map(Number);
  const first = new Date(y, m - 1, 1);
  const startOffset = (first.getDay() + 6) % 7;
  const gridStart = new Date(y, m - 1, 1 - startOffset);
  const rangeStart = state.view === "week" ? state.weekStart : `${state.miniMonth}-01`;
  const rangeEnd = state.view === "week" ? addDays(state.weekStart, 7) : rangeStart;

  const cells = DOW_ZH.map((d) => h("div", "cal-mini__dow", d));
  for (let i = 0; i < 42; i++) {
    const cellDate = new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + i);
    const iso = toIsoDate(cellDate);
    const inMonth = cellDate.getMonth() === m - 1;
    const isToday = iso === todayIso;
    const inRange = state.view === "week" && iso >= rangeStart && iso < rangeEnd;
    const dayEvents = byDay.get(iso) || [];

    // 最多显示 3 个不同/对应的彩色小圆点，一眼看清哪天有事
    const dots = dayEvents.slice(0, 3).map((ev) => h("span", `cal-mini__dot ${colorClass(ev)}`));
    const cls = [
      "cal-mini__day",
      !inMonth ? "is-out" : "",
      inRange && !isToday ? "is-in-range" : "",
      dayEvents.length ? "has-events" : "",
      isToday ? "is-today" : "",
    ].filter(Boolean).join(" ");

    const title = dayEvents.length
      ? `${iso}（${dayEvents.length} 项）：${dayEvents.slice(0, 3).map((e) => e.summary).join("、")}`
      : iso;

    const cell = h("button", cls, null, [
      h("span", null, String(cellDate.getDate())),
      h("span", "cal-mini__dots", null, dots),
    ]);
    cell.type = "button";
    cell.title = title;
    cell.addEventListener("click", () => actions.jumpDate(iso));
    cells.push(cell);
  }

  return h("div", "cal-mini", null, [
    h("div", "cal-mini__head", null, [
      h("span", "cal-mini__title", `${y}年${m}月`),
      h("div", "cal-mini__nav", null, [
        btn("cal-btn cal-btn--ghost cal-btn--icon", "‹", () => actions.shiftMiniMonth(-1), { "aria-label": "上个月" }),
        btn("cal-btn cal-btn--ghost cal-btn--icon", "›", () => actions.shiftMiniMonth(1), { "aria-label": "下个月" }),
      ]),
    ]),
    h("div", "cal-mini__grid", null, cells),
  ]);
}

// 顶部极薄行动横幅
function renderActionBanner(data, todayIso, onSelectEvent) {
  const live = data.current?.[0];
  const next = data.next;
  if (live) {
    const node = h("button", `cal-banner is-live ${colorClass(live)}`, null, [
      h("span", "cal-banner__left", null, [
        h("span", "cal-banner__pill", `进行中 · ${fmtRemaining(live.remaining_minutes)}`),
        h("span", "cal-banner__title", live.summary),
      ]),
      h("span", "cal-banner__meta", [fmtTimeRange(live), live.location].filter(Boolean).join(" · ")),
    ]);
    node.type = "button";
    node.addEventListener("click", () => onSelectEvent(live));
    return node;
  }
  if (next) {
    const node = h("button", `cal-banner ${colorClass(next)}`, null, [
      h("span", "cal-banner__left", null, [
        h("span", "cal-banner__pill", `下一步 · ${fmtMinutesAhead(next.starts_in_minutes)}`),
        h("span", "cal-banner__title", next.summary),
      ]),
      h("span", "cal-banner__meta", [`${fmtDayFriendly(next.start.slice(0, 10), todayIso)} ${fmtTimeRange(next)}`, next.location].filter(Boolean).join(" · ")),
    ]);
    node.type = "button";
    node.addEventListener("click", () => onSelectEvent(next));
    return node;
  }
  return h("div", "cal-banner is-idle", null, [
    h("span", "cal-banner__left", null, [
      h("span", "cal-banner__pill", "空闲"),
      h("span", "cal-banner__title", "近期没有紧迫日程"),
    ]),
  ]);
}

// 视图 1：本周（智能裁剪活跃时段 + 透亮左竖条卡片）
function renderWeekView(state, events, byDay, data, todayIso, actions) {
  const days = Array.from({ length: 7 }, (_, i) => addDays(state.weekStart, i));
  const daySet = new Set(days);
  const weekEvents = events.filter((ev) => {
    const s = ev.start.slice(0, 10);
    const e = ev.end.slice(0, 10);
    return ev.all_day ? (s < addDays(state.weekStart, 7) && e > state.weekStart) : (daySet.has(s) || daySet.has(e));
  });

  // 智能计算活跃时段：避免大片凌晨空白，也绝不裁掉早 7 点事件
  const timedEvents = weekEvents.filter((ev) => !ev.all_day);
  let startHour = 7;
  let endHour = 22;
  for (const ev of timedEvents) {
    const sHr = Math.floor(parseLocalMinutes(ev.start) / 60);
    const eHr = Math.ceil(parseLocalMinutes(ev.end) / 60);
    if (sHr < startHour) startHour = Math.max(0, sHr);
    if (eHr > endHour) endHour = Math.min(24, eHr + 1);
  }
  startHour = Math.max(0, startHour - 1);
  if (endHour - startHour < 12) endHour = Math.min(24, startHour + 12);
  const totalHours = endHour - startHour;

  const tzShort = (data.timezone || "").replace(/^Asia\//, "") || "CST";
  const colHeads = h("div", "cal-colheads", null, [
    h("div", "cal-tz", tzShort.length <= 5 ? tzShort : "CST"),
    ...days.map((iso) => {
      const d = parseIsoDate(iso);
      const dow = DOW_ZH[(d.getDay() + 6) % 7];
      const count = (byDay.get(iso) || []).length;
      return h("div", `cal-colhead${iso === todayIso ? " is-today" : ""}`, null, [
        h("span", "cal-colhead__dow", `周${dow}`),
        h("span", "cal-colhead__num", String(d.getDate())),
        count ? h("span", "cal-colhead__count", null) : null,
      ]);
    }),
  ]);
  colHeads.style.setProperty("--cal-cols", "7");

  const allDayEvents = weekEvents.filter((ev) => ev.all_day);
  let allDayRow = null;
  if (allDayEvents.length) {
    allDayRow = h("div", "cal-allday", null, [
      h("div", "cal-allday__label", "全天"),
      ...days.map((iso) => {
        const chips = allDayEvents
          .filter((ev) => ev.start.slice(0, 10) <= iso && iso < ev.end.slice(0, 10))
          .map((ev) => btn(`cal-allday__chip ${colorClass(ev)}`, ev.summary, () => actions.selectEvent(ev), { title: ev.summary }));
        return h("div", "cal-allday__cell", null, chips);
      }),
    ]);
    allDayRow.style.setProperty("--cal-cols", "7");
  }

  const nowIso = data.now || new Date().toISOString();
  const nowMins = parseLocalMinutes(nowIso);
  const nowInRange = nowMins >= startHour * 60 && nowMins <= endHour * 60;
  const nowTopPx = ((nowMins - startHour * 60) / 60) * HOUR_H;
  const hasToday = days.includes(todayIso);

  const gutterChildren = [];
  for (let hr = startHour + 1; hr < endHour; hr++) {
    const top = (hr - startHour) * HOUR_H;
    if (hasToday && nowInRange && Math.abs(top - nowTopPx) < 16) continue;
    const lbl = h("span", "cal-gutter__hour", `${pad2(hr)}:00`);
    lbl.style.top = `${top}px`;
    gutterChildren.push(lbl);
  }
  if (hasToday && nowInRange) {
    const pill = h("span", "cal-nowpill", nowIso.slice(11, 16));
    pill.style.top = `${nowTopPx}px`;
    gutterChildren.push(pill);
  }
  const gutter = h("div", "cal-gutter", null, gutterChildren);

  const nowTs = Date.now() / 1000;
  const liveId = data.current?.[0]?.id;
  const nextId = data.next?.id;

  const dayCols = days.map((iso) => {
    const rawItems = [];
    for (const ev of timedEvents) {
      const sDay = ev.start.slice(0, 10);
      const eDay = ev.end.slice(0, 10);
      if (iso < sDay || iso > eDay) continue;
      const startMin = iso === sDay ? parseLocalMinutes(ev.start) : 0;
      let endMin = iso === eDay ? parseLocalMinutes(ev.end) : 1440;
      if (iso === eDay && iso > sDay && endMin === 0) continue;
      if (endMin <= startMin) endMin = Math.min(1440, startMin + 42);
      else endMin = Math.max(startMin + 42, endMin);
      rawItems.push({ ev, startMin, endMin });
    }

    const laidOut = layoutDayEvents(rawItems);
    const nodes = laidOut.map(({ ev, startMin, endMin, lane, cols }) => {
      const clampedStart = Math.max(startHour * 60, startMin);
      const clampedEnd = Math.min(endHour * 60, Math.max(clampedStart + 36, endMin));
      const top = ((clampedStart - startHour * 60) / 60) * HOUR_H;
      const height = Math.max(36, ((clampedEnd - clampedStart) / 60) * HOUR_H - 3);
      const widthPct = 100 / cols;
      const leftPct = lane * widthPct;
      const isPast = ev.end_ts <= nowTs;
      const isLive = ev.id === liveId;
      const isNext = !liveId && ev.id === nextId;

      const cls = [
        "cal-ev",
        colorClass(ev),
        isPast ? "is-past" : "",
        ev.tentative ? "is-tentative" : "",
        isLive ? "is-live" : "",
        isNext ? "is-next" : "",
      ].filter(Boolean).join(" ");

      const block = h("button", cls, null, [
        h("span", "cal-ev__title", ev.summary),
        h("span", "cal-ev__time", fmtTimeRange(ev)),
        ev.location && height >= 56 ? h("span", "cal-ev__loc", ev.location) : null,
      ]);
      block.type = "button";
      block.style.top = `${top}px`;
      block.style.height = `${height}px`;
      block.style.left = `calc(${leftPct}% + 2px)`;
      block.style.width = `calc(${widthPct}% - 4px)`;
      block.title = `${ev.summary} (${fmtTimeRange(ev)})`;
      block.addEventListener("click", () => actions.selectEvent(ev));
      return block;
    });

    if (iso === todayIso && nowInRange) {
      const line = h("div", "cal-nowline");
      line.style.top = `${nowTopPx}px`;
      nodes.push(line);
    }

    return h("div", `cal-daycol${iso === todayIso ? " is-today" : ""}`, null, nodes);
  });

  const grid = h("div", "cal-grid", null, [gutter, ...dayCols]);
  grid.style.setProperty("--cal-cols", "7");
  grid.style.height = `${totalHours * HOUR_H}px`;

  const scroll = h("div", "cal-scroll cal-anim", null, [grid]);
  return { nodes: [colHeads, allDayRow, scroll], scroll };
}

// 视图 2：本月（整月事件鸟瞰，一眼看清哪些天有事）
function renderMonthView(state, byDay, todayIso, actions) {
  const [y, m] = state.miniMonth.split("-").map(Number);
  const first = new Date(y, m - 1, 1);
  const startOffset = (first.getDay() + 6) % 7;
  const gridStart = new Date(y, m - 1, 1 - startOffset);

  const head = h("div", "cal-month-head", null, DOW_ZH.map((d) => h("div", null, `周${d}`)));
  const cells = [];
  for (let i = 0; i < 42; i++) {
    const cellDate = new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + i);
    const iso = toIsoDate(cellDate);
    const inMonth = cellDate.getMonth() === m - 1;
    const isToday = iso === todayIso;
    const dayEvents = byDay.get(iso) || [];

    const numBtn = btn("cal-month-cell__num", String(cellDate.getDate()), () => {
      actions.openWeekOf(iso);
    }, { title: `查看 ${iso} 所在周` });

    const pills = dayEvents.slice(0, 3).map((ev) => {
      const p = h("button", `cal-month-pill ${colorClass(ev)}`, null, [
        !ev.all_day ? h("span", "cal-month-pill__time", ev.start.slice(11, 16)) : null,
        h("span", "cal-month-pill__title", ev.summary),
      ]);
      p.type = "button";
      p.title = `${ev.summary} (${fmtTimeRange(ev)})`;
      p.addEventListener("click", (e) => { e.stopPropagation(); actions.selectEvent(ev); });
      return p;
    });

    const more = dayEvents.length > 3 ? h("span", "cal-month-more", `+${dayEvents.length - 3} 项`) : null;
    const cell = h("div", `cal-month-cell${!inMonth ? " is-out" : ""}${isToday ? " is-today" : ""}`, null, [numBtn, ...pills, more]);
    cells.push(cell);
  }

  return { nodes: [head, h("div", "cal-month-grid cal-anim", null, cells)], scroll: null };
}

// 视图 3：行动清单（零空白时间线，未来 60 天所有要做的事）
function renderAgendaView(events, todayIso, actions) {
  const nowTs = Date.now() / 1000;
  const future = events
    .filter((ev) => ev.all_day ? ev.end.slice(0, 10) > todayIso : ev.end_ts > nowTs)
    .sort((a, b) => a.start.localeCompare(b.start));

  if (!future.length) {
    return { nodes: [h("div", "cal-empty-state cal-anim", null, [h("p", null, "接下来 60 天没有待办日程")])], scroll: null };
  }

  const groups = new Map();
  for (const ev of future) {
    const day = ev.start.slice(0, 10);
    if (!groups.has(day)) groups.set(day, []);
    groups.get(day).push(ev);
  }

  const rows = [];
  for (const [day, list] of groups.entries()) {
    const d = parseIsoDate(day);
    const dow = DOW_ZH[(d.getDay() + 6) % 7];
    const cards = list.map((ev) => {
      const card = h("button", `cal-agenda-card ${colorClass(ev)}`, null, [
        h("div", "cal-agenda-card__main", null, [
          h("div", "cal-agenda-card__title", ev.summary),
          h("div", "cal-agenda-card__meta", [fmtTimeRange(ev), ev.location].filter(Boolean).join(" · ")),
        ]),
        h("span", "cal-agenda-card__cal", ev.calendar_name),
      ]);
      card.type = "button";
      card.addEventListener("click", () => actions.selectEvent(ev));
      return card;
    });

    rows.push(h("div", `cal-agenda-day${day === todayIso ? " is-today" : ""}`, null, [
      h("div", "cal-agenda-day__date", null, [
        h("div", "cal-agenda-day__label", fmtDayFriendly(day, todayIso)),
        h("div", "cal-agenda-day__sub", `${day} 周${dow}`),
      ]),
      h("div", "cal-agenda-cards", null, cards),
    ]));
  }

  return { nodes: [h("div", "cal-agenda-view cal-anim", null, rows)], scroll: null };
}

function renderExpanded(state, data, actions) {
  const todayIso = data.today_date || toIsoDate(new Date());
  const visibleCalIds = new Set((data.calendars || []).filter((c) => !state.hiddenCals.has(c.id)).map((c) => c.id));
  const events = (data.events || []).filter((ev) => visibleCalIds.has(ev.calendar_id));
  const byDay = indexEventsByDay(events);

  // 左栏：彩色热力迷你月历 + 接下来要做什么（前 5 项） + 极简日历列表
  const upcomingFiltered = (data.upcoming || []).filter((ev) => visibleCalIds.has(ev.calendar_id)).slice(0, 5);
  const upcomingSection = h("div", "cal-rail-sec", null, [
    h("div", "cal-rail-sec__title", null, [h("span", null, "接下来"), h("span", null, `${upcomingFiltered.length} 项`)]),
    ...(upcomingFiltered.length
      ? upcomingFiltered.map((ev) => {
          const day = ev.start.slice(0, 10);
          const item = h("button", `cal-upcoming-item ${colorClass(ev)}`, null, [
            h("span", "cal-upcoming-item__when", `${fmtDayFriendly(day, todayIso)} ${ev.all_day ? "全天" : ev.start.slice(11, 16)}`),
            h("span", "cal-upcoming-item__title", ev.summary),
          ]);
          item.type = "button";
          item.addEventListener("click", () => actions.selectEvent(ev));
          return item;
        })
      : [h("div", "cal-rail-sec__title", "暂无近期安排")]),
  ]);

  const calRows = (data.calendars || []).map((cal) => {
    const active = !state.hiddenCals.has(cal.id);
    const row = h("button", `cal-cals__row cal-c-${cal.color || "sky"}`, null, [
      h("span", "cal-cals__swatch"),
      h("span", "cal-cals__name", cal.name),
    ]);
    row.type = "button";
    row.title = cal.raw_name || cal.name;
    row.setAttribute("aria-pressed", String(active));
    row.addEventListener("click", () => actions.toggleCalendar(cal.id));
    return row;
  });

  const rail = h("aside", "cal-rail", null, [
    renderMiniMonth(state, byDay, todayIso, actions),
    upcomingSection,
    calRows.length ? h("div", "cal-cals", null, calRows) : null,
  ]);

  // 顶栏
  const [my, mm] = state.miniMonth.split("-").map(Number);
  const titleMonth = state.view === "week"
    ? `${parseIsoDate(state.weekStart).getFullYear()}年${parseIsoDate(state.weekStart).getMonth() + 1}月`
    : `${my}年${mm}月`;
  const titleSub = state.view === "week"
    ? `第 ${isoWeekNumber(state.weekStart)} 周`
    : state.view === "month"
      ? "整月分布"
      : "未来日程";

  const viewSeg = h("div", "cal-seg", null, [
    ["week", "本周"],
    ["month", "本月"],
    ["agenda", "清单"],
  ].map(([key, label]) =>
    btn("", label, () => actions.setView(key), { "aria-selected": String(state.view === key) })));

  const topbar = h("header", "cal-topbar", null, [
    h("div", "cal-topbar__left", null, [
      h("span", "cal-topbar__month", titleMonth),
      h("span", "cal-topbar__sub", titleSub),
    ]),
    h("div", "cal-topbar__right", null, [
      state.view !== "agenda" ? btn("cal-btn", "今天", actions.goToday) : null,
      state.view !== "agenda" ? btn("cal-btn cal-btn--icon", "‹", () => actions.step(-1), { "aria-label": "上一周期" }) : null,
      state.view !== "agenda" ? btn("cal-btn cal-btn--icon", "›", () => actions.step(1), { "aria-label": "下一周期" }) : null,
      viewSeg,
      btn("cal-btn cal-btn--icon", "↻", actions.refresh, { "aria-label": "刷新", title: "刷新" }),
    ]),
  ]);

  const banner = renderActionBanner(data, todayIso, actions.selectEvent);

  const viewResult = state.view === "month"
    ? renderMonthView(state, byDay, todayIso, actions)
    : state.view === "agenda"
      ? renderAgendaView(events, todayIso, actions)
      : renderWeekView(state, events, byDay, data, todayIso, actions);

  const main = h("section", "cal-main", null, [topbar, banner, ...viewResult.nodes]);
  const shell = h("div", "cal-shell", null, [rail, main]);
  return { shell, scroll: viewResult.scroll };
}

function mount(host, context) {
  const todayIso = toIsoDate(new Date());
  const state = {
    data: memSnapshot,
    error: "",
    view: "week",
    weekStart: mondayOf(todayIso),
    miniMonth: todayIso.slice(0, 7),
    hiddenCals: readHiddenCalendars(),
    selectedEvent: null,
  };

  let alive = true;
  let inflight = false;

  const onKeyDown = (e) => {
    if (e.key === "Escape" && state.selectedEvent) {
      state.selectedEvent = null;
      draw();
    }
  };
  window.addEventListener("keydown", onKeyDown);

  async function loadSnapshot(refresh = false, extraRange = null) {
    if (inflight && !refresh) return;
    inflight = true;
    try {
      const payload = { refresh, ...(extraRange || {}) };
      const res = await context.query("snapshot", payload, { cache: "none" });
      state.data = res;
      if (res?.ready) {
        memSnapshot = res;
        if (res.calendars && !localStorage.getItem(HIDDEN_CALS_KEY)) {
          const defaults = new Set(res.calendars.filter((c) => c.selected === false).map((c) => c.id));
          if (defaults.size) state.hiddenCals = defaults;
        }
      }
      state.error = "";
    } catch (err) {
      if (!state.data) state.error = `读取日程失败：${err instanceof Error ? err.message : err}`;
    } finally {
      inflight = false;
      if (alive) draw();
    }
  }

  function ensureWindowCovers(startIso, endIso) {
    const win = state.data?.window;
    if (!win || startIso < win.start || endIso > win.end) {
      void loadSnapshot(false, { start: startIso, end: endIso });
    }
  }

  const actions = {
    selectEvent(ev) { state.selectedEvent = ev; draw(); },
    toggleCalendar(calId) {
      if (state.hiddenCals.has(calId)) state.hiddenCals.delete(calId);
      else state.hiddenCals.add(calId);
      writeHiddenCalendars(state.hiddenCals);
      draw();
    },
    setView(v) { state.view = v; draw(); },
    goToday() {
      const t = state.data?.today_date || toIsoDate(new Date());
      state.weekStart = mondayOf(t);
      state.miniMonth = t.slice(0, 7);
      draw();
    },
    step(dir) {
      if (state.view === "month") {
        actions.shiftMiniMonth(dir);
        return;
      }
      state.weekStart = addDays(state.weekStart, dir * 7);
      state.miniMonth = state.weekStart.slice(0, 7);
      ensureWindowCovers(state.weekStart, addDays(state.weekStart, 7));
      draw();
    },
    jumpDate(iso) {
      state.weekStart = mondayOf(iso);
      state.miniMonth = iso.slice(0, 7);
      if (state.view === "agenda") state.view = "week";
      ensureWindowCovers(state.weekStart, addDays(state.weekStart, 7));
      draw();
    },
    openWeekOf(iso) {
      state.weekStart = mondayOf(iso);
      state.miniMonth = iso.slice(0, 7);
      state.view = "week";
      ensureWindowCovers(state.weekStart, addDays(state.weekStart, 7));
      draw();
    },
    shiftMiniMonth(delta) {
      const [y, m] = state.miniMonth.split("-").map(Number);
      const next = new Date(y, m - 1 + delta, 1);
      state.miniMonth = `${next.getFullYear()}-${pad2(next.getMonth() + 1)}`;
      const mStart = `${state.miniMonth}-01`;
      const mEnd = addDays(mStart, 32);
      ensureWindowCovers(mStart, mEnd);
      draw();
    },
    refresh() { void loadSnapshot(true); },
  };

  function draw() {
    if (!alive) return;
    const size = context.panel.size();
    const root = h("div", "cal-root");

    if (state.error) {
      root.append(h("div", "cal-empty-state", null, [
        h("p", null, state.error),
        btn("cal-btn", "重试", actions.refresh),
      ]));
      host.replaceChildren(root);
      return;
    }

    if (!state.data) {
      root.append(h("div", "cal-empty-state", null, [h("p", null, "正在同步日程…")]));
      host.replaceChildren(root);
      return;
    }

    if (!state.data.ready) {
      root.append(h("div", "cal-empty-state", null, [
        h("p", null, state.data.reason || "Google 日历尚未就绪"),
        btn("cal-btn", "刷新", actions.refresh),
      ]));
      host.replaceChildren(root);
      return;
    }

    if (size === "compact") {
      root.append(renderCompact(state.data, context.panel, actions.refresh, actions.selectEvent));
    } else {
      const { shell } = renderExpanded(state, state.data, actions);
      root.append(shell);
    }

    if (state.selectedEvent) {
      root.append(renderPopover(state.selectedEvent, () => { state.selectedEvent = null; draw(); }));
    }

    host.replaceChildren(root);
  }

  const stops = [
    context.onInvalidate(draw),
    context.onShown(() => { void loadSnapshot(false); }),
  ];

  draw();
  void loadSnapshot(false);
  return () => {
    alive = false;
    window.removeEventListener("keydown", onKeyDown);
    for (const stop of stops) stop();
    host.replaceChildren();
  };
}

const panel = {
  label: "日程",
  iconSvg: ICON_SVG,
  async status({ query }) {
    try {
      const data = await query("summary", {}, { cache: "none" });
      if (!data?.ready || !data.badge?.text) return null;
      return { text: String(data.badge.text), alert: Boolean(data.badge.alert) };
    } catch {
      return null;
    }
  },
  mount,
};

export default { slots: { "sidebar.panels": panel } };
