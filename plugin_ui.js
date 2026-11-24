// 侧栏日历面板：Arc Week Calendar 视觉风格 + 「此刻/下一步」行动焦点，纯只读轻交互。

const ICON_SVG = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2.25" y="3.25" width="11.5" height="10.5" rx="2"/><path d="M5.25 1.75v2.5M10.75 1.75v2.5M2.25 6.5h11.5"/></svg>';
const DOW_ZH = ["一", "二", "三", "四", "五", "六", "日"];
const HOUR_H = 56;
const HIDDEN_CALS_KEY = "akashic.calendar.hidden-cals.v1";

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

function pad2(n) {
  return String(n).padStart(2, "0");
}

function toIsoDate(d) {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

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
  const offset = (d.getDay() + 6) % 7;
  d.setDate(d.getDate() - offset);
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

function fmtMinutesAhead(mins) {
  if (mins <= 1) return "即将开始";
  if (mins < 60) return `${mins} 分钟后`;
  const hrs = Math.floor(mins / 60);
  const rem = mins % 60;
  if (hrs < 24) return rem ? `${hrs} 小时 ${rem} 分钟后` : `${hrs} 小时后`;
  const days = Math.floor(hrs / 24);
  const remH = hrs % 24;
  return remH ? `${days} 天 ${remH} 小时后` : `${days} 天后`;
}

function fmtRemaining(mins) {
  if (mins <= 1) return "即将结束";
  if (mins < 60) return `还剩 ${mins} 分钟`;
  const hrs = Math.floor(mins / 60);
  const rem = mins % 60;
  return rem ? `还剩 ${hrs} 小时 ${rem} 分钟` : `还剩 ${hrs} 小时`;
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
  if (day === endDay) {
    return `${day} 周${dow}  ${ev.start.slice(11, 16)} – ${ev.end.slice(11, 16)}`;
  }
  return `${day} ${ev.start.slice(11, 16)} – ${endDay} ${ev.end.slice(11, 16)}`;
}

function colorClass(ev) {
  const c = ev?.color || "sage";
  return `cal-c-${c}`;
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
  try {
    localStorage.setItem(HIDDEN_CALS_KEY, JSON.stringify([...set]));
  } catch {
    // 忽略存储不可用
  }
}

// 同一天重叠事件的精准分列布局
function layoutDayEvents(items) {
  const sorted = [...items].sort((a, b) => a.startMin - b.startMin || b.endMin - a.endMin);
  const placed = [];
  let cluster = [];
  let clusterEnd = -1;

  const flushCluster = () => {
    if (!cluster.length) return;
    const laneEnds = [];
    for (const item of cluster) {
      let lane = laneEnds.findIndex((end) => end <= item.startMin);
      if (lane === -1) {
        lane = laneEnds.length;
        laneEnds.push(item.endMin);
      } else {
        laneEnds[lane] = item.endMin;
      }
      item.lane = lane;
    }
    const cols = laneEnds.length;
    for (const item of cluster) {
      item.cols = cols;
      placed.push(item);
    }
    cluster = [];
    clusterEnd = -1;
  };

  for (const item of sorted) {
    if (cluster.length && item.startMin >= clusterEnd) {
      flushCluster();
    }
    cluster.push(item);
    clusterEnd = Math.max(clusterEnd, item.endMin);
  }
  flushCluster();
  return placed;
}

// 渲染「此刻 / 下一步」行动焦点卡（紧凑态与展开态左栏共用）
function renderFocusCard(data, onSelectEvent) {
  const live = data.current?.[0];
  const next = data.next;
  const allDay = data.today_all_day?.[0];

  if (live) {
    const card = h("div", `cal-focus is-live ${colorClass(live)}`, null, [
      h("div", "cal-focus__kicker", null, [
        h("span", "cal-focus__dot"),
        h("span", null, `正在进行 · ${fmtRemaining(live.remaining_minutes)}`),
      ]),
      h("p", "cal-focus__title", live.summary),
      h("div", "cal-focus__meta", null, [
        h("span", null, fmtTimeRange(live)),
        live.location ? h("span", null, live.location) : null,
      ]),
    ]);
    card.style.cursor = "pointer";
    card.addEventListener("click", () => onSelectEvent(live));
    return card;
  }

  if (next) {
    const dayTag = next.start.slice(0, 10) === data.today_date
      ? "今天"
      : next.start.slice(0, 10) === addDays(data.today_date, 1)
        ? "明天"
        : next.start.slice(5, 10);
    const card = h("div", `cal-focus ${colorClass(next)}`, null, [
      h("div", "cal-focus__kicker", null, [
        h("span", "cal-focus__dot"),
        h("span", null, `下一步 · ${fmtMinutesAhead(next.starts_in_minutes)}`),
      ]),
      h("p", "cal-focus__title", next.summary),
      h("div", "cal-focus__meta", null, [
        h("span", null, `${dayTag} ${fmtTimeRange(next)}`),
        next.location ? h("span", null, next.location) : null,
      ]),
    ]);
    card.style.cursor = "pointer";
    card.addEventListener("click", () => onSelectEvent(next));
    return card;
  }

  if (allDay) {
    const card = h("div", `cal-focus ${colorClass(allDay)}`, null, [
      h("div", "cal-focus__kicker", null, [
        h("span", "cal-focus__dot"),
        h("span", null, "今日全天事项"),
      ]),
      h("p", "cal-focus__title", allDay.summary),
      h("div", "cal-focus__meta", null, [h("span", null, allDay.calendar_name)]),
    ]);
    card.style.cursor = "pointer";
    card.addEventListener("click", () => onSelectEvent(allDay));
    return card;
  }

  return h("div", "cal-focus is-idle", null, [
    h("div", "cal-focus__kicker", null, [
      h("span", "cal-focus__dot"),
      h("span", null, "此刻状态"),
    ]),
    h("p", "cal-focus__title", "接下来没有紧迫日程"),
    h("div", "cal-focus__meta", null, [h("span", null, "可以按自己的节奏推进手头的事")]),
  ]);
}

// 渲染只读详情浮层
function renderPopover(ev, onClose) {
  const backdrop = h("div", "cal-popover-backdrop");
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) onClose();
  });

  const rows = [
    h("div", "cal-popover__row", null, [h("strong", null, "时间："), document.createTextNode(fmtFullTime(ev))]),
    ev.location ? h("div", "cal-popover__row", null, [h("strong", null, "地点："), document.createTextNode(ev.location)]) : null,
    ev.tentative ? h("div", "cal-popover__row", null, [h("strong", null, "状态："), document.createTextNode("待确认 / 暂定")]) : null,
  ];

  const card = h("div", `cal-popover ${colorClass(ev)}`, null, [
    h("div", "cal-popover__head", null, [
      h("div", null, null, [
        h("span", "cal-popover__badge", ev.calendar_name || "日程"),
        h("h3", "cal-popover__title", ev.summary),
      ]),
      btn("cal-popover__close", "×", onClose, { "aria-label": "关闭详情" }),
    ]),
    h("div", "cal-popover__rows", null, rows),
    ev.description ? h("div", "cal-popover__desc", ev.description) : null,
    ev.html_link
      ? (() => {
          const a = h("a", "cal-popover__link", "在 Google 日历中查看");
          a.href = ev.html_link;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          return a;
        })()
      : null,
  ]);
  backdrop.append(card);
  return backdrop;
}

