/* 我的课表 · 前端
   流程：登录 → 拉学年学期 → 拉课表 → 按周（网格）或按天（日程）渲染
   无框架，原生 JS；所有用户数据进 DOM 前都过 escapeHtml。 */

const API = "";
const $ = (id) => document.getElementById(id);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const MOCK = new URLSearchParams(location.search).has("mock");

const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const DAY_CHARS = ["一", "二", "三", "四", "五", "六", "日"];
const ROWS = 12;
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)");
const mqlDark = window.matchMedia("(prefers-color-scheme: dark)");
const mqlWide = window.matchMedia("(min-width: 901px)");

/* ---------------- 本地存储 ---------------- */
const store = {
  get token() { return localStorage.getItem("kcb.token") || ""; },
  set token(v) { v ? localStorage.setItem("kcb.token", v) : localStorage.removeItem("kcb.token"); },
  get startDate() { return localStorage.getItem("kcb.startDate") || ""; },
  set startDate(v) { v ? localStorage.setItem("kcb.startDate", v) : localStorage.removeItem("kcb.startDate"); },
  get theme() { return localStorage.getItem("kcb.theme") || "system"; },
  set theme(v) { localStorage.setItem("kcb.theme", v); },
  get view() { return localStorage.getItem("kcb.view") || ""; },
  set view(v) { localStorage.setItem("kcb.view", v); },
};

/* ---------------- 状态 ---------------- */
const state = {
  terms: null,
  courses: [],
  practices: [],
  slots: [],
  week: 1,
  maxWeek: 20,
  todayWeek: null,
  day: 1,
  view: "week",
  palette: new Map(),
};

/* ---------------- 主题 ---------------- */
function applyTheme() {
  const t = store.theme;
  const dark = t === "dark" || (t === "system" && mqlDark.matches);
  const root = document.documentElement;
  root.dataset.theme = t;
  root.dataset.scheme = dark ? "dark" : "light";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", dark ? "#0d1013" : "#f0f1ec");
  $$("#theme-seg .seg-item").forEach((b) => {
    const on = b.dataset.theme === t;
    b.classList.toggle("is-on", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

mqlDark.addEventListener("change", () => { if (store.theme === "system") applyTheme(); });

/* ---------------- 工具 ---------------- */
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (m) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[m]));
}

const pad2 = (n) => String(n).padStart(2, "0");
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

function toMin(hhmm) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(hhmm || ""));
  return m ? Number(m[1]) * 60 + Number(m[2]) : 0;
}

function slotOf(idx) { return state.slots.find((s) => s.idx === idx); }

function startMonday() {
  if (!store.startDate) return null;
  const d = new Date(store.startDate + "T00:00:00");
  return isNaN(d.getTime()) ? null : d;
}

function dateFor(week, weekday) {
  const start = startMonday();
  if (!start) return null;
  const d = new Date(start);
  d.setDate(start.getDate() + (week - 1) * 7 + (weekday - 1));
  return d;
}

function fmtMD(d) { return d ? `${d.getMonth() + 1}/${d.getDate()}` : ""; }

function realWeekday() {
  const js = new Date().getDay();
  return js === 0 ? 7 : js;
}

function computeTodayWeek() {
  const start = startMonday();
  if (!start) return (state.todayWeek = null);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.floor((today - start) / 86400000);
  const w = Math.floor(days / 7) + 1;
  state.todayWeek = w >= 1 ? w : null;
  return state.todayWeek;
}

const inWeek = (c, week) => !c.weeks || !c.weeks.length || c.weeks.includes(week);

/* ---------------- 课程配色（按课名排序后顺序取色，保证相邻不撞色） ---------------- */
function buildPalette(courses) {
  const names = [...new Set(courses.map((c) => c.name))]
    .sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
  state.palette = new Map(names.map((n, i) => [n, (i % 7) + 1]));
}
const paletteOf = (name) => state.palette.get(name) || 1;

