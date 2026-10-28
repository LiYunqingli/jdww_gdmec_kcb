/* 前端逻辑：登录 → 拉学年学期 → 拉课表 → 按周渲染 */

const API = "";
const $ = (id) => document.getElementById(id);
const MOCK = new URLSearchParams(location.search).has("mock");

const store = {
  get token() { return localStorage.getItem("kcb.token") || ""; },
  set token(v) { v ? localStorage.setItem("kcb.token", v) : localStorage.removeItem("kcb.token"); },
  get startDate() { return localStorage.getItem("kcb.startDate") || ""; },
  set startDate(v) { v ? localStorage.setItem("kcb.startDate", v) : localStorage.removeItem("kcb.startDate"); },
};

const state = {
  terms: null,
  courses: [],
  practices: [],
  slots: [],
  week: 1,
  maxWeek: 20,
  todayWeek: null,
};

/* ---------------- 工具 ---------------- */
async function api(path, opts = {}) {
  if (MOCK) return mockApi(path);
  const headers = { ...(opts.headers || {}) };
  if (store.token) headers.Authorization = `Bearer ${store.token}`;
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(API + path, { ...opts, headers });
  let data = null;
  try { data = await res.json(); } catch { /* 忽略非 JSON */ }
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

function setMsg(text, ok = false) {
  const el = $("login-msg");
  el.textContent = text || "";
  el.classList.toggle("ok", !!ok);
}

function hueOf(str) {
  let h = 0;
  for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) % 360;
  return h;
}

/* ---------------- 登录 ---------------- */
function showLogin(msg) {
  $("app-view").classList.add("hidden");
  $("login-view").classList.remove("hidden");
  setMsg(msg || "");
}

function showApp() {
  $("login-view").classList.add("hidden");
  $("app-view").classList.remove("hidden");
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    const mode = tab.dataset.mode;
    $("pwd-form").classList.toggle("hidden", mode !== "pwd");
    $("cookie-form").classList.toggle("hidden", mode !== "cookie");
    setMsg("");
  });
});

$("pwd-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = $("username").value.trim();
  const password = $("password").value;
  if (!username || !password) return setMsg("账号和密码都要填");
  $("pwd-submit").disabled = true;
  setMsg("正在登录…", true);
  try {
    const data = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    store.token = data.token;
    setMsg("");
    await boot();
  } catch (err) {
    setMsg(err.message);
  } finally {
    $("pwd-submit").disabled = false;
  }
});

$("cookie-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const cookie = $("cookie").value.trim();
  if (!cookie) return setMsg("请粘贴 Cookie");
  $("cookie-submit").disabled = true;
  setMsg("正在校验 Cookie…", true);
  try {
    const data = await api("/api/login-cookie", {
      method: "POST",
      body: JSON.stringify({ cookie }),
    });
    store.token = data.token;
    setMsg("");
    await boot();
  } catch (err) {
    setMsg(err.message);
  } finally {
    $("cookie-submit").disabled = false;
  }
});

$("logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch { /* 忽略 */ }
  store.token = "";
  showLogin("已退出");
});

/* ---------------- 启动 ---------------- */
async function boot() {
  showApp();
  const terms = await api("/api/terms");
  state.terms = terms;

  const selXnm = $("sel-xnm");
  const selXqm = $("sel-xqm");
  selXnm.innerHTML = terms.xnm.map((o) => `<option value="${o.value}">${o.label || o.value}</option>`).join("");
  selXqm.innerHTML = terms.xqm.map((o) => `<option value="${o.value}">${o.label || o.value}</option>`).join("");
  selXnm.value = terms.current.xnm;
  selXqm.value = terms.current.xqm;

  selXnm.onchange = () => loadTimetable();
  selXqm.onchange = () => loadTimetable();

  initStartDate();
  await loadTimetable();
}

function initStartDate() {
  const input = $("start-date");
  if (store.startDate) { input.value = store.startDate; return; }
  const now = new Date();
  const y = now.getMonth() >= 8 ? now.getFullYear() : now.getFullYear() - 1;
  const guess = now.getMonth() >= 8
    ? new Date(y, 8, 1)      // 9 月 1 日
    : new Date(y + 1, 1, 23); // 次年 2 月下旬
  const iso = guess.toISOString().slice(0, 10);
  input.value = iso;
  store.startDate = iso;
}