// 紧凑态（弹出卡片）
function renderCompact(state, panel, onRefresh, onSelectEvent) {
  const data = state.summary;
  const nowTs = Date.now() / 1000;
  const todayList = [...(data.today_all_day || []), ...(data.today || [])];
  const tomorrowList = data.tomorrow || [];

  const makeRow = (ev) => {
    const isPast = !ev.all_day && ev.end_ts <= nowTs;
    const cls = [
      "cal-agenda-item",
      colorClass(ev),
      isPast ? "is-past" : "",
      ev.tentative ? "is-tentative" : "",
    ].filter(Boolean).join(" ");
    const timeLabel = ev.all_day ? "全天" : ev.start.slice(11, 16);
    const node = h("button", cls, null, [
      h("span", "cal-agenda-item__time", timeLabel),
      h("span", "cal-agenda-item__body", null, [
        h("span", "cal-agenda-item__title", ev.summary),
        ev.location ? h("span", "cal-agenda-item__loc", ev.location) : null,
      ]),
    ]);
    node.type = "button";
    node.addEventListener("click", () => onSelectEvent(ev));
    return node;
  };

  const sections = [renderFocusCard(data, onSelectEvent)];

  if (todayList.length) {
    sections.push(h("div", null, null, [
      h("div", "cal-sec-title", null, [h("span", null, "今天"), h("span", null, `${todayList.length} 项`)]),
      h("div", "cal-agenda-list", null, todayList.slice(0, 6).map(makeRow)),
    ]));
  }

  if (tomorrowList.length) {
    sections.push(h("div", null, null, [
      h("div", "cal-sec-title", null, [h("span", null, "明天"), h("span", null, `${tomorrowList.length} 项`)]),
      h("div", "cal-agenda-list", null, tomorrowList.slice(0, 3).map(makeRow)),
    ]));
  }

  sections.push(h("div", "cal-compact__foot", null, [
    btn("cal-btn cal-btn--primary", "展开周视图", () => panel.expand()),
    btn("cal-btn cal-btn--icon", "↻", onRefresh, { "aria-label": "刷新日程", title: "刷新" }),
  ]));

  return h("div", "cal-compact", null, sections);
}