/* ---------------- 接口 ---------------- */
async function api(path, opts = {}) {
  if (MOCK) return mockApi(path);
  const headers = { ...(opts.headers || {}) };
  if (store.token) headers.Authorization = `Bearer ${store.token}`;
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(API + path, { ...opts, headers });
  let data = null;
  try { data = await res.json(); } catch { /* 非 JSON 响应 */ }
  if (res.status === 401 && !path.startsWith("/api/login")) {
    store.token = "";
    showLogin("登录已过期，请重新登录");
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    const msg = (data && (data.message || data.detail)) || `请求失败（HTTP ${res.status}）`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

/* ---------------- 视图切换 ---------------- */
function showLogin(msg) {
  closeOverlay("detail");
  closeOverlay("settings");
  $("app-view").hidden = true;
  $("login-view").hidden = false;
  setLoginMsg(msg || "");
}

function showApp() {
  $("login-view").hidden = true;
  $("app-view").hidden = false;
}

function setLoginMsg(text, kind) {
  const el = $("login-msg");
  el.textContent = text || "";
  el.classList.toggle("ok", kind === "ok");
}

function setAppMsg(text) {
  $("app-msg").textContent = text || "";
}

/* ---------------- 登录交互 ---------------- */
$$(".seg-tabs .seg-item").forEach((tab) => {
  tab.addEventListener("click", () => {
    const mode = tab.dataset.mode;
    $$(".seg-tabs .seg-item").forEach((t) => {
      const on = t === tab;
      t.classList.toggle("is-on", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    $("pwd-form").hidden = mode !== "pwd";
    $("cookie-form").hidden = mode !== "cookie";
    setLoginMsg("");
  });
});

$("pwd-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = $("username").value.trim();
  const password = $("password").value;
  if (!username || !password) return setLoginMsg("学号和密码都要填");
  $("pwd-submit").disabled = true;
  setLoginMsg("正在登录…", "ok");
  try {
    const data = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    store.token = data.token;
    setLoginMsg("");
    await boot();
  } catch (err) {
    setLoginMsg(err.message === "unauthorized" ? "登录失败，请重试" : err.message);
  } finally {
    $("pwd-submit").disabled = false;
  }
});

$("cookie-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const cookie = $("cookie").value.trim();
  if (!cookie) return setLoginMsg("先粘贴 Cookie");
  $("cookie-submit").disabled = true;
  setLoginMsg("正在校验 Cookie…", "ok");
  try {
    const data = await api("/api/login-cookie", {
      method: "POST",
      body: JSON.stringify({ cookie }),
    });
    store.token = data.token;
    setLoginMsg("");
    await boot();
  } catch (err) {
    setLoginMsg(err.message);
  } finally {
    $("cookie-submit").disabled = false;
  }
});

/* ---------------- 启动 ---------------- */
async function boot() {
  if (!store.view) store.view = mqlWide.matches ? "week" : "day";
  state.view = store.view;
  showApp();
  setLoader(true);

  const terms = await api("/api/terms");
  state.terms = terms;

  const selXnm = $("sel-xnm");
  const selXqm = $("sel-xqm");
  selXnm.innerHTML = terms.xnm.map((o) => `<option value="${escapeHtml(o.value)}">${escapeHtml(o.label || o.value)}</option>`).join("");
  selXqm.innerHTML = terms.xqm.map((o) => `<option value="${escapeHtml(o.value)}">${escapeHtml(o.label || o.value)}</option>`).join("");
  selXnm.value = terms.current.xnm;
  selXqm.value = terms.current.xqm;

  initStartDate();
  await loadTimetable();
}

function initStartDate() {
  if (store.startDate) return;
  const now = new Date();
  const y = now.getMonth() >= 8 ? now.getFullYear() : now.getFullYear() - 1;
  const guess = now.getMonth() >= 8
    ? new Date(y, 8, 1)        // 9 月 1 日
    : new Date(y + 1, 1, 23);  // 次年 2 月下旬
  store.startDate = `${guess.getFullYear()}-${pad2(guess.getMonth() + 1)}-${pad2(guess.getDate())}`;
}

function setLoader(on) {
  $("loader").hidden = !on;
  if (on) {
    $("grid-wrap").hidden = true;
    $("agenda").hidden = true;
    $("daystrip").hidden = true;
    $("nowbar").hidden = true;
  }
}

function termText() {
  const a = $("sel-xnm").selectedOptions[0];
  const b = $("sel-xqm").selectedOptions[0];
  const parts = [a && a.textContent.trim(), b && b.textContent.trim()].filter(Boolean);
  return parts.join(" · ") || "—";
}

/* ---------------- 拉课表 ---------------- */
async function loadTimetable() {
  const xnm = $("sel-xnm").value;
  const xqm = $("sel-xqm").value;
  setAppMsg("");
  setLoader(true);
  try {
    const data = await api(`/api/timetable?xnm=${encodeURIComponent(xnm)}&xqm=${encodeURIComponent(xqm)}`);
    state.courses = data.courses || [];
    state.practices = data.practices || [];
    state.slots = data.slots || [];
    state.maxWeek = Math.min(30, Math.max(20, data.meta?.max_week || 0));
    buildPalette(state.courses);

    const s = data.student || {};
    const meta = [
      s.name ? `<b>${escapeHtml(s.name)}</b>` : "",
      s.class_name ? escapeHtml(s.class_name) : "",
      s.id ? `<span class="mono">${escapeHtml(s.id)}</span>` : "",
    ].filter(Boolean);
    if (MOCK) meta.unshift('<span class="tag">演示</span>');
    $("meta-student").innerHTML = meta.join('<span class="sep">·</span>') || "—";
    $("term-chip-text").textContent = termText();

    computeTodayWeek();
    state.week = state.todayWeek && state.todayWeek <= state.maxWeek ? state.todayWeek : 1;
    state.day = state.week === state.todayWeek ? realWeekday() : 1;

    buildWeekStrip();
    renderPractices();
    setLoader(false);
    render();

    if (!state.courses.length) {
      setAppMsg("这个学期没查到课程。如果确认有课，换个学年/学期试试。");
    }
  } catch (err) {
    setLoader(false);
    if (err.message !== "unauthorized") {
      setAppMsg(err.message);
      $("grid-wrap").hidden = state.view !== "week";
      $("agenda").hidden = state.view !== "day";
      $("daystrip").hidden = state.view !== "day";
    }
  }
}

/* ---------------- 周次条 ---------------- */
function buildWeekStrip() {
  const strip = $("weekstrip");
  strip.innerHTML = "";
  for (let w = 1; w <= state.maxWeek; w++) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "wk";
    b.dataset.w = String(w);
    b.textContent = String(w);
    const isCur = w === state.todayWeek;
    b.classList.toggle("is-current", isCur);
    b.setAttribute("aria-label", `第 ${w} 周${isCur ? "（本周）" : ""}`);
    b.addEventListener("click", () => { setWeek(w); });
    strip.appendChild(b);
  }
}

function syncWeekStrip() {
  const strip = $("weekstrip");
  let active = null;
  $$("#weekstrip .wk").forEach((b) => {
    const on = Number(b.dataset.w) === state.week;
    b.classList.toggle("is-on", on);
    b.setAttribute("aria-current", on ? "true" : "false");
    if (on) active = b;
  });
  if (!active) return;
  const target = active.offsetLeft - strip.clientWidth / 2 + active.offsetWidth / 2;
  strip.scrollTo({ left: Math.max(0, target), behavior: REDUCED.matches ? "auto" : "smooth" });
}

function setWeek(w) {
  state.week = clamp(w, 1, state.maxWeek);
  render();
}

function shiftWeek(d) { setWeek(state.week + d); }

function shiftDay(d) {
  let day = state.day + d;
  let week = state.week;
  if (day > 7) { day = 1; week = clamp(week + 1, 1, state.maxWeek); }
  if (day < 1) { day = 7; week = clamp(week - 1, 1, state.maxWeek); }
  state.day = day;
  state.week = week;
  render();
}

function goThisWeek() {
  const w = computeTodayWeek();
  if (!w) return;
  state.week = clamp(w, 1, state.maxWeek);
  state.day = realWeekday();
  render();
}

$("prev-week").addEventListener("click", () => (state.view === "day" ? shiftDay(-1) : shiftWeek(-1)));
$("next-week").addEventListener("click", () => (state.view === "day" ? shiftDay(1) : shiftWeek(1)));
$("this-week").addEventListener("click", goThisWeek);

$$(".seg-view .seg-item").forEach((b) => {
  b.addEventListener("click", () => {
    if (state.view === b.dataset.view) return;
    state.view = b.dataset.view;
    store.view = state.view;
    render();
  });
});

/* ---------------- 渲染入口 ---------------- */
function render() {
  const isWeek = state.view === "week";

  $("grid-wrap").hidden = !isWeek;
  $("agenda").hidden = isWeek;
  $("daystrip").hidden = isWeek;
  $$(".seg-view .seg-item").forEach((b) => {
    b.classList.toggle("is-on", b.dataset.view === state.view);
    b.setAttribute("aria-pressed", b.dataset.view === state.view ? "true" : "false");
  });

  if (isWeek) renderGrid();
  else { renderDayStrip(); renderAgenda(); }

  syncWeekStrip();
  renderNow();

  const w = state.week;
  document.title = `${w === state.todayWeek ? "本周 · " : ""}第 ${w} 周 · 我的课表`;
}

/* ---------------- 周视图 ---------------- */
function renderGrid() {
  const grid = $("grid");
  grid.innerHTML = "";
  const isCurrent = state.week === state.todayWeek;
  const today = realWeekday();
  const lanes = laneInfoFor(state.week);
  const visible = state.courses.filter(
    (c) => c.weekday >= 1 && c.weekday <= 7 && c.start >= 1 && inWeek(c, state.week)
  );

  /* 只画到最后一节课之后一行，别留一大片空网格 */
  const lastEnd = visible.reduce((m, c) => Math.max(m, clamp(c.end, 1, ROWS)), 0);
  const rowCount = clamp(lastEnd + 1, 8, ROWS);
  grid.style.gridTemplateRows = `50px repeat(${rowCount}, var(--row-h))`;

  const corner = document.createElement("div");
  corner.className = "g-corner";
  corner.textContent = "节次";
  grid.appendChild(corner);

  for (let wd = 1; wd <= 7; wd++) {
    const d = dateFor(state.week, wd);
    const head = document.createElement("div");
    head.className = "g-head" + (isCurrent && wd === today ? " is-today" : "");
    head.innerHTML = `<span class="d-name">${WEEKDAYS[wd - 1]}</span>` +
      (d ? `<span class="d-date">${fmtMD(d)}</span>` : "");
    grid.appendChild(head);
  }

  for (let s = 1; s <= rowCount; s++) {
    const g = document.createElement("div");
    g.className = "g-gutter";
    g.style.gridRow = String(s + 1);
    g.style.gridColumn = "1";
    const slot = slotOf(s);
    g.innerHTML = `<b>${s}</b><span>${slot ? slot.start : ""}</span>`;
    grid.appendChild(g);

    for (let wd = 1; wd <= 7; wd++) {
      const cell = document.createElement("div");
      cell.className = "g-cell" + (isCurrent && wd === today ? " is-today" : "");
      cell.style.gridRow = String(s + 1);
      cell.style.gridColumn = String(wd + 1);
      grid.appendChild(cell);
    }
  }

  const nowMin = nowMinutes();
  const nowWd = realWeekday();

  visible.forEach((c) => {
    const start = clamp(c.start, 1, ROWS);
    const end = clamp(c.end, start, ROWS);
    const span = end - start + 1;
    const slotA = slotOf(c.start);
    const slotB = slotOf(c.end);
    const ongoing = isCurrent && c.weekday === nowWd && slotA && slotB &&
      nowMin >= toMin(slotA.start) && nowMin <= toMin(slotB.end);

    const el = document.createElement("button");
    el.type = "button";
    el.className = `course k${paletteOf(c.name)}` + (span < 2 ? " tight" : "") + (ongoing ? " is-now" : "");
    el.style.gridRow = `${start + 1} / span ${span}`;
    el.style.gridColumn = String(c.weekday + 1);
    const lane = lanes.get(c.id);
    if (lane && lane.n > 1) {
      el.style.setProperty("--n", lane.n);
      el.style.setProperty("--lane", lane.lane);
    }
    el.innerHTML = `
      <span class="c-sec">${c.start}–${c.end} 节</span>
      <span class="c-name">${escapeHtml(c.name)}</span>
      ${c.room ? `<span class="c-meta">${escapeHtml(c.room)}</span>` : ""}
      ${c.teacher ? `<span class="c-meta">${escapeHtml(c.teacher)}</span>` : ""}`;
    el.addEventListener("click", () => openDetail(c));
    grid.appendChild(el);
  });
}

/* 同一天互相重叠的课，平分列宽而不是叠在一起 */
function laneInfoFor(week) {
  const info = new Map();
  for (let wd = 1; wd <= 7; wd++) {
    const items = state.courses
      .filter((c) => c.weekday === wd && inWeek(c, week))
      .sort((a, b) => a.start - b.start || a.end - b.end);
    let group = [];
    let maxEnd = 0;

    const flush = () => {
      if (!group.length) return;
      if (group.length === 1) {
        info.set(group[0].id, { n: 1, lane: 0 });
      } else {
        const ends = [];
        group.forEach((c) => {
          let lane = ends.findIndex((e) => e < c.start);
          if (lane === -1) { lane = ends.length; ends.push(c.end); }
          else ends[lane] = c.end;
          info.set(c.id, { n: group.length, lane });
        });
      }
      group = [];
    };

    items.forEach((c) => {
      if (group.length && c.start > maxEnd) { flush(); maxEnd = 0; }
      group.push(c);
      maxEnd = Math.max(maxEnd, c.end);
    });
    flush();
  }
  return info;
}

/* ---------------- 日视图 ---------------- */
function renderDayStrip() {
  const strip = $("daystrip");
  strip.innerHTML = "";
  const isCurrent = state.week === state.todayWeek;
  const today = realWeekday();

  for (let wd = 1; wd <= 7; wd++) {
    const d = dateFor(state.week, wd);
    const b = document.createElement("button");
    b.type = "button";
    b.className = "day" +
      (wd === state.day ? " is-on" : "") +
      (isCurrent && wd === today ? " is-today" : "");
    b.innerHTML = `
      <span class="d-w">${DAY_CHARS[wd - 1]}</span>
      <span class="d-n">${d ? d.getDate() : wd}</span>
      <span class="d-dot"></span>`;
    b.setAttribute("aria-label", `${WEEKDAYS[wd - 1]}${d ? " " + fmtMD(d) : ""}`);
    b.setAttribute("aria-pressed", wd === state.day ? "true" : "false");
    b.addEventListener("click", () => { state.day = wd; render(); });
    strip.appendChild(b);
  }
}

function renderAgenda() {
  const wrap = $("agenda");
  const d = dateFor(state.week, state.day);
  const isCurrent = state.week === state.todayWeek;
  const list = state.courses
    .filter((c) => c.weekday === state.day && inWeek(c, state.week))
    .sort((a, b) => a.start - b.start || a.end - b.end);

  const sections = list.reduce((n, c) => n + (c.end - c.start + 1), 0);
  const nowMin = nowMinutes();
  const isToday = isCurrent && state.day === realWeekday();

  let html = `<div class="ag-head">
    <h2>${WEEKDAYS[state.day - 1]}${d ? `<span class="ag-date">${fmtMD(d)}</span>` : ""}</h2>
    <span class="ag-count">${list.length ? `${list.length} 门课 · ${sections} 节` : "没课"}</span>
  </div>`;

  if (!list.length) {
    html += `<div class="ag-empty">
      <b>${WEEKDAYS[state.day - 1]}空着</b>
      <span>换一天看看，或者在上方的周次条跳到别的周。</span>
    </div>`;
    wrap.innerHTML = html;
    return;
  }

  list.forEach((c) => {
    const a = slotOf(c.start);
    const b = slotOf(c.end);
    const ongoing = isToday && a && b && nowMin >= toMin(a.start) && nowMin <= toMin(b.end);
    const meta = [c.room, c.teacher].filter(Boolean).join(" · ");
    html += `<div class="ag-row k${paletteOf(c.name)}">
      <div class="ag-time">
        <b>${a ? a.start : "—"}</b>
        <span>${c.start}–${c.end}</span>
      </div>
      <div class="ag-spine"></div>
      <button class="ag-card${ongoing ? " is-now" : ""}" type="button" data-id="${escapeHtml(c.id)}">
        ${ongoing ? '<span class="ag-nowflag">正在进行</span>' : ""}
        <span class="ag-name">${escapeHtml(c.name)}</span>
        ${meta ? `<span class="ag-meta">${escapeHtml(meta)}</span>` : ""}
        <span class="ag-range">${a && b ? `${a.start} — ${b.end}` : `第 ${c.start}–${c.end} 节`}</span>
      </button>
    </div>`;
  });

  wrap.innerHTML = html;
  $$(".ag-card", wrap).forEach((btn) => {
    btn.addEventListener("click", () => {
      const c = state.courses.find((x) => x.id === btn.dataset.id);
      if (c) openDetail(c);
    });
  });
}

/* ---------------- 现在 / 下一节 ---------------- */
function nowMinutes() {
  const n = new Date();
  return n.getHours() * 60 + n.getMinutes();
}

function renderNow() {
  const bar = $("nowbar");
  if (state.week !== state.todayWeek || state.todayWeek == null) {
    bar.hidden = true;
    return;
  }
  const wd = realWeekday();
  const now = nowMinutes();
  const todayCourses = state.courses
    .filter((c) => c.weekday === wd && inWeek(c, state.todayWeek))
    .sort((a, b) => a.start - b.start);

  let html = "";
  const running = todayCourses.find((c) => {
    const a = slotOf(c.start), b = slotOf(c.end);
    return a && b && now >= toMin(a.start) && now <= toMin(b.end);
  });

  if (running) {
    const b = slotOf(running.end);
    const left = toMin(b.end) - now;
    html = `<span class="now-live">正在进行</span>
      <b>${escapeHtml(running.name)}</b>
      <span>${escapeHtml(running.room || "")}</span>
      <span class="now-time">还剩 ${left} 分钟</span>`;
  } else {
    const next = todayCourses.find((c) => {
      const a = slotOf(c.start);
      return a && toMin(a.start) > now;
    });
    if (next) {
      const a = slotOf(next.start);
      const diff = toMin(a.start) - now;
      html = `<span class="now-live">下一节</span>
        <b>${escapeHtml(next.name)}</b>
        <span>${escapeHtml(next.room || "")}</span>
        <span class="now-time">${a.start} 开始 · ${diff >= 60 ? `${Math.floor(diff / 60)} 小时 ${diff % 60} 分钟后` : `${diff} 分钟后`}</span>`;
    } else if (todayCourses.length) {
      html = `<span class="now-live">今天</span><b>课上完了</b><span>共 ${todayCourses.length} 门课</span>`;
    } else {
      html = `<span class="now-live">今天</span><b>没有课</b><span>这一天整天空着。</span>`;
    }
  }

  bar.innerHTML = html;
  bar.hidden = !html;
}

/* ---------------- 实践环节 ---------------- */
function renderPractices() {
  const box = $("practices");
  const list = state.practices.filter((p) => p && p.name);
  if (!list.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.innerHTML = `<span class="p-label">实践环节</span>` + list.map((p) =>
    `<span class="p-chip">${escapeHtml(p.name)}${p.weeks_text ? `<em>${escapeHtml(p.weeks_text)}</em>` : ""}</span>`
  ).join("");
  box.hidden = false;
}

/* ---------------- 课程详情 ---------------- */
const SPEC_ICONS = {
  teacher: "user", room: "pin", campus: "building",
  time: "clock", weeks: "calendar", code: "hash", klass: "users", note: "note",
};

function openDetail(c) {
  const a = slotOf(c.start);
  const b = slotOf(c.end);
  const timeText = a && b ? `${a.start} — ${b.end}` : "";

  const rows = [
    ["teacher", "教师", c.teacher],
    ["room", "地点", c.room],
    ["campus", "校区", c.campus],
    ["time", "上课时间", timeText ? `${timeText}（第 ${c.start}–${c.end} 节）` : `第 ${c.start}–${c.end} 节`],
    ["weeks", "周次", c.weeks_text || (c.weeks && c.weeks.length ? `${c.weeks.length} 周` : "")],
    ["code", "课程号", c.course_no],
    ["klass", "教学班", c.class_name],
    ["note", "备注", c.note],
  ].filter(([, , v]) => v);

  $("detail-card").className = `sheet k${paletteOf(c.name)}`;
  $("detail-body").innerHTML = `
    <div class="detail-head">
      <span class="detail-kicker">${escapeHtml(c.weekday_cn || WEEKDAYS[c.weekday - 1])} · 第 ${c.start}–${c.end} 节</span>
      <h2 id="detail-title">${escapeHtml(c.name)}</h2>
    </div>
    <dl class="spec">
      ${rows.map(([key, label, val]) => `
        <div class="spec-row">
          <dt><svg class="ic" aria-hidden="true"><use href="#i-${SPEC_ICONS[key]}"/></svg>${label}</dt>
          <dd>${escapeHtml(val)}</dd>
        </div>`).join("")}
    </dl>`;

  openOverlay("detail");
}

/* ---------------- 覆盖层 ---------------- */
let lastFocus = null;

function openOverlay(id) {
  lastFocus = document.activeElement;
  const box = $(id);
  if (!box) return;
  box.hidden = false;
  document.body.classList.add("no-scroll");
  const sheet = box.querySelector(".sheet");
  if (sheet) {
    sheet.setAttribute("tabindex", "-1");
    sheet.focus({ preventScroll: true });
  }
}

function closeOverlay(id) {
  const box = $(id);
  if (!box || box.hidden) return;
  box.hidden = true;
  if ($("detail").hidden && $("settings").hidden) {
    document.body.classList.remove("no-scroll");
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
}

$$(".sheet-close").forEach((b) => {
  b.addEventListener("click", () => closeOverlay(b.dataset.close));
});

$$(".overlay").forEach((ov) => {
  ov.addEventListener("click", (e) => {
    if (e.target === ov) closeOverlay(ov.id);
  });
});

/* ---------------- 设置 ---------------- */
function openSettings() {
  if (state.terms) {
    $("sel-xnm").value = $("sel-xnm").value || state.terms.current.xnm;
  }
  $("start-date").value = store.startDate || "";
  applyTheme();
  openOverlay("settings");
}

$("open-settings").addEventListener("click", openSettings);
$("term-chip").addEventListener("click", openSettings);

$("sel-xnm").addEventListener("change", () => { loadTimetable(); });
$("sel-xqm").addEventListener("change", () => { loadTimetable(); });

$("start-date").addEventListener("change", (e) => {
  store.startDate = e.target.value;
  computeTodayWeek();
  buildWeekStrip();
  render();
});

$$("#theme-seg .seg-item").forEach((b) => {
  b.addEventListener("click", () => {
    store.theme = b.dataset.theme;
    applyTheme();
  });
});

$("logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch { /* 忽略 */ }
  store.token = "";
  closeOverlay("settings");
  showLogin("已退出登录");
});

/* ---------------- 键盘 ---------------- */
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeOverlay("detail");
    closeOverlay("settings");
    return;
  }
  if (!$("detail").hidden || !$("settings").hidden) return;
  if ($("app-view").hidden) return;
  const tag = (document.activeElement && document.activeElement.tagName) || "";
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag)) return;

  const step = (d) => (state.view === "day" ? shiftDay(d) : shiftWeek(d));
  if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
  else if (e.key === "t" || e.key === "T") goThisWeek();
});