$("start-date").addEventListener("change", (e) => {
  store.startDate = e.target.value;
  computeTodayWeek();
  render();
});

function computeTodayWeek() {
  if (!store.startDate) return (state.todayWeek = null);
  const start = new Date(store.startDate + "T00:00:00");
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.floor((today - start) / 86400000);
  const w = Math.floor(days / 7) + 1;
  state.todayWeek = w >= 1 ? w : null;
  return state.todayWeek;
}

/* ---------------- 课表 ---------------- */
async function loadTimetable() {
  const xnm = $("sel-xnm").value;
  const xqm = $("sel-xqm").value;
  $("app-msg").textContent = "正在加载课表…";
  try {
    const data = await api(`/api/timetable?xnm=${encodeURIComponent(xnm)}&xqm=${encodeURIComponent(xqm)}`);
    state.courses = data.courses || [];
    state.practices = data.practices || [];
    state.slots = data.slots || [];
    state.maxWeek = Math.max(20, data.meta?.max_week || 0);

    const s = data.student || {};
    $("student-bar").innerHTML = [
      s.name ? `<span><b>${escapeHtml(s.name)}</b></span>` : "",
      s.id ? `<span>${escapeHtml(s.id)}</span>` : "",
      s.class_name ? `<span>${escapeHtml(s.class_name)}</span>` : "",
      s.major ? `<span>${escapeHtml(s.major)}</span>` : "",
      data.term?.label ? `<span style="opacity:.7">${escapeHtml(data.term.label)}</span>` : "",
    ].filter(Boolean).join("");
    if (MOCK) {
      $("student-bar").insertAdjacentHTML("afterbegin",
        '<span style="color:#f59e0b;font-weight:600">演示数据</span>');
    }

    computeTodayWeek();
    state.week = state.todayWeek && state.todayWeek <= state.maxWeek ? state.todayWeek : 1;

    const selWeek = $("sel-week");
    selWeek.innerHTML = Array.from({ length: state.maxWeek }, (_, i) => {
      const w = i + 1;
      const tag = w === state.todayWeek ? "（本周）" : "";
      return `<option value="${w}">第 ${w} 周${tag}</option>`;
    }).join("");
    selWeek.value = String(state.week);
    selWeek.onchange = () => { state.week = Number(selWeek.value); render(); };

    $("app-msg").textContent = state.courses.length
      ? ""
      : "该学期没有查到课程。若确认有课，请检查学年/学期选择是否正确。";
    render();
  } catch (err) {
    if (err.message !== "unauthorized") $("app-msg").textContent = err.message;
  }
}

$("prev-week").addEventListener("click", () => shiftWeek(-1));
$("next-week").addEventListener("click", () => shiftWeek(1));
$("this-week").addEventListener("click", () => {
  const w = computeTodayWeek();
  if (w) { state.week = w; $("sel-week").value = String(w); render(); }
});
function shiftWeek(d) {
  const w = Math.min(state.maxWeek, Math.max(1, state.week + d));
  state.week = w;
  $("sel-week").value = String(w);
  render();
}

/* ---------------- 渲染 ---------------- */
const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