// 左侧迷你月历
function renderMiniMonth(state, onJumpDate, onShiftMonth) {
  const [y, m] = state.miniMonth.split("-").map(Number);
  const first = new Date(y, m - 1, 1);
  const startOffset = (first.getDay() + 6) % 7;
  const gridStart = new Date(y, m - 1, 1 - startOffset);
  const busySet = new Set(state.rangeData?.busy_days || []);
  const todayIso = state.rangeData?.today_date || toIsoDate(new Date());
  const rangeStart = state.visibleStart;
  const rangeEnd = addDays(state.visibleStart, state.viewDays);

  const cells = DOW_ZH.map((d) => h("div", "cal-mini__dow", d));
  for (let i = 0; i < 42; i++) {
    const cellDate = new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + i);
    const iso = toIsoDate(cellDate);
    const inMonth = cellDate.getMonth() === m - 1;
    const isToday = iso === todayIso;
    const inRange = iso >= rangeStart && iso < rangeEnd;
    const hasEvents = busySet.has(iso);
    const cls = [
      "cal-mini__day",
      !inMonth ? "is-out" : "",
      inRange && !isToday ? "is-in-range" : "",
      isToday ? "is-today" : "",
      hasEvents ? "has-events" : "",
    ].filter(Boolean).join(" ");
    cells.push(btn(cls, String(cellDate.getDate()), () => onJumpDate(iso), { title: iso }));
  }

  return h("div", "cal-mini", null, [
    h("div", "cal-mini__head", null, [
      h("span", "cal-mini__title", `${y}年${m}月`),
      h("div", "cal-mini__nav", null, [
        btn("", "‹", () => onShiftMonth(-1), { "aria-label": "上个月" }),
        btn("", "›", () => onShiftMonth(1), { "aria-label": "下个月" }),
      ]),
    ]),
    h("div", "cal-mini__grid", null, cells),
  ]);
}