/* ---------------- 演示数据（?mock=1） ---------------- */
const MOCK_SLOTS = [
  { idx: 1, start: "08:00", end: "08:45" }, { idx: 2, start: "08:55", end: "09:40" },
  { idx: 3, start: "10:00", end: "10:45" }, { idx: 4, start: "10:55", end: "11:40" },
  { idx: 5, start: "14:00", end: "14:45" }, { idx: 6, start: "14:55", end: "15:40" },
  { idx: 7, start: "16:00", end: "16:45" }, { idx: 8, start: "16:55", end: "17:40" },
  { idx: 9, start: "19:00", end: "19:45" }, { idx: 10, start: "19:55", end: "20:40" },
  { idx: 11, start: "20:50", end: "21:35" }, { idx: 12, start: "21:45", end: "22:30" },
];

const range = (a, b) => Array.from({ length: b - a + 1 }, (_, i) => a + i);
const W = ["", "周一", "周二", "周三", "周四", "周五", "周六", "周日"];

const MOCK_RAW = [
  ["高等数学 A", "张伟", "教学楼 B203", 1, 1, 2, "1-16周", range(1, 16)],
  ["大学英语（一）", "李娜", "教学楼 A105", 1, 3, 4, "1-16周", range(1, 16)],
  ["程序设计基础（C）", "陈强", "实训楼 302", 2, 1, 2, "1-16周", range(1, 16)],
  ["体育（一）", "王磊", "田径场", 2, 5, 6, "1-16周", range(1, 16)],
  ["高等数学 A", "张伟", "教学楼 B203", 3, 1, 2, "1-15周(单)", range(1, 16).filter((w) => w % 2)],
  ["数据结构", "刘洋", "实训楼 401", 3, 3, 4, "1-15周", range(1, 15)],
  ["思想道德与法治", "赵敏", "教学楼 C301", 4, 3, 4, "1-16周", range(1, 16)],
  ["程序设计基础（C）", "陈强", "实训楼 302", 4, 5, 6, "1-16周", range(1, 16)],
  ["大学生心理健康", "孙丽", "教学楼 A210", 5, 1, 2, "2-16周", range(2, 16)],
  ["大学英语（一）", "李娜", "教学楼 A105", 5, 3, 4, "1-16周", range(1, 16)],
  ["Python 数据分析（选修）", "周杰", "实训楼 205", 5, 7, 8, "1-12周(双)", range(1, 13).filter((w) => w % 2 === 0)],
  ["形势与政策", "赵敏", "教学楼 C102", 6, 3, 4, "5-8周", range(5, 8)],
];