function render() {
  const grid = $("grid");
  grid.innerHTML = "";
  const rows = 12;

  // 当前真实星期（用于高亮表头）
  const jsDay = new Date().getDay();           // 0=周日
  const todayIdx = jsDay === 0 ? 7 : jsDay;    // 1..7

  // 表头
  const corner = document.createElement("div");
  corner.className = "g-head corner";
  corner.textContent = "节次";
  corner.style.gridRow = "1";
  corner.style.gridColumn = "1";
  grid.appendChild(corner);

  WEEKDAYS.forEach((name, i) => {
    const head = document.createElement("div");
    head.className = "g-head" + (i + 1 === todayIdx ? " today" : "");
    head.textContent = name;
    head.style.gridRow = "1";
    head.style.gridColumn = String(i + 2);
    grid.appendChild(head);
  });

  // 节次列
  for (let s = 1; s <= rows; s++) {
    const cell = document.createElement("div");
    cell.className = "g-time";
    cell.style.gridRow = String(s + 1);
    cell.style.gridColumn = "1";
    const slot = state.slots.find((x) => x.idx === s);
    cell.innerHTML = `<b>${s}</b><span>${slot ? slot.start : ""}</span><span>${slot ? slot.end : ""}</span>`;
    grid.appendChild(cell);
  }

  // 空底格
  for (let s = 1; s <= rows; s++) {
    for (let w = 1; w <= 7; w++) {
      const cell = document.createElement("div");
      cell.className = "g-cell";
      cell.style.gridRow = String(s + 1);
      cell.style.gridColumn = String(w + 1);
      grid.appendChild(cell);
    }
  }

  // 课程
  const visible = state.courses.filter(
    (c) => c.weekday >= 1 && c.weekday <= 7 && c.start >= 1 && (!c.weeks.length || c.weeks.includes(state.week))
  );

  const seen = {};
  visible.forEach((c) => {
    const start = Math.max(1, Math.min(rows, c.start));
    const end = Math.max(start, Math.min(rows, c.end));
    const el = document.createElement("div");
    const hue = hueOf(c.name);
    el.className = "course";
    el.style.gridRow = `${start + 1} / ${end + 2}`;
    el.style.gridColumn = String(c.weekday + 1);
    el.style.setProperty("--c", `hsl(${hue} 62% 52%)`);
    el.style.setProperty("--bgc", `hsla(${hue}, 72%, 52%, .18)`);
    el.innerHTML = `
      <div class="c-name">${escapeHtml(c.name)}</div>
      <div class="c-meta">${escapeHtml(c.room || "")}</div>
      <div class="c-meta">${escapeHtml(c.teacher || "")}</div>`;
    el.addEventListener("click", () => openDetail(c));
    grid.appendChild(el);

    const key = `${c.weekday}-${start}`;
    seen[key] = (seen[key] || 0) + 1;
    if (seen[key] > 1) el.style.boxShadow = "4px 4px 0 -1px rgba(0,0,0,.12)";
  });

  document.title = `第 ${state.week} 周 · 我的课表`;
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, (m) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[m]));
}

function openDetail(c) {
  const slots = state.slots.filter((s) => s.idx >= c.start && s.idx <= c.end);
  const timeText = slots.length ? `${slots[0].start} - ${slots[slots.length - 1].end}` : "";
  $("modal-body").innerHTML = `
    <h3>${escapeHtml(c.name)}</h3>
    <p class="m-sub">${escapeHtml(c.weekday_cn)} 第 ${c.start}-${c.end} 节 ${timeText ? "· " + timeText : ""}</p>
    <dl class="kv">
      <dt>教师</dt><dd>${escapeHtml(c.teacher) || "—"}</dd>
      <dt>地点</dt><dd>${escapeHtml(c.room) || "—"}</dd>
      <dt>校区</dt><dd>${escapeHtml(c.campus) || "—"}</dd>
      <dt>周次</dt><dd>${escapeHtml(c.weeks_text) || (c.weeks.join(",") + " 周") || "—"}</dd>
      <dt>课程号</dt><dd>${escapeHtml(c.course_no) || "—"}</dd>
      <dt>教学班</dt><dd>${escapeHtml(c.class_name) || "—"}</dd>
      <dt>备注</dt><dd>${escapeHtml(c.note) || "—"}</dd>
    </dl>`;
  $("modal").classList.remove("hidden");
}

$("modal-close").addEventListener("click", () => $("modal").classList.add("hidden"));
$("modal").addEventListener("click", (e) => {
  if (e.target === $("modal")) $("modal").classList.add("hidden");
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("modal").classList.add("hidden");
  if ($("app-view").classList.contains("hidden")) return;
  if (e.key === "ArrowLeft") shiftWeek(-1);
  if (e.key === "ArrowRight") shiftWeek(1);
});

/* ---------------- 演示模式（?mock=1） ---------------- */
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
      current: { xnm: "2025", xqm: "12" },
    });
  }
  if (path.startsWith("/api/timetable")) {
    return Promise.resolve({
      ok: true,
      student: { name: "演示学生", id: "20250001" },
      courses: MOCK_COURSES, practices: [], slots: MOCK_SLOTS,
      meta: { max_week: 16, course_count: MOCK_COURSES.length },
    });
  }
  return Promise.resolve({ ok: true });
}

/* ---------------- 入口 ---------------- */
(async function init() {
  if (MOCK) {
    store.token = "mock";
    await boot();
    return;
  }
  if (!store.token) return showLogin("");
  try {
    await boot();
  } catch {
    showLogin("");
  }
})();