// 展开态主视图（Arc Week Calendar 布局）
function renderExpanded(state, actions) {
  const data = state.rangeData;
  const todayIso = data.today_date || toIsoDate(new Date());
  const days = Array.from({ length: state.viewDays }, (_, i) => addDays(state.visibleStart, i));
  const visibleCalendars = (data.calendars || []).filter((c) => !state.hiddenCals.has(c.id));
  const visibleCalIds = new Set(visibleCalendars.map((c) => c.id));
  const events = (data.events || []).filter((ev) => visibleCalIds.has(ev.calendar_id));
  const nowTs = Date.now() / 1000;
  const liveId = data.current?.[0]?.id;
  const nextId = data.next?.id;

  // 左侧栏：行动焦点卡 + 迷你月历 + 日历筛选
  const calRows = (data.calendars || []).map((cal) => {
    const active = !state.hiddenCals.has(cal.id);
    const row = h("button", `cal-cals__row cal-c-${cal.color || "sage"}`, null, [
      h("span", "cal-cals__box", active ? "✓" : ""),
      h("span", "cal-cals__name", cal.name),
    ]);
    row.type = "button";
    row.setAttribute("aria-pressed", String(active));
    row.addEventListener("click", () => actions.toggleCalendar(cal.id));
    return row;
  });

  const rail = h("aside", "cal-rail", null, [
    renderFocusCard(data, actions.selectEvent),
    renderMiniMonth(state, actions.jumpDate, actions.shiftMiniMonth),
    calRows.length ? h("div", "cal-cals", null, [h("div", "cal-cals__head", "日历"), ...calRows]) : null,
  ]);

  // 右侧顶栏
  const anchorDate = parseIsoDate(days[0]);
  const monthLabel = `${anchorDate.getFullYear()}年${anchorDate.getMonth() + 1}月`;
  const weekLabel = `第 ${isoWeekNumber(days[0])} 周`;

  const viewSeg = h("div", "cal-seg", null, [
    [7, "本周"],
    [3, "三天"],
    [1, "当日"],
  ].map(([count, label]) =>
    btn("", label, () => actions.setViewDays(count), { "aria-selected": String(state.viewDays === count) })));

  const topbar = h("header", "cal-topbar", null, [
    h("div", "cal-topbar__title", null, [
      h("span", "cal-topbar__month", monthLabel),
      h("span", "cal-topbar__week", weekLabel),
    ]),
    h("div", "cal-topbar__controls", null, [
      btn("cal-btn", "今天", actions.goToday),
      h("div", "cal-nav-group", null, [
        btn("cal-btn cal-btn--icon", "‹", () => actions.shiftRange(-1), { "aria-label": "上一周期" }),
        btn("cal-btn cal-btn--icon", "›", () => actions.shiftRange(1), { "aria-label": "下一周期" }),
      ]),
      viewSeg,
      btn("cal-btn cal-btn--icon", "↻", actions.refresh, { "aria-label": "刷新", title: "刷新" }),
    ]),
  ]);

  // 列头
  const tzShort = (data.timezone || "").replace(/^Asia\//, "") || "本地";
  const colHeads = h("div", "cal-colheads", null, [
    h("div", "cal-tz", tzShort.length <= 6 ? tzShort : "本地"),
    ...days.map((iso) => {
      const d = parseIsoDate(iso);
      const dow = DOW_ZH[(d.getDay() + 6) % 7];
      return h("div", `cal-colhead${iso === todayIso ? " is-today" : ""}`, null, [
        h("span", "cal-colhead__dow", `周${dow}`),
        h("span", "cal-colhead__num", String(d.getDate())),
      ]);
    }),
  ]);
  colHeads.style.setProperty("--cal-cols", String(days.length));

  // 全天事件行（只有存在全天事件时才占高度）
  const allDayEvents = events.filter((ev) => ev.all_day);
  let allDayRow = null;
  if (allDayEvents.length) {
    allDayRow = h("div", "cal-allday", null, [
      h("div", "cal-allday__label", "全天"),
      ...days.map((iso) => {
        const dayChips = allDayEvents
          .filter((ev) => ev.start.slice(0, 10) <= iso && iso < ev.end.slice(0, 10))
          .map((ev) => btn(`cal-allday__chip ${colorClass(ev)}`, ev.summary, () => actions.selectEvent(ev), { title: ev.summary }));
        return h("div", "cal-allday__cell", null, dayChips);
      }),
    ]);
    allDayRow.style.setProperty("--cal-cols", String(days.length));
  }

  // 时间刻度列
  const nowDate = new Date();
  const nowMins = nowDate.getHours() * 60 + nowDate.getMinutes();
  const nowTopPx = (nowMins / 60) * HOUR_H;
  const hasTodayInView = days.includes(todayIso);

  const gutterChildren = [];
  for (let hr = 1; hr < 24; hr++) {
    const label = h("span", "cal-gutter__hour", `${pad2(hr)}:00`);
    label.style.top = `${hr * HOUR_H}px`;
    gutterChildren.push(label);
  }
  if (hasTodayInView) {
    const pill = h("span", "cal-nowpill", `${pad2(nowDate.getHours())}:${pad2(nowDate.getMinutes())}`);
    pill.style.top = `${nowTopPx}px`;
    gutterChildren.push(pill);
  }
  const gutter = h("div", "cal-gutter", null, gutterChildren);

  // 每天的事件列
  const timedEvents = events.filter((ev) => !ev.all_day);
  const dayCols = days.map((iso) => {
    const dayStartTs = parseIsoDate(iso).getTime() / 1000;
    const dayEndTs = dayStartTs + 86400;
    const rawItems = [];

    for (const ev of timedEvents) {
      if (ev.end_ts <= dayStartTs || ev.start_ts >= dayEndTs) continue;
      const clampedStart = Math.max(ev.start_ts, dayStartTs);
      const clampedEnd = Math.min(ev.end_ts, dayEndTs);
      const startMin = (clampedStart - dayStartTs) / 60;
      const endMin = Math.max(startMin + 18, (clampedEnd - dayStartTs) / 60);
      rawItems.push({ ev, startMin, endMin });
    }

    const laidOut = layoutDayEvents(rawItems);
    const nodes = laidOut.map(({ ev, startMin, endMin, lane, cols }) => {
      const top = (startMin / 60) * HOUR_H;
      const height = Math.max(20, ((endMin - startMin) / 60) * HOUR_H - 2);
      const widthPct = 100 / cols;
      const leftPct = lane * widthPct;
      const isShort = (endMin - startMin) <= 35;
      const isPast = ev.end_ts <= nowTs;
      const isLive = ev.id === liveId;
      const isNext = !liveId && ev.id === nextId;

      const cls = [
        "cal-ev",
        colorClass(ev),
        isShort ? "is-short" : "",
        isPast ? "is-past" : "",
        ev.tentative ? "is-tentative" : "",
        isLive ? "is-live" : "",
        isNext ? "is-next" : "",
      ].filter(Boolean).join(" ");

      const children = [
        h("span", "cal-ev__title", ev.summary),
        h("span", "cal-ev__time", isShort ? ev.start.slice(11, 16) : fmtTimeRange(ev)),
        !isShort && ev.location && height >= 52 ? h("span", "cal-ev__loc", ev.location) : null,
      ];
      const block = h("button", cls, null, children);
      block.type = "button";
      block.style.top = `${top}px`;
      block.style.height = `${height}px`;
      block.style.left = `calc(${leftPct}% + 2px)`;
      block.style.width = `calc(${widthPct}% - 4px)`;
      block.title = `${ev.summary} (${fmtTimeRange(ev)})`;
      block.addEventListener("click", () => actions.selectEvent(ev));
      return block;
    });

    if (iso === todayIso) {
      const line = h("div", "cal-nowline");
      line.style.top = `${nowTopPx}px`;
      nodes.push(line);
    }

    return h("div", `cal-daycol${iso === todayIso ? " is-today" : ""}`, null, nodes);
  });

  const grid = h("div", "cal-grid", null, [gutter, ...dayCols]);
  grid.style.setProperty("--cal-cols", String(days.length));
  const scroll = h("div", "cal-scroll", null, [grid]);

  const board = h("section", "cal-board", null, [topbar, colHeads, allDayRow, scroll]);
  const shell = h("div", "cal-shell", null, [rail, board]);

  return { shell, scroll, nowTopPx };
}

function mount(host, context) {
  const todayIso = toIsoDate(new Date());
  const state = {
    summary: null,
    rangeData: null,
    loading: true,
    error: "",
    viewDays: window.innerWidth < 760 ? 3 : 7,
    visibleStart: window.innerWidth < 760 ? todayIso : mondayOf(todayIso),
    miniMonth: todayIso.slice(0, 7),
    hiddenCals: readHiddenCalendars(),
    selectedEvent: null,
    didAutoScroll: false,
  };

  let alive = true;
  let lastSize = "";
  let scrollEl = null;

  const onKeyDown = (e) => {
    if (e.key === "Escape" && state.selectedEvent) {
      state.selectedEvent = null;
      draw();
    }
  };
  window.addEventListener("keydown", onKeyDown);

  async function loadCompact(refresh = false) {
    state.loading = !state.summary;
    if (state.loading) draw();
    try {
      state.summary = await context.query("summary", { refresh }, { cache: "none" });
      state.error = "";
    } catch (err) {
      state.error = `读取日程失败：${err instanceof Error ? err.message : err}`;
    } finally {
      state.loading = false;
      if (alive) draw();
    }
  }

  async function loadRange(refresh = false) {
    state.loading = !state.rangeData;
    if (state.loading) draw();
    try {
      const end = addDays(state.visibleStart, state.viewDays);
      state.rangeData = await context.query("range", {
        start: state.visibleStart,
        end,
        month: state.miniMonth,
        refresh,
      }, { cache: "none" });
      state.error = "";
      // 同步把日历默认未勾选的状态纳入初次隐藏集合（仅用户从未手动设置过时）
      if (state.rangeData?.calendars && !localStorage.getItem(HIDDEN_CALS_KEY)) {
        const defaults = new Set(
          state.rangeData.calendars.filter((c) => c.selected === false).map((c) => c.id)
        );
        if (defaults.size) state.hiddenCals = defaults;
      }
    } catch (err) {
      state.error = `读取日程失败：${err instanceof Error ? err.message : err}`;
    } finally {
      state.loading = false;
      if (alive) draw();
    }
  }

  const actions = {
    selectEvent(ev) {
      state.selectedEvent = ev;
      draw();
    },
    toggleCalendar(calId) {
      if (state.hiddenCals.has(calId)) state.hiddenCals.delete(calId);
      else state.hiddenCals.add(calId);
      writeHiddenCalendars(state.hiddenCals);
      draw();
    },
    setViewDays(count) {
      state.viewDays = count;
      if (count === 7) state.visibleStart = mondayOf(state.visibleStart);
      void loadRange(false);
    },
    shiftRange(dir) {
      state.visibleStart = addDays(state.visibleStart, dir * state.viewDays);
      state.miniMonth = state.visibleStart.slice(0, 7);
      void loadRange(false);
    },
    goToday() {
      const today = toIsoDate(new Date());
      state.visibleStart = state.viewDays === 7 ? mondayOf(today) : today;
      state.miniMonth = today.slice(0, 7);
      state.didAutoScroll = false;
      void loadRange(false);
    },
    jumpDate(iso) {
      state.visibleStart = state.viewDays === 7 ? mondayOf(iso) : iso;
      state.miniMonth = iso.slice(0, 7);
      void loadRange(false);
    },
    shiftMiniMonth(delta) {
      const [y, m] = state.miniMonth.split("-").map(Number);
      const next = new Date(y, m - 1 + delta, 1);
      state.miniMonth = `${next.getFullYear()}-${pad2(next.getMonth() + 1)}`;
      void loadRange(false);
    },
    refresh() {
      if (context.panel.size() === "compact") void loadCompact(true);
      else void loadRange(true);
    },
  };

  function draw() {
    if (!alive) return;
    const size = context.panel.size();
    const prevScrollTop = scrollEl ? scrollEl.scrollTop : null;

    if (size !== lastSize) {
      lastSize = size;
      if (size === "compact" && !state.summary) void loadCompact(false);
      if (size !== "compact" && !state.rangeData) void loadRange(false);
    }

    const root = h("div", "cal-root");

    if (state.error) {
      root.append(h("div", "cal-empty-state", null, [
        h("p", null, state.error),
        btn("cal-btn", "重试", actions.refresh),
      ]));
      host.replaceChildren(root);
      return;
    }

    if (size === "compact") {
      if (!state.summary) {
        root.append(h("div", "cal-empty-state", null, [h("p", null, "正在读取日程…")]));
      } else if (!state.summary.ready) {
        root.append(h("div", "cal-empty-state", null, [
          h("p", null, state.summary.reason || "Google 日历尚未就绪"),
          btn("cal-btn", "刷新", actions.refresh),
        ]));
      } else {
        root.append(renderCompact(state, context.panel, actions.refresh, actions.selectEvent));
      }
      if (state.selectedEvent) {
        root.append(renderPopover(state.selectedEvent, () => { state.selectedEvent = null; draw(); }));
      }
      scrollEl = null;
      host.replaceChildren(root);
      return;
    }

    // 展开态
    if (!state.rangeData) {
      root.append(h("div", "cal-empty-state", null, [h("p", null, "正在加载本周日程…")]));
      host.replaceChildren(root);
      return;
    }
    if (!state.rangeData.ready) {
      root.append(h("div", "cal-empty-state", null, [
        h("p", null, state.rangeData.reason || "Google 日历尚未就绪"),
        btn("cal-btn", "刷新", actions.refresh),
      ]));
      host.replaceChildren(root);
      return;
    }

    const { shell, scroll, nowTopPx } = renderExpanded(state, actions);
    root.append(shell);
    if (state.selectedEvent) {
      root.append(renderPopover(state.selectedEvent, () => { state.selectedEvent = null; draw(); }));
    }
    scrollEl = scroll;
    host.replaceChildren(root);

    if (!state.didAutoScroll) {
      state.didAutoScroll = true;
      const target = Math.max(7.5 * HOUR_H, nowTopPx - 2.2 * HOUR_H);
      scroll.scrollTop = Math.min(target, 16 * HOUR_H);
    } else if (prevScrollTop != null) {
      scroll.scrollTop = prevScrollTop;
    }
  }

  // 每分钟轻刷新红线位置
  const tickTimer = window.setInterval(() => {
    if (alive && context.panel.size() !== "compact" && state.rangeData?.ready) draw();
  }, 60_000);

  const stops = [
    context.onInvalidate(draw),
    context.onShown(() => {
      if (context.panel.size() === "compact") void loadCompact(false);
      else void loadRange(false);
    }),
  ];

  draw();
  return () => {
    alive = false;
    window.clearInterval(tickTimer);
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