const MOCK_COURSES = MOCK_RAW.map(([name, teacher, room, weekday, start, end, weeksText, weeks], i) => ({
  id: `mock-${i}`, name, teacher, room, campus: "北校区",
  weekday, weekday_cn: W[weekday], start, end, weeks, weeks_text: weeksText,
  course_no: `C${1000 + i}`, class_name: `${name} 01 班`, credit: "3.0", note: "",
}));

function mockApi(path) {
  if (path.startsWith("/api/me")) {
    return Promise.resolve({ ok: true, name: "演示学生", id: "20250001", class_name: "25 软件技术 1 班", major: "软件技术" });
  }
  if (path.startsWith("/api/terms")) {
    return Promise.resolve({
      ok: true,
      xnm: [{ value: "2025", label: "2025-2026" }, { value: "2024", label: "2024-2025" }],
      xqm: [{ value: "3", label: "第一学期" }, { value: "12", label: "第二学期" }],
      current: { xnm: "2025", xqm: "3" },
    });
  }
  if (path.startsWith("/api/timetable")) {
    return Promise.resolve({
      ok: true,
      student: { name: "演示学生", id: "20250001", class_name: "25 软件技术 1 班" },
      courses: MOCK_COURSES,
      practices: [{ name: "金工实习", weeks_text: "17-18 周" }],
      slots: MOCK_SLOTS,
      meta: { max_week: 18, course_count: MOCK_COURSES.length },
    });
  }
  return Promise.resolve({ ok: true });
}

/* ---------------- 入口 ---------------- */
(async function init() {
  applyTheme();
  if (MOCK) {
    // 演示模式只在内存里跑，不写 localStorage，免得之后正常打开还是假数据
    try { await boot(); } catch (err) { showLogin(err.message); }
    return;
  }
  if (!state.view) state.view = store.view || (mqlWide.matches ? "week" : "day");
  if (!store.token) return showLogin("");
  try {
    await boot();
  } catch {
    showLogin("");
  }
})();
